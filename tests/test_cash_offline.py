"""现金腿（cash）的离线单测（阶段 4）。运行： python -m pytest tests/ -q"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prototype import daily_close  # noqa: E402
from src.aggregate import CASH_KEY, holding_spec, weighted_path_nav  # noqa: E402
from src.analysis import parse_holding, turnover  # noqa: E402
from src.config import CASH_KEY as CONFIG_CASH_KEY  # noqa: E402
from src.config import (  # noqa: E402
    Investor,
    Item,
    investor_weight_segments,
    load_investors,
)
from tools import admin_ops  # noqa: E402


def _s(pairs) -> pd.Series:
    return pd.Series([v for _, v in pairs], index=pd.to_datetime([d for d, _ in pairs]))


def test_cash_key_is_consistent():
    assert CONFIG_CASH_KEY == CASH_KEY == "cash"


def test_cash_item_key_and_spec():
    assert Item("现金", "CASH", "cn", "cash").key == "cash"
    assert holding_spec({"cn_stock_600519": 0.6, "cash": 0.4}) == "cash:0.4|cn_stock_600519:0.6"


def test_cash_leg_needs_no_symbol(tmp_path):
    p = tmp_path / "investors.yaml"
    p.write_text("""
investors:
  - nickname: Alice
    holdings:
      - {symbol: "600519", market: cn, type: stock, weight: 0.6}
      - {type: cash, weight: 0.4}
""", encoding="utf-8")
    inv = load_investors(p)[0]
    assert inv.weights == {"cn_stock_600519": 0.6, "cash": 0.4}
    assert inv.holding_spec == "cash:0.4|cn_stock_600519:0.6"
    assert inv.legs[1].key == "cash" and inv.legs[1].type == "cash"


def test_cash_dampens_return():
    """60% 股票 + 40% 现金：股票 +10% → 组合 +6%（现金不涨不跌）。"""
    series = {"cn_stock_600519": _s([("2026-09-24", 100.0), ("2026-09-25", 110.0)])}
    df = weighted_path_nav(series, [("2026-09-24", {"cn_stock_600519": 0.6, "cash": 0.4})],
                           principal=100.0)
    assert df["value"].tolist() == pytest.approx([100.0, 106.0])
    assert df["holding"].iloc[0] == "cash:0.4|cn_stock_600519:0.6"


def test_switch_to_cash_then_back():
    """调仓到 100% 现金 → 市值冻结；再调回股票继续跟涨。"""
    series = {"A": _s([("2026-09-24", 100.0), ("2026-09-25", 110.0),
                       ("2026-09-28", 121.0), ("2026-09-29", 133.1)])}
    segs = [("2026-09-24", {"A": 1.0}),
            ("2026-09-25", {"cash": 1.0}),      # 09-25 收盘清仓为现金
            ("2026-09-28", {"A": 1.0})]         # 09-28 收盘再买回
    df = weighted_path_nav(series, segs, principal=100.0)
    by = dict(zip(df["date"].dt.strftime("%Y-%m-%d"), df["value"]))
    assert by["2026-09-25"] == pytest.approx(110.0)             # 当日仍算旧组合（A）
    assert by["2026-09-28"] == pytest.approx(110.0)             # 持现金：冻结
    assert by["2026-09-29"] == pytest.approx(110.0 * 133.1 / 121.0)
    assert df["holding"].tolist() == ["A", "A", "cash", "A"]


def test_all_cash_uses_calendar_hint():
    hint = _s([("2026-09-24", 1.0), ("2026-09-25", 2.0), ("2026-09-28", 3.0)])
    df = weighted_path_nav({}, [("2026-09-24", {"cash": 1.0})], principal=100.0,
                           calendar_hint=hint)
    assert df["value"].tolist() == pytest.approx([100.0, 100.0, 100.0])
    assert df["nav"].tolist() == pytest.approx([100.0, 100.0, 100.0])


def test_all_cash_without_hint_raises():
    with pytest.raises(ValueError):
        weighted_path_nav({}, [("2026-09-24", {"cash": 1.0})], principal=100.0)


def test_missing_stock_price_still_raises():
    with pytest.raises(ValueError):
        weighted_path_nav({}, [("2026-09-24", {"A": 0.6, "cash": 0.4})], principal=100.0)


def test_turnover_with_cash():
    assert turnover("cn_stock_600519", "cash:0.4|cn_stock_600519:0.6") == pytest.approx(0.4)
    assert turnover("cash", "cash") == pytest.approx(0.0)
    assert turnover("cash", "cn_stock_600519") == pytest.approx(1.0)


def test_cash_excluded_from_instruments():
    inv = Investor("Alice", "600519", "cn", "stock", "2026-09-24")
    inv.holdings = [Item("Alice", "600519", "cn", "stock", weight=0.6),
                    Item("Alice", "CASH", "cn", "cash", weight=0.4)]
    segs = investor_weight_segments([inv], [])["Alice"]
    items = daily_close._instruments({"Alice": segs}, [])
    assert [it.key for it in items] == ["cn_stock_600519"]      # 现金腿不进抓取清单


def test_legs_from_rows_cash():
    rows = [{"代码": "600519", "市场": "cn", "类型": "stock", "权重": 0.6},
            {"代码": "", "市场": None, "类型": "cash", "权重": 0.4}]
    assert admin_ops.legs_from_rows(rows) == [
        ("600519", "cn", "stock", 0.6), ("CASH", "cn", "cash", 0.4)]


def test_parse_holding_with_cash():
    assert parse_holding("cash:0.4|cn_stock_600519:0.6") == {"cash": 0.4, "cn_stock_600519": 0.6}
