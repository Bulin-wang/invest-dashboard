"""【预留】组合聚合：把多标的按权重合并成一条总收益曲线。

当前版本先做单标的展示，此模块为后续扩展占位。

实现要点（待接）：
- 权重来源：holdings.yaml 的 weight 字段（等权 / 手工权重 / 市值权重）
- 汇率折算：A股人民币 + 美股美元 -> 统一基准币（base_currency）
  可用 yfinance "CNY=X" 或 akshare 汇率接口取每日中间价
- 建议按「每日再平衡」或「买入后不再平衡(buy&hold)」两种口径都算
"""
from __future__ import annotations

import pandas as pd


def portfolio_nav(returns_by_key: dict[str, pd.DataFrame],
                  weights: dict[str, float],
                  rebalance: bool = False) -> pd.DataFrame:
    """把各标的累计收益合并成组合净值曲线（归一化到 100）。

    入参：
      returns_by_key: {key: DataFrame[date, cum_return]}
      weights:        {key: weight}，内部会归一化到和为 1
      rebalance:      True=每日再平衡；False=买入后持有（初始权重权重）
    返回：DataFrame[date, nav]
    """
    raise NotImplementedError(
        "组合聚合待实现：需要权重（holdings.yaml.weight）与汇率折算（CNY=X）。"
    )
