"""管理台录入的离线回归测试（不联网）。

★ 背景：管理台崩过一次
    `legs_from_rows()` 的返回值从 4 元组改成 5 元组（加了「到期日」）后，
    `admin_app.py` 里 4 处 `s, m, t, w = legs[0]` 仍在按 4 元组解包，
    于是提交表单时直接 `ValueError: too many values to unpack (expected 4)`。

本文件锁死两件事：
  1. 所有录入入口都**兼容 4/5 元组**，不会再崩
  2. `expires` 会一路透传：录入 → YAML → make_investors → investors.yaml → load_investors

不使用 pytest 的 `tmp_path`（沙箱里跑不起来），改用工作区临时目录。
"""
from __future__ import annotations

import shutil
import sys
import uuid
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.config import load_investors  # noqa: E402
from tools import admin_ops, make_investors  # noqa: E402

MEMBERS_TMPL = """\
base_currency: CNY
start_date: 2026-09-24
principal: 1000000

members:
  - {real_name: "S001", symbol: "600519", market: cn, type: stock}

# 基准指数
benchmarks:
  - {name: 沪深300, symbol: "000300", market: cn, type: index}
"""

ROSTER_TMPL = "real_name,nickname\nS001,investor01\n"
SWITCHES_TMPL = """\
# 调仓流水（append-only）
switches:
"""


@pytest.fixture()
def workdir():
    d = _ROOT / f".tmp_test_admin_{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "members.yaml").write_text(MEMBERS_TMPL, encoding="utf-8")
    (d / "roster.csv").write_text(ROSTER_TMPL, encoding="utf-8")
    (d / "switches.yaml").write_text(SWITCHES_TMPL, encoding="utf-8")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _read_members(p: Path):
    body = p.read_text(encoding="utf-8").split("\nmembers:", 1)[1].split("\nbenchmarks:", 1)[0]
    return (yaml.safe_load("members:" + body) or {}).get("members") or []


def _read_switches(p: Path):
    body = p.read_text(encoding="utf-8").split("\nswitches:", 1)[1]
    return (yaml.safe_load("switches:" + body) or {}).get("switches") or []


# --------------------------------------------------------------------- ★ 元组兼容
def test_leg_helpers_accept_both_4_and_5_tuples(workdir):
    """★ 4 元组（旧调用方）与 5 元组都能正常工作，不再崩。"""
    m = workdir / "members.yaml"
    # 4 元组
    d1 = admin_ops.append_member_from_legs(m, "S010", [("600519", "cn", "stock", 1.0)])
    assert "600519" in d1
    # 5 元组（带 expires）
    d2 = admin_ops.append_member_from_legs(
        m, "S011", [("SC2611", "cn", "futures", 0.3, "2026-10-30")])
    assert "SC2611" in d2

    sw = workdir / "switches.yaml"
    t1 = admin_ops.append_switch_from_legs(sw, "2026-10-08", "investor01",
                                           [("600519", "cn", "stock", 1.0)])
    t2 = admin_ops.append_switch_from_legs(sw, "2026-10-09", "investor01",
                                           [("SC2611", "cn", "futures", 1.0, "2026-10-30")])
    assert "600519" in t1 and "SC2611" in t2


def test_single_leg_switch_writes_expires(workdir):
    """★ 单标的写法也要写出 expires（之前只有多标的写法支持）。"""
    sw = workdir / "switches.yaml"
    admin_ops.append_switch_from_legs(sw, "2026-10-09", "investor01",
                                      [("SC2611", "cn", "futures", 1.0, "2026-10-30")])
    rows = _read_switches(sw)
    assert rows[0]["symbol"] == "SC2611"
    assert rows[0]["expires"] == "2026-10-30"


def test_single_leg_member_writes_expires(workdir):
    m = workdir / "members.yaml"
    admin_ops.append_member_from_legs(m, "S012",
                                      [("SC2611", "cn", "futures", 1.0, "2026-10-30")])
    rec = [x for x in _read_members(m) if x["real_name"] == "S012"][0]
    assert rec["expires"] == "2026-10-30"


def test_expires_absent_is_not_written(workdir):
    """没填到期日 → 不写 expires 字段（保持旧文件格式干净）。"""
    m = workdir / "members.yaml"
    admin_ops.append_member_from_legs(m, "S013", [("600519", "cn", "stock", 1.0)])
    rec = [x for x in _read_members(m) if x["real_name"] == "S013"][0]
    assert "expires" not in rec


# --------------------------------------------------------------------- 多标的
def test_multi_leg_switch_writes_holdings(workdir):
    sw = workdir / "switches.yaml"
    desc = admin_ops.append_switch_from_legs(sw, "2026-10-09", "investor01", [
        ("01810", "hk", "stock", 0.7, ""),
        ("ONDO", "us", "crypto", 0.3, ""),
    ])
    assert "70%" in desc and "30%" in desc
    rows = _read_switches(sw)
    assert rows[0]["nickname"] == "investor01"
    assert [l["symbol"] for l in rows[0]["holdings"]] == ["01810", "ONDO"]


def test_multi_leg_member_with_expires(workdir):
    m = workdir / "members.yaml"
    admin_ops.append_member_from_legs(m, "S014", [
        ("SC2611", "cn", "futures", 0.3, "2026-10-30"),
        ("600519", "cn", "stock", 0.7, ""),
    ])
    rec = [x for x in _read_members(m) if x["real_name"] == "S014"][0]
    legs = rec["holdings"]
    assert legs[0]["expires"] == "2026-10-30"
    assert "expires" not in legs[1]


# --------------------------------------------------------------------- ★ 端到端
def test_admin_to_dashboard_end_to_end(workdir):
    """★ 完整链路：录入（含 expires 与多标的）→ YAML → make_investors → load_investors。

    这是对"管理台崩了"那次事故的端到端复现：
    录入后能正确解析出 futures 的 expires 与多标的权重。
    """
    m = workdir / "members.yaml"
    r = workdir / "roster.csv"
    sw = workdir / "switches.yaml"

    # ① 新增一位投资者：单标的 + 到期日
    with open(r, "a", encoding="utf-8") as f:
        f.write("S002,investor02\n")
    admin_ops.append_member_from_legs(m, "S002",
                                      [("SC2611", "cn", "futures", 1.0, "2026-10-30")])
    # ② 再追加一次调仓：多标的组合
    admin_ops.append_switch_from_legs(sw, "2026-10-09", "investor02", [
        ("01810", "hk", "stock", 0.7, ""),
        ("ONDO", "us", "crypto", 0.3, ""),
    ])

    # ③ 生成公开清单
    pub = workdir / "investors.yaml"
    assert make_investors.build(m, r, pub) == 0

    # ④ 能被 config 正确加载
    inv = {i.nickname: i for i in load_investors(pub)}
    assert inv["investor02"].legs[0].expires == "2026-10-30"      # expires 透传
    assert inv["investor02"].legs[0].type == "futures"

    # ⑤ switches 也能被加载，且多标的权重正确
    from src.config import load_switches
    switches = load_switches(sw)
    assert len(switches) == 1
    assert switches[0].nickname == "investor02"
    assert {it.key for it in switches[0].legs} == {"hk_stock_01810", "us_crypto_ONDO"}
    assert switches[0].weights["us_crypto_ONDO"] == pytest.approx(0.3)
