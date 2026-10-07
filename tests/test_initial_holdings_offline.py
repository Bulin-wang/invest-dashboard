"""初始持仓（initial holdings）相关回归测试 —— 离线，不联网。

覆盖：
- ★ 初始持仓就是**多标的组合**（1/2/3 腿 + 现金腿 + 省略权重=等权）
- ★ `make_investors` 对**现金腿**（没有 symbol）不能崩
- `holdings` → `holding_spec` 的规范化（权重归一、按 key 排序）
- 组合收益 = Σwᵢ·Pᵢ(t)/Pᵢ(基点)，与文档口径逐位一致

注：不使用 pytest 的 `tmp_path`（沙箱里需要目录符号链接，会失败），
    改用工作区内的临时目录（见 `workdir`）。
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

from src.aggregate import holding_spec, weighted_path_nav  # noqa: E402
from src.config import Item, load_investors  # noqa: E402
from tools import make_investors  # noqa: E402

HEAD = """
base_currency: CNY
start_date: 2026-09-24
principal: 1000000
"""

# 四种写法：单标的 / 两腿显式权重 / 三腿含现金 / 多腿等权
BODY = """
  - {real_name: "S001", symbol: "600519", market: cn, type: stock}
  - real_name: "S002"
    holdings:
      - {symbol: "600519", market: cn, type: stock, weight: 0.7}
      - {symbol: "AAPL", market: us, type: stock, weight: 0.3}
  - real_name: "S003"
    holdings:
      - {symbol: "600519", market: cn, type: stock, weight: 0.4}
      - {symbol: "AAPL", market: us, type: stock, weight: 0.3}
      - {type: cash, weight: 0.3}
  - real_name: "S004"
    holdings:
      - {symbol: "600519", market: cn, type: stock}
      - {symbol: "AAPL", market: us, type: stock}
"""

ROSTER = "real_name,nickname\nS001,investor01\nS002,investor02\nS003,investor03\nS004,investor04\n"


@pytest.fixture()
def workdir():
    d = _ROOT / f".tmp_test_initial_{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True, exist_ok=True)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


@pytest.fixture()
def built(workdir):
    """跑一遍 make_investors，返回 (members_path, public_path)。"""
    members = workdir / "members.yaml"
    members.write_text(HEAD + "members:" + BODY, encoding="utf-8")
    roster = workdir / "roster.csv"
    roster.write_text(ROSTER, encoding="utf-8")
    public = workdir / "investors.yaml"
    assert make_investors.build(members, roster, public) == 0
    return members, public


# --------------------------------------------------------------------- 解析
def test_initial_holdings_parsed_as_multi_asset(built):
    """★ 初始持仓可以直接是多标的组合（不是只能全仓单一标的）。"""
    inv = {i.nickname: i for i in load_investors(built[1])}

    assert len(inv["investor01"].legs) == 1                     # 旧写法仍等价单腿
    assert inv["investor01"].holding_spec == "cn_stock_600519"

    assert inv["investor02"].holding_spec == "cn_stock_600519:0.7|us_stock_AAPL:0.3"
    assert inv["investor02"].weights == {"cn_stock_600519": 0.7, "us_stock_AAPL": 0.3}

    assert set(inv["investor03"].weights) == {"cn_stock_600519", "us_stock_AAPL", "cash"}
    assert inv["investor03"].weights["cash"] == pytest.approx(0.3)

    # 省略 weight → 等权
    assert inv["investor04"].weights == {"cn_stock_600519": 0.5, "us_stock_AAPL": 0.5}


def test_weights_normalized_to_one(built):
    """乱写权重（不求和为 1）也要内部归一化。"""
    inv = {i.nickname: i for i in load_investors(built[1])}
    for nick in ("investor02", "investor03", "investor04"):
        assert sum(inv[nick].weights.values()) == pytest.approx(1.0)


# --------------------------------------------------------------------- ★ 现金腿
def test_make_investors_handles_cash_leg(built):
    """★ 现金腿没有 symbol，make_investors 不能因此崩溃（曾经的 KeyError）。"""
    pub = yaml.safe_load(built[1].read_text(encoding="utf-8"))
    legs = next(r for r in pub["investors"] if r["nickname"] == "investor03")["holdings"]
    cash = [l for l in legs if l["type"] == "cash"]
    assert len(cash) == 1
    assert cash[0]["symbol"] == "CASH"        # 自动补上占位 symbol
    assert cash[0]["weight"] == pytest.approx(0.3)


def test_make_investors_rejects_leg_without_symbol(workdir):
    """既没有 symbol 也不是 cash → 明确报错，而不是写入坏数据。"""
    members = workdir / "bad_members.yaml"
    members.write_text(HEAD + 'members:\n  - real_name: "S001"\n'
                              "    holdings:\n"
                              '      - {market: cn, type: stock, weight: 1.0}\n',
                       encoding="utf-8")
    roster = workdir / "bad_roster.csv"
    roster.write_text("real_name,nickname\nS001,investor01\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="holdings"):
        make_investors.build(members, roster, workdir / "bad_out.yaml")


# --------------------------------------------------------------------- 收益口径
def test_multi_asset_initial_holding_return_math():
    """初始就是组合时，收益 = Σwᵢ·Pᵢ(t)/Pᵢ(基点)——与文档口径逐位一致。"""
    dates = pd.to_datetime(["2026-09-24", "2026-09-25", "2026-09-28"])
    a = pd.Series([100.0, 110.0, 120.0], index=dates)     # +20%
    b = pd.Series([50.0, 50.0, 55.0], index=dates)        # +10%
    segs = [("2026-09-24", {"A": 0.6, "B": 0.3, "cash": 0.1})]
    df = weighted_path_nav({"A": a, "B": b}, segs, 1_000_000.0)

    want = (0.6 * 120 / 100 + 0.3 * 55 / 50 + 0.1) * 1_000_000.0
    assert df["value"].iloc[-1] == pytest.approx(want)
    assert df["value"].iloc[0] == pytest.approx(1_000_000.0)      # 基点必为 100 万
    assert df["cum_return"].iloc[-1] == pytest.approx(want / 1e6 - 1)
    assert df["holding"].iloc[-1] == "A:0.6|B:0.3|cash:0.1"


def test_holding_spec_is_sorted_and_normalized():
    assert holding_spec({"B": 0.3, "A": 0.7}) == "A:0.7|B:0.3"     # 按 key 排序
    assert holding_spec({"A": 7, "B": 3}) == "A:0.7|B:0.3"         # 归一化
    assert holding_spec({"A": 1.0}) == "A"                         # 单腿 = 裸 key
    assert holding_spec({"cash": 0.4, "A": 0.6}) == "A:0.6|cash:0.4"


def test_item_expires_survives_holdings_path():
    """expires 也要能从 holdings 腿透传出来（期货组合持仓）。"""
    it = Item(name="x", symbol="SC2611", market="cn", type="futures",
              expires="2026-10-30")
    assert it.expires == "2026-10-30"
    assert it.key == "cn_futures_SC2611"
