"""管理者（券商）视角（Streamlit）—— 手续费 hypothetical。

⚠️ 主口径**不计手续费**；本页仅演示「若每次调仓收万分之一」时，管理者靠手续费收入、
把钱投入 SP500 能累积到多少。初始本金 0，只把收到的费用投入 SP500。
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

from common import UNIT, load_all, load_price
from src.analysis import fee_events, manager_fee_series

st.set_page_config(page_title="管理者视角", page_icon="🏦", layout="wide")

investors, benchmarks, cfg, portfolios, bench_frames, meta, index_df = load_all()

st.title("🏦 管理者（券商）视角 · 手续费 hypothetical")
st.warning("这是 **hypothetical 收益**：主口径不计手续费。本页仅演示"
           "「每次调仓收万分之一手续费、费用投入 SP500」的累积效果。")

if not portfolios:
    st.warning("还没有数据。请先运行 `python -m prototype.backfill` / `python -m prototype.daily_close`。")
    st.stop()

fee_bp = st.number_input("手续费（万分之几）", min_value=0.0, value=1.0, step=0.5, format="%.1f")
fee_rate = fee_bp / 10000.0

sp500 = load_price("us_index_GSPC")
if sp500 is None:
    st.error("缺少 SP500 数据（`data/prices/us_index_GSPC.csv`）。")
    st.stop()

srs = manager_fee_series(portfolios, sp500, fee_rate)
ev = fee_events(portfolios, fee_rate)

total_fee = float(ev["fee"].sum()) if not ev.empty else 0.0
final_assets = float(srs["assets"].iloc[-1]) if not srs.empty else 0.0

c1, c2, c3, c4 = st.columns(4)
c1.metric("调仓次数", int(len(ev)))
c2.metric(f"累计手续费（{fee_bp:g}‱）", f"{total_fee:,.0f} 元")
c3.metric("投入 SP500 后市值", f"{final_assets:,.0f} 元")
c4.metric("SP500 增值收益", f"{(final_assets - total_fee):,.0f} 元",
          f"{(final_assets / total_fee - 1):+.2%}" if total_fee else None)

if not srs.empty:
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=srs["date"], y=srs["assets"] / 1e4, name="管理者资产（万元）",
                             line=dict(color="#2ca02c", width=3)))
    fig.add_trace(go.Scatter(x=srs["date"], y=srs["fees_received"] / 1e4, name="累计手续费·面值（万元）",
                             line=dict(color="#888888", width=1.5, dash="dot")))
    fig.update_layout(height=460, hovermode="x unified", yaxis_title="万元", xaxis_title="日期",
                      legend=dict(orientation="h", yanchor="bottom", y=1.02),
                      margin=dict(l=10, r=10, t=40, b=10))
    st.plotly_chart(fig, use_container_width=True)

st.subheader("各投资者贡献的手续费")
if ev.empty:
    st.info("暂无调仓记录。")
else:
    agg = (ev.groupby("nickname")
             .agg(调仓次数=("fee", "size"), 手续费合计=("fee", "sum"))
             .sort_values("手续费合计", ascending=False))
    st.dataframe(agg, use_container_width=True,
                 column_config={"手续费合计": st.column_config.NumberColumn(format="%.0f")})

st.caption("口径：每次调仓按**调仓当日组合市值 × 万分之几**收手续费；费用**自调仓日（次交易日起）**"
           "投入并跟踪 SP500 收益累乘；管理者初始本金 0（只累积费用）。不含汇率折算。")
