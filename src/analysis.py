"""附加分析：管理者（券商）手续费视角 + 每次调仓的 What-if 反事实。

都是**基于已落盘数据**（data/portfolios、data/prices）的再计算，不改主口径。
"""
from __future__ import annotations

import pandas as pd

DEFAULT_FEE_RATE = 0.0001   # 万分之一（0.01%）


def parse_holding(spec) -> dict[str, float]:
    """把组合序列的 `holding` 值解析成 {key: 权重}（内部归一化）。

    - 单腿（旧格式）：``cn_stock_600519``                      -> {key: 1.0}
    - 多腿（新格式）：``cn_stock_600519:0.6|us_stock_AAPL:0.4`` -> 归一化权重
    """
    s = str(spec or "").strip()
    if not s:
        return {}
    if "|" not in s and ":" not in s:
        return {s: 1.0}
    out: dict[str, float] = {}
    for chunk in s.split("|"):
        k, _, w = chunk.partition(":")
        k = k.strip()
        if not k:
            continue
        try:
            v = float(w) if w else 1.0
        except ValueError:
            v = 1.0
        out[k] = out.get(k, 0.0) + v
    total = sum(out.values())
    if total <= 0:
        return {}
    return {k: v / total for k, v in out.items()}


def turnover(old_spec, new_spec) -> float:
    """**单边换手率** = ½ × Σ|Δ权重|（0 = 没动，1 = 整仓换标的）。

    例：100% A -> 100% B 得 1.0；50/50 -> 100% A 得 0.5（只需卖出 B 的一半）；
    A -> 60/40 得 0.4。无法解析（组合序列缺 `holding`）时退化为 1.0，与旧口径一致。
    """
    a, b = parse_holding(old_spec), parse_holding(new_spec)
    if not a or not b:
        return 1.0
    keys = set(a) | set(b)
    return 0.5 * sum(abs(b.get(k, 0.0) - a.get(k, 0.0)) for k in keys)


def _basket_ratio(weights: dict[str, float],
                  price_by_key: dict[str, pd.Series],
                  ts) -> pd.Series | None:
    """旧持仓组合按 **buy & hold** 相对调仓基准的比值序列：Σ wᵢ × Pᵢ(t)/Pᵢ(ts)。

    返回以日期为索引的 Series（自 ts 起）；任一条腿缺价或缺基准价 → None。
    """
    if not weights:
        return None
    ts = pd.Timestamp(ts)
    parts: dict[str, pd.Series] = {}
    for k, w in weights.items():
        p = price_by_key.get(k)
        if p is None:
            return None
        p = p.dropna().sort_index()
        if p.empty:
            return None
        before = p.loc[p.index <= ts]
        if before.empty:
            return None
        base = float(before.iloc[-1])
        if base <= 0:
            return None
        parts[k] = p / base * float(w)
    frame = pd.DataFrame(parts).sort_index().ffill()
    frame = frame.loc[frame.index >= ts].fillna(0.0)
    if frame.empty:
        return None
    return frame.sum(axis=1)


def switch_days(df: pd.DataFrame) -> pd.DataFrame:
    """从投资者组合序列里找每次调仓。

    入参 df：data/portfolios/{昵称}.csv（含 date, holding, value）。
    返回每行 = 一次调仓：
      {date(调仓日·当日算旧), old_key, new_key, value(调仓当日组合市值), turnover(单边换手率)}。
    """
    d = df.sort_values("date").reset_index(drop=True)
    changes = [i for i in d.index[d["holding"] != d["holding"].shift()].tolist() if i > 0]
    rows = [{
        "date": d["date"].iloc[i - 1],       # 新标的首个交易日的"前一天" = 调仓日（当日算旧）
        "old_key": d["holding"].iloc[i - 1],
        "new_key": d["holding"].iloc[i],
        "value": float(d["value"].iloc[i - 1]),
        "turnover": turnover(d["holding"].iloc[i - 1], d["holding"].iloc[i]),
    } for i in changes]
    return pd.DataFrame(rows, columns=["date", "old_key", "new_key", "value", "turnover"])


def fee_events(portfolios: dict[str, pd.DataFrame],
               fee_rate: float = DEFAULT_FEE_RATE) -> pd.DataFrame:
    """每次调仓收 **fee_rate × 当日组合市值 × 换手率**。

    换手率 = ½ Σ|Δ权重|：单标的全仓切换 = 1（与原口径一致）；多标的只调一部分时按比例收。
    返回 [date, nickname, value, turnover, fee]。
    """
    rows = []
    for inv, df in portfolios.items():
        for _, r in switch_days(df).iterrows():
            turn = float(r["turnover"])
            rows.append({"date": pd.Timestamp(r["date"]), "nickname": inv,
                         "value": r["value"], "turnover": turn,
                         "fee": r["value"] * fee_rate * turn})
    return pd.DataFrame(rows, columns=["date", "nickname", "value", "turnover", "fee"])


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
    """逐次 What-if：对每次调仓，假设"这次不换、继续持有当时的旧持仓组合到今天"。

    入参 price_by_key: {key: 收盘价 Series}。返回每行 = 一次调仓的对比。

    注：`old_key` 为单标的 key 或**多标的权重组合**（`key:w|key:w`）；
    任有一条腿缺价 → 该行 whatif/diff 为 NaN（看板显示「—（缺价）」）。
    """
    d = df.sort_values("date").reset_index(drop=True)
    today = d["date"].iloc[-1]
    actual_today = float(d["value"].iloc[-1])
    rows = []
    for _, sw in switch_days(d).iterrows():
        old_key = sw["old_key"]
        base_val = float(sw["value"])
        rec = {
            "switch_date": sw["date"], "old_key": old_key, "new_key": sw["new_key"],
            "value_at_switch": base_val, "actual_today": actual_today,
            "whatif_today": None, "diff": None,
        }
        ratio = _basket_ratio(parse_holding(old_key), price_by_key, sw["date"])
        if ratio is not None:
            upto = ratio.loc[ratio.index <= today]
            if not upto.empty:
                whatif = base_val * float(upto.iloc[-1])
                rec["whatif_today"] = whatif
                rec["diff"] = actual_today - whatif
        rows.append(rec)
    out = pd.DataFrame(rows, columns=["switch_date", "old_key", "new_key",
                                      "value_at_switch", "actual_today",
                                      "whatif_today", "diff"])
    # 缺价（含多标的权重组合，其 old_key 不是单个标的）→ NaN，保证下游可做数值运算
    for c in ("value_at_switch", "actual_today", "whatif_today", "diff"):
        out[c] = pd.to_numeric(out[c], errors="coerce")
    return out


def counterfactual_curve(df: pd.DataFrame, switch_date, spec,
                         price_by_key: dict[str, pd.Series]) -> pd.Series:
    """某次调仓的 What-if 反事实曲线：从调仓日起**保持当时的旧持仓组合（buy & hold）**的市值序列。

    `spec` 可为单标的 key（旧格式）或权重组合（`key:w|key:w`）；
    `price_by_key` = {key: 收盘价 Series}。任一条腿缺价 → 返回空 Series（看板跳过该曲线）。
    """
    weights = parse_holding(spec)
    if not weights:
        return pd.Series(dtype=float)
    d = df.sort_values("date").reset_index(drop=True)
    row = d.loc[d["date"] == pd.Timestamp(switch_date)]
    if row.empty:
        return pd.Series(dtype=float)
    ratio = _basket_ratio(weights, price_by_key, switch_date)
    if ratio is None or ratio.empty:
        return pd.Series(dtype=float)
    return (ratio * float(row["value"].iloc[0])).rename("whatif")
