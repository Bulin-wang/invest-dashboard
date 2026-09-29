"""投资组合配置层 / 匿名化工具的离线单测（不联网）。运行： python -m pytest tests/ -q"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import (  # noqa: E402
    Investor,
    load_investor_benchmarks,
    load_investor_config,
    load_investors,
)
from src.returns import cumulative_return  # noqa: E402
from tools import make_investors  # noqa: E402


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "investors.yaml"
    p.write_text(text, encoding="utf-8")
    return p


# --------------------------------------------------------------------- 配置层
def test_investor_key():
    assert Investor("Alice", "600519", "cn", "stock").key == "cn_stock_600519"
    assert Investor("Bob", "^GSPC", "us", "index").key == "us_index_GSPC"
    # 同 market、同代码、不同品种 → key 不同（type 消歧）
    assert (Investor("平安银行", "000001", "cn", "stock").key
            != Investor("上证指数", "000001", "cn", "index").key)
    assert Investor("上证指数", "000001", "cn", "index").key == "cn_index_000001"


def test_load_investors_inherits_defaults(tmp_path):
    p = _write(tmp_path, """
base_currency: CNY
start_date: 2026-09-24
principal: 1000000
investors:
  - {nickname: Alice, symbol: "600519", market: cn, type: stock}
  - {nickname: Bob, symbol: AAPL, market: us, type: stock, principal: 500000}
""")
    invs = load_investors(p)
    assert [i.nickname for i in invs] == ["Alice", "Bob"]
    assert all(i.start_date == "2026-09-24" for i in invs)   # 继承顶层 start_date
    assert invs[0].principal == 1_000_000.0
    assert invs[1].principal == 500_000.0                    # 逐条覆盖
    assert invs[1].key == "us_stock_AAPL"


def test_load_investor_config_defaults(tmp_path):
    p = _write(tmp_path,
               "start_date: 2026-09-24\nprincipal: 1000000\n"
               "investors:\n  - {nickname: A, symbol: X, market: us, type: stock}\n")
    cfg = load_investor_config(p)
    assert cfg["start_date"] == "2026-09-24"
    assert cfg["principal"] == 1_000_000.0
    assert cfg["base_currency"] == "CNY"


def test_load_investors_requires_entries(tmp_path):
    p = _write(tmp_path, "investors: []\n")
    with pytest.raises(ValueError):
        load_investors(p)


def test_load_investors_accepts_hk(tmp_path):
    p = _write(tmp_path,
               "investors:\n  - {nickname: A, symbol: \"00700\", market: hk, type: stock}\n")
    invs = load_investors(p)
    assert invs[0].key == "hk_stock_00700"     # hk 现为合法 market


def test_load_investors_rejects_bad_market(tmp_path):
    p = _write(tmp_path,
               "investors:\n  - {nickname: A, symbol: X, market: jp, type: stock}\n")
    with pytest.raises(ValueError):
        load_investors(p)


def test_load_investor_benchmarks(tmp_path):
    p = _write(tmp_path,
               "investors:\n  - {nickname: A, symbol: X, market: us, type: stock}\n"
               "benchmarks:\n  - {name: 沪深300, symbol: \"000300\", market: cn, type: index}\n")
    bs = load_investor_benchmarks(p)
    assert len(bs) == 1 and bs[0].key == "cn_index_000300" and bs[0].is_benchmark


# --------------------------------------------------------------------- 100 万口径
def test_market_value_scales_with_principal():
    """市值 = 本金 × close(t)/close(base)，与看板口径一致。"""
    prices = pd.DataFrame({"date": pd.to_datetime(["2026-09-24", "2026-09-25"]),
                           "close": [100.0, 110.0]})
    ret = cumulative_return(prices, "2026-09-24")
    principal = 1_000_000
    value = principal * ret["close"].iloc[-1] / ret["close"].iloc[0]
    assert value == pytest.approx(1_100_000)
    # 等价：nav（基准=100）× principal/1e6 = 万元
    assert ret["nav"].iloc[-1] * principal / 1e6 == pytest.approx(110.0)


# --------------------------------------------------------------------- 匿名化工具
def test_make_investors_hides_real_names(tmp_path):
    src = tmp_path / "priv.yaml"
    src.write_text("""
start_date: 2026-09-24
principal: 1000000
members:
  - {real_name: 张三, symbol: "600519", market: cn, type: stock}
  - {real_name: 李四, symbol: AAPL, market: us, type: stock}
benchmarks:
  - {name: 沪深300, symbol: "000300", market: cn, type: index}
""", encoding="utf-8")
    out = tmp_path / "investors.yaml"
    mp = tmp_path / "nickname_map.csv"
    make_investors.build(src, out, mp)

    text = out.read_text(encoding="utf-8")
    assert "张三" not in text and "李四" not in text   # 真实姓名绝不进公开文件
    assert "Alice" in text and "Bob" in text
    assert "000300" in text                            # 基准保留

    mapping = mp.read_text(encoding="utf-8")
    assert "张三" in mapping and "Alice" in mapping     # 反向映射（私密）里才有真名

    # 生成物能被配置层正常读取
    invs = load_investors(out)
    assert [i.nickname for i in invs] == ["Alice", "Bob"]


def test_assign_nicknames_overflows_pool():
    names = make_investors._assign_nicknames(len(make_investors.NICKNAMES) + 2)
    assert len(names) == len(make_investors.NICKNAMES) + 2
    assert names[-1].endswith("2")                     # 超出池后追加轮次序号
