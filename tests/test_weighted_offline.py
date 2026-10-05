"""多标的权重组合（buy & hold）的离线单测（不联网）。运行： python -m pytest tests/ -q"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.aggregate import holding_spec, normalize_weights, weighted_path_nav  # noqa: E402
from src.config import (  # noqa: E402
    Investor,
    Switch,
    investor_weight_segments,
    load_investors,
    load_switches,
)
from src.portfolio import portfolio_nav  # noqa: E402


def _s(pairs) -> pd.Series:
    return pd.Series([v for _, v in pairs], index=pd.to_datetime([d for d, _ in pairs]))


# --------------------------------------------------------------- 权重 / 标签
def test_normalize_weights_and_holding_spec():
    assert normalize_weights({"a": 1, "b": 1}) == {"a": 0.5, "b": 0.5}
    assert normalize_weights({"a": 2, "b": 6}) == {"a": 0.25, "b": 0.75}
    assert normalize_weights({"a": 3}) == {"a": 1.0}
    assert holding_spec({"cn_stock_600519": 1.0}) == "cn_stock_600519"     # 单腿 = 裸 key
    assert holding_spec({"b": 0.25, "a": 0.75}) == "a:0.75|b:0.25"          # 按 key 排序
    with pytest.raises(ValueError):
        normalize_weights({"a": 0})


# --------------------------------------------------------------- 组合数学
def test_two_legs_equally_weighted_buy_and_hold():
    """50/50 买入持有：一腿 +10%、一腿 -10% → 组合 0%（不再平衡）。"""
    series = {
        "A": _s([("2026-09-24", 100.0), ("2026-09-25", 110.0)]),
        "B": _s([("2026-09-24", 100.0), ("2026-09-25", 90.0)]),
    }
    df = weighted_path_nav(series, [("2026-09-24", {"A": 0.5, "B": 0.5})], principal=100.0)
    assert df["value"].tolist() == pytest.approx([100.0, 100.0])
    assert df["cum_return"].iloc[-1] == pytest.approx(0.0)


def test_weights_normalized_and_value_scales():
    """权重不必手工归一化：3:1 且两腿同涨 10% → 组合 +10%。"""
    series = {
        "A": _s([("2026-09-24", 10.0), ("2026-09-25", 11.0)]),
        "B": _s([("2026-09-24", 20.0), ("2026-09-25", 22.0)]),
    }
    df = weighted_path_nav(series, [("2026-09-24", {"A": 3, "B": 1})], principal=200.0)
    assert df["value"].tolist() == pytest.approx([200.0, 220.0])
    assert df["holding"].iloc[0] == "A:0.75|B:0.25"


def test_switch_day_uses_old_basket():
    """调仓日当日算旧组合，次交易日起算新组合。"""
    series = {
        "A": _s([("2026-09-24", 100.0), ("2026-09-25", 110.0), ("2026-09-26", 121.0)]),
        "B": _s([("2026-09-24", 50.0), ("2026-09-25", 60.0), ("2026-09-26", 66.0)]),
    }
    segs = [("2026-09-24", {"A": 1.0}), ("2026-09-25", {"B": 1.0})]
    df = weighted_path_nav(series, segs, principal=100.0)
    by_date = dict(zip(df["date"].dt.strftime("%Y-%m-%d"), df["value"]))
    assert by_date["2026-09-25"] == pytest.approx(110.0)   # 当日仍算 A
    assert by_date["2026-09-26"] == pytest.approx(121.0)   # 次日起算 B（1.10 × 1.10）
    assert df["holding"].tolist() == ["A", "A", "B"]


def test_weight_switch_keeps_ratio_chain():
    """权重组合切换：旧组合 60/40 涨到 1.06，换成 100% C 后再涨 10% → 1.166。"""
    series = {
        "A": _s([("2026-09-24", 100.0), ("2026-09-25", 110.0)]),
        "B": _s([("2026-09-24", 100.0), ("2026-09-25", 100.0)]),
        "C": _s([("2026-09-24", 10.0), ("2026-09-25", 11.0), ("2026-09-26", 12.1)]),
    }
    segs = [("2026-09-24", {"A": 0.6, "B": 0.4}), ("2026-09-25", {"C": 1.0})]
    df = weighted_path_nav(series, segs, principal=100.0)
    by_date = dict(zip(df["date"].dt.strftime("%Y-%m-%d"), df["value"]))
    assert by_date["2026-09-25"] == pytest.approx(106.0)   # 0.6×1.10 + 0.4×1.00
    assert by_date["2026-09-26"] == pytest.approx(116.6)   # 106 × 1.10


def test_single_leg_matches_legacy_implementation():
    """回归保护：单标的路径下，新实现与旧的 src.portfolio.portfolio_nav 完全一致。"""
    series = {
        "A": _s([("2026-09-24", 100.0), ("2026-09-25", 110.0), ("2026-09-26", 99.0)]),
        "B": _s([("2026-09-24", 50.0), ("2026-09-26", 60.0), ("2026-09-28", 30.0)]),
    }
    segments = [("2026-09-24", "A"), ("2026-09-25", "B")]
    old = portfolio_nav(series, segments, principal=100.0)
    new = weighted_path_nav(series, [(d, {k: 1.0}) for d, k in segments], principal=100.0)
    assert new["date"].tolist() == old["date"].tolist()
    assert new["holding"].tolist() == old["holding"].tolist()
    assert new["value"].tolist() == pytest.approx(old["value"].tolist())


def test_missing_prices_raises():
    with pytest.raises(ValueError):
        weighted_path_nav({}, [("2026-09-24", {"A": 1.0})])


# --------------------------------------------------------------- 配置层
def test_load_investors_with_holdings(tmp_path):
    p = tmp_path / "investors.yaml"
    p.write_text("""
start_date: 2026-09-24
principal: 1000000
investors:
  - {nickname: Alice, symbol: "600519", market: cn, type: stock}
  - nickname: Bob
    holdings:
      - {symbol: "600519", market: cn, type: stock, weight: 0.6}
      - {symbol: AAPL, market: us, type: stock, weight: 0.4}
""", encoding="utf-8")
    invs = load_investors(p)
    assert invs[0].holding_spec == "cn_stock_600519"            # 单标的：裸 key
    bob = invs[1]
    assert bob.holdings is not None and len(bob.holdings) == 2
    assert bob.weights == {"cn_stock_600519": 0.6, "us_stock_AAPL": 0.4}
    assert bob.holding_spec == "cn_stock_600519:0.6|us_stock_AAPL:0.4"
    assert bob.key == "cn_stock_600519"                          # .key = 第一条腿（兼容）


def test_load_investors_equal_weight_when_omitted(tmp_path):
    p = tmp_path / "investors.yaml"
    p.write_text("""
investors:
  - nickname: Bob
    holdings:
      - {symbol: "600519", market: cn, type: stock}
      - {symbol: AAPL, market: us, type: stock}
""", encoding="utf-8")
    invs = load_investors(p)
    assert invs[0].weights == {"cn_stock_600519": 0.5, "us_stock_AAPL": 0.5}


def test_load_switches_with_holdings(tmp_path):
    p = tmp_path / "switches.yaml"
    p.write_text("""
switches:
  - {date: 2026-10-01, nickname: Alice, symbol: AAPL, market: us, type: stock}
  - date: 2026-10-05
    nickname: Bob
    holdings:
      - {symbol: "600519", market: cn, type: stock, weight: 1}
      - {symbol: "511010", market: cn, type: bond, weight: 3}
""", encoding="utf-8")
    sw = load_switches(p)
    assert sw[0].key == "us_stock_AAPL"                          # 旧写法仍可用
    assert sw[1].holding_spec == "cn_bond_511010:0.75|cn_stock_600519:0.25"


def test_investor_weight_segments_path():
    invs = [Investor("Bob", "601318", "cn", "stock", "2026-09-24")]
    sw = [Switch("2026-09-25", "Bob", "AAPL", "us", "stock")]
    segs = investor_weight_segments(invs, sw)["Bob"]
    assert [(d, [(it.key, w) for it, w in legs]) for d, legs in segs] == [
        ("2026-09-24", [("cn_stock_601318", 1.0)]),
        ("2026-09-25", [("us_stock_AAPL", 1.0)]),
    ]


def test_make_investors_passes_through_holdings(tmp_path):
    """private 里的多标的成员应原样匿名化成 investors.yaml 的 holdings。"""
    from tools import make_investors

    members = tmp_path / "members.yaml"
    members.write_text("""
members:
  - {real_name: 张三, symbol: "600519", market: cn, type: stock}
  - real_name: 李四
    holdings:
      - {symbol: "600519", market: cn, type: stock, weight: 0.6}
      - {symbol: AAPL, market: us, type: stock, weight: 0.4}
""", encoding="utf-8")
    roster = tmp_path / "roster.csv"
    roster.write_text("real_name,nickname\n张三,investor01\n李四,investor02\n", encoding="utf-8")
    out = tmp_path / "investors.yaml"
    make_investors.build(members, roster, out)

    text = out.read_text(encoding="utf-8")
    assert "张三" not in text and "李四" not in text          # 真名绝不进公开文件
    invs = load_investors(out)
    assert invs[0].holding_spec == "cn_stock_600519"
    assert invs[1].holding_spec == "cn_stock_600519:0.6|us_stock_AAPL:0.4"


def test_counterfactuals_are_numeric_for_multi_leg():
    """多标的组合的 what-if 应是 NaN（而非 None），保证看板里的除法不会崩。"""
    from src.analysis import switch_counterfactuals

    df = pd.DataFrame({
        "date": pd.to_datetime(["2026-09-24", "2026-09-25", "2026-09-28"]),
        "holding": ["A", "A", "A:0.6|B:0.4"],
        "value": [100.0, 110.0, 120.0],
    })
    cf = switch_counterfactuals(df, {})            # 没有价格 → 无法反事实
    assert len(cf) == 1
    assert cf["whatif_today"].isna().all()
    assert (cf["whatif_today"] / 1e4).isna().all()   # 可做除法（不会 TypeError）
