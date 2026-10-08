"""「陆续建仓」模式的离线测试（不联网）。

场景：所有投资者在 start_date 都持有 100 万**现金**，之后**按各自提交日**陆续建仓；
建仓记录在 `switches.yaml`（起点 = 现金，目标 = 真实标的）。

配套改动：
  - `tools/make_investors --initial-cash`：把初始持仓生成为现金（保留计划持仓作注释）
  - `tools/rebuild_switches`：把「目标 == 初始持仓」的恒等调仓，重建为「现金 → 建仓」

★ 关键不变量：**建仓日之前收益恒为 0**；建仓日收盘价买入，次日起跟随标的。
不使用 pytest 的 `tmp_path`（沙箱里跑不起来），改用工作区临时目录。
"""
from __future__ import annotations

import shutil
import sys
import uuid
from pathlib import Path

import pandas as pd
import pytest
import yaml

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.aggregate import weighted_path_nav  # noqa: E402
from src.config import (Investor, Item, Switch,  # noqa: E402
                        investor_weight_segments, load_investors)
from tools import make_investors  # noqa: E402

MEMBERS = """\
base_currency: CNY
start_date: 2026-09-24
principal: 1000000

members:
  - {real_name: "S001", symbol: "510300", market: cn, type: etf}
  - {real_name: "S002", symbol: "NVDA", market: us, type: stock}
  - real_name: "S003"
    holdings:
      - {symbol: "01810", market: hk, type: stock, weight: 0.7}
      - {symbol: "ONDO", market: us, type: crypto, weight: 0.3}
  - {real_name: "S004", symbol: "999999", market: cn, type: cash}

benchmarks:
  - {name: 沪深300, symbol: "000300", market: cn, type: index}
"""

ROSTER = "real_name,nickname\nS001,investor01\nS002,investor02\nS003,investor03\nS004,investor04\n"


@pytest.fixture()
def workdir():
    d = _ROOT / f".tmp_test_ramp_{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "members.yaml").write_text(MEMBERS, encoding="utf-8")
    (d / "roster.csv").write_text(ROSTER, encoding="utf-8")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------- --initial-cash
def test_initial_cash_makes_everyone_cash(workdir):
    """★ 所有人初始持仓 = 现金；计划持仓保留为注释，便于对照。"""
    out = workdir / "investors.yaml"
    assert make_investors.build(workdir / "members.yaml", workdir / "roster.csv",
                                out, initial_cash=True) == 0
    cfg = yaml.safe_load(out.read_text(encoding="utf-8"))
    for rec in cfg["investors"]:
        assert rec["type"] == "cash", rec
        assert rec["symbol"] == make_investors.CASH_MARK

    raw = out.read_text(encoding="utf-8")
    assert "计划持仓: 510300/cn/etf" in raw
    assert "计划持仓: NVDA/us/stock" in raw
    assert "01810/hk/stock 70%" in raw          # 多标的计划持仓
    assert "计划持仓: 999999/cn/cash" in raw     # 本来就是 cash 的照旧

    # 基准不受影响
    assert [b["name"] for b in cfg["benchmarks"]] == ["沪深300"]


def test_initial_cash_loads_as_valid_config(workdir):
    """生成的文件必须能被 load_investors 正常解析（每人一条现金腿）。"""
    out = workdir / "investors.yaml"
    make_investors.build(workdir / "members.yaml", workdir / "roster.csv",
                         out, initial_cash=True)
    invs = {i.nickname: i for i in load_investors(out)}
    assert len(invs) == 4
    for i in invs.values():
        assert len(i.legs) == 1
        assert i.legs[0].type == "cash"
        assert i.legs[0].key == "cash"


def test_without_flag_keeps_real_holdings(workdir):
    """不加 --initial-cash 时行为不变（回归保护）。"""
    out = workdir / "investors.yaml"
    make_investors.build(workdir / "members.yaml", workdir / "roster.csv", out)
    cfg = yaml.safe_load(out.read_text(encoding="utf-8"))
    by = {r["nickname"]: r for r in cfg["investors"]}
    assert by["investor01"]["symbol"] == "510300"
    assert by["investor01"]["type"] == "etf"
    assert len(by["investor03"]["holdings"]) == 2


# --------------------------------------------------------------------- ★ 现金 → 建仓
def _series():
    dates = pd.to_datetime(["2026-09-24", "2026-09-25", "2026-09-28",
                            "2026-09-29", "2026-09-30", "2026-10-01"])
    # 建仓日 09-28 的收盘价 = 11.40；之后涨到 12.00 / 12.60
    return pd.Series([11.30, 11.35, 11.40, 12.00, 12.60, 13.00], index=dates)


def _run_cash_then_stock(switch_date: str):
    inv = Investor(nickname="inv", symbol=make_investors.CASH_MARK,
                   market="cn", type="cash", start_date="2026-09-24")
    sw = [Switch(date=switch_date, nickname="inv", symbol="510300",
                 market="cn", type="stock")]
    segs = investor_weight_segments([inv], sw)["inv"]
    wsegs = [(d, {it.key: w for it, w in legs}) for d, legs in segs]
    return weighted_path_nav({"cn_stock_510300": _series()}, wsegs, 1_000_000.0)


def test_cash_until_switch_then_follows_symbol():
    """★ 建仓日之前收益恒为 0；建仓日收盘买入，次日起跟随标的。"""
    df = _run_cash_then_stock("2026-09-28").set_index("date")

    # 建仓日之前（含 09-25、09-28 当天）→ 0%
    for d in ("2026-09-24", "2026-09-25", "2026-09-28"):
        assert df.loc[pd.Timestamp(d), "cum_return"] == pytest.approx(0.0), d
        assert df.loc[pd.Timestamp(d), "holding"] == "cash", d

    # 建仓日次日切换为标的
    assert df.loc[pd.Timestamp("2026-09-29"), "holding"] == "cn_stock_510300"
    # 收益 = 12.00 / 11.40 - 1
    assert df.loc[pd.Timestamp("2026-09-29"), "cum_return"] == pytest.approx(12.00 / 11.40 - 1)
    assert df.loc[pd.Timestamp("2026-09-30"), "cum_return"] == pytest.approx(12.60 / 11.40 - 1)


def test_switch_on_last_available_date_gives_zero():
    """★ 建仓日 == 序列最后一天：新段的基准价就是当天收盘价 → 收益 0。

    这是框架口径的必然结果（不是 bug）：数据里没有建仓之后的价格，
    所以要到下一个交易日才会出现非零收益。
    """
    df = _run_cash_then_stock("2026-10-01").set_index("date")
    last = df.index[-1]
    assert df.loc[last, "cum_return"] == pytest.approx(0.0)
    assert df.loc[last, "holding"] == "cash"        # 当日仍算旧状态（现金）


def test_pure_cash_portfolio_is_flat():
    """未建仓（无 switch）→ 全程 0%，且曲线存在（不是消失）。"""
    inv = Investor(nickname="inv", symbol=make_investors.CASH_MARK,
                   market="cn", type="cash", start_date="2026-09-24")
    segs = investor_weight_segments([inv], [])["inv"]
    wsegs = [(d, {it.key: w for it, w in legs}) for d, legs in segs]
    hint = _series().index
    df = weighted_path_nav({}, wsegs, 1_000_000.0, calendar_hint=hint)
    assert len(df) == len(hint)
    assert (df["cum_return"].abs() < 1e-12).all()
    assert set(df["holding"]) == {"cash"}


def test_cash_leg_key_is_cash_regardless_of_symbol():
    """`type: cash` 压过 symbol：key 恒为 'cash'（建仓模式的基石）。"""
    for sym in ("510300", "", "999999"):
        it = Item(name="x", symbol=sym, market="cn", type="cash")
        assert it.key == "cash"
