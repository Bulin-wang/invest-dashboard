"""持仓价格看板（Streamlit）—— 看各标的的**价格变化**（未复权收盘价）。

读取 data/ 下由 prototype.daily_close / src.pipeline 生成的结果并可视化。
本地运行：  streamlit run app/streamlit_app.py
云端：      Streamlit Community Cloud，主文件填 app/streamlit_app.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # 让 `import src...` 生效

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from src.config import RETURNS_DIR, META_PATH, INDEX_PATH, load_benchmarks, load_holdings

st.set_page_config(page_title="持仓价格看板", page_icon="📈", layout="wide")


# ----------------------------------------------------------------------------- 数据加载
@st.cache_data(ttl=1800, show_spinner="加载数据中…")
def load_all():
    holdings = load_holdings()
    benchmarks = load_benchmarks()
    frames: dict[str, pd.DataFrame] = {}
    for it in holdings + benchmarks:
        f = RETURNS_DIR / f"{it.key}.csv"
        if f.exists():
            frames[it.key] = pd.read_csv(f, parse_dates=["date"])
    meta = {}
    if META_PATH.exists():
        meta = json.loads(META_PATH.read_text(encoding="utf-8"))
    index_df = None
    if INDEX_PATH.exists():
        index_df = pd.read_csv(INDEX_PATH, parse_dates=["date"])
    return holdings, benchmarks, frames, meta, index_df


holdings, benchmarks, frames, meta, index_df = load_all()

PALETTE = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
           "#8c564b", "#e377c2", "#17becf", "#bcbd22", "#7f7f7f"]


def fmt_pct(x) -> str:
    return "—" if x is None or x != x else f"{x:+.2%}"


# ----------------------------------------------------------------------------- 顶部
st.title("📈 持仓价格看板")
st.caption("口径：未复权收盘价的**价格变化**（不含分红 / 除权），自各标的起始日归一化。")

if not frames:
    st.warning(
        "还没有数据。请先在项目根目录运行：\n\n"
        "```\n"
        "python -m prototype.backfill --days 400   # 首次：回填历史\n"
        "python -m prototype.daily_close           # 每天：取当日收盘价\n"
        "```\n\n"
        "生成 `data/` 后刷新本页。")
    st.stop()

updated = meta.get("updated_at", "未知")
ok = sum(1 for v in meta.get("items", {}).values() if v.get("status") == "ok")
errs = [v["name"] for v in meta.get("items", {}).values() if v.get("status") == "error"]
st.caption(f"数据更新时间(UTC)：{updated} ｜ 成功 {ok} 个"
           + (f" ｜ ⚠️ 失败：{', '.join(errs)}" if errs else ""))
if index_df is not None and not index_df.empty:
    iv = float(index_df["index"].iloc[-1])
    st.caption(f"自定义等权指数（基准日 = 100）：**{iv:.2f}** 点"
               f"（{iv - 100:+.2f} 点 / {iv / 100 - 1:+.2%}）")


# ----------------------------------------------------------------------------- 侧边栏筛选
with st.sidebar:
    st.header("筛选")
    markets = sorted({it.market for it in holdings})
    types = sorted({it.type for it in holdings})
    mkt_labels = {"cn": "A股/境内", "us": "美股/境外"}
    type_labels = {"stock": "股票", "etf": "ETF", "bond": "债券", "index": "指数"}

    sel_mkt = st.multiselect("市场", markets, default=markets,
                             format_func=lambda m: mkt_labels.get(m, m))
    sel_type = st.multiselect("类型", types, default=types,
                              format_func=lambda t: type_labels.get(t, t))

    candidates = [it for it in holdings
                  if it.market in sel_mkt and it.type in sel_type]
    sel_keys = st.multiselect(
        "标的", [it.key for it in candidates], default=[it.key for it in candidates],
        format_func=lambda k: next(it.name for it in holdings if it.key == k))

    st.divider()
    show_bench = st.checkbox("叠加基准指数", value=True)
    show_index = st.checkbox("叠加等权指数", value=True)
    norm100 = st.checkbox("净值化（起始=100）", value=False)

    st.divider()
    st.header("时间范围")
    presets = {"全部": None, "近1年": 365, "近3年": 1095, "今年": "ytd"}
    choice = st.radio("预设", list(presets), index=0, horizontal=False)
    days = presets[choice]
    if days == "ytd":
        range_start = pd.Timestamp(pd.Timestamp.today().year, 1, 1)
    elif days:
        range_start = pd.Timestamp.today() - pd.Timedelta(days=days)
    else:
        range_start = None
    custom = st.date_input("自定义起点", value=None)
    if custom:
        range_start = pd.Timestamp(custom)


# ----------------------------------------------------------------------------- 取选中数据
def slice_df(df: pd.DataFrame) -> pd.DataFrame:
    if range_start is None:
        return df
    return df[df["date"] >= range_start]


selected = [it for it in holdings if it.key in sel_keys]
series = [(it, slice_df(frames[it.key])) for it in selected if it.key in frames]
series = [(it, d) for it, d in series if not d.empty]

# ----------------------------------------------------------------------------- 主图
fig = go.Figure()
for i, (it, d) in enumerate(series):
    y = d["nav"] if norm100 else d["cum_return"] * 100.0
    fig.add_trace(go.Scatter(
        x=d["date"], y=y, name=it.name, mode="lines",
        line=dict(color=PALETTE[i % len(PALETTE)], width=2)))

if show_bench:
    for it in benchmarks:
        if it.key not in frames:
            continue
        b = slice_df(frames[it.key])
        if b.empty:
            continue
        by = b["cum_return"] * 100.0
        fig.add_trace(go.Scatter(
            x=b["date"], y=by, name=f"[基准] {it.name}", mode="lines",
            line=dict(width=1.5, dash="dash", color="rgba(120,120,120,0.9)")))

if show_index and index_df is not None and not index_df.empty:
    ix = slice_df(index_df)
    if not ix.empty:
        iy = ix["index"] if norm100 else ix["index"] - 100.0
        fig.add_trace(go.Scatter(
            x=ix["date"], y=iy, name="★ 等权指数", mode="lines",
            line=dict(color="#111111", width=3)))

fig.update_layout(
    height=520, hovermode="x unified",
    yaxis_title=("净值（起始=100）" if norm100 else "累计涨跌（%）"),
    xaxis_title="日期", legend=dict(orientation="h", yanchor="bottom", y=1.02),
    margin=dict(l=10, r=10, t=40, b=10))
fig.add_hline(y=(100 if norm100 else 0), line_width=1, line_color="rgba(0,0,0,0.3)")
st.plotly_chart(fig, use_container_width=True)

# ----------------------------------------------------------------------------- 明细表
st.subheader("明细")
rows = []
for it, d in series:
    rec = meta.get("items", {}).get(it.key, {})
    rows.append({
        "标的": it.name,
        "代码": it.symbol,
        "市场": it.market.upper(),
        "类型": it.type,
        "起始日": d["date"].iloc[0].date(),
        "起始价": round(float(d["close"].iloc[0]), 3),
        "最新价": round(float(d["close"].iloc[-1]), 3),
        "累计涨跌": fmt_pct(float(d["cum_return"].iloc[-1])),
        "年化": fmt_pct(rec.get("annualized")),
        "最大回撤": fmt_pct(rec.get("max_drawdown")),
    })
table = pd.DataFrame(rows)
# 按累计涨跌降序（把 % 转回数值排序）
table["_c"] = [float(d["cum_return"].iloc[-1]) for _, d in series]
table = table.sort_values("_c", ascending=False).drop(columns="_c").set_index("标的")
st.dataframe(table, use_container_width=True)

st.caption("说明：全部为**价格**口径（未复权收盘价，不含分红 / 除权）。"
           "年化按实际天数几何折算；最大回撤基于归一化价格序列。"
           "组合聚合（含汇率折算）为后续扩展。")
