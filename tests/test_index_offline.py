"""群体平均指数构建的离线单测（不联网）。运行： python -m pytest tests/ -q"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prototype import index_build  # noqa: E402
from src.config import Investor  # noqa: E402

_CFG = {"start_date": "2026-09-24", "principal": 1_000_000, "base_currency": "CNY"}


def _series(pairs) -> pd.Series:
    return pd.Series([v for _, v in pairs],
                     index=pd.to_datetime([d for d, _ in pairs]))


def _patch(monkeypatch, investors, navs):
    monkeypatch.setattr(index_build, "load_investors", lambda: investors)
    monkeypatch.setattr(index_build, "load_investor_config", lambda: dict(_CFG))
    monkeypatch.setattr(index_build, "_load_nav", lambda nick: navs[nick])


def test_index_averages_investor_navs(monkeypatch):
    a = _series([("2026-09-24", 100.0), ("2026-09-25", 110.0)])   # +10%
    b = _series([("2026-09-24", 100.0), ("2026-09-25", 90.0)])    # -10%
    investors = [Investor("Alice", "AAA", "cn", "stock", "2026-09-24"),
                 Investor("Bob", "BBB", "cn", "stock", "2026-09-24")]
    _patch(monkeypatch, investors, {"Alice": a, "Bob": b})

    df, names, d0 = index_build.build()
    assert str(d0.date()) == "2026-09-24"
    assert str(df["date"].iloc[0].date()) == "2026-09-24"     # 不展示基准日之前
    assert df["index"].iloc[0] == pytest.approx(100.0)
    assert df["index"].iloc[-1] == pytest.approx(100.0)        # (110+90)/2 = 100
    assert names == ["Alice", "Bob"]


def test_index_each_investor_equal_weight(monkeypatch):
    a = _series([("2026-09-24", 100.0), ("2026-09-25", 200.0)])   # +100%
    b = _series([("2026-09-24", 100.0), ("2026-09-25", 100.0)])   # 持平
    c = _series([("2026-09-24", 100.0), ("2026-09-25", 100.0)])   # 持平
    investors = [Investor("A", "A", "cn", "stock", "2026-09-24"),
                 Investor("B", "B", "cn", "stock", "2026-09-24"),
                 Investor("C", "C", "cn", "stock", "2026-09-24")]
    _patch(monkeypatch, investors, {"A": a, "B": b, "C": c})
    df, _, _ = index_build.build()
    # (200 + 100 + 100)/3 = 133.33
    assert df["index"].iloc[-1] == pytest.approx(100.0 * (2.0 + 1.0 + 1.0) / 3)


def test_index_warns_and_shifts_when_investor_starts_late(monkeypatch, capsys):
    a = _series([("2026-09-24", 100.0), ("2026-09-25", 110.0)])
    b = _series([("2026-09-26", 100.0)])            # 晚于基准日
    investors = [Investor("Alice", "AAA", "cn", "stock", "2026-09-24"),
                 Investor("Bob", "BBB", "cn", "stock", "2026-09-24")]
    _patch(monkeypatch, investors, {"Alice": a, "Bob": b})

    df, _, d0 = index_build.build()
    assert str(d0.date()) == "2026-09-26"           # 顺延到共同起点
    assert "起于" in capsys.readouterr().out
