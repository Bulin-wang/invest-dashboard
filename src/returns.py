"""累计收益 / 年化 / 最大回撤计算。

所有收益率口径基于「后复权 / 调整后价格」，即已含分红再投与拆股。
cumulative_return(t) = close(t) / close(start) - 1
"""
from __future__ import annotations

import pandas as pd


def cumulative_return(prices: pd.DataFrame, start_date) -> pd.DataFrame:
    """按起始日归一化。

    入参 prices: 含 [date, close] 的 DataFrame（已排序）。
    start_date 若非交易日，自动取其后第一个交易日为基准。
    返回新增列 cum_return（区间累计收益）与 nav（归一化到 100）。
    """
    ts = pd.Timestamp(start_date)
    sub = prices[prices["date"] >= ts].copy()
    if sub.empty:
        raise ValueError(f"起始日 {start_date} 之后没有行情数据")
    sub = sub.sort_values("date").reset_index(drop=True)
    base = float(sub["close"].iloc[0])
    if base <= 0:
        raise ValueError("基准价 <= 0，无法计算收益")
    sub["cum_return"] = sub["close"] / base - 1.0
    sub["nav"] = sub["close"] / base * 100.0
    return sub


def max_drawdown(nav: pd.Series) -> float:
    """给定归一化净值序列（起始=100），返回最大回撤（负数，如 -0.23）。"""
    roll_max = nav.cummax()
    dd = nav / roll_max - 1.0
    return float(dd.min())


def annualized_return(ret: pd.DataFrame) -> float:
    """按实际天数折算年化（几何），ret 需含 [date, cum_return]。"""
    if len(ret) < 2:
        return float("nan")
    days = (ret["date"].iloc[-1] - ret["date"].iloc[0]).days
    if days <= 0:
        return float("nan")
    total = float(ret["cum_return"].iloc[-1])
    years = days / 365.25
    base = 1.0 + total
    if base <= 0:  # 亏损超过 100%，年化无意义
        return float("nan")
    return base ** (1.0 / years) - 1.0


def calmar_ratio(ret: pd.DataFrame) -> float:
    """年化收益 / |最大回撤|，衡量风险调整后收益。"""
    mdd = max_drawdown(ret["nav"])
    if not mdd or mdd == 0:
        return float("nan")
    return annualized_return(ret) / abs(mdd)
