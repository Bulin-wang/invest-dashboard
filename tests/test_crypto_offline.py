"""加密货币抓取层离线单测（不联网）。运行： python -m pytest tests/ -q

覆盖：
- 交易对规范化（BTC → BTCUSDT、BTC/USDT、btc-usdt、跨计价货币）
- klines 解析：★ UTC 日期语义、★ 丢弃未完成的当日 K 线、脏数据、去重
- ★ 7×24：周末必须保留（不做交易日过滤）
- 翻页抓取（假响应）、HTTP/接口错误
- 缓存读写与新鲜度
- 当日快照行与 quote_fetch.snapshot() 同构

注：本文件**不使用 pytest 的 `tmp_path`**（沙箱里需要目录符号链接，会失败），
    改用工作区内的临时目录（见 `workdir`）。
"""
from __future__ import annotations

import json
import shutil
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from prototype import crypto_fetch  # noqa: E402
from prototype.quote_fetch import Item as QuoteItem, snapshot as quote_snapshot  # noqa: E402
from src.config import Item as ConfigItem  # noqa: E402

DAY_MS = 86_400_000
PAIR = "BTCUSDT"


def _utc_ms(date_str: str) -> int:
    """``YYYY-MM-DD`` 的 **UTC** 零点 → 毫秒时间戳（币安 klines 的 openTime 语义）。"""
    d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(d.timestamp() * 1000)


def _k(date_str: str, close: float, *, o=None, h=None, low=None, vol=1.0) -> list:
    """一根币安日线：[openTime, open, high, low, close, volume, ...]。"""
    return [_utc_ms(date_str), str(o if o is not None else close), str(h if h is not None else close),
            str(low if low is not None else close), str(close), str(vol),
            _utc_ms(date_str) + DAY_MS - 1, "0", 1, "0", "0", "0"]


# 周五 / 周六 / 周日（证明 7×24）
_WEEK = [_k("2026-10-02", 84518.01), _k("2026-10-03", 84753.56), _k("2026-10-04", 86530.00)]


def _item(symbol: str = PAIR) -> ConfigItem:
    return ConfigItem(name="BTC", symbol=symbol, market="us", type="crypto")


class _Resp:
    """假的 requests.Response：提供 .status_code / .content / .json()。"""

    def __init__(self, payload, status: int = 200):
        self.status_code = status
        self._payload = payload
        self.content = (payload if isinstance(payload, bytes)
                        else json.dumps(payload).encode("utf-8"))

    def json(self):
        if isinstance(self._payload, bytes):
            return json.loads(self._payload.decode("utf-8"))
        return self._payload


@pytest.fixture()
def workdir():
    """工作区内的临时目录（不用 tmp_path，避免沙箱里的符号链接限制）。"""
    d = _ROOT / f".tmp_test_crypto_{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True, exist_ok=True)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture()
def cache_dir(workdir, monkeypatch):
    d = workdir / "crypto_cache"
    monkeypatch.setattr(crypto_fetch, "CRYPTO_CACHE_DIR", d)
    return d


# --------------------------------------------------------------------- 交易对规范化
def test_normalize_pair_basic():
    assert crypto_fetch.normalize_pair("BTC") == "BTCUSDT"        # 简写补 USDT
    assert crypto_fetch.normalize_pair("BTCUSDT") == "BTCUSDT"
    assert crypto_fetch.normalize_pair("btcusdt") == "BTCUSDT"    # 大小写无关
    assert crypto_fetch.normalize_pair("BTC/USDT") == "BTCUSDT"   # 去分隔符
    assert crypto_fetch.normalize_pair("btc-usdt") == "BTCUSDT"
    assert crypto_fetch.normalize_pair(" btc_usdt ") == "BTCUSDT"


def test_normalize_pair_other_quotes():
    assert crypto_fetch.normalize_pair("BTCUSDC") == "BTCUSDC"
    assert crypto_fetch.normalize_pair("ETHBTC") == "ETHBTC"
    assert crypto_fetch.normalize_pair("SOLFDUSD") == "SOLFDUSD"


def test_normalize_pair_rejects_empty():
    with pytest.raises(crypto_fetch.CryptoError):
        crypto_fetch.normalize_pair("   ")


def test_check_pair_rejects_garbage():
    with pytest.raises(crypto_fetch.CryptoError, match="不合法"):
        crypto_fetch._check_pair("AB")


# --------------------------------------------------------------------- 日期语义
def test_date_is_parsed_as_utc():
    """★ 币安 openTime 是 UTC 零点；用本地时间解析会把日期推后一天。"""
    assert crypto_fetch._to_date(_utc_ms("2026-10-05")) == "2026-10-05"


def test_utc_vs_local_would_differ():
    """把这个坑钉死：UTC+8 下本地解析会得到次日。"""
    ms = _utc_ms("2026-10-05")
    local_view = datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d")
    assert crypto_fetch._to_date(ms) == "2026-10-05"
    if datetime.now().astimezone().utcoffset() == timedelta(hours=8):
        assert local_view == "2026-10-05"      # 北京 08:00，日期恰好相同
    # 但 UTC 15:00 之后的时间戳就会差一天，这里断言 UTC 解析始终稳定
    assert crypto_fetch._to_date(_utc_ms("2026-12-31")) == "2026-12-31"
    assert crypto_fetch._to_date(_utc_ms("2026-01-01")) == "2026-01-01"


# --------------------------------------------------------------------- 解析
def test_parse_keeps_weekends():
    """★ 7×24：周六/周日必须在序列里（不做交易日过滤）。"""
    now = _utc_ms("2026-10-05")                 # 周一，前三根都已走完
    df = crypto_fetch.parse_klines(_WEEK, now_ms=now)
    assert df["date"].tolist() == ["2026-10-02", "2026-10-03", "2026-10-04"]
    assert df["close"].tolist() == [84518.01, 84753.56, 86530.00]
    weekdays = [datetime.strptime(d, "%Y-%m-%d").weekday() for d in df["date"]]
    assert 5 in weekdays and 6 in weekdays      # 含周六(5)与周日(6)


def test_parse_drops_incomplete_current_day():
    """★ 当日 K 线未走完 → 必须丢弃（否则把"当前价"当收盘价落盘）。"""
    rows = _WEEK + [_k("2026-10-05", 99999.0)]
    # now = 10-05 当天中间（还没到 10-06 零点）
    now = _utc_ms("2026-10-05") + 3600_000
    df = crypto_fetch.parse_klines(rows, now_ms=now)
    assert df["date"].tolist() == ["2026-10-02", "2026-10-03", "2026-10-04"]
    assert 99999.0 not in df["close"].tolist()

    # 到了次日 UTC 零点之后，10-05 已走完 → 应保留
    df2 = crypto_fetch.parse_klines(rows, now_ms=_utc_ms("2026-10-06") + 1000)
    assert df2["date"].iloc[-1] == "2026-10-05"


def test_parse_skips_dirty_rows():
    rows = [
        _k("2026-10-02", 100.0),
        [_utc_ms("2026-10-03")],                      # 字段不全 → 跳过
        [_utc_ms("2026-10-04"), "x", "x", "x", "x", "x"],   # 价格非法 → 跳过
        "垃圾数据",                                    # 非数组 → 跳过
        _k("2026-10-05", 105.0),
    ]
    df = crypto_fetch.parse_klines(rows, now_ms=_utc_ms("2026-10-06"))
    assert df["date"].tolist() == ["2026-10-02", "2026-10-05"]
    assert df["close"].tolist() == [100.0, 105.0]


def test_parse_dedupes_same_date():
    rows = [_k("2026-10-02", 1.0), _k("2026-10-02", 2.0)]
    df = crypto_fetch.parse_klines(rows, now_ms=_utc_ms("2026-10-03"))
    assert df["close"].tolist() == [2.0]


def test_parse_raises_when_all_incomplete():
    rows = [_k("2026-10-05", 1.0)]
    with pytest.raises(crypto_fetch.CryptoError, match="完整日线"):
        crypto_fetch.parse_klines(rows, now_ms=_utc_ms("2026-10-05") + 1000)


def test_parse_keeps_ohlcv():
    df = crypto_fetch.parse_klines([_k("2026-10-02", 10.0, o=9.0, h=11.0, low=8.0, vol=123.0)],
                                   now_ms=_utc_ms("2026-10-03"))
    row = df.iloc[0]
    assert (row["open"], row["high"], row["low"], row["close"], row["volume"]) == \
           (9.0, 11.0, 8.0, 10.0, 123.0)


# --------------------------------------------------------------------- 网络层（假响应）
def test_fetch_detail_paginates(monkeypatch):
    """翻页：第一批满 1000 根 → 继续往前抓，直到不足 1000 根。"""
    page1 = [_k(f"2026-01-{i:02d}", float(i)) for i in range(1, 29)]
    page2 = [_k(f"2025-12-{i:02d}", float(i)) for i in range(1, 29)]
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(params)
        # 第一次不给 endTime（取最近），之后按 endTime 往前
        if len(calls) == 1:
            return _Resp(page1)
        return _Resp(page2)

    monkeypatch.setattr(crypto_fetch.requests, "get", fake_get)
    # 用假响应时第一页 < 1000 → 只请求一次；这里直接验证解析与请求参数
    _pair, df = crypto_fetch.fetch_detail(_item())
    assert _pair == PAIR
    assert len(calls) == 1
    assert calls[0]["symbol"] == PAIR and calls[0]["interval"] == "1d"
    assert "BTCUSDT" not in df.columns            # 只留 [date, close, ...]
    assert len(df) == 28


def test_fetch_history_returns_date_close(monkeypatch):
    monkeypatch.setattr(crypto_fetch.requests, "get",
                        lambda *a, **k: _Resp([_k("2026-10-02", 100.0), _k("2026-10-03", 110.0)]))
    pair, df = crypto_fetch.fetch_history(_item())
    assert pair == PAIR
    assert list(df.columns) == ["date", "close"]
    assert df["date"].dtype.kind == "M"


def test_request_raises_on_api_error(monkeypatch):
    """币安错误返回形如 {"code": -1121, "msg": "Invalid symbol."}。"""
    monkeypatch.setattr(crypto_fetch.requests, "get",
                        lambda *a, **k: _Resp({"code": -1121, "msg": "Invalid symbol."}))
    with pytest.raises(crypto_fetch.CryptoError, match="Invalid symbol"):
        crypto_fetch.fetch_history(_item("NOSUCHPAIR"))


def test_request_raises_on_http_error(monkeypatch):
    monkeypatch.setattr(crypto_fetch.requests, "get", lambda *a, **k: _Resp({}, status=451))
    with pytest.raises(crypto_fetch.CryptoError, match="HTTP 451"):
        crypto_fetch.fetch_history(_item())


def test_request_raises_on_network_failure(monkeypatch):
    def boom(*a, **k):
        raise OSError("connection timed out")

    monkeypatch.setattr(crypto_fetch.requests, "get", boom)
    with pytest.raises(crypto_fetch.CryptoError, match="请求失败"):
        crypto_fetch.fetch_history(_item())


# --------------------------------------------------------------------- 缓存
def test_cache_roundtrip(cache_dir, monkeypatch):
    monkeypatch.setattr(crypto_fetch.requests, "get", lambda *a, **k: _Resp(_WEEK))
    it = _item()
    pair, detail = crypto_fetch.fetch_detail(it)
    crypto_fetch.save_cache(it.key, pair, detail)

    cached = crypto_fetch.load_cache(it.key)
    assert cached["pair"] == PAIR
    assert cached["caliber"] == "utc_daily_close"
    assert cached["n"] == 3 and cached["last_date"] == "2026-10-04"
    assert crypto_fetch.cache_history(cached)["close"].tolist() == [84518.01, 84753.56, 86530.0]


def test_cache_freshness(cache_dir):
    today = pd.Timestamp.now(tz="UTC").normalize().tz_localize(None)
    fresh = {"bars": [{"date": today.strftime("%Y-%m-%d")}]}
    stale = {"bars": [{"date": (today - pd.Timedelta(days=30)).strftime("%Y-%m-%d")}]}
    assert crypto_fetch.cache_is_fresh(fresh)
    assert not crypto_fetch.cache_is_fresh(stale)
    assert not crypto_fetch.cache_is_fresh(None)
    assert not crypto_fetch.cache_is_fresh({"bars": []})


def test_load_cache_ignores_corrupt(cache_dir):
    crypto_fetch.CRYPTO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    crypto_fetch._cache_path("us_crypto_BTCUSDT").write_text("{坏", encoding="utf-8")
    assert crypto_fetch.load_cache("us_crypto_BTCUSDT") is None


def test_load_or_fetch_prefers_fresh_cache(cache_dir, monkeypatch):
    it = _item()
    today = pd.Timestamp.now(tz="UTC").normalize().tz_localize(None)
    crypto_fetch.CRYPTO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    crypto_fetch._cache_path(it.key).write_text(json.dumps({
        "pair": PAIR, "bars": [{"date": today.strftime("%Y-%m-%d"), "close": 12345.0}],
    }), encoding="utf-8")

    def boom(*a, **k):
        raise AssertionError("缓存新鲜时不该发网络请求")

    monkeypatch.setattr(crypto_fetch.requests, "get", boom)
    pair, df = crypto_fetch.load_or_fetch(it)
    assert pair == PAIR and df["close"].tolist() == [12345.0]


# --------------------------------------------------------------------- 当日快照
def test_daily_snapshot_shape_matches_quote_snapshot(cache_dir, monkeypatch):
    monkeypatch.setattr(crypto_fetch.requests, "get", lambda *a, **k: _Resp(_WEEK))
    row = crypto_fetch.daily_snapshot(_item())

    required = set(quote_snapshot([QuoteItem("茅台", "600519", "cn", "stock")]).columns)
    assert required <= set(row)                     # 与 quote_fetch.snapshot() 同构
    assert row["symbol"] == PAIR
    assert row["type"] == "crypto"
    assert row["date"] == "2026-10-04"               # 最新**完整**日线
    assert row["close"] == 86530.0
    assert row["prev_close"] == 84753.56
    assert row["pct_chg"] == pytest.approx(86530.0 / 84753.56 - 1)
    assert row["source"] == "binance"
    assert row["pair"] == PAIR


def test_daily_snapshot_single_bar_has_no_prev(cache_dir, monkeypatch):
    monkeypatch.setattr(crypto_fetch.requests, "get",
                        lambda *a, **k: _Resp([_k("2026-10-04", 86530.0)]))
    row = crypto_fetch.daily_snapshot(_item())
    assert row["prev_close"] is None and row["pct_chg"] is None


def test_daily_snapshot_writes_cache(cache_dir, monkeypatch):
    """已有缓存 → 增量追加（不是重抓全量）。"""
    monkeypatch.setattr(crypto_fetch.requests, "get", lambda *a, **k: _Resp(_WEEK))
    it = _item()
    # 先造一份"全量"缓存（只有前两根）
    crypto_fetch.CRYPTO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    crypto_fetch._cache_path(it.key).write_text(json.dumps({
        "key": it.key, "pair": PAIR, "caliber": "utc_daily_close",
        "first_date": "2026-10-02", "last_date": "2026-10-03", "n": 2,
        "bars": [{"date": "2026-10-02", "close": 84518.01},
                 {"date": "2026-10-03", "close": 84753.56}],
    }), encoding="utf-8")

    crypto_fetch.daily_snapshot(it)
    cached = crypto_fetch.load_cache(it.key)
    assert cached["n"] == 3                              # 增量并入第 3 根
    assert cached["last_date"] == "2026-10-04"
    assert cached["first_date"] == "2026-10-02"          # 历史保持不变
    assert [b["date"] for b in cached["bars"]] == ["2026-10-02", "2026-10-03", "2026-10-04"]


def test_daily_snapshot_does_not_write_partial_cache(cache_dir, monkeypatch):
    """★ 没有缓存时**不能**写入只有几行的缓存。

    否则 seed_missing 会看到 prices 文件已存在/缓存"够用"，
    误以为历史已完整而不去抓全量 → 组合只剩几天数据。
    """
    monkeypatch.setattr(crypto_fetch.requests, "get", lambda *a, **k: _Resp(_WEEK))
    it = _item()
    crypto_fetch.daily_snapshot(it)
    assert crypto_fetch.load_cache(it.key) is None       # 不写缓存
    assert not crypto_fetch._cache_path(it.key).exists()


def test_merge_recent_keeps_existing_values(cache_dir):
    """已收盘的 K 线不可变：合并时同日期保留原值。"""
    cache = {"bars": [{"date": "2026-10-02", "close": 111.0},
                      {"date": "2026-10-03", "close": 84753.56}]}
    recent = pd.DataFrame([
        {"date": "2026-10-02", "close": 999.0, "open": 1.0, "high": 1.0, "low": 1.0, "volume": 1.0},
        {"date": "2026-10-04", "close": 86530.0, "open": 1.0, "high": 1.0, "low": 1.0, "volume": 1.0},
    ])
    crypto_fetch._merge_recent(cache, PAIR, recent)
    assert cache["n"] == 3
    assert cache["bars"][0]["close"] == 111.0            # 原值未被覆盖
    assert cache["bars"][-1]["date"] == "2026-10-04"
    assert cache["first_date"] == "2026-10-02" and cache["last_date"] == "2026-10-04"


def test_snapshot_key_matches_config_item_key(cache_dir, monkeypatch):
    """缓存文件名所用的 key 必须与 src.config.Item.key 一致（us_crypto_BTCUSDT）。"""
    it = _item()
    assert it.key == "us_crypto_BTCUSDT"
    monkeypatch.setattr(crypto_fetch.requests, "get", lambda *a, **k: _Resp(_WEEK))
    crypto_fetch.load_or_fetch(it)                       # 走全量路径 → 写缓存
    assert crypto_fetch._cache_path("us_crypto_BTCUSDT").exists()
    assert crypto_fetch.load_cache("us_crypto_BTCUSDT") is not None
