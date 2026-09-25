"""计算层单测（不依赖网络）。运行： python -m pytest tests/ -q"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.returns import (  # noqa: E402
    annualized_return,
    cumulative_return,
    max_drawdown,
)


def _prices():
    return pd.DataFrame({
        "date": pd.to_datetime(["2023-01-03", "2023-06-01", "2023-12-29", "2024-12-31"]),
        "close": [10.0, 12.0, 9.0, 15.0],
    })


def test_cumulative_return_base_is_zero():
    ret = cumulative_return(_prices(), "2023-01-03")
    assert ret["cum_return"].iloc[0] == 0.0
    assert ret["nav"].iloc[0] == 100.0
    # 10 -> 15 => +50%
    assert ret["cum_return"].iloc[-1] == pytest.approx(0.5)


def test_start_date_not_trading_day_rolls_forward():
    ret = cumulative_return(_prices(), "2023-01-01")  # 元旦，非交易日
    assert ret["date"].iloc[0] == pd.Timestamp("2023-01-03")
    assert ret["cum_return"].iloc[0] == 0.0


def test_start_after_data_raises():
    with pytest.raises(ValueError):
        cumulative_return(_prices(), "2030-01-01")


def test_max_drawdown():
    ret = cumulative_return(_prices(), "2023-01-03")
    # 峰值 12 -> 谷底 9 => -25%
    assert max_drawdown(ret["nav"]) == pytest.approx(-0.25)


def test_annualized_return_positive():
    ret = cumulative_return(_prices(), "2023-01-03")
    ann = annualized_return(ret)
    assert ann > 0  # 总收益 +50%，跨 ~2 年 -> 年化为正
