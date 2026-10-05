"""多标的加权组合：把「多个标的 + 权重」合成组合净值曲线。

阶段 1（现状）口径 = **buy & hold（买入后持有）**
------------------------------------------------
段起点按权重 wᵢ 分配本金，段内**不再平衡**（权重随价格自然漂移）：

    组合收益(t) = Σ wᵢ × Pᵢ(t) / Pᵢ(段起点)

调仓（换一组权重）= 段切换：**调仓日收盘**切换、**当日算旧组合、次交易日起算新组合**
（与单标的 `src.portfolio.portfolio_nav` 口径完全一致）。不计费用 / 不计汇率。

单标的组合走同一套代码，结果与旧实现逐行一致（回归用例见 tests/test_weighted_offline.py）。

两个入口
--------
1. ``weighted_path_nav(series_by_key, segments, principal)``
   管线入口（`prototype.daily_close` 调用）：按**权重路径**分段合成。
2. ``portfolio_nav(returns_by_key, weights, rebalance=False)``
   预留接口（按各标的已算好的累计收益合并，归一化到 100）；`rebalance=True`
   走**每日再平衡**口径，供后续阶段使用。

``holding`` 列格式
------------------
- 单腿：``cn_stock_600519``（与旧格式一致）
- 多腿：``cn_stock_600519:0.6|us_stock_AAPL:0.4``（按 key 排序，稳定可比）
"""
from __future__ import annotations

import pandas as pd

CALIBER = "buy_hold"

CASH_KEY = "cash"     # 与 src.config.CASH_KEY 一致（此处独立定义，避免模块导入环）


def _is_cash(key) -> bool:
    """现金腿：价格恒为 1（不涨不跌），不需要行情数据。"""
    return str(key) == CASH_KEY


def normalize_weights(weights: dict) -> dict:
    """权重归一化到和为 1（忽略非正权重；全为非正 → 报错）。"""
    pos: dict = {}
    for k, v in weights.items():
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"权重不是数字：{k}={v!r}") from None
        if f > 0:
            pos[str(k)] = f
    total = sum(pos.values())
    if not pos or total <= 0:
        raise ValueError(f"权重非法（需要至少一个正权重）：{weights!r}")
    return {k: v / total for k, v in pos.items()}


def holding_spec(weights: dict) -> str:
    """权重向量 → ``holding`` 列字符串；单腿时退化为裸 key（兼容旧数据）。"""
    w = normalize_weights(weights)
    keys = sorted(w)
    if len(keys) == 1:
        return keys[0]
    return "|".join(f"{k}:{w[k]:.6g}" for k in keys)


def _last_at(s: pd.Series, ts) -> float | None:
    """ts（含）之前最后一个有效价；没有则取 ts 之后第一个有效价；都没有 → None。"""
    pre = s.loc[:ts].dropna()
    if not pre.empty:
        return float(pre.iloc[-1])
    post = s.loc[ts:].dropna()
    if not post.empty:
        return float(post.iloc[0])
    return None


def weighted_path_nav(series_by_key: dict,
                      segments: list,
                      principal: float = 1_000_000.0,
                      calendar_hint=None) -> pd.DataFrame:
    """按**权重路径**合成投资者组合收益（buy & hold，分段切换）。

    入参：
      series_by_key:  {key: 未复权收盘价 Series（日期索引）}；**现金腿不需要行情**
      segments:       [(日期, {key: weight}), ...]；首段 = 初始建仓，其后每条 = 一次调仓
      principal:      本金（元）
      calendar_hint:  可选；全现金组合借用的交易日历（Series / DatetimeIndex）
    返回：DataFrame[date, holding, value, nav, cum_return, daily_return]
    """
    if not segments:
        raise ValueError("segments 为空")
    segs = [(str(d), normalize_weights(w)) for d, w in segments]

    frames: dict = {}
    for k in sorted({k for _d, w in segs for k in w if not _is_cash(k)}):
        s = series_by_key.get(k)
        if s is None or s.dropna().empty:
            raise ValueError(f"缺少 {k} 的价格数据")
        frames[k] = s.dropna().sort_index()

    if frames:
        all_cal = pd.DatetimeIndex(sorted(set().union(*[frames[k].index for k in frames])))
    elif calendar_hint is not None and len(pd.Series(calendar_hint).dropna()):
        all_cal = pd.DatetimeIndex(pd.Series(calendar_hint).dropna().sort_index().index)
    else:
        raise ValueError("全现金组合需要 calendar_hint 才能确定交易日历")

    def _snap(raw):
        cand = all_cal[all_cal >= pd.Timestamp(raw)]
        return cand[0] if len(cand) else None

    def _ratio(weights: dict, base_ts, at) -> float:
        """组合相对段起点的比值 = Σ wᵢ × Pᵢ(at)/Pᵢ(base_ts)（现金腿恒为 1）。"""
        total = 0.0
        for k, w in weights.items():
            if _is_cash(k):
                total += w                # 现金：不涨不跌
                continue
            s = frames[k]
            base = _last_at(s, base_ts)
            if base is None:
                total += w              # 该腿此时还没有行情 → 视作现金，不贡献涨跌
                continue
            now = _last_at(s, at)
            total += w * (float(now) / base if now else 1.0)
        return total

    b = [_snap(raw) for raw, _ in segs]
    if b[0] is None:
        raise ValueError(f"起始日 {segs[0][0]} 之后没有行情数据")

    out: dict = {}
    hold: dict = {}
    ratio = 1.0                          # 已结束各段的连乘比值
    for i, (_raw_date, weights) in enumerate(segs):
        base_ts = b[i]
        if base_ts is None:              # 调仓日在数据之后 → 该段无数据
            break
        px_keys = [k for k in weights if not _is_cash(k)]
        seg_cal = (pd.DatetimeIndex(sorted(set().union(*[frames[k].index for k in px_keys])))
                   if px_keys else all_cal)      # 纯现金段：借用全局日历
        dates = seg_cal[seg_cal >= base_ts] if i == 0 else seg_cal[seg_cal > base_ts]
        end_ts = b[i + 1] if i + 1 < len(segs) else None
        if end_ts is not None:
            dates = dates[dates <= end_ts]
        spec = holding_spec(weights)
        for t in dates:
            out[t] = principal * ratio * _ratio(weights, base_ts, t)
            hold[t] = spec
        if end_ts is not None:
            ratio = ratio * _ratio(weights, base_ts, end_ts)

    if not out:
        raise ValueError("没有生成任何组合数据")
    df = (pd.DataFrame({"date": list(out.keys()), "value": list(out.values())})
          .sort_values("date").reset_index(drop=True))
    df["holding"] = df["date"].map(hold)
    df["nav"] = df["value"] / principal * 100.0
    df["cum_return"] = df["nav"] / 100.0 - 1.0
    df["daily_return"] = df["value"].pct_change()
    return df[["date", "holding", "value", "nav", "cum_return", "daily_return"]]


def portfolio_nav(returns_by_key: dict,
                  weights: dict,
                  rebalance: bool = False) -> pd.DataFrame:
    """【预留接口，阶段 1 实现】按权重合并各标的收益，归一化到 100。

    入参：
      returns_by_key: {key: DataFrame[date, cum_return]}（src.returns.cumulative_return 的输出）
      weights:        {key: weight}（内部归一化）
      rebalance:      False = buy & hold（默认，与主口径一致）；True = 每日再平衡
    返回：DataFrame[date, nav]
    """
    w = normalize_weights(weights)
    frames: dict = {}
    for k in w:
        if _is_cash(k):
            continue                       # 现金：不需要收益数据
        df = returns_by_key.get(k)
        if df is None or len(df) == 0:
            raise ValueError(f"缺少 {k} 的收益数据")
        frames[k] = df.dropna(subset=["date"]).sort_values("date").set_index("date")
    if not frames:
        raise ValueError("全现金组合需要至少一个标的的收益数据")
    cal = pd.DatetimeIndex(sorted(set().union(*[f.index for f in frames.values()])))
    book = pd.DataFrame({k: 1.0 + frames[k]["cum_return"].reindex(cal) for k in frames})
    if any(_is_cash(k) for k in w):
        book[CASH_KEY] = 1.0               # 现金恒为 1
    if rebalance:
        r = (book / book.shift(1) - 1.0).fillna(0.0)
        nav = 100.0 * (1.0 + sum(r[k] * w[k] for k in w)).cumprod()
    else:
        nav = 100.0 * sum(book[k].fillna(1.0) * w[k] for k in w)
    return pd.DataFrame({"date": cal, "nav": nav.values})
