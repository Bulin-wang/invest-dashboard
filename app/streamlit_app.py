"""投资组合价值看板（Streamlit）—— 看 ~60 位投资者的**组合累计收益**（含调仓）。

- 每位投资者有一条持仓路径（全仓切换，`switches.yaml`）；看板汇报其**组合收益**
  （`data/portfolios/{Investor}.csv`，由 prototype.daily_close 合成）。
- 本金 principal（元），Y 轴单位为「万元」，基准线画在 100 万；**不展示起始日之前**。
- 展示用昵称（真实姓名映射在本地，不入库）。

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

from src.config import (
    INDEX_PATH,
    META_PATH,
    PORTFOLIOS_DIR,
    RETURNS_DIR,
    load_investor_benchmarks,
    load_investor_config,
    load_investors,
    portfolio_id,
)

st.set_page_config(page_title="投资组合价值看板", page_icon="📈", layout="wide")

UNIT = 1_000_000.0   # 100 万；`nav`（基准=100）换算成「万元」的缩放因子 = principal/UNIT


# ----------------------------------------------------------------------------- 数据加载
@st.cache_data(ttl=1800, show_spinner="加载数据中…")
def load_all():
    investors = load_investors()
    benchmarks = load_investor_benchmarks()
    cfg = load_investor_config()
    portfolios: dict[str, pd.DataFrame] = {}
    for inv in investors:
        f = PORTFOLIOS_DIR / f"{portfolio_id(inv.nickname)}.csv"
        if f.exists():
            portfolios[inv.nickname] = pd.read_csv(f, parse_dates=["date"])
    bench_frames: dict[str, pd.DataFrame] = {}
    for b in benchmarks:
        f = RETURNS_DIR / f"{b.key}.csv"
        if f.exists():
            bench_frames[b.key] = pd.read_csv(f, parse_dates=["date"])
    meta = {}
    if META_PATH.exists():
        meta = json.loads(META_PATH.read_text(encoding="utf-8"))
    index_df = None
    if INDEX_PATH.exists():
        index_df = pd.read_csv(INDEX_PATH, parse_dates=["date"])
    return investors, benchmarks, cfg, portfolios, bench_frames, meta, index_df


investors, benchmarks, cfg, portfolios, bench_frames, meta, index_df = load_all()

PRINCIPAL = float(cfg.get("principal", UNIT))
SCALE = PRINCIPAL / UNIT                       # nav → 万元
BASE_DATE = pd.Timestamp(cfg["start_date"]) if cfg.get("start_date") else None
PF_META = {p["nickname"]: p for p in meta.get("portfolios", [])}

PALETTE = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
           "#8c564b", "#e377c2", "#17becf", "#bcbd22", "#7f7f7f"]


# ----------------------------------------------------------------------------- 顶部
st.title("📈 投资组合价值看板")
st.caption(f"口径：每人本金 **{PRINCIPAL / 1e4:,.0f} 万元**，自 {cfg.get('start_date')} 起按持仓路径"
           "（全仓切换）计算**组合收益**；调仓日收盘切换、不计费用/汇率。Y 轴单位为**万元**，起点 100 万。")

if not portfolios:
    st.warning(
        "还没有数据。请先在项目根目录运行：\n\n"
        "```\n"
        "python -m prototype.backfill --days 400   # 首次：回填历史\n"
        "python -m prototype.daily_close           # 每天：取当日收盘价 + 合成组合收益\n"
        "```\n\n"
        "生成 `data/` 后刷新本页。")
    st.stop()

updated = meta.get("updated_at", "未知")
ok = sum(1 for v in meta.get("items", {}).values() if v.get("status") == "ok")
n_sw = meta.get("n_switches", "?")
n_inv = len(investors)
st.caption(f"数据更新时间(UTC)：{updated} ｜ {n_inv} 位投资者 / {ok} 个标的 ｜ 调仓流水 {n_sw} 条")
if index_df is not None and not index_df.empty:
    iv_wan = float(index_df["index"].iloc[-1]) * SCALE
    st.caption(f"群体平均（基准 {cfg.get('start_date')} = 100 万）：**{iv_wan:,.0f} 万元**"
               f"（{(iv_wan * 1e4 / PRINCIPAL - 1):+.2%}）")


# ----------------------------------------------------------------------------- 侧边栏筛选
with st.sidebar:
    st.header("筛选")
    all_nicks = [inv.nickname for inv in investors]
    sel_names = st.multiselect("投资者（昵称）", all_nicks, default=all_nicks)

    st.divider()
    show_bench = st.checkbox("叠加基准指数", value=True)
    show_index = st.checkbox("叠加群体平均", value=True)
    show_profit = st.checkbox("显示盈亏额（万元）", value=False,
                              help="勾选后 Y 轴为相对 100 万的盈亏（万元）；默认显示市值。")

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


# ----------------------------------------------------------------------------- 取选中数据（不早于基准日）
def floor_start():
    if BASE_DATE is None:
        return range_start
    if range_start is None:
        return BASE_DATE
    return max(range_start, BASE_DATE)


START = floor_start()


def slice_df(df: pd.DataFrame) -> pd.DataFrame:
    if START is None:
        return df
    return df[df["date"] >= START]


selected = [inv for inv in investors if inv.nickname in sel_names]


def to_wan(nav_or_index: pd.Series) -> pd.Series:
    """把「基准=100」的序列换算成万元（相对 100 万本金）。"""
    return nav_or_index * SCALE


def to_profit_wan(nav_or_index: pd.Series) -> pd.Series:
    """换算成相对 100 万的盈亏（万元）。"""
    return (nav_or_index - 100.0) * SCALE


# ----------------------------------------------------------------------------- 主图
fig = go.Figure()
for i, inv in enumerate(selected):
    full = portfolios.get(inv.nickname)
    if full is None:
        continue
    d = slice_df(full)
    if d.empty:
        continue
    y = to_profit_wan(d["nav"]) if show_profit else to_wan(d["nav"])
    fig.add_trace(go.Scatter(
        x=d["date"], y=y, name=inv.nickname, mode="lines",
        line=dict(color=PALETTE[i % len(PALETTE)], width=1.6)))

if show_bench:
    bench_dashes = ["dash", "dot"]
    for j, b in enumerate(benchmarks):
        bf = bench_frames.get(b.key)
        if bf is None or BASE_DATE is None:
            continue
        after = bf[bf["date"] >= BASE_DATE]
        if after.empty:
            continue
        base_close = float(after["close"].iloc[0])
        d = slice_df(bf)
        if d.empty:
            continue
        series = d["close"] / base_close * 100.0     # 基准日 = 100（万元）
        y = to_profit_wan(series) if show_profit else to_wan(series)
        fig.add_trace(go.Scatter(
            x=d["date"], y=y, name=f"[基准] {b.name}", mode="lines",
            line=dict(width=1.5, dash=bench_dashes[j % len(bench_dashes)],
                      color="rgba(120,120,120,0.9)")))

if show_index and index_df is not None and not index_df.empty:
    ix = slice_df(index_df)
    if not ix.empty:
        y = to_profit_wan(ix["index"]) if show_profit else to_wan(ix["index"])
        fig.add_trace(go.Scatter(
            x=ix["date"], y=y, name="★ 群体平均", mode="lines",
            line=dict(color="#111111", width=3)))

fig.update_layout(
    height=520, hovermode="x unified",
    yaxis_title=("盈亏（万元，相对 100 万）" if show_profit else "市值（万元）"),
    xaxis_title="日期", legend=dict(orientation="h", yanchor="bottom", y=1.02),
    margin=dict(l=10, r=10, t=40, b=10))
fig.add_hline(y=(0 if show_profit else 100 * SCALE), line_width=1,
              line_color="rgba(0,0,0,0.3)")
st.plotly_chart(fig, use_container_width=True)

# ----------------------------------------------------------------------------- 明细表
st.subheader("明细")
rows = []
for inv in selected:
    full = portfolios.get(inv.nickname)
    if full is None:
        continue
    d = slice_df(full)
    if d.empty:
        continue
    rec = PF_META.get(inv.nickname, {})
    cur = rec.get("current", {})
    cur_label = f"{cur.get('symbol', '')} ({cur.get('market', '').upper()})".strip()
    rows.append({
        "昵称": inv.nickname,
        "当前持仓": cur_label,
        "调仓次数": rec.get("n_switches", 0),
        "起始日": d["date"].iloc[0].date(),
        # 以下三列存 **数值（分数/元）**，点列头即可按数值正确排序；显示交给 column_config
        "市值(万元)": float(d["value"].iloc[-1]) / 1e4,
        "收益额(万元)": (float(d["value"].iloc[-1]) - PRINCIPAL) / 1e4,
        "收益率": float(d["cum_return"].iloc[-1]),
        "年化": rec.get("annualized"),
        "最大回撤": rec.get("max_drawdown"),
        "更新日": d["date"].iloc[-1].date(),
    })
table = pd.DataFrame(rows).sort_values("收益率", ascending=False).set_index("昵称")
st.dataframe(
    table, use_container_width=True,
    column_config={
        "调仓次数": st.column_config.NumberColumn(format="%d"),
        "市值(万元)": st.column_config.NumberColumn(format="%.1f"),
        "收益额(万元)": st.column_config.NumberColumn(format="%+.1f"),
        "收益率": st.column_config.NumberColumn(format="percent"),
        "年化": st.column_config.NumberColumn(format="percent"),
        "最大回撤": st.column_config.NumberColumn(format="percent"),
    })

st.caption("说明：全部为**价格**口径（未复权收盘价，不含分红 / 汇率）。"
           "组合收益 = 各段标的收益**连乘**（调仓日收盘全仓切换，当日算旧标的、次日起算新标的）。"
           "年化按实际天数几何折算；最大回撤基于组合净值序列。仅展示起始日及之后的数据。"
           "真实姓名 ↔ 昵称映射保存在本地，看板只显示昵称。")
