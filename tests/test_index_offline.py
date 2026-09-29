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


def _patch(monkeypatch, investors, prices):
    monkeypatch.setattr(index_build, "load_investors", lambda: investors)
    monkeypatch.setattr(index_build, "load_investor_config", lambda: dict(_CFG))
    monkeypatch.setattr(index_build, "_load_prices", lambda k: prices[k])


def test_index_base_is_configured_start(monkeypatch):
    a = _series([("2026-09-22", 10.0), ("2026-09-23", 11.0),
                 ("2026-09-24", 12.0), ("2026-09-25", 13.0)])
    b = _series([("2026-09-23", 20.0), ("2026-09-24", 22.0), ("2026-09-25", 24.0)])
    investors = [Investor("Alice", "AAA", "cn", "stock", "2026-09-24"),
                 Investor("Bob", "BBB", "cn", "stock", "2026-09-24")]
    _patch(monkeypatch, investors, {"cn_stock_AAA": a, "cn_stock_BBB": b})

    df, keys, d0 = index_build.build()
    assert str(d0.date()) == "2026-09-24"                     # 基准日 = 配置的 start_date
    assert str(df["date"].iloc[0].date()) == "2026-09-24"     # 不展示基准日之前
    assert df.loc[df["date"] == d0, "index"].iloc[0] == pytest.approx(100.0)
    # 2026-09-25：A=13/12，B=24/22 → 等权均值 ×100
    exp = 100.0 * ((13 / 12 + 24 / 22) / 2)
    assert df["index"].iloc[-1] == pytest.approx(exp, abs=1e-3)


def test_index_weights_by_holder_count(monkeypatch):
    """同一标的被 2 人持有时，权重按人数累加（按人平均，而非按标的平均）。"""
    a = _series([("2026-09-24", 10.0), ("2026-09-25", 20.0)])   # +100%
    b = _series([("2026-09-24", 10.0), ("2026-09-25", 10.0)])   # 持平
    investors = [Investor("Alice", "AAA", "cn", "stock", "2026-09-24"),
                 Investor("Bob", "AAA", "cn", "stock", "2026-09-24"),
                 Investor("Carol", "BBB", "cn", "stock", "2026-09-24")]
    _patch(monkeypatch, investors, {"cn_stock_AAA": a, "cn_stock_BBB": b})

    df, keys, d0 = index_build.build()
    # 按人平均：(2/3)×(20/10) + (1/3)×(10/10) = 1.6667 → 166.67
    assert df["index"].iloc[-1] == pytest.approx(100.0 * (2 / 3 * 2.0 + 1 / 3 * 1.0), abs=1e-3)


def test_index_warns_and_shifts_when_component_starts_late(monkeypatch, capsys):
    a = _series([("2026-09-24", 12.0), ("2026-09-25", 13.0)])
    b = _series([("2026-09-26", 5.0)])            # 晚于基准日（模拟新加未回填）
    investors = [Investor("Alice", "AAA", "cn", "stock", "2026-09-24"),
                 Investor("Bob", "BBB", "cn", "stock", "2026-09-24")]
    _patch(monkeypatch, investors, {"cn_stock_AAA": a, "cn_stock_BBB": b})

    df, keys, d0 = index_build.build()
    assert str(d0.date()) == "2026-09-26"         # 顺延到共同起点
    assert "backfill" in capsys.readouterr().out  # 且明确提示补 backfill
