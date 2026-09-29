"""backfill 历史回填的路由逻辑离线单测（不联网）。运行： python -m pytest tests/ -q"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prototype import backfill  # noqa: E402
from prototype.quote_fetch import Item  # noqa: E402


def test_hk_history_routes_to_tencent(monkeypatch):
    """港股历史现在走腾讯 fqkline（hk 前缀代码），不再报错。"""
    seen = {}

    def fake(code, n):
        seen["code"] = code
        seen["n"] = n
        return pd.DataFrame({"date": ["2026-09-24"], "close": [100.0]})

    monkeypatch.setattr(backfill, "_tencent_cn_daily", fake)
    df = backfill.fetch_history(Item("腾讯控股", "00700", "hk", "stock"), 300)
    assert seen["code"] == "hk00700" and seen["n"] == 300
    assert df["close"].iloc[0] == 100.0


def test_cn_history_routes_to_tencent(monkeypatch):
    seen = {}

    def fake(code, n):
        seen["code"] = code
        return pd.DataFrame({"date": ["2026-09-24"], "close": [10.0]})

    monkeypatch.setattr(backfill, "_tencent_cn_daily", fake)
    backfill.fetch_history(Item("贵州茅台", "600519", "cn", "stock"), 300)
    assert seen["code"] == "sh600519"


def test_us_history_routes_to_sina(monkeypatch):
    seen = {}

    def fake(sym, n):
        seen["sym"] = sym
        return pd.DataFrame({"date": ["2026-09-24"], "close": [1.0]})

    monkeypatch.setattr(backfill, "_sina_us_daily", fake)
    backfill.fetch_history(Item("苹果", "AAPL", "us", "stock"), 300)
    assert seen["sym"] == "AAPL"
