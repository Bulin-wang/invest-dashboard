"""本地管理台（Streamlit）—— 辅助维护「投资者名单」与「调仓流水」。

⚠️ **仅本地使用**：本页会写入 `private/` 与 `switches.yaml`，**切勿部署到公网 / Streamlit Cloud**。

运行：
    python -m streamlit run tools/admin_app.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd
import streamlit as st

from src.config import PRIVATE_DIR, SWITCHES_PATH, load_investors, load_switches
from tools import admin_ops, make_investors

ROSTER_PATH = PRIVATE_DIR / "roster.csv"
MEMBERS_PATH = PRIVATE_DIR / "investors.private.yaml"
MARKETS = ["cn", "us", "hk"]
TYPES = ["stock", "etf", "bond", "index"]

st.set_page_config(page_title="本地管理台", page_icon="⚙️", layout="wide")
st.title("⚙️ 投资组合 · 本地管理台")
st.warning("**仅本地使用**：本页会写入 `private/` 与 `switches.yaml`。请勿部署到公网 / Streamlit Cloud。")

if "flash" in st.session_state:
    flash = st.session_state["flash"]
    del st.session_state["flash"]
    st.success(flash)
    if flash.startswith("✅ 已追加"):
        st.info("提醒：运行 `python -m prototype.daily_close` 重算组合收益。")

investors = load_investors()
roster_rev = {nk: rn for rn, nk in admin_ops.read_roster(ROSTER_PATH)}


def disp(nk: str) -> str:
    rn = roster_rev.get(nk)
    return f"{nk}（{rn}）" if rn else nk


auto_nk = admin_ops.next_nickname([i.nickname for i in investors])

c1, c2, c3 = st.columns(3)
c1.metric("投资者", len(investors))
c2.metric("调仓条数", len(load_switches()))
c3.metric("下一个自动昵称", auto_nk)

tab_add, tab_switch = st.tabs(["➕ 新增投资者", "🔁 追加调仓"])

# ----------------------------------------------------------------- 新增投资者
with tab_add:
    st.caption(f"写 `{ROSTER_PATH.name}` + `{MEMBERS_PATH.name}`，并重生成 `investors.yaml`")
    with st.form("add_investor"):
        real_name = st.text_input("真实姓名（仅本地私密）", key="a_name")
        nickname = st.text_input("昵称（留空 = 自动）", value=auto_nk, key="a_nick")
        col1, col2, col3 = st.columns(3)
        symbol = col1.text_input("初始标的代码", placeholder="600519 / AAPL / 00700", key="a_sym")
        market = col2.selectbox("市场", MARKETS, key="a_mkt")
        type_ = col3.selectbox("类型", TYPES, key="a_typ")
        submitted = st.form_submit_button("提交")

    if submitted:
        rn = real_name.strip()
        nk = nickname.strip() or auto_nk
        existing_real = {r for r, _ in admin_ops.read_roster(ROSTER_PATH)}
        existing_nicks = {i.nickname for i in investors} | {n for _, n in admin_ops.read_roster(ROSTER_PATH)}
        errs = []
        if not rn:
            errs.append("真实姓名不能为空")
        if rn in existing_real:
            errs.append(f"真实姓名「{rn}」已存在")
        if not symbol.strip():
            errs.append("标的代码不能为空")
        if nk in existing_nicks:
            errs.append(f"昵称「{nk}」已被占用")
        if errs:
            st.error("；".join(errs))
        else:
            admin_ops.append_roster(ROSTER_PATH, rn, nk)
            admin_ops.append_member(MEMBERS_PATH, rn, symbol.strip(), market, type_)
            make_investors.build()          # 重生成公开的 investors.yaml
            n = len(load_investors())
            st.session_state["flash"] = (
                f"✅ 已新增 {nk}（{rn}） · 初始持仓 {symbol.strip()}（{market}/{type_}）"
                f" → investors.yaml 现 {n} 人")
            st.rerun()

# ----------------------------------------------------------------- 追加调仓
with tab_switch:
    st.caption(f"追加到 `{SWITCHES_PATH.name}`（append-only）；改完跑 `python -m prototype.daily_close` 重算")
    if not investors:
        st.info("还没有投资者，请先在「新增投资者」里添加。")
    else:
        with st.form("add_switch"):
            pick = st.selectbox("投资者", investors, format_func=lambda it: disp(it.nickname), key="s_inv")
            date = st.date_input("生效日（该日收盘全仓切换；逢非交易日自动顺延）", key="s_date")
            col1, col2, col3 = st.columns(3)
            symbol = col1.text_input("目标标的代码", placeholder="600519 / AAPL / 00700", key="s_sym")
            market = col2.selectbox("市场", MARKETS, key="s_mkt")
            type_ = col3.selectbox("类型", TYPES, key="s_typ")
            submitted2 = st.form_submit_button("提交")

        if submitted2:
            if not symbol.strip():
                st.error("目标标的代码不能为空")
            else:
                admin_ops.append_switch(SWITCHES_PATH, date.isoformat(), pick.nickname,
                                        symbol.strip(), market, type_)
                st.session_state["flash"] = (
                    f"✅ 已追加：{disp(pick.nickname)} 于 {date.isoformat()} 切换至 "
                    f"{symbol.strip()}（{market}/{type_}）")
                st.rerun()

# ----------------------------------------------------------------- 当前名单
st.divider()
with st.expander("当前名单（真名 ↔ 昵称 ↔ 初始标的）"):
    sym = {i.nickname: (i.symbol, i.market, i.type) for i in investors}
    rows = []
    for rn, nk in admin_ops.read_roster(ROSTER_PATH):
        s = sym.get(nk, ("—", "", ""))
        rows.append({"真实姓名": rn, "昵称": nk, "初始标的": s[0], "市场": s[1], "类型": s[2]})
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
