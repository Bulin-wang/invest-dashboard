"""开放式基金净值抓取层离线单测（不联网）。运行： python -m pytest tests/ -q

覆盖：
- 代码规范化（去 sh/sz 前缀、补 6 位）
- pingzhongdata 解析：累计净值 vs 单位净值、分红说明、缺值回落、脏数据
- ★ 时间戳必须按**本地时间**解析（用 utcfromtimestamp 会整体提前一天）
- ★ `var X = ...` 取值不能以分号为界（字符串里可能含分号）
- 缓存读写与新鲜度判定
- 当日快照行的字段与 quote_fetch.snapshot() 同构

注：本文件**不使用 pytest 的 `tmp_path`** —— 它需要创建目录符号链接，
在受限令牌的沙箱里会以 WinError 1314/5 失败。改用工作区内的临时目录（见 `workdir`）。
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

from prototype import fund_fetch  # noqa: E402
from prototype.quote_fetch import Item as QuoteItem, snapshot as quote_snapshot  # noqa: E402
from src.config import Item as ConfigItem  # noqa: E402

_CODE = "002910"
_NAME = "易方达供给改革混合"
# 与真实端点同形的三个交易日：(日期, 单位净值, 日涨幅%, 分红说明)
_UNIT = (("2026-09-28", 7.8674, -1.50, ""),
         ("2026-09-29", 7.8755, 0.10, ""),
         ("2026-09-30", 7.9474, 0.91, ""))
# 累计净值（本样本从未分红，故与单位净值相同）
_CUM = (("2026-09-28", 7.8674), ("2026-09-29", 7.8755), ("2026-09-30", 7.9474))


def _local_midnight_ms(date_str: str) -> int:
    """``YYYY-MM-DD`` 的**本地**零点 → 毫秒时间戳；纯算术，不受运行机时区影响。"""
    d = datetime.strptime(date_str, "%Y-%m-%d")
    epoch_days = (d.date() - datetime(1970, 1, 1).date()).days
    offset_ms = (datetime(d.year, d.month, d.day).astimezone().utcoffset()
                 or timedelta(0)).total_seconds() * 1000
    return int(epoch_days * 86_400_000 - offset_ms)


def _pz_js(name: str = _NAME, unit=_UNIT, cum=_CUM) -> str:
    """拼一段与真实 pingzhongdata 端点同构的 JS 文本。"""
    net = json.dumps(
        [{"x": _local_midnight_ms(d), "y": y, "equityReturn": r,
          "unitMoney": (json.dumps(m, ensure_ascii=False) if m else "")}
         for d, y, r, m in unit], ensure_ascii=False)
    ac = json.dumps([[_local_midnight_ms(d), y] for d, y in cum])
    return (f'var fS_name = "{name}";var fS_code = "{_CODE}";'
            f"var Data_netWorthTrend = {net};"
            f"var Data_ACWorthTrend = {ac};"
            f'var fund_sourceRate="1.50";')


def _fund_item(code: str = _CODE) -> ConfigItem:
    """用 `src.config.Item`：真实管线里传给抓取层的就是它（带 `.key`）。"""
    return ConfigItem(name=_NAME, symbol=code, market="cn", type="fund")


class _Resp:
    def __init__(self, text: str, status: int = 200):
        self.status_code = status
        self.content = text.encode("utf-8")


@pytest.fixture()
def workdir():
    """工作区内的临时目录（不用 tmp_path，避免沙箱里的符号链接限制）。"""
    d = _ROOT / f".tmp_test_fund_{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True, exist_ok=True)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture()
def cache_dir(workdir, monkeypatch):
    d = workdir / "fund_cache"
    monkeypatch.setattr(fund_fetch, "FUND_CACHE_DIR", d)
    return d


# --------------------------------------------------------------------- 代码规范化
def test_normalize_code():
    assert fund_fetch.normalize_code("002910") == "002910"
    assert fund_fetch.normalize_code("  2910 ") == "002910"      # 补足 6 位
    assert fund_fetch.normalize_code("sh510300") == "510300"     # 去前缀
    assert fund_fetch.normalize_code("SZ159915") == "159915"     # 大小写无关


def test_fetch_rejects_bad_code():
    with pytest.raises(fund_fetch.FundError, match="6 位数字"):
        fund_fetch.fetch_history(ConfigItem(name="x", symbol="abc", market="cn", type="fund"))


# --------------------------------------------------------------------- 时间戳口径
def test_timestamp_is_parsed_as_local_time():
    """★ 东方财富的 x 是**本地时间零点**；用 UTC 解析会整体提前一天。"""
    assert fund_fetch._to_date(_local_midnight_ms("2026-09-30")) == "2026-09-30"


def test_all_dates_round_trip():
    """连续多日都必须原样还原（覆盖跨月 / 跨年 / 闰年）。"""
    for d in ("2026-09-30", "2026-01-01", "2026-12-31", "2025-02-28", "2024-02-29"):
        assert fund_fetch._to_date(_local_midnight_ms(d)) == d


def test_utc_parsing_would_be_wrong():
    """把这个坑钉死：UTC 解析在 UTC+8 下会得到前一天（本机即为 UTC+8）。"""
    ms = _local_midnight_ms("2026-09-30")
    assert fund_fetch._to_date(ms) == "2026-09-30"
    if datetime.now().astimezone().utcoffset() == timedelta(hours=8):
        utc_view = datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")
        assert utc_view == "2026-09-29"        # 这正是先前误判的由来


# --------------------------------------------------------------------- 取值边界
def test_js_var_survives_semicolon_inside_string():
    """★ 字符串里的分号不能截断取值：否则 json.loads 报 Extra data。"""
    js = ('var fS_name = "A；B";'
          'var Data_netWorthTrend = [{"x": 1, "y": 1.0, "unitMoney": "a;b"}];')
    raw = fund_fetch._js_var(js, "Data_netWorthTrend")
    assert json.loads(raw) == [{"x": 1, "y": 1.0, "unitMoney": "a;b"}]


def test_js_var_survives_jsdoc_comment_boundary():
    """★ 真实响应变量之间夹着 JSDoc 注释，注释里的 `*` 不能被当成变量边界。

    真实形状：`...}];/*累计净值走势*/var Data_ACWorthTrend = ...`
    """
    js = ('var Data_netWorthTrend = [{"x": 1, "y": 1.0}];/*累计净值走势*/'
          'var Data_ACWorthTrend = [[1, 1.0]];/*累计收益率走势*/'
          'var Data_grandTotal = [];')
    assert json.loads(fund_fetch._js_var(js, "Data_netWorthTrend")) == [{"x": 1, "y": 1.0}]
    assert json.loads(fund_fetch._js_var(js, "Data_ACWorthTrend")) == [[1, 1.0]]
    assert json.loads(fund_fetch._js_var(js, "Data_grandTotal")) == []


def test_js_var_last_variable_without_comment():
    """注释之后的表达式里若含字符串 'var'，也要能正确截断。"""
    js = 'var Data_ACWorthTrend = [[1, 1.0]];/*x*/var other = "var not a declaration";'
    assert json.loads(fund_fetch._js_var(js, "Data_ACWorthTrend")) == [[1, 1.0]]


def test_js_var_missing_returns_none():
    assert fund_fetch._js_var('var fS_name = "X";', "Data_netWorthTrend") is None


def test_parse_real_shape_with_comments():
    """用贴近真实响应的（带注释）文本跑一遍完整解析。"""
    js = _pz_js().replace("var Data_ACWorthTrend", "/*累计净值走势*/var Data_ACWorthTrend")
    js = js.replace('var fund_sourceRate', '/*累计收益率走势*/var fund_sourceRate')
    name, df = fund_fetch.parse_pingzhong(js)
    assert name == _NAME
    assert df["close"].tolist() == [7.8674, 7.8755, 7.9474]


def test_parse_survives_semicolon_in_detail_and_name():
    """真实数据里基金名/分红说明可能含分号，解析必须不受影响。"""
    js = _pz_js(name="某某基金；A类",
                unit=(("2026-09-30", 1.5, 0.5, "分红；每份派现金0.03元"),))
    name, df = fund_fetch.parse_pingzhong(js)
    assert name == "某某基金；A类"
    assert df["detail"].iloc[0] == "分红；每份派现金0.03元"


# --------------------------------------------------------------------- 解析
def test_parse_uses_cumulative_nav_as_close():
    name, df = fund_fetch.parse_pingzhong(_pz_js())
    assert name == _NAME
    assert list(df.columns) == ["date", "nav_unit", "nav_cum", "close", "pct_chg", "detail"]
    assert df["date"].tolist() == ["2026-09-28", "2026-09-29", "2026-09-30"]   # 升序
    assert df["close"].tolist() == [7.8674, 7.8755, 7.9474]      # 落盘口径 = 累计净值
    assert df["nav_unit"].tolist() == [7.8674, 7.8755, 7.9474]
    assert df["pct_chg"].tolist() == [-1.50, 0.10, 0.91]


def test_parse_prefers_cumulative_over_unit_when_they_differ():
    """分红过的基金：单位净值被除权压低，累计净值才是可比口径。"""
    unit = (("2026-09-28", 0.8289, -2.59, ""),
            ("2026-09-29", 0.8282, -0.08, "分红 每份派现金0.03元"))
    cum = (("2026-09-28", 11.1627), ("2026-09-29", 11.1599))
    _name, df = fund_fetch.parse_pingzhong(_pz_js(unit=unit, cum=cum))
    assert df["nav_unit"].tolist() == [0.8289, 0.8282]          # 单位净值原样保留
    assert df["close"].tolist() == [11.1627, 11.1599]           # 落盘用累计净值
    assert df["detail"].iloc[1] == "分红 每份派现金0.03元"


def test_parse_falls_back_to_unit_when_cum_missing():
    """累计净值缺失（尚未公布）时回落为单位净值，不丢点。"""
    _n, df = fund_fetch.parse_pingzhong(_pz_js(cum=(("2026-09-28", 7.8674),)))
    assert df["close"].tolist() == [7.8674, 7.8755, 7.9474]


def test_parse_dedupes_same_date():
    """同一天重复出现 → 保留最后一条。"""
    unit = (("2026-09-30", 1.0, 0.0, ""), ("2026-09-30", 2.0, 0.0, ""))
    _n, df = fund_fetch.parse_pingzhong(_pz_js(unit=unit, cum=()))
    assert df["close"].tolist() == [2.0]


def test_parse_skips_dirty_points():
    """缺值 / 坏时间戳 / 非字典的点跳过，不污染序列。"""
    js = ('var fS_name = "X";'
          'var Data_netWorthTrend = ['
          f'{{"x": {_local_midnight_ms("2026-09-30")}, "y": 1.5, "equityReturn": null}},'
          f'{{"x": {_local_midnight_ms("2026-09-29")}, "y": null}},'      # 无净值 → 跳过
          '{"x": null, "y": 2.0},'                                        # 无日期 → 跳过
          '"垃圾数据",'                                                    # 非 dict → 跳过
          f'{{"x": {_local_midnight_ms("2026-09-28")}, "y": 1.2}}]'
          'var Data_ACWorthTrend = [];')
    _n, df = fund_fetch.parse_pingzhong(js)
    assert df["date"].tolist() == ["2026-09-28", "2026-09-30"]
    assert df["close"].tolist() == [1.2, 1.5]


def test_parse_decodes_html_in_detail():
    raw = "<b>分红</b> 每份派现金0.03元&nbsp;"
    _n, df = fund_fetch.parse_pingzhong(_pz_js(unit=(("2026-09-30", 1.0, 0.0, raw),), cum=()))
    assert df["detail"].iloc[0] == "分红 每份派现金0.03元"


def test_parse_tolerates_malformed_accumulated_series():
    """累计净值段是脏数据时，退化为单位净值，不抛异常。"""
    js = ('var fS_name = "X";'
          f'var Data_netWorthTrend = [{{"x": {_local_midnight_ms("2026-09-30")}, "y": 3.0}}];'
          'var Data_ACWorthTrend = "不是数组";')
    _n, df = fund_fetch.parse_pingzhong(js)
    assert df["close"].tolist() == [3.0]


def test_parse_raises_on_empty_or_missing():
    with pytest.raises(fund_fetch.FundError):
        fund_fetch.parse_pingzhong("var Data_netWorthTrend = [];")
    with pytest.raises(fund_fetch.FundError):
        fund_fetch.parse_pingzhong('var fS_name = "X";')          # 完全没有净值字段


# --------------------------------------------------------------------- 网络层（假响应）
def test_fetch_history_end_to_end(monkeypatch):
    seen = {}

    def fake_get(url, headers=None, timeout=None):
        seen["url"] = url
        seen["referer"] = (headers or {}).get("Referer")
        return _Resp(_pz_js())

    monkeypatch.setattr(fund_fetch.requests, "get", fake_get)
    name, df = fund_fetch.fetch_history(_fund_item())
    assert name == _NAME
    assert list(df.columns) == ["date", "close"]
    assert df["date"].dtype.kind == "M"                          # 已转 datetime
    assert f"{_CODE}.js" in seen["url"]
    assert seen["referer"] == "https://fund.eastmoney.com/"      # 缺 Referer 会被拒


def test_fetch_history_raises_on_http_error(monkeypatch):
    monkeypatch.setattr(fund_fetch.requests, "get", lambda *a, **k: _Resp("", status=403))
    with pytest.raises(fund_fetch.FundError, match="HTTP 403"):
        fund_fetch.fetch_history(_fund_item())


# --------------------------------------------------------------------- 缓存
def test_cache_roundtrip(cache_dir, monkeypatch):
    it = _fund_item()
    monkeypatch.setattr(fund_fetch.requests, "get", lambda *a, **k: _Resp(_pz_js()))
    name, detail = fund_fetch.fetch_detail(it)
    fund_fetch.save_cache(it.key, name, detail)

    cached = fund_fetch.load_cache(it.key)
    assert cached["code"] == _CODE
    assert cached["caliber"] == "cumulative_nav"
    assert cached["n"] == 3
    assert cached["last_date"] == "2026-09-30"
    assert fund_fetch.cache_history(cached)["close"].tolist() == [7.8674, 7.8755, 7.9474]


def test_cache_freshness(cache_dir):
    today = pd.Timestamp.today().normalize()
    fresh = {"nav": [{"date": today.strftime("%Y-%m-%d")}]}
    stale = {"nav": [{"date": (today - pd.Timedelta(days=40)).strftime("%Y-%m-%d")}]}
    assert fund_fetch.cache_is_fresh(fresh)
    assert not fund_fetch.cache_is_fresh(stale)
    assert not fund_fetch.cache_is_fresh(None)
    assert not fund_fetch.cache_is_fresh({"nav": []})


def test_load_cache_ignores_corrupt_file(cache_dir):
    fund_fetch.FUND_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fund_fetch._cache_path(f"cn_fund_{_CODE}").write_text("{ 坏 JSON", encoding="utf-8")
    assert fund_fetch.load_cache(f"cn_fund_{_CODE}") is None


def test_load_or_fetch_prefers_fresh_cache(cache_dir, monkeypatch):
    """缓存新鲜 → 不发请求；缓存过期 → 重新抓取并覆盖。"""
    it = _fund_item()
    today = pd.Timestamp.today().normalize()
    fund_fetch.FUND_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fund_fetch._cache_path(it.key).write_text(json.dumps({
        "name": "缓存里的名字", "caliber": "cumulative_nav",
        "nav": [{"date": today.strftime("%Y-%m-%d"), "close": 9.9}],
    }, ensure_ascii=False), encoding="utf-8")

    def boom(*a, **k):
        raise AssertionError("缓存新鲜时不该发网络请求")

    monkeypatch.setattr(fund_fetch.requests, "get", boom)
    name, df = fund_fetch.load_or_fetch(it)
    assert name == "缓存里的名字" and df["close"].tolist() == [9.9]

    fund_fetch._cache_path(it.key).write_text(json.dumps({
        "name": "旧", "nav": [{"date": (today - pd.Timedelta(days=40)).strftime("%Y-%m-%d"),
                               "close": 1.0}]}, ensure_ascii=False), encoding="utf-8")
    calls = {"n": 0}

    def fake_get(*a, **k):
        calls["n"] += 1
        return _Resp(_pz_js())

    monkeypatch.setattr(fund_fetch.requests, "get", fake_get)
    name, df = fund_fetch.load_or_fetch(it)
    assert calls["n"] == 1 and name == _NAME


# --------------------------------------------------------------------- 当日快照
def test_daily_snapshot_shape_matches_quote_snapshot(cache_dir, monkeypatch):
    """快照必须与 quote_fetch.snapshot() 的行同构，daily_close 才能无改动复用。"""
    monkeypatch.setattr(fund_fetch.requests, "get", lambda *a, **k: _Resp(_pz_js()))
    row = fund_fetch.daily_snapshot(_fund_item())

    required = set(quote_snapshot([QuoteItem("茅台", "600519", "cn", "stock")]).columns)
    assert required <= set(row)                        # 必需的列一个都不能少
    assert row["symbol"] == _CODE
    assert row["type"] == "fund"
    assert row["market"] == "cn"
    assert row["date"] == "2026-09-30"
    assert row["close"] == 7.9474                      # 累计净值
    assert row["unit_nav"] == 7.9474
    assert row["pct_chg"] == pytest.approx(0.0091)     # 0.91% → 0.0091
    assert row["prev_close"] == pytest.approx(7.8755, abs=1e-3)   # 单位净值口径昨值
    assert row["source"] == "eastmoney"


def test_daily_snapshot_writes_cache(cache_dir, monkeypatch):
    """抓当日时顺手落缓存，seed_missing 就不必再抓一次全量。"""
    monkeypatch.setattr(fund_fetch.requests, "get", lambda *a, **k: _Resp(_pz_js()))
    it = _fund_item()
    fund_fetch.daily_snapshot(it)
    cached = fund_fetch.load_cache(it.key)
    assert cached is not None and cached["n"] == 3


def test_daily_snapshot_handles_missing_pct(cache_dir, monkeypatch):
    js = _pz_js(unit=(("2026-09-30", 7.9474, None, ""),), cum=(("2026-09-30", 7.9474),))
    monkeypatch.setattr(fund_fetch.requests, "get", lambda *a, **k: _Resp(js))
    row = fund_fetch.daily_snapshot(_fund_item())
    assert row["pct_chg"] is None and row["prev_close"] is None
