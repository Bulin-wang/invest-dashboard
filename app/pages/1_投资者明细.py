"""投资者调仓明细页（Streamlit）—— 看某位投资者每个交易日的持仓与收益。

展示自 start_date 起：每个交易日**持有什么标的**、**当日收益**、**截至当日的累计收益**（时间序列）。
数据来自 data/portfolios/{Investor}.csv（date, holding, value, nav, cum_return, daily_return）。
（旧数据缺 holding/daily_return 时，会用 meta 里的 holdings 兜底推算。）
"""
from __future__ import annotations

import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent   # app/
ROOT = APP.parent                              # 仓库根
for _p in (str(ROOT), str(APP)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from common import UNIT, holding_label, load_all

st.set_page_config(page_title="投资者明细", page_icon="🧑‍💼", layout="wide")

investors, benchmarks, cfg, portfolios, bench_frames, meta, index_df = load_all()

PRINCIPAL = float(cfg.get("principal", UNIT))
PF_META = {p["nickname"]: p for p in meta.get("portfolios", [])}

st.title("🧑‍💼 投资者调仓明细")

if not portfolios:
    st.warning("还没有数据。请先运行 `python -m prototype.backfill` / `python -m prototype.daily_close`。")
    st.stop()

nicks = list(portfolios.keys())
nick = st.selectbox("选择投资者（昵称）", nicks, index=0)

df = portfolios[nick].copy().sort_values("date").reset_index(drop=True)
rec = PF_META.get(nick, {})
cur = rec.get("current", {})
holdings = rec.get("holdings", [])

# --- 兼容旧数据：缺 holding 时用 meta 的 holdings 按日期推算当日持仓 ---
def derive_holding() -> bool:
    """把当日持仓（key）写回 df['holding']。返回是否成功。"""
    if not holdings or "value" not in df.columns:
        return False
    # 段起点 b[j] = 第一个 >= 该调仓日的交易日；t 严格晚于 b[j] 时进入第 j 段（当日算旧标的）
    bnds = []
    for h in holdings:
        cand = df.loc[df["date"] >= pd.Timestamp(h["date"]), "date"]
        bnds.append(cand.iloc[0] if len(cand) else None)

    def _hold(t) -> str:
        k = 0
        for j in range(1, len(bnds)):
            if bnds[j] is not None and bnds[j] < t:
                k = j
        return holdings[k]["key"]

    df["holding"] = df["date"].map(_hold)
    return True


if "holding" not in df.columns:
    derive_holding()
if "daily_return" not in df.columns and "value" in df.columns:
    df["daily_return"] = df["value"].pct_change()

_missing = [c for c in ("holding", "daily_return", "value", "nav", "cum_return")
            if c not in df.columns]
if _missing:
    st.error(f"`{nick}` 的数据缺少列 {_missing}，且无法从 meta 兜底。"
             "请在项目根目录重跑 `python -m prototype.daily_close` 重新生成 `data/`，再刷新本页。")
    st.stop()

# 标出调仓日（相对前一交易日换标的）
df["switched"] = (df["holding"] != df["holding"].shift()) & (df.index > 0)

# ----------------------------------------------------------------------------- 概览
c1, c2, c3, c4 = st.columns(4)
if cur.get("holding"):                       # 多标的：渲染权重组合
    c1.metric("当前持仓", holding_label(cur["holding"]))
else:                                        # 兼容旧 meta
    c1.metric("当前持仓", f"{cur.get('symbol', '—')} ({cur.get('market', '').upper()})")
c2.metric("调仓次数", rec.get("n_switches", 0))
c3.metric("最新市值", f"{float(df['value'].iloc[-1]) / 1e4:,.1f} 万元")
c4.metric("累计收益", f"{float(df['cum_return'].iloc[-1]):+.2%}")

st.caption(f"共 {len(df)} 个交易日 ｜ {df['date'].iloc[0].date()} ~ {df['date'].iloc[-1].date()}")

# ----------------------------------------------------------------------------- 时间序列图：累计收益(线) + 当日收益(柱)
fig = make_subplots(specs=[[{"secondary_y": True}]])
fig.add_trace(
    go.Bar(x=df["date"], y=df["daily_return"] * 100.0, name="当日收益(%)",
           marker_color="rgba(140,140,140,0.45)"),
    secondary_y=False)
fig.add_trace(
    go.Scatter(x=df["date"], y=df["cum_return"] * 100.0, name="累计收益(%)",
               mode="lines+markers", line=dict(color="#d62728", width=2.5)),
    secondary_y=True)
# 调仓日竖线
for _, r in df[df["switched"]].iterrows():
    fig.add_vline(x=r["date"], line_dash="dot", line_color="rgba(30,30,30,0.5)")
fig.update_layout(height=460, hovermode="x unified",
                  legend=dict(orientation="h", yanchor="bottom", y=1.02),
                  margin=dict(l=10, r=10, t=40, b=10))
fig.update_yaxes(title_text="当日收益(%)", secondary_y=False)
fig.update_yaxes(title_text="累计收益(%)", secondary_y=True)
st.plotly_chart(fig, use_container_width=True)

# ----------------------------------------------------------------------------- 每交易日明细表
st.subheader("逐日明细")
show = pd.DataFrame({
    "日期": df["date"].dt.strftime("%Y-%m-%d"),
    "持有标的": df["holding"].map(holding_label),
    "调仓": df["switched"].map(lambda b: "🔁" if b else ""),
    "当日收益": df["daily_return"],
    "累计收益": df["cum_return"],
    "市值(万元)": df["value"] / 1e4,
})
st.dataframe(
    show, use_container_width=True, hide_index=True,
    column_config={
        "当日收益": st.column_config.NumberColumn(format="percent"),
        "累计收益": st.column_config.NumberColumn(format="percent"),
        "市值(万元)": st.column_config.NumberColumn(format="%.1f"),
    })

# ----------------------------------------------------------------------------- 调仓记录
if len(holdings) > 1:
    with st.expander(f"调仓记录（{len(holdings) - 1} 次，含初始建仓共 {len(holdings)} 段）"):
        rows = []
        for i, h in enumerate(holdings):
            rows.append({
                "日期": h["date"],
                "标的": holding_label(h["key"]),
                "类型": "初始建仓" if i == 0 else "调仓",
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

st.caption("口径：调仓日**收盘价**切换（当日算旧组合，次交易日起算新组合）；"
           "段内 buy & hold：组合收益 = Σ 权重ᵢ × Pᵢ(t)/Pᵢ(段起点)；不计费用 / 汇率。")
