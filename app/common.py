"""看板共用：路径引导 + 数据加载（供 app/streamlit_app.py 与 app/pages/* 复用）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # 仓库根
APP = Path(__file__).resolve().parent           # app/
for _p in (str(ROOT), str(APP)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pandas as pd          # noqa: E402
import streamlit as st       # noqa: E402

from src.config import (  # noqa: E402
    INDEX_PATH,
    META_PATH,
    PORTFOLIOS_DIR,
    PRICES_DIR,
    RETURNS_DIR,
    load_investor_benchmarks,
    load_investor_config,
    load_investors,
    portfolio_id,
)

UNIT = 1_000_000.0   # 100 万；`nav`（基准=100）换算成「万元」的缩放因子 = principal/UNIT
PALETTE = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
           "#8c564b", "#e377c2", "#17becf", "#bcbd22", "#7f7f7f"]


def holding_label(key: str) -> str:
    """把 key（market_type_symbol）渲染成给人看的「代码 (市场)」。"""
    parts = str(key).split("_", 2)
    if len(parts) == 3:
        return f"{parts[2]} ({parts[0].upper()})"
    return str(key)


def load_price(key: str) -> pd.Series | None:
    """读某标的的未复权收盘价序列（data/prices/{key}.csv，日期索引）。"""
    p = PRICES_DIR / f"{key}.csv"
    if not p.exists():
        return None
    s = pd.read_csv(p, parse_dates=["date"]).set_index("date")["close"].sort_index()
    return s if not s.dropna().empty else None


def ensure_holding(df: pd.DataFrame, rec: dict) -> pd.DataFrame:
    """兼容旧数据：portfolios 缺 `holding` 列时，用 meta 的 holdings 按日期推算当日持仓。"""
    if "holding" in df.columns:
        return df
    holdings = rec.get("holdings", [])
    d = df.sort_values("date").reset_index(drop=True).copy()
    if not holdings:
        return d
    bnds = []
    for h in holdings:
        cand = d.loc[d["date"] >= pd.Timestamp(h["date"]), "date"]
        bnds.append(cand.iloc[0] if len(cand) else None)

    def _hold(t) -> str:
        k = 0
        for j in range(1, len(bnds)):
            if bnds[j] is not None and bnds[j] < t:
                k = j
        return holdings[k]["key"]

    d["holding"] = d["date"].map(_hold)
    return d


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
