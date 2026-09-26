"""自定义等权指数构建的离线单测（不联网）。运行： python -m pytest tests/ -q"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prototype import index_build  # noqa: E402
from src.config import Item  # noqa: E402


def _series(pairs) -> pd.Series:
    return pd.Series([v for _, v in pairs],
                     index=pd.to_datetime([d for d, _ in pairs]))


def test_index_base_is_configured_start(monkeypatch):
    a = _series([("2026-09-22", 10.0), ("2026-09-23", 11.0),
                 ("2026-09-24", 12.0), ("2026-09-25", 13.0)])
    b = _series([("2026-09-23", 20.0), ("2026-09-24", 22.0), ("2026-09-25", 24.0)])
    holdings = [Item("A", "AAA", "cn", "stock", "2026-09-24"),
                Item("B", "BBB", "cn", "stock", "2026-09-24")]
    monkeypatch.setattr(index_build, "load_holdings", lambda: holdings)
    monkeypatch.setattr(index_build, "_load_prices",
                        lambda k: {"cn_AAA": a, "cn_BBB": b}[k])

    df, keys, d0 = index_build.build()
    assert str(d0.date()) == "2026-09-24"                     # 基准日 = 配置的 start_date
    assert df.loc[df["date"] == d0, "index"].iloc[0] == pytest.approx(100.0)
    # 2026-09-25：A=13/12，B=24/22 → 等权均值 ×100
    exp = 100.0 * ((13 / 12 + 24 / 22) / 2)
    assert df["index"].iloc[-1] == pytest.approx(exp, abs=1e-3)


def test_index_warns_and_shifts_when_component_starts_late(monkeypatch, capsys):
    a = _series([("2026-09-24", 12.0), ("2026-09-25", 13.0)])
    b = _series([("2026-09-26", 5.0)])            # 晚于基准日（模拟新加未回填）
    holdings = [Item("A", "AAA", "cn", "stock", "2026-09-24"),
                Item("B", "BBB", "cn", "stock", "2026-09-24")]
    monkeypatch.setattr(index_build, "load_holdings", lambda: holdings)
    monkeypatch.setattr(index_build, "_load_prices",
                        lambda k: {"cn_AAA": a, "cn_BBB": b}[k])

    df, keys, d0 = index_build.build()
    assert str(d0.date()) == "2026-09-26"         # 顺延到共同起点
    assert "backfill" in capsys.readouterr().out  # 且明确提示补 backfill
