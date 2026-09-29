"""调仓流水 / 持仓路径 的离线单测（不联网）。运行： python -m pytest tests/ -q"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import (  # noqa: E402
    Investor,
    Switch,
    investor_segments,
    load_switches,
    portfolio_id,
)


def test_switch_key_and_item():
    s = Switch("2026-09-25", "Alice", "00700", "hk", "stock")
    assert s.key == "hk_stock_00700"
    assert s.item().key == "hk_stock_00700"


def test_portfolio_id_sanitizes():
    assert portfolio_id("Alice") == "Alice"
    assert portfolio_id("a/b c") == "a_b_c"


def test_load_switches_missing_file_returns_empty(tmp_path):
    assert load_switches(tmp_path / "nope.yaml") == []


def test_load_switches_parses(tmp_path):
    p = tmp_path / "switches.yaml"
    p.write_text("""
switches:
  - {date: 2026-09-25, nickname: Alice, symbol: AAPL, market: us, type: stock}
  - {date: 2026-09-28, nickname: Bob, symbol: "00700", market: hk, type: stock}
""", encoding="utf-8")
    sw = load_switches(p)
    assert [s.nickname for s in sw] == ["Alice", "Bob"]
    assert sw[0].key == "us_stock_AAPL"
    assert sw[1].key == "hk_stock_00700"


def test_load_switches_rejects_bad_market(tmp_path):
    p = tmp_path / "switches.yaml"
    p.write_text("switches:\n  - {date: 2026-09-25, nickname: A, symbol: X, market: jp, type: stock}\n",
                 encoding="utf-8")
    with pytest.raises(ValueError):
        load_switches(p)


def test_investor_segments_orders_and_prepends_initial():
    invs = [Investor("Alice", "600519", "cn", "stock", "2026-09-24")]
    sw = [
        Switch("2026-09-26", "Alice", "AAPL", "us", "stock"),   # 乱序，应被排序
        Switch("2026-09-25", "Alice", "00700", "hk", "stock"),
    ]
    segs = investor_segments(invs, sw)["Alice"]
    assert [(d, it.key) for d, it in segs] == [
        ("2026-09-24", "cn_stock_600519"),   # 首段 = 初始持仓
        ("2026-09-25", "hk_stock_00700"),
        ("2026-09-26", "us_stock_AAPL"),
    ]


def test_investor_segments_no_switches_single_segment():
    invs = [Investor("Bob", "601318", "cn", "stock", "2026-09-24")]
    segs = investor_segments(invs, [])["Bob"]
    assert len(segs) == 1 and segs[0][1].key == "cn_stock_601318"


def test_investor_segments_ignores_other_nicknames():
    invs = [Investor("Bob", "601318", "cn", "stock", "2026-09-24")]
    sw = [Switch("2026-09-25", "Carol", "AAPL", "us", "stock")]   # 不属于 Bob
    segs = investor_segments(invs, sw)["Bob"]
    assert len(segs) == 1
