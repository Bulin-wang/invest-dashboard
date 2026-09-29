"""附加分析：管理者（券商）手续费视角 + 每次调仓的 What-if 反事实。

都是**基于已落盘数据**（data/portfolios、data/prices）的再计算，不改主口径。
"""
from __future__ import annotations

import pandas as pd

DEFAULT_FEE_RATE = 0.0001   # 万分之一（0.01%）


def switch_days(df: pd.DataFrame) -> pd.DataFrame:
    """从投资者组合序列里找每次调仓。

    入参 df：data/portfolios/{昵称}.csv（含 date, holding, value）。
    返回每行 = 一次调仓 {date(调仓日·当日算旧), old_key, new_key, value(调仓当日组合市值)}。
    """
    d = df.sort_values("date").reset_index(drop=True)
    changes = [i for i in d.index[d["holding"] != d["holding"].shift()].tolist() if i > 0]
    rows = [{
        "date": d["date"].iloc[i - 1],       # 新标的首个交易日的"前一天" = 调仓日（当日算旧）
        "old_key": d["holding"].iloc[i - 1],
        "new_key": d["holding"].iloc[i],
        "value": float(d["value"].iloc[i - 1]),
    } for i in changes]
    return pd.DataFrame(rows, columns=["date", "old_key", "new_key", "value"])


def fee_events(portfolios: dict[str, pd.DataFrame],
               fee_rate: float = DEFAULT_FEE_RATE) -> pd.DataFrame:
    """每次调仓收 fee_rate × 当日组合市值。返回 [date, nickname, value, fee]。"""
    rows = []
    for inv, df in portfolios.items():
        for _, r in switch_days(df).iterrows():
            rows.append({"date": pd.Timestamp(r["date"]), "nickname": inv,
                         "value": r["value"], "fee": r["value"] * fee_rate})
    return pd.DataFrame(rows, columns=["date", "nickname", "value", "fee"])


def manager_fee_series(portfolios: dict[str, pd.DataFrame],
                       sp500: pd.Series,
                       fee_rate: float = DEFAULT_FEE_RATE) -> pd.DataFrame:
    """管理者（券商）视角：手续费收入投入 SP500，自调仓日（次交易日起）按 SP500 累乘。

    返回 DataFrame[date, fees_received(累计面值), assets(投 SP500 后市值)]，日期为 SP500 日历。
    """
    cal = sp500.dropna().sort_index()
    ev = fee_events(portfolios, fee_rate)
    if cal.empty or ev.empty:
        return pd.DataFrame(columns=["date", "fees_received", "assets"])
    assets = pd.Series(0.0, index=cal.index)
    face = pd.Series(0.0, index=cal.index)
    for _, e in ev.iterrows():
        cand = cal.index[cal.index >= e["date"]]      # 调仓日若非 SP500 交易日 → 顺延到下一交易日
        if len(cand) == 0:
            continue
        b = cand[0]
        seg = cal.loc[cal.index >= b] / cal.loc[b] * float(e["fee"])
        assets = assets.add(seg.reindex(cal.index).fillna(0.0))
        face = face.add(pd.Series(float(e["fee"]), index=cal.index[cal.index >= b])
                        .reindex(cal.index).fillna(0.0))
    return pd.DataFrame({"date": cal.index, "fees_received": face.values, "assets": assets.values})


def switch_counterfactuals(df: pd.DataFrame,
                           price_by_key: dict[str, pd.Series]) -> pd.DataFrame:
    """逐次 What-if：对每次调仓，假设"这次不换、继续持旧标的到今天"。

    入参 price_by_key: {key: 收盘价 Series}。返回每行 = 一次调仓的对比。
    """
    d = df.sort_values("date").reset_index(drop=True)
    today = d["date"].iloc[-1]
    actual_today = float(d["value"].iloc[-1])
    rows = []
    for _, sw in switch_days(d).iterrows():
        old_key = sw["old_key"]
        base_val = float(sw["value"])
        p = price_by_key.get(old_key)
        rec = {
            "switch_date": sw["date"], "old_key": old_key, "new_key": sw["new_key"],
            "value_at_switch": base_val, "actual_today": actual_today,
            "whatif_today": None, "diff": None,
        }
        if p is not None and not p.dropna().empty:
            p = p.dropna().sort_index()
            p0 = p.loc[p.index <= sw["date"]]
            p1 = p.loc[p.index <= today]
            if len(p0) and len(p1):
                base_px, last_px = float(p0.iloc[-1]), float(p1.iloc[-1])
                whatif = base_val * last_px / base_px
                rec["whatif_today"] = whatif
                rec["diff"] = actual_today - whatif
        rows.append(rec)
    return pd.DataFrame(rows, columns=["switch_date", "old_key", "new_key",
                                       "value_at_switch", "actual_today", "whatif_today", "diff"])


def counterfactual_curve(df: pd.DataFrame, switch_date, old_key: str,
                         price: pd.Series) -> pd.Series:
    """某次调仓的 What-if 反事实曲线（从调仓日持旧标的到今天）的市值序列。"""
    d = df.sort_values("date").reset_index(drop=True)
    row = d.loc[d["date"] == pd.Timestamp(switch_date)]
    if row.empty or price is None or price.dropna().empty:
        return pd.Series(dtype=float)
    base_val = float(row["value"].iloc[0])
    p = price.dropna().sort_index()
    p0 = p.loc[p.index <= pd.Timestamp(switch_date)]
    if p0.empty:
        return pd.Series(dtype=float)
    base_px = float(p0.iloc[-1])
    seg = p.loc[p.index >= pd.Timestamp(switch_date)]
    return (seg / base_px * base_val).rename("whatif")
