"""投资者组合收益（全仓切换，分段连乘）的离线单测（不联网）。"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.portfolio import portfolio_nav  # noqa: E402


def _s(pairs) -> pd.Series:
    return pd.Series([v for _, v in pairs], index=pd.to_datetime([d for d, _ in pairs]))


def test_single_segment_matches_price_return():
    series = {"A": _s([("2026-09-24", 100.0), ("2026-09-25", 110.0), ("2026-09-26", 99.0)])}
    df = portfolio_nav(series, [("2026-09-24", "A")], principal=100)
    assert df["value"].tolist() == pytest.approx([100.0, 110.0, 99.0])   # 单段 = 纯价格收益
    assert df["nav"].iloc[0] == 100.0
    assert df["cum_return"].iloc[-1] == pytest.approx(-0.01)
    assert df["holding"].tolist() == ["A", "A", "A"]                     # 全程持有 A


def test_two_segments_chain_and_switch_day_is_old():
    series = {
        "A": _s([("2026-09-24", 100.0), ("2026-09-25", 110.0), ("2026-09-26", 121.0)]),
        "B": _s([("2026-09-24", 50.0), ("2026-09-25", 60.0), ("2026-09-26", 66.0)]),
    }
    # 09-25 收盘从 A 全仓换到 B：09-25 当天算 A，09-26 起算 B
    df = portfolio_nav(series, [("2026-09-24", "A"), ("2026-09-25", "B")], principal=100)
    by_date = dict(zip(df["date"].dt.strftime("%Y-%m-%d"), df["value"]))
    assert by_date["2026-09-24"] == pytest.approx(100.0)
    assert by_date["2026-09-25"] == pytest.approx(110.0)     # 调仓当日仍算旧标的 A（不是 B 的 120）
    assert by_date["2026-09-26"] == pytest.approx(121.0)     # 1.10 × 1.10 = 1.21
    assert df["holding"].tolist() == ["A", "A", "B"]         # 09-25 仍算 A，09-26 起 B
    assert df["daily_return"].tolist()[1:] == pytest.approx([0.10, 0.10])


def test_switch_date_rolls_forward_to_next_trading_day():
    series = {
        "A": _s([("2026-09-24", 100.0), ("2026-09-25", 100.0)]),
        "B": _s([("2026-09-24", 10.0), ("2026-09-28", 20.0), ("2026-09-29", 30.0)]),
    }
    # 调仓日 09-26 非交易日 → 顺延到下一交易日 09-28
    df = portfolio_nav(series, [("2026-09-24", "A"), ("2026-09-26", "B")], principal=100)
    by_date = dict(zip(df["date"].dt.strftime("%Y-%m-%d"), df["value"]))
    assert by_date["2026-09-28"] == pytest.approx(100.0)     # 顺延当日仍算旧标的 A（持平）
    assert by_date["2026-09-29"] == pytest.approx(150.0)     # B 20→30 = +50%


def test_missing_prices_raises():
    with pytest.raises(ValueError):
        portfolio_nav({}, [("2026-09-24", "A")])
