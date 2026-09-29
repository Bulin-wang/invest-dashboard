"""调仓复盘 · What-if（Streamlit）—— 逐次评价每位投资者的调仓行为。

对每次调仓：假设"这次不换、继续持旧标的到今天"，对比实际今日市值。
这是 hypothetical 对照，不改主口径。
"""
from __future__ import annotations

import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
ROOT = APP.parent
for _p in (str(ROOT), str(APP)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from common import UNIT, ensure_holding, holding_label, load_all, load_price
from src.analysis import counterfactual_curve, switch_counterfactuals, switch_days

st.set_page_config(page_title="调仓复盘", page_icon="🔍", layout="wide")

investors, benchmarks, cfg, portfolios, bench_frames, meta, index_df = load_all()
PF_META = {p["nickname"]: p for p in meta.get("portfolios", [])}

st.title("🔍 调仓复盘 · What-if")
st.caption("逐次评价：对每次调仓，假设「这次不换、继续持旧标的到今天」，与实际今日市值对比。")

if not portfolios:
    st.warning("还没有数据。请先运行 `python -m prototype.backfill` / `python -m prototype.daily_close`。")
    st.stop()

nicks = list(portfolios.keys())
nick = st.selectbox("选择投资者（昵称）", nicks, index=0)

df = ensure_holding(portfolios[nick], PF_META.get(nick, {}))
if "value" not in df.columns:
    st.error("数据缺 value 列，请重跑 `python -m prototype.daily_close`。")
    st.stop()

sw = switch_days(df)
if sw.empty:
    st.info(f"{nick} 没有调仓记录（一直持初始标的）。")
    st.stop()

# 载入涉及的标的收盘价
price_by_key: dict[str, pd.Series] = {}
for k in set(sw["old_key"]).union(sw["new_key"]):
    p = load_price(k)
    if p is not None:
        price_by_key[k] = p

cf = switch_counterfactuals(df, price_by_key)
actual_today = float(df["value"].iloc[-1])

c1, c2, c3 = st.columns(3)
c1.metric("实际今日市值", f"{actual_today / 1e4:,.1f} 万元")
c2.metric("累计收益", f"{float(df['cum_return'].iloc[-1]):+.2%}" if "cum_return" in df.columns
          else f"{actual_today / UNIT - 1:+.2%}")
c3.metric("调仓次数", len(sw))


def _verdict(d):
    if d is None or pd.isna(d):
        return "—（缺价）"
    return "✅ 调仓更赚" if d > 0 else "❌ 不调更赚"


show = pd.DataFrame({
    "调仓日": cf["switch_date"].dt.strftime("%Y-%m-%d"),
    "从": cf["old_key"].map(holding_label),
    "到": cf["new_key"].map(holding_label),
    "调仓时市值(万)": cf["value_at_switch"] / 1e4,
    "若不调仓·今日(万)": cf["whatif_today"] / 1e4,
    "实际今日(万)": cf["actual_today"] / 1e4,
    "差异(万)": cf["diff"] / 1e4,
    "评价": cf["diff"].map(_verdict),
})
st.dataframe(
    show, use_container_width=True, hide_index=True,
    column_config={
        "调仓时市值(万)": st.column_config.NumberColumn(format="%.1f"),
        "若不调仓·今日(万)": st.column_config.NumberColumn(format="%.1f"),
        "实际今日(万)": st.column_config.NumberColumn(format="%.1f"),
        "差异(万)": st.column_config.NumberColumn(format="%+.1f"),
    })

# ---- 图：实际曲线 + 各次调仓的反事实曲线 ----
fig = go.Figure()
fig.add_trace(go.Scatter(x=df["date"], y=df["value"] / 1e4, name="实际", mode="lines",
                         line=dict(color="#111111", width=3)))
for _, r in cf.iterrows():
    curve = counterfactual_curve(df, r["switch_date"], r["old_key"], price_by_key.get(r["old_key"]))
    if not curve.empty:
        fig.add_trace(go.Scatter(
            x=curve.index, y=curve / 1e4,
            name=f"若不调（持 {holding_label(r['old_key'])}）", mode="lines",
            line=dict(width=1.5, dash="dot")))
fig.update_layout(height=480, hovermode="x unified", yaxis_title="市值（万元）", xaxis_title="日期",
                  legend=dict(orientation="h", yanchor="bottom", y=1.02),
                  margin=dict(l=10, r=10, t=40, b=10))
fig.add_hline(y=100.0, line_width=1, line_color="rgba(0,0,0,0.3)")
st.plotly_chart(fig, use_container_width=True)

st.caption("口径（逐次 what-if）：对第 k 次调仓，假设该次不换仓、**之后一直持该旧标的到今天**，"
           "从调仓当日组合市值按旧标的涨跌推算到今天的市值；与「实际今日市值」对比。"
           "差异 > 0 表示该次调仓（相对一直持有旧标的）更赚。")
