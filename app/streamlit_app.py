"""投资组合价值看板（Streamlit）—— 看 ~60 位投资者 **100 万本金**的累计市值。

- 每人持有 1 个标的，本金 principal（元），自 start_date 起按标的**未复权价格收益**折算：
      value(t) = principal × close(t) / close(start_date)
- Y 轴单位为「万元」，基准线画在 100 万；**不展示起始日之前**的信息。
- 展示用昵称（真实姓名映射在本地，不入库）。

读取 data/ 下由 prototype.daily_close 生成的结果并可视化。
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
    RETURNS_DIR,
    load_investor_benchmarks,
    load_investor_config,
    load_investors,
)

st.set_page_config(page_title="投资组合价值看板", page_icon="📈", layout="wide")

UNIT = 1_000_000.0   # 100 万；`nav`（基准=100）换算成「万元」的缩放因子 = principal/UNIT


# ----------------------------------------------------------------------------- 数据加载
@st.cache_data(ttl=1800, show_spinner="加载数据中…")
def load_all():
    investors = load_investors()
    benchmarks = load_investor_benchmarks()
    cfg = load_investor_config()
    keys = sorted({inv.key for inv in investors} | {b.key for b in benchmarks})
    frames: dict[str, pd.DataFrame] = {}
    for k in keys:
        f = RETURNS_DIR / f"{k}.csv"
        if f.exists():
            frames[k] = pd.read_csv(f, parse_dates=["date"])
    meta = {}
    if META_PATH.exists():
        meta = json.loads(META_PATH.read_text(encoding="utf-8"))
    index_df = None
    if INDEX_PATH.exists():
        index_df = pd.read_csv(INDEX_PATH, parse_dates=["date"])
    return investors, benchmarks, cfg, frames, meta, index_df


investors, benchmarks, cfg, frames, meta, index_df = load_all()

PRINCIPAL = float(cfg.get("principal", UNIT))
SCALE = PRINCIPAL / UNIT                       # nav → 万元
BASE_DATE = pd.Timestamp(cfg["start_date"]) if cfg.get("start_date") else None
ITEMS_META = meta.get("items", {})

PALETTE = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
           "#8c564b", "#e377c2", "#17becf", "#bcbd22", "#7f7f7f"]


# ----------------------------------------------------------------------------- 顶部
st.title("📈 投资组合价值看板")
st.caption(f"口径：每人本金 **{PRINCIPAL / 1e4:,.0f} 万元**，自 {cfg.get('start_date')} 起按标的"
           "**未复权价格收益**折算市值（不含分红 / 汇率）；Y 轴单位为**万元**，起点 100 万。")

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
ok = sum(1 for v in ITEMS_META.values() if v.get("status") == "ok")
errs = [v.get("name", "") for v in ITEMS_META.values() if v.get("status") == "error"]
n_inv = len(investors)
st.caption(f"数据更新时间(UTC)：{updated} ｜ {n_inv} 位投资者 / {ok} 个标的成功"
           + (f" ｜ ⚠️ 失败：{', '.join(errs)}" if errs else ""))
if index_df is not None and not index_df.empty:
    iv_wan = float(index_df["index"].iloc[-1]) * SCALE
    st.caption(f"群体平均（基准 {cfg.get('start_date')} = 100 万）：**{iv_wan:,.0f} 万元**"
               f"（{(iv_wan * 1e4 / PRINCIPAL - 1):+.2%}）")


# ----------------------------------------------------------------------------- 侧边栏筛选
with st.sidebar:
    st.header("筛选")
    markets = sorted({inv.market for inv in investors})
    types = sorted({inv.type for inv in investors})
    mkt_labels = {"cn": "A股/境内", "us": "美股/境外"}
    type_labels = {"stock": "股票", "etf": "ETF", "bond": "债券", "index": "指数"}

    sel_mkt = st.multiselect("市场", markets, default=markets,
                             format_func=lambda m: mkt_labels.get(m, m))
    sel_type = st.multiselect("类型", types, default=types,
                              format_func=lambda t: type_labels.get(t, t))

    candidates = [inv for inv in investors
                  if inv.market in sel_mkt and inv.type in sel_type]
    sel_names = st.multiselect(
        "投资者（昵称）", [inv.nickname for inv in candidates],
        default=[inv.nickname for inv in candidates])

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
    d = slice_df(frames[inv.key]) if inv.key in frames else None
    if d is None or d.empty:
        continue
    y = to_profit_wan(d["nav"]) if show_profit else to_wan(d["nav"])
    fig.add_trace(go.Scatter(
        x=d["date"], y=y, name=inv.nickname, mode="lines",
        line=dict(color=PALETTE[i % len(PALETTE)], width=1.6)))

if show_bench:
    bench_dashes = ["dash", "dot"]              # 沪深300=虚线、标普500=点线（均为灰色参考线）
    for j, b in enumerate(benchmarks):
        bf = frames.get(b.key)
        if bf is None or BASE_DATE is None:
            continue
        after = bf[bf["date"] >= BASE_DATE]     # 以基准日 = 100 万归一
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
    d = slice_df(frames[inv.key]) if inv.key in frames else None
    if d is None or d.empty:
        continue
    rec = ITEMS_META.get(inv.key, {})
    base_close = float(d["close"].iloc[0])
    last_close = float(d["close"].iloc[-1])
    rows.append({
        "昵称": inv.nickname,
        "代码": inv.symbol,
        "市场": inv.market.upper(),
        "类型": inv.type,
        "起始日": d["date"].iloc[0].date(),
        "起始价": base_close,
        "最新价": last_close,
        # 以下三列存 **数值（分数）**，点列头即可按数值正确排序；显示交给 column_config
        "市值(万元)": last_close / base_close * PRINCIPAL / 1e4,
        "收益额(万元)": (last_close / base_close - 1.0) * PRINCIPAL / 1e4,
        "收益率": float(d["cum_return"].iloc[-1]),
        "年化": rec.get("annualized"),
        "最大回撤": rec.get("max_drawdown"),
        "更新日": d["date"].iloc[-1].date(),
    })
table = pd.DataFrame(rows).sort_values("收益率", ascending=False).set_index("昵称")
st.dataframe(
    table, use_container_width=True,
    column_config={
        "起始价": st.column_config.NumberColumn(format="%.3f"),
        "最新价": st.column_config.NumberColumn(format="%.3f"),
        "市值(万元)": st.column_config.NumberColumn(format="%.1f"),
        "收益额(万元)": st.column_config.NumberColumn(format="%+.1f"),
        "收益率": st.column_config.NumberColumn(format="percent"),
        "年化": st.column_config.NumberColumn(format="percent"),
        "最大回撤": st.column_config.NumberColumn(format="percent"),
    })

st.caption("说明：全部为**价格**口径（未复权收盘价，不含分红 / 汇率）。"
           "市值 = 本金 × close(t)/close(起始日)；年化按实际天数几何折算；"
           "最大回撤基于归一化价格序列。仅展示起始日及之后的数据。"
           "真实姓名 ↔ 昵称映射保存在本地，看板只显示昵称。")
