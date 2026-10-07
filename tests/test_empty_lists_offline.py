"""空/半空清单文件的回归测试 —— 离线，不联网。

★ 为什么单独一个文件：`tests/test_switches_offline.py` 等用的是 pytest 的 `tmp_path`，
   而 `tmp_path` 在本机沙箱里需要目录符号链接、会直接 ERROR 跑不起来 ——
   所以那里即使写了"空文件"用例也**从来没真正执行过**。
   这里改用工作区内的临时目录（见 `workdir`），保证这些边界情况真的被跑到。

覆盖的坑：YAML 里"**键存在但值为 null**"与"键缺失"是两回事：
    `cfg.get("switches", [])` 在 `switches:`（值为 None）时返回 **None 而不是 []**，
    直接迭代会 `TypeError: 'NoneType' object is not iterable`。
    —— 用户"清空调仓记录"时最自然的写法就是留一个空的 `switches:`，正好踩中。
"""
from __future__ import annotations

import shutil
import sys
import uuid
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.config import (load_benchmarks, load_holdings, load_investor_benchmarks,  # noqa: E402
                        load_investors, load_switches)


@pytest.fixture()
def workdir():
    d = _ROOT / f".tmp_test_empty_{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True, exist_ok=True)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------- switches.yaml
@pytest.mark.parametrize("content,label", [
    ("switches:\n", "键存在、值为 null（清空记录最常见的写法）"),
    ("switches: []\n", "显式空列表"),
    ("switches: null\n", "显式 null"),
    ("# 只剩注释\n", "文件里只有注释"),
    ("", "完全空文件"),
    ("\n\n", "只有空行"),
])
def test_load_switches_accepts_all_empty_forms(workdir, content, label):
    """★ 所有"空"写法都必须返回 []，且不抛异常。"""
    p = workdir / "switches.yaml"
    p.write_text(content, encoding="utf-8")
    assert load_switches(p) == [], f"失败于：{label}"


def test_load_switches_missing_file(workdir):
    assert load_switches(workdir / "nope.yaml") == []


def test_empty_switches_collapses_everyone_to_one_segment(workdir):
    """清空调仓后：每位投资者只剩「初始持仓」这一段，多标的初始持仓不受影响。"""
    from src.config import investor_weight_segments
    inv = workdir / "investors.yaml"
    inv.write_text("""
start_date: 2026-09-24
investors:
  - {nickname: A, symbol: "600519", market: cn, type: stock}
  - nickname: B
    holdings:
      - {symbol: "01810", market: hk, type: stock, weight: 0.7}
      - {symbol: ONDO, market: us, type: crypto, weight: 0.3}
""", encoding="utf-8")
    sw = workdir / "switches.yaml"
    sw.write_text("switches:\n", encoding="utf-8")

    segs = investor_weight_segments(load_investors(inv), load_switches(sw))
    assert set(segs) == {"A", "B"}
    assert all(len(v) == 1 for v in segs.values())            # 全部单段
    assert len(segs["B"][0][1]) == 2                          # 初始多标的仍在


# --------------------------------------------------------------------- 同类空值
def test_load_investor_benchmarks_accepts_null(workdir):
    """`benchmarks:` 留空不该崩（同类坑）。"""
    p = workdir / "investors.yaml"
    p.write_text("""
start_date: 2026-09-24
investors:
  - {nickname: A, symbol: "600519", market: cn, type: stock}
benchmarks:
""", encoding="utf-8")
    assert load_investor_benchmarks(p) == []
    assert len(load_investors(p)) == 1


def test_load_holdings_and_benchmarks_accept_null(workdir):
    """备用后端的 holdings.yaml 同样要能接受空值。"""
    p = workdir / "holdings.yaml"
    p.write_text("base_currency: CNY\nholdings:\nbenchmarks:\n", encoding="utf-8")
    assert load_holdings(p) == []
    assert load_benchmarks(p) == []


def test_load_investors_empty_still_errors_loudly(workdir):
    """但「一个投资者都没有」必须明确报错，不能静默返回空表。"""
    p = workdir / "investors.yaml"
    p.write_text("start_date: 2026-09-24\ninvestors:\n", encoding="utf-8")
    with pytest.raises(ValueError, match="没有 investors"):
        load_investors(p)
