"""本地管理台纯逻辑（tools/admin_ops）离线单测（不联网）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_investors, load_switches  # noqa: E402
from tools import admin_ops, make_investors  # noqa: E402


def test_next_nickname():
    assert admin_ops.next_nickname([]) == "investor01"
    assert admin_ops.next_nickname(["investor01", "investor07", "Alice"]) == "investor08"
    assert admin_ops.next_nickname(["investor99"]) == "investor100"


def test_read_roster_missing_returns_empty(tmp_path):
    assert admin_ops.read_roster(tmp_path / "nope.csv") == []


def test_append_roster_roundtrip(tmp_path):
    p = tmp_path / "roster.csv"
    admin_ops.append_roster(p, "张三", "investor01")
    admin_ops.append_roster(p, "李四", "investor02")
    assert admin_ops.read_roster(p) == [("张三", "investor01"), ("李四", "investor02")]


def test_append_member_inserts_before_benchmarks(tmp_path):
    p = tmp_path / "members.yaml"
    p.write_text("""\
base_currency: CNY
start_date: 2026-09-24
principal: 1000000

members:
  - {real_name: 张三, symbol: "600519", market: cn, type: stock}

benchmarks:
  - {name: 沪深300, symbol: "000300", market: cn, type: index}
""", encoding="utf-8")
    admin_ops.append_member(p, "李四", "AAPL", "us", "stock")
    text = p.read_text(encoding="utf-8")
    assert text.index("张三") < text.index("李四") < text.index("benchmarks:")   # 插到 members 块内


def test_append_switch_from_scratch_and_append(tmp_path):
    p = tmp_path / "switches.yaml"
    admin_ops.append_switch(p, "2026-10-15", "investor01", "AAPL", "us", "stock")
    admin_ops.append_switch(p, "2026-10-20", "investor02", "00700", "hk", "stock")
    sw = load_switches(p)
    assert [(s.date, s.nickname, s.key) for s in sw] == [
        ("2026-10-15", "investor01", "us_stock_AAPL"),
        ("2026-10-20", "investor02", "hk_stock_00700"),
    ]


def test_add_investor_end_to_end(tmp_path):
    """模拟管理台「新增投资者」：写 roster + members → make_investors → 出现在 investors.yaml。"""
    roster = tmp_path / "roster.csv"
    members = tmp_path / "members.yaml"
    out = tmp_path / "investors.yaml"
    admin_ops.append_roster(roster, "张三", "investor01")
    members.write_text("""\
base_currency: CNY
start_date: 2026-09-24
principal: 1000000
members:
  - {real_name: 张三, symbol: "600519", market: cn, type: stock}
""", encoding="utf-8")
    make_investors.build(members, roster, out)
    invs = load_investors(out)
    assert [i.nickname for i in invs] == ["investor01"]
    assert invs[0].key == "cn_stock_600519"

    # 再追加一个人
    admin_ops.append_roster(roster, "李四", "investor02")
    admin_ops.append_member(members, "李四", "AAPL", "us", "stock")
    make_investors.build(members, roster, out)
    invs = load_investors(out)
    assert [i.nickname for i in invs] == ["investor01", "investor02"]
    assert invs[1].key == "us_stock_AAPL"
