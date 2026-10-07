"""多标的 What-if（反事实）与录入行解析的离线单测（阶段 3）。运行： python -m pytest tests/ -q"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.analysis import (  # noqa: E402
    counterfactual_curve,
    parse_holding,
    switch_counterfactuals,
)
from tools import admin_ops  # noqa: E402


def _pf(holdings, values, dates=("2026-09-24", "2026-09-25", "2026-09-28", "2026-09-29")):
    return pd.DataFrame({"date": pd.to_datetime(list(dates)),
                         "holding": holdings, "value": values})


def _prices():
    return {
        "A": pd.Series([10.0, 11.0, 12.0, 12.0],
                       index=pd.to_datetime(["2026-09-24", "2026-09-25", "2026-09-28", "2026-09-29"])),
        "B": pd.Series([20.0, 20.0, 22.0, 24.0],
                       index=pd.to_datetime(["2026-09-24", "2026-09-25", "2026-09-28", "2026-09-29"])),
        "C": pd.Series([5.0, 5.0, 5.0, 5.0],
                       index=pd.to_datetime(["2026-09-24", "2026-09-25", "2026-09-28", "2026-09-29"])),
    }


# --------------------------------------------------------------- 单标的（回归）
def test_single_leg_whatif_unchanged():
    df = _pf(["A", "A", "B", "B"], [1_000_000.0, 1_100_000.0, 1_200_000.0, 1_300_000.0])
    cf = switch_counterfactuals(df, _prices())
    assert len(cf) == 1
    # 不调仓 = 一直持 A：1_100_000 × A(09-29)/A(09-25) = 1_100_000 × 12/11
    assert cf["whatif_today"].iloc[0] == pytest.approx(1_100_000.0 * 12 / 11)


# --------------------------------------------------------------- 多标的
def test_multi_leg_whatif_uses_old_basket():
    """旧组合 60/40（A/B）→ 调仓到 C；不调仓 = 按 buy & hold 继续持 60/40。"""
    df = _pf(["A:0.6|B:0.4", "A:0.6|B:0.4", "C", "C"],
             [1_000_000.0, 1_100_000.0, 1_150_000.0, 1_200_000.0])
    cf = switch_counterfactuals(df, _prices())
    assert len(cf) == 1
    r = cf.iloc[0]
    assert str(r["switch_date"].date()) == "2026-09-25"      # 调仓日（当日算旧组合）
    # 0.6 × A(09-29)/A(09-25) + 0.4 × B(09-29)/B(09-25) = 0.6×12/11 + 0.4×24/20
    expect = 1_100_000.0 * (0.6 * 12 / 11 + 0.4 * 24 / 20)
    assert r["whatif_today"] == pytest.approx(expect)
    assert r["diff"] == pytest.approx(1_200_000.0 - expect)


def test_multi_leg_missing_leg_gives_nan():
    df = _pf(["A:0.5|ZZZ:0.5", "A:0.5|ZZZ:0.5", "C", "C"],
             [1_000_000.0, 1_100_000.0, 1_150_000.0, 1_200_000.0])
    cf = switch_counterfactuals(df, _prices())          # ZZZ 没有价格
    assert cf["whatif_today"].isna().all()
    assert cf["diff"].isna().all()
    assert (cf["whatif_today"] / 1e4).isna().all()      # 下游可做除法


def test_counterfactual_curve_multi_leg():
    df = _pf(["A:0.6|B:0.4", "A:0.6|B:0.4", "C", "C"],
             [1_000_000.0, 1_100_000.0, 1_150_000.0, 1_200_000.0])
    curve = counterfactual_curve(df, "2026-09-25", "A:0.6|B:0.4", _prices())
    assert list(curve.index.strftime("%Y-%m-%d")) == ["2026-09-25", "2026-09-28", "2026-09-29"]
    assert curve.iloc[0] == pytest.approx(1_100_000.0)                       # 调仓日 = 基准
    assert curve.iloc[-1] == pytest.approx(1_100_000.0 * (0.6 * 12 / 11 + 0.4 * 24 / 20))
    # 缺一条腿 → 空曲线（看板跳过该线）
    assert counterfactual_curve(df, "2026-09-25", "A:0.5|ZZZ:0.5", _prices()).empty


def test_counterfactual_curve_single_leg_regression():
    df = _pf(["A", "A", "B", "B"], [1_000_000.0, 1_100_000.0, 1_200_000.0, 1_300_000.0])
    curve = counterfactual_curve(df, "2026-09-25", "A", _prices())
    assert curve.iloc[0] == pytest.approx(1_100_000.0)
    assert curve.iloc[-1] == pytest.approx(1_100_000.0 * 12 / 11)


# --------------------------------------------------------------- 管理台行解析
def test_legs_from_rows_filters_and_merges():
    rows = [
        {"代码": "600519", "市场": "cn", "类型": "stock", "权重": 0.6},
        {"代码": "AAPL", "市场": "us", "类型": "stock", "权重": 0.4},
        {"代码": "", "市场": "cn", "类型": "stock", "权重": float("nan")},   # 空行 → 丢弃
        {"代码": "AAPL", "市场": "us", "类型": "stock", "权重": 0.2},        # 重复 → 权重相加
    ]
    assert admin_ops.legs_from_rows(rows) == [
        ("600519", "cn", "stock", 0.6, ""),
        ("AAPL", "us", "stock", 0.6000000000000001, "")]


def test_legs_from_rows_defaults():
    rows = [{"代码": "600519", "市场": None, "类型": None, "权重": None},
            {"代码": "X", "市场": "jp", "类型": "stock", "权重": 0}]
    out = admin_ops.legs_from_rows(rows)
    # 非法 market 回落；权重 0 → 丢弃；第 5 项 = 到期日（未填则空）
    assert out == [("600519", "cn", "stock", 1.0, "")]


def test_legs_from_rows_rejects_unknown_type():
    """未知类型必须**报错**，不能静默变成 stock（会污染 key 与取数分支）。"""
    rows = [{"代码": "002910", "市场": "cn", "类型": "fnd", "权重": 1.0}]
    with pytest.raises(ValueError, match="未知的类型"):
        admin_ops.legs_from_rows(rows)
    with pytest.raises(ValueError, match="fund"):
        admin_ops.legs_from_rows(rows)          # 报错信息里应提示正确写法


def test_legs_from_rows_accepts_fund():
    rows = [{"代码": "002910", "市场": "cn", "类型": "fund", "权重": 1.0}]
    assert admin_ops.legs_from_rows(rows) == [("002910", "cn", "fund", 1.0, "")]


def test_legs_from_rows_accepts_futures_with_expiry():
    """期货：第 5 项带到期日；空值/NaN 要规范成空串。"""
    rows = [{"代码": "SC2611", "市场": "cn", "类型": "futures", "权重": 0.3,
             "到期日": "2026-10-30"},
            {"代码": "SC2612", "市场": "cn", "类型": "futures", "权重": 0.2,
             "到期日": float("nan")}]
    assert admin_ops.legs_from_rows(rows) == [
        ("SC2611", "cn", "futures", 0.3, "2026-10-30"),
        ("SC2612", "cn", "futures", 0.2, "")]


def test_legs_from_rows_expiry_merges_for_same_symbol():
    """同一标的重复出现：权重相加，到期日取最后一次填的值。"""
    rows = [{"代码": "SC2611", "市场": "cn", "类型": "futures", "权重": 0.2},
            {"代码": "SC2611", "市场": "cn", "类型": "futures", "权重": 0.1,
             "到期日": "2026-10-30"}]
    assert admin_ops.legs_from_rows(rows) == [("SC2611", "cn", "futures", 0.30000000000000004,
                                              "2026-10-30")]
