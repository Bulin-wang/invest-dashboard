"""期货抓取层 + 到期语义的离线单测（不联网）。运行： python -m pytest tests/ -q

覆盖：
- 合约代码规范化 / 校验（仅放通 SC）
- ★ 日线解析：落盘取 ``c``（= 当日结算价），而不是实时报价的"最新价"
- 实时报价解析（诊断用）
- 抓取（假响应）、错误
- 缓存读写与新鲜度
- ★ `expires` 到期语义：到期日次日起按现金处理（不涨不跌）
- ★ 日历修复：某腿到期后组合曲线继续延伸成平线，而不是整条消失
- ★ 回归：正常持仓（腿的日期是 hint 的子集）不受日历并集影响

注：不使用 pytest 的 `tmp_path`（沙箱里需要目录符号链接，会失败），
    改用工作区内的临时目录（见 `workdir`）。
"""
from __future__ import annotations

import json
import shutil
import sys
import uuid
from pathlib import Path

import pandas as pd
import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from prototype import futures_fetch  # noqa: E402
from prototype.quote_fetch import Item as QuoteItem, snapshot as quote_snapshot  # noqa: E402
from src.aggregate import weighted_path_nav  # noqa: E402
from src.config import Item as ConfigItem  # noqa: E402

SYM = "SC2611"
# 与真实响应同形的日线（d/o/h/l/c/v/p/s）
_BARS = [
    {"d": "2026-09-28", "o": "741.400", "h": "750.900", "l": "717.500",
     "c": "727.300", "v": "102089", "p": "26000", "s": "734.100"},
    {"d": "2026-09-29", "o": "727.000", "h": "737.200", "l": "700.400",
     "c": "711.900", "v": "195579", "p": "25500", "s": "716.900"},
    {"d": "2026-09-30", "o": "689.900", "h": "716.700", "l": "674.700",
     "c": "711.000", "v": "175510", "p": "24978", "s": "696.100"},
]
_QUOTE = ('var hq_str_nf_SC2611="上海原油2611,150000,689.900,716.700,674.700,'
          '711.000,711.300,711.500,711.000,696.100,716.900,1,1,24978.000,'
          '175510,沪,上海原油,2026-09-30";')


def _klines_payload(bars=None) -> str:
    return "var t=(" + json.dumps(bars if bars is not None else _BARS) + ");"


def _item(symbol: str = SYM, **kw) -> ConfigItem:
    return ConfigItem(name="原油", symbol=symbol, market="cn", type="futures", **kw)


class _Resp:
    def __init__(self, text: str, status: int = 200):
        self.status_code = status
        self.content = text.encode("gbk")


@pytest.fixture()
def workdir():
    d = _ROOT / f".tmp_test_futures_{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True, exist_ok=True)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture()
def cache_dir(workdir, monkeypatch):
    d = workdir / "futures_cache"
    monkeypatch.setattr(futures_fetch, "FUTURES_CACHE_DIR", d)
    return d


# --------------------------------------------------------------------- 代码
def test_normalize_symbol():
    assert futures_fetch.normalize_symbol("SC2611") == "SC2611"
    assert futures_fetch.normalize_symbol("sc2611") == "SC2611"
    assert futures_fetch.normalize_symbol(" nf_sc2611 ") == "SC2611"
    assert futures_fetch.normalize_symbol("sc-2611") == "SC2611"


def test_check_symbol_rejects_garbage():
    with pytest.raises(futures_fetch.FuturesError, match="不合法"):
        futures_fetch._check_symbol("SC26")
    with pytest.raises(futures_fetch.FuturesError, match="不合法"):
        futures_fetch._check_symbol("2611")


def test_check_symbol_rejects_unsupported_exchange():
    """目前只放通 SC —— 其它品种必须明确报"暂不支持"，而不是含糊的"格式不合法"。"""
    with pytest.raises(futures_fetch.FuturesError, match="暂不支持"):
        futures_fetch._check_symbol("RB2611")      # 上期所螺纹
    with pytest.raises(futures_fetch.FuturesError, match="暂不支持"):
        futures_fetch._check_symbol("IF2611")      # 中金所股指
    with pytest.raises(futures_fetch.FuturesError, match="不合法"):
        futures_fetch._check_symbol("SC26")        # 月份位数不对


def test_fetch_rejects_unsupported_before_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("不支持品种时不该发请求")

    monkeypatch.setattr(futures_fetch.requests, "get", boom)
    with pytest.raises(futures_fetch.FuturesError, match="暂不支持"):
        futures_fetch.fetch_history(_item("RB2611"))


# --------------------------------------------------------------------- 解析
def test_parse_klines_uses_close_not_quote_last():
    """★ 落盘口径是日线 c（结算价），不是实时报价的"最新价"。"""
    df = futures_fetch.parse_klines(_klines_payload())
    assert df["date"].tolist() == ["2026-09-28", "2026-09-29", "2026-09-30"]
    assert df["close"].tolist() == [727.3, 711.9, 711.0]      # = c，而非 711.5
    row = df.iloc[-1]
    assert (row["open"], row["high"], row["low"]) == (689.9, 716.7, 674.7)
    assert row["volume"] == 175510.0
    assert row["open_interest"] == 24978.0
    assert row["prev_settle"] == 696.1


def test_parse_klines_sorts_and_dedupes():
    bars = [_BARS[2], _BARS[0], _BARS[2].copy()]
    bars[2]["c"] = "999.0"
    df = futures_fetch.parse_klines(_klines_payload(bars))
    assert df["date"].tolist() == ["2026-09-28", "2026-09-30"]
    assert df["close"].tolist() == [727.3, 999.0]            # 同日保留最后一条


def test_parse_klines_skips_dirty():
    bars = [{"d": "2026-09-30", "c": "711.0"},
            {"d": "", "c": "1.0"},                            # 无日期
            {"d": "2026-09-29", "c": "abc"},                  # 价格非法
            "垃圾",                                            # 非 dict
            {"d": "2026-09-28", "c": "700.0"}]
    df = futures_fetch.parse_klines(_klines_payload(bars))
    assert df["date"].tolist() == ["2026-09-28", "2026-09-30"]
    assert df["close"].tolist() == [700.0, 711.0]


def test_parse_klines_raises_on_empty_or_bad():
    with pytest.raises(futures_fetch.FuturesError):
        futures_fetch.parse_klines(_klines_payload([]))
    with pytest.raises(futures_fetch.FuturesError, match="未取到日线"):
        futures_fetch.parse_klines("这不是 JSONP")


def test_parse_quote_fields():
    q = futures_fetch.parse_quote(_QUOTE)
    assert q["name"] == "上海原油2611"
    assert q["open"] == 689.9 and q["high"] == 716.7 and q["low"] == 674.7
    assert q["bid"] == 711.0 and q["ask"] == 711.3
    assert q["last"] == 711.5                  # 最新价
    assert q["settle"] == 711.0                # 今结算 —— 对应日线 c
    assert q["prev_settle"] == 696.1
    assert q["open_interest"] == 24978.0
    assert q["date"] == "2026-09-30"


def test_parse_quote_rejects_empty():
    with pytest.raises(futures_fetch.FuturesError, match="报价为空"):
        futures_fetch.parse_quote('var hq_str_nf_SC2611="";')


# --------------------------------------------------------------------- 网络（假响应）
def test_fetch_history_end_to_end(monkeypatch):
    seen = {}
    monkeypatch.setattr(futures_fetch.requests, "get",
                        lambda url, headers=None, timeout=None:
                        (seen.update(url=url), _Resp(_klines_payload()))[1])
    sym, df = futures_fetch.fetch_history(_item())
    assert sym == SYM
    assert list(df.columns) == ["date", "close"]
    assert df["date"].dtype.kind == "M"
    assert "SC2611" in seen["url"]


def test_fetch_raises_on_http_error(monkeypatch):
    monkeypatch.setattr(futures_fetch.requests, "get",
                        lambda *a, **k: _Resp("", status=403))
    with pytest.raises(futures_fetch.FuturesError, match="HTTP 403"):
        futures_fetch.fetch_history(_item())


def test_fetch_raises_on_network_failure(monkeypatch):
    def boom(*a, **k):
        raise OSError("timed out")

    monkeypatch.setattr(futures_fetch.requests, "get", boom)
    with pytest.raises(futures_fetch.FuturesError, match="请求失败"):
        futures_fetch.fetch_history(_item())


# --------------------------------------------------------------------- 缓存
def test_cache_roundtrip(cache_dir, monkeypatch):
    monkeypatch.setattr(futures_fetch.requests, "get", lambda *a, **k: _Resp(_klines_payload()))
    it = _item()
    sym, detail = futures_fetch.fetch_detail(it)
    futures_fetch.save_cache(it.key, sym, detail)

    cached = futures_fetch.load_cache(it.key)
    assert cached["symbol"] == SYM
    assert cached["caliber"] == "settlement_price"
    assert cached["n"] == 3 and cached["last_date"] == "2026-09-30"
    assert futures_fetch.cache_history(cached)["close"].tolist() == [727.3, 711.9, 711.0]


def test_load_or_fetch_prefers_fresh_cache(cache_dir, monkeypatch):
    it = _item()
    today = pd.Timestamp.today().normalize()
    futures_fetch.FUTURES_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    futures_fetch._cache_path(it.key).write_text(json.dumps({
        "symbol": SYM, "bars": [{"date": today.strftime("%Y-%m-%d"), "close": 700.0}],
    }), encoding="utf-8")

    def boom(*a, **k):
        raise AssertionError("缓存新鲜时不该发请求")

    monkeypatch.setattr(futures_fetch.requests, "get", boom)
    sym, df = futures_fetch.load_or_fetch(it)
    assert sym == SYM and df["close"].tolist() == [700.0]


def test_cache_freshness_allows_stale_expired_contract(cache_dir):
    """到期合约不再更新，但缓存仍在有效期内就不该反复重抓。"""
    today = pd.Timestamp.today().normalize()
    assert futures_fetch.cache_is_fresh(
        {"bars": [{"date": today.strftime("%Y-%m-%d")}]})
    assert futures_fetch.cache_is_fresh(
        {"bars": [{"date": (today - pd.Timedelta(days=5)).strftime("%Y-%m-%d")}]})
    assert not futures_fetch.cache_is_fresh(
        {"bars": [{"date": (today - pd.Timedelta(days=40)).strftime("%Y-%m-%d")}]})
    assert not futures_fetch.cache_is_fresh(None)


# --------------------------------------------------------------------- 当日快照
def test_daily_snapshot_shape_matches_quote_snapshot(cache_dir, monkeypatch):
    monkeypatch.setattr(futures_fetch.requests, "get", lambda *a, **k: _Resp(_klines_payload()))
    row = futures_fetch.daily_snapshot(_item())
    required = set(quote_snapshot([QuoteItem("茅台", "600519", "cn", "stock")]).columns)
    assert required <= set(row)
    assert row["type"] == "futures"
    assert row["date"] == "2026-09-30"
    assert row["close"] == 711.0                  # 结算价
    assert row["prev_close"] == 711.9
    assert row["pct_chg"] == pytest.approx(711.0 / 711.9 - 1)
    assert row["source"] == "sina"


def test_daily_snapshot_does_not_write_partial_cache(cache_dir, monkeypatch):
    """未接入 seed 前不写缓存 —— 与 fund/crypto 保持同一契约。"""
    monkeypatch.setattr(futures_fetch.requests, "get", lambda *a, **k: _Resp(_klines_payload()))
    it = _item()
    futures_fetch.daily_snapshot(it)
    assert futures_fetch.load_cache(it.key) is None


# --------------------------------------------------------------------- 配置：expires
def test_item_key_for_futures():
    assert _item().key == "cn_futures_SC2611"


def test_config_parses_expires(workdir):
    from src.config import load_investors
    p = workdir / "inv.yaml"
    p.write_text("""
start_date: 2026-09-24
investors:
  - {nickname: A, symbol: SC2611, market: cn, type: futures, expires: "2026-10-30"}
  - {nickname: B, symbol: SC2612, market: cn, type: futures}
""", encoding="utf-8")
    inv = {i.nickname: i for i in load_investors(p)}
    assert inv["A"].expires == "2026-10-30"
    assert inv["B"].expires is None                       # 不填 = 不到期
    assert inv["A"].legs[0].expires == "2026-10-30"       # 透传到腿


# --------------------------------------------------------------------- ★ 到期语义
def _series():
    return pd.Series([100.0, 110.0, 120.0],
                     index=pd.to_datetime(["2026-09-28", "2026-09-29", "2026-09-30"]))


def test_expires_clips_series_to_expiry_day():
    """到期日**当日仍有价**，次日起按现金（不涨不跌）。"""
    hint = pd.DatetimeIndex(pd.to_datetime(
        ["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-08", "2026-10-09"]))
    df = weighted_path_nav({"F": _series()},
                           [("2026-09-28", {"F": 1.0})],
                           1_000_000.0, calendar_hint=hint,
                           expires_by_key={"F": "2026-09-29"})
    assert df["date"].dt.strftime("%Y-%m-%d").tolist() == [
        "2026-09-28", "2026-09-29", "2026-09-30", "2026-10-08", "2026-10-09"]
    # 09-30 已到期（> 09-29）→ 冻结在 09-29 的值（1.1），不再涨到 1.2
    vals = df["value"].tolist()
    assert vals[1] == pytest.approx(1_100_000.0)
    assert vals[2] == vals[3] == vals[4] == pytest.approx(1_100_000.0)


def test_without_expires_series_keeps_moving():
    """不填 expires 时不该被截断（保持原行为）。"""
    hint = pd.DatetimeIndex(pd.to_datetime(
        ["2026-09-28", "2026-09-29", "2026-09-30"]))
    df = weighted_path_nav({"F": _series()}, [("2026-09-28", {"F": 1.0})],
                           1_000_000.0, calendar_hint=hint)
    assert df["value"].iloc[-1] == pytest.approx(1_200_000.0)


def test_expiry_beyond_data_is_noop():
    hint = pd.DatetimeIndex(pd.to_datetime(["2026-09-28", "2026-09-30"]))
    df = weighted_path_nav({"F": _series()}, [("2026-09-28", {"F": 1.0})],
                           1_000_000.0, calendar_hint=hint,
                           expires_by_key={"F": "2030-01-01"})
    assert df["value"].iloc[-1] == pytest.approx(1_200_000.0)


# --------------------------------------------------------------------- ★ 日历修复
def test_calendar_hint_extends_portfolio_after_leg_ends():
    """★ 某腿价格序列结束后，组合曲线必须继续延伸成平线（= 该腿变现金）。

    修复前：日历只取"各腿价格日期的并集"，腿一结束整条曲线就断了、在看板上消失。
    """
    hint = pd.DatetimeIndex(pd.to_datetime(
        ["2026-09-29", "2026-09-30", "2026-10-08"]))
    df = weighted_path_nav({"F": _series()}, [("2026-09-28", {"F": 0.3, "cash": 0.7})],
                           1_000_000.0, calendar_hint=hint)
    # 日历 = 腿的日期(09-28/29/30) ∪ hint(09-29/30,10-08) = 4 天
    assert len(df) == 4
    assert df["date"].iloc[0] == pd.Timestamp("2026-09-28")
    assert df["date"].iloc[-1] == pd.Timestamp("2026-10-08")    # 腿结束后仍延伸
    assert df["value"].iloc[-1] == df["value"].iloc[-2]          # 之后不涨不跌


def test_normal_portfolio_unaffected_by_hint():
    """回归：正常持仓（腿日期 ⊇ hint）时，日历并集不改变任何结果。"""
    s = pd.Series([100.0, 110.0, 120.0],
                  index=pd.to_datetime(["2026-09-28", "2026-09-29", "2026-09-30"]))
    hint = pd.DatetimeIndex(pd.to_datetime(["2026-09-28", "2026-09-29"]))   # hint 是子集
    with_hint = weighted_path_nav({"F": s}, [("2026-09-28", {"F": 1.0})],
                                  1_000_000.0, calendar_hint=hint)
    without = weighted_path_nav({"F": s}, [("2026-09-28", {"F": 1.0})], 1_000_000.0)
    pd.testing.assert_frame_equal(with_hint, without)


def test_all_cash_still_requires_calendar_hint():
    with pytest.raises(ValueError, match="calendar_hint"):
        weighted_path_nav({}, [("2026-09-28", {"cash": 1.0})], 1_000_000.0)
    df = weighted_path_nav(
        {}, [("2026-09-28", {"cash": 1.0})], 1_000_000.0,
        calendar_hint=pd.DatetimeIndex(pd.to_datetime(["2026-09-28", "2026-09-29"])))
    assert len(df) == 2 and df["value"].tolist() == [1_000_000.0, 1_000_000.0]


def test_hint_index_accepts_various_types():
    """calendar_hint 兼容 DatetimeIndex / Series / 普通日期序列。"""
    from src.aggregate import _hint_index
    want = pd.to_datetime(["2026-09-28", "2026-09-29"])
    assert list(_hint_index(pd.DatetimeIndex(want))) == list(want)
    assert list(_hint_index(pd.Series([1, 2], index=want))) == list(want)
    assert list(_hint_index(["2026-09-28", "2026-09-29"])) == list(want)
    assert _hint_index(None) is None
    assert _hint_index([]) is None
