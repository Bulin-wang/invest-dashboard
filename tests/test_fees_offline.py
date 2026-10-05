"""手续费（换手率口径）与多标的调仓写入的离线单测（阶段 2）。运行： python -m pytest tests/ -q"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.analysis import fee_events, parse_holding, switch_days, turnover  # noqa: E402
from src.config import load_switches  # noqa: E402
from tools import admin_ops  # noqa: E402


# --------------------------------------------------------------- holding 解析
def test_parse_holding_single_and_multi():
    assert parse_holding("cn_stock_600519") == {"cn_stock_600519": 1.0}
    assert parse_holding("a:0.75|b:0.25") == {"a": 0.75, "b": 0.25}
    assert parse_holding("a:3|b:1") == {"a": 0.75, "b": 0.25}      # 自动归一化
    assert parse_holding("") == {} and parse_holding(None) == {}


# --------------------------------------------------------------- 换手率
def test_turnover_full_switch_is_one():
    assert turnover("A", "B") == pytest.approx(1.0)


def test_turnover_partial_rebalance():
    # 50/50 -> 100% A：只需卖出 B 的 50% → 单边换手 0.5
    assert turnover("A:0.5|B:0.5", "A") == pytest.approx(0.5)


def test_turnover_same_basket_is_zero():
    assert turnover("A:0.6|B:0.4", "A:0.6|B:0.4") == pytest.approx(0.0)   # 顺序不同也不算换手
    assert turnover("A:0.6|B:0.4", "B:0.4|A:0.6") == pytest.approx(0.0)


def test_turnover_unparseable_falls_back_to_full():
    assert turnover(None, "B") == pytest.approx(1.0)                       # 缺 holding → 按整仓计


def test_turnover_from_all_a_to_6040():
    # 100% A -> 60/40：卖掉 40% 的 A、买入 40% 的 B → 单边换手 0.4
    assert turnover("A", "A:0.6|B:0.4") == pytest.approx(0.4)


# --------------------------------------------------------------- 手续费
def test_fee_events_full_switch_matches_old_caliber():
    """回归：单标的全仓切换的手续费仍 = 当日市值 × 费率。"""
    df = pd.DataFrame({
        "date": pd.to_datetime(["2026-09-24", "2026-09-25", "2026-09-28"]),
        "holding": ["A", "A", "B"],
        "value": [1_000_000.0, 1_100_000.0, 1_200_000.0],
    })
    ev = fee_events({"inv": df}, fee_rate=0.0001)
    assert len(ev) == 1
    assert ev["turnover"].iloc[0] == pytest.approx(1.0)
    assert ev["fee"].iloc[0] == pytest.approx(1_100_000.0 * 0.0001)


def test_fee_events_partial_rebalance_costs_less():
    """多标的只调一半 → 手续费按换手率打折（0.5 倍）。"""
    df = pd.DataFrame({
        "date": pd.to_datetime(["2026-09-24", "2026-09-25", "2026-09-28"]),
        "holding": ["A:0.5|B:0.5", "A:0.5|B:0.5", "A"],
        "value": [1_000_000.0, 1_000_000.0, 1_100_000.0],
    })
    ev = fee_events({"inv": df}, fee_rate=0.0001)
    assert ev["turnover"].iloc[0] == pytest.approx(0.5)
    assert ev["fee"].iloc[0] == pytest.approx(1_000_000.0 * 0.0001 * 0.5)


def test_switch_days_carries_turnover():
    df = pd.DataFrame({
        "date": pd.to_datetime(["2026-09-24", "2026-09-25", "2026-09-28", "2026-09-29"]),
        "holding": ["A", "A", "A:0.6|B:0.4", "A:0.6|B:0.4"],
        "value": [100.0, 110.0, 120.0, 130.0],
    })
    sw = switch_days(df)
    assert len(sw) == 1
    assert str(sw["date"].iloc[0].date()) == "2026-09-25"      # 调仓日（当日算旧组合）
    assert sw["turnover"].iloc[0] == pytest.approx(0.4)


# --------------------------------------------------------------- 多标的写入
def test_admin_ops_switch_multi_roundtrip(tmp_path):
    p = tmp_path / "switches.yaml"
    admin_ops.append_switch_multi(
        p, "2026-10-15", "investor01",
        [("600519", "cn", "stock", 0.6), ("AAPL", "us", "stock", 0.4)])
    sw = load_switches(p)
    assert len(sw) == 1
    assert sw[0].holding_spec == "cn_stock_600519:0.6|us_stock_AAPL:0.4"
    assert sw[0].weights == {"cn_stock_600519": 0.6, "us_stock_AAPL": 0.4}


def test_admin_ops_switch_multi_appends_after_existing(tmp_path):
    p = tmp_path / "switches.yaml"
    admin_ops.append_switch(p, "2026-10-01", "investor01", "AAPL", "us", "stock")
    admin_ops.append_switch_multi(
        p, "2026-10-15", "investor02",
        [("600519", "cn", "stock", 1), ("511010", "cn", "bond", 1)])
    sw = load_switches(p)
    assert [s.date for s in sw] == ["2026-10-01", "2026-10-15"]
    assert sw[1].holding_spec == "cn_bond_511010:0.5|cn_stock_600519:0.5"


def test_admin_ops_member_multi_inserts_inside_members(tmp_path):
    p = tmp_path / "members.yaml"
    p.write_text("""\
base_currency: CNY
start_date: 2026-09-24

members:
  - {real_name: 张三, symbol: "600519", market: cn, type: stock}

benchmarks:
  - {name: 沪深300, symbol: "000300", market: cn, type: index}
""", encoding="utf-8")
    admin_ops.append_member_multi(
        p, "李四", [("600519", "cn", "stock", 0.5), ("AAPL", "us", "stock", 0.5)])

    text = p.read_text(encoding="utf-8")
    assert text.index("张三") < text.index("李四") < text.index("benchmarks:")
    cfg = yaml.safe_load(text)
    assert [m["real_name"] for m in cfg["members"]] == ["张三", "李四"]
    assert cfg["members"][1]["holdings"][1]["symbol"] == "AAPL"
    assert len(cfg["benchmarks"]) == 1                     # 基准段未被破坏
