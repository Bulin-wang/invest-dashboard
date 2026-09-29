"""投资者组合收益：把一条**持仓路径**（全仓切换）合成为累计收益曲线。

口径
----
- 全仓切换：在调仓日 t 的**收盘价**整体换仓；**当日算旧标的**，**次交易日起算新标的**。
- 无费用 / 无滑点 / 不计汇率。
- 连乘（复利）：
      value(t) = 本金 × ∏(已结束段收益) × close_当前段(t) / close_当前段(段起点)
    段收益 = close_下段起点 / close_本段起点。

用法：
    from src.portfolio import portfolio_nav
    df = portfolio_nav(series_by_key, [("2026-09-24", "cn_stock_600519"),
                                       ("2026-09-25", "us_stock_AAPL")], principal=1_000_000)
"""
from __future__ import annotations

import pandas as pd


def portfolio_nav(series_by_key: dict[str, pd.Series],
                  segments: list[tuple[str, str]],
                  principal: float = 1_000_000.0) -> pd.DataFrame:
    """按持仓路径合成投资者组合收益。

    入参：
      series_by_key: {key: 以日期为索引的收盘价 Series}（未复权）
      segments:      [(日期, key), ...]，首段为起始；后续每段表示一次全仓切换
      principal:     本金（元）
    返回：DataFrame[date, value, nav, cum_return]，date 自起始日（含）起。
    """
    if not segments:
        raise ValueError("segments 为空")
    frames: dict[str, pd.Series] = {}
    for _, k in segments:
        s = series_by_key.get(k)
        if s is None or s.dropna().empty:
            raise ValueError(f"缺少 {k} 的价格数据")
        frames[k] = s.dropna()

    cal = pd.DatetimeIndex(sorted(set().union(*[s.index for s in frames.values()])))
    px = pd.DataFrame({k: frames[k].reindex(cal).ffill() for k in frames})

    def _boundary(raw) -> pd.Timestamp | None:
        """调仓日若非交易日 → 顺延到下一交易日。"""
        cand = cal[cal >= pd.Timestamp(raw)]
        return cand[0] if len(cand) else None

    b = [_boundary(raw) for raw, _ in segments]
    if b[0] is None:
        raise ValueError(f"起始日 {segments[0][0]} 之后没有行情数据")
    d0 = b[0]

    out: dict[pd.Timestamp, float] = {}
    ratio = 1.0            # 当前段起点处的累计比值（相对本金）
    m = len(segments)
    for k in range(m):
        s = px[segments[k][1]]
        if k == 0:
            base_ts, base_px = d0, s.loc[d0]
            dates = s.index[s.index >= base_ts]
        else:
            if b[k] is None:          # 调仓日已在数据之后 → 该段无数据
                break
            base_ts, base_px = b[k], s.loc[b[k]]
            dates = s.index[s.index > base_ts]     # 调仓当日算旧标的，次日起算新标的
        end_ts = b[k + 1] if k + 1 < m else None
        if end_ts is not None:
            dates = dates[dates <= end_ts]
        for t in dates:
            out[t] = principal * ratio * (s.loc[t] / base_px)
        if end_ts is not None:
            ratio = ratio * (s.loc[end_ts] / base_px)

    df = (pd.DataFrame({"date": list(out.keys()), "value": list(out.values())})
          .sort_values("date").reset_index(drop=True))
    df["nav"] = df["value"] / principal * 100.0
    df["cum_return"] = df["nav"] / 100.0 - 1.0
    return df
