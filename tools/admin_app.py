"""本地管理台（Streamlit）—— 辅助维护「投资者名单」与「调仓流水」。

支持**多标的权重**：初始持仓 / 调仓都可以在表格里填多行（一行 = 一个标的 + 权重）。
（只填 1 行 = 原来的单标的写法，行为与以前一致。）

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
# fund   = **场外开放式基金**（无交易所前缀、走东方财富净值接口、累计净值口径）
#          与 etf（场内 ETF/LOF，走行情端点）是两回事，别混用：
#          同一只基金若有人写 fund、有人写 etf，会各抓一份、各算一套收益。
# crypto = **加密货币现货交易对**（走币安公开镜像、UTC 日线收盘价、**含周末**）；
#          代码写 BTCUSDT 或简写 BTC（自动补 USDT）；market 填 us 即可。
# futures= **期货合约**（新浪期货、结算价口径、**有到期日**）；目前仅支持上期能源原油 SC，
#          代码写 SC2611；**必须在「到期日」列填最后交易日**，否则到期后不会转现金。
TYPES = ["stock", "etf", "bond", "index", "fund", "crypto", "futures", "cash"]  # cash = 现金腿（价格恒为 1）

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


def hold_text(inv) -> str:
    """某投资者持仓的短文本（多标的带权重）。"""
    if not inv.holdings:
        return f"{inv.symbol} ({inv.market}/{inv.type})"
    w = inv.weights
    return " + ".join(f"{it.symbol} ({it.market}) {w[it.key]:.0%}" for it in inv.holdings)


def legs_editor(key: str):
    """持仓/调仓的表格录入：一行 = 一个标的（可加行 + 权重 + 到期日）。"""
    seed = pd.DataFrame([{"代码": "", "市场": "cn", "类型": "stock",
                          "权重": 1.0, "到期日": None}])
    return st.data_editor(
        seed, num_rows="dynamic", hide_index=True, key=key,
        column_config={
            "市场": st.column_config.SelectboxColumn("市场", options=MARKETS, required=True),
            "类型": st.column_config.SelectboxColumn("类型", options=TYPES, required=True),
            "权重": st.column_config.NumberColumn("权重", min_value=0.0, step=0.1, format="%.2f"),
            "到期日": st.column_config.TextColumn(
                "到期日", help="仅期货等有到期日的标的需填：最后交易日，如 2026-10-30。"
                              "当天仍有价，次日起余额按现金处理（= 到期未滚仓则不涨不跌）。留空 = 不到期。"),
        })


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
        st.markdown("**初始持仓**：1 行 = 单标的；多行 + 权重 = 权重组合（权重可留空 = 等权）；"
                    "类型选 `cash` = 现金腿（代码可留空）")
        legs_df = legs_editor("a_legs")
        submitted = st.form_submit_button("提交")

    if submitted:
        rn = real_name.strip()
        nk = nickname.strip() or auto_nk
        legs = admin_ops.legs_from_rows(legs_df.to_dict("records"))
        existing_real = {r for r, _ in admin_ops.read_roster(ROSTER_PATH)}
        existing_nicks = {i.nickname for i in investors} | {n for _, n in admin_ops.read_roster(ROSTER_PATH)}
        errs = []
        if not rn:
            errs.append("真实姓名不能为空")
        if rn in existing_real:
            errs.append(f"真实姓名「{rn}」已存在")
        if not legs:
            errs.append("初始持仓至少要填一行（代码不能为空）")
        if nk in existing_nicks:
            errs.append(f"昵称「{nk}」已被占用")
        if errs:
            st.error("；".join(errs))
        else:
            admin_ops.append_roster(ROSTER_PATH, rn, nk)
            if len(legs) == 1:
                s1, m1, t1, _w1 = legs[0]
                admin_ops.append_member(MEMBERS_PATH, rn, s1, m1, t1)   # 单标的：旧写法
                hold = f"{s1}（{m1}/{t1}）"
            else:
                admin_ops.append_member_multi(MEMBERS_PATH, rn, legs)   # 多标的：holdings
                hold = " + ".join(f"{s1} ({m1}) {w1:.0%}" for s1, m1, _t1, w1 in legs)
            make_investors.build()          # 重生成公开的 investors.yaml
            n = len(load_investors())
            st.session_state["flash"] = (
                f"✅ 已新增 {nk}（{rn}） · 初始持仓 {hold} → investors.yaml 现 {n} 人")
            st.rerun()

# ----------------------------------------------------------------- 追加调仓
with tab_switch:
    st.caption(f"追加到 `{SWITCHES_PATH.name}`（append-only）；改完跑 `python -m prototype.daily_close` 重算")
    if not investors:
        st.info("还没有投资者，请先在「新增投资者」里添加。")
    else:
        with st.form("add_switch"):
            pick = st.selectbox("投资者", investors, format_func=lambda it: disp(it.nickname), key="s_inv")
            st.caption(f"当前持仓：{hold_text(pick)}")
            date = st.date_input("生效日（该日收盘切换；逢非交易日自动顺延）", key="s_date")
            st.markdown("**目标持仓**：1 行 = 全仓切换到该标的；多行 + 权重 = 调仓到权重组合；"
                        "`类型=cash` = 现金腿（代码可留空，可用来减仓/空仓）")
            legs_df2 = legs_editor("s_legs")
            submitted2 = st.form_submit_button("提交")

        if submitted2:
            legs2 = admin_ops.legs_from_rows(legs_df2.to_dict("records"))
            if not legs2:
                st.error("目标持仓至少要填一行（代码不能为空）")
            else:
                if len(legs2) == 1:
                    s2, m2, t2, _w2 = legs2[0]
                    admin_ops.append_switch(SWITCHES_PATH, date.isoformat(),
                                            pick.nickname, s2, m2, t2)   # 单标的：旧写法
                    tgt = f"{s2}（{m2}/{t2}）"
                else:
                    admin_ops.append_switch_multi(SWITCHES_PATH, date.isoformat(),
                                                  pick.nickname, legs2)  # 多标的：holdings
                    tgt = " + ".join(f"{s2} ({m2}) {w2:.0%}" for s2, m2, _t2, w2 in legs2)
                st.session_state["flash"] = (
                    f"✅ 已追加：{disp(pick.nickname)} 于 {date.isoformat()} 调仓至 {tgt}")
                st.rerun()

# ----------------------------------------------------------------- 当前名单
st.divider()
with st.expander("当前名单（真名 ↔ 昵称 ↔ 初始标的）"):
    hold = {i.nickname: hold_text(i) for i in investors}
    rows = []
    for rn, nk in admin_ops.read_roster(ROSTER_PATH):
        rows.append({"真实姓名": rn, "昵称": nk, "初始持仓": hold.get(nk, "—")})
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
