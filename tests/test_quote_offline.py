"""报价端点层离线单测（不联网）。运行： python -m pytest tests/ -q"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prototype.quote_fetch import (  # noqa: E402
    Item,
    parse_sina,
    parse_tencent,
    sina_code,
    tencent_code,
)
from prototype.daily_close import _append  # noqa: E402


# --------------------------------------------------------------------- 代码映射
def test_sina_code_cn():
    assert sina_code(Item("茅台", "600519", "cn", "stock")) == "sh600519"
    assert sina_code(Item("平安银行", "000001", "cn", "stock")) == "sz000001"
    assert sina_code(Item("创业板ETF", "159915", "cn", "etf")) == "sz159915"
    assert sina_code(Item("沪深300ETF", "510300", "cn", "etf")) == "sh510300"
    assert sina_code(Item("沪深300", "000300", "cn", "index")) == "sh000300"


def test_sina_code_us_and_hk():
    assert sina_code(Item("苹果", "AAPL", "us", "stock")) == "gb_aapl"
    assert sina_code(Item("标普500", "^GSPC", "us", "index")) == "gb_$inx"
    assert sina_code(Item("腾讯", "00700", "hk", "stock")) == "hk00700"


def test_tencent_code():
    assert tencent_code(Item("茅台", "600519", "cn", "stock")) == "sh600519"
    assert tencent_code(Item("苹果", "AAPL", "us", "stock")) == "usAAPL"
    assert tencent_code(Item("腾讯", "00700", "hk", "stock")) == "hk00700"


# --------------------------------------------------------------------- 新浪解析
def _sina_cn_payload():
    f = ["贵州茅台", "1250.010", "1251.240", "1237.000", "1256.130", "1231.050"]
    f += ["0"] * 24 + ["2026-09-24", "15:34:59", "00"]
    return ",".join(f)


def test_parse_sina_cn():
    q = parse_sina("sh600519", _sina_cn_payload())
    assert q == {"name": "贵州茅台", "close": 1237.0, "prev_close": 1251.24, "date": "2026-09-24"}


def test_parse_sina_us():
    q = parse_sina("gb_aapl", "苹果,339.1600,0.96,2026-09-26 00:50:26,3.2400,336.0400")
    assert q["close"] == 339.16
    assert q["prev_close"] == 335.92          # close - change
    assert q["date"] == "2026-09-26"


def test_parse_sina_hk():
    f = ["TENCENT", "腾讯控股", "433.8", "438.4", "437.2", "431.2", "436.6", "-1.8", "-0.411"]
    f += ["0"] * 8 + ["2026/09/25", "16:08"]
    q = parse_sina("hk00700", ",".join(f))
    assert q == {"name": "腾讯控股", "close": 436.6, "prev_close": 438.4, "date": "2026-09-25"}


def test_parse_sina_empty():
    assert parse_sina("sh600519", "") is None


# --------------------------------------------------------------------- 腾讯解析（备用源）
def test_parse_tencent_cn_compact_date():
    """A股腾讯返回的日期是 20260924161444（无分隔符），必须能解析。"""
    payload = 'v_sh600519="1~贵州茅台~600519~1237.00~1251.24~1250.01~0~0~0~20260924161444~-14.24~-1.14";'
    q = parse_tencent(payload)
    assert q["name"] == "贵州茅台"
    assert q["close"] == 1237.0
    assert q["prev_close"] == 1251.24
    assert q["date"] == "2026-09-24"


def test_parse_tencent_us_dashed_date():
    payload = 'v_usAAPL="200~苹果~AAPL.OQ~339.12~335.92~336.04~0~2026-09-25 12:50:19~3.20~0.95";'
    q = parse_tencent(payload)
    assert q["close"] == 339.12
    assert q["date"] == "2026-09-25"


def test_parse_tencent_empty():
    assert parse_tencent('v_x="";') is None


# --------------------------------------------------------------------- 增量去重
def test_append_is_idempotent_per_day():
    hist = pd.DataFrame({"date": pd.to_datetime([]), "close": pd.Series(dtype=float)})
    hist, added = _append(hist, "2026-09-24", 100.0)
    assert added and len(hist) == 1
    hist, added = _append(hist, "2026-09-24", 100.0)   # 同一天
    assert not added and len(hist) == 1
    hist, added = _append(hist, "2026-09-25", 110.0)
    assert added and len(hist) == 2
