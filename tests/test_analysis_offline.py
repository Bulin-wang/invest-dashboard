"""附加分析（管理者手续费 / What-if）的离线单测（不联网）。"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.analysis import (  # noqa: E402
    manager_fee_series,
    switch_counterfactuals,
    switch_days,
)


def _pf() -> pd.DataFrame:
    return pd.DataFrame({
        "date": pd.to_datetime(["2026-09-24", "2026-09-25", "2026-09-28", "2026-09-29"]),
        "holding": ["A", "A", "B", "B"],
        "value": [1_000_000.0, 1_100_000.0, 1_210_000.0, 1_331_000.0],
    })


def _series(pairs) -> pd.Series:
    return pd.Series([v for _, v in pairs], index=pd.to_datetime([d for d, _ in pairs]))


def test_switch_days():
    sw = switch_days(_pf())
    assert len(sw) == 1
    r = sw.iloc[0]
    assert str(r["date"].date()) == "2026-09-25"      # 调仓日（当日算旧）
    assert r["old_key"] == "A" and r["new_key"] == "B"
    assert r["value"] == 1_100_000.0                  # 手续费基数 = 调仓当日组合市值


def test_manager_fee_series():
    sp500 = _series([("2026-09-24", 100.0), ("2026-09-25", 100.0),
                     ("2026-09-28", 110.0), ("2026-09-29", 121.0)])
    out = manager_fee_series({"inv": _pf()}, sp500, fee_rate=0.0001)
    # 09-25 收手续费 1100000×0.0001 = 110，投入 SP500，自 09-25 基准=100 累乘
    assert out["fees_received"].iloc[-1] == 110.0
    assert out["assets"].iloc[-1] == 110.0 * 121.0 / 100.0    # = 133.1
    assert out["assets"].iloc[0] == 0.0                       # 尚无费用


def test_switch_counterfactual_to_today():
    price = {
        "A": _series([("2026-09-24", 10.0), ("2026-09-25", 11.0),
                      ("2026-09-28", 12.0), ("2026-09-29", 12.0)]),
        "B": _series([("2026-09-28", 20.0), ("2026-09-29", 22.0)]),
    }
    cf = switch_counterfactuals(_pf(), price)
    assert len(cf) == 1
    r = cf.iloc[0]
    # 若无此次调仓：从 09-25 起一直持 A -> 1100000 × A(09-29)/A(09-25) = 1100000×12/11
    assert r["whatif_today"] == 1_100_000.0 * 12 / 11
    assert r["actual_today"] == 1_331_000.0
    assert r["diff"] == 1_331_000.0 - 1_100_000.0 * 12 / 11
