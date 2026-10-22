"""★ 回归：`stable_key` 必须是「标的身份 → 稳定键」的唯一实现。

真实事故（2026-10-07 ~ 10-21，静默 10 天）：
    引入 `quote_fetch.snapshot()` 的 `key` 列时，直接拼了
        f"{it.market}_{it.type}_{it.symbol}"        → us_index_^GSPC
    而 `src.config.Item.key` 会**清洗 symbol**：
        symbol.replace("^", "").replace("=", "_")   → us_index_GSPC

    于是 `daily_close` 按 `it.key = us_index_GSPC` 去查 quotes，
    而 snapshot 里只有 `us_index_^GSPC` → **永远 miss**。
    表现：标普500 每天打印 `[ERR] us_index_GSPC ... 本次未取到报价`，
    报价其实抓到了、但查不到，价格文件停在 10-07 不动。

教训：key 的拼法只能有一处实现。任何 `f"{market}_{type}_{symbol}"` 都是隐患。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from prototype import crypto_fetch, fund_fetch, futures_fetch, quote_fetch  # noqa: E402
from src.config import Item as CfgItem  # noqa: E402
from src.config import (CASH_KEY, Investor, Switch, Item,  # noqa: E402
                        stable_key)

# 覆盖所有需要清洗/特殊处理的情形
CASES = [
    ("us", "index", "^GSPC"),      # ★ 事故代码：^ 必须去掉
    ("us", "index", "^IXIC"),
    ("us", "index", "^DJI"),
    ("us", "index", "^NDX"),
    ("us", "stock", "AAPL"),
    ("us", "stock", "BRK.B"),      # 带点号
    ("cn", "index", "000300"),
    ("cn", "index", "000001"),     # 与 cn_stock_000001 同号
    ("cn", "stock", "000001"),
    ("cn", "etf", "510300"),
    ("cn", "fund", "002910"),
    ("us", "crypto", "ONDO"),
    ("cn", "futures", "SC2611"),
    ("hk", "stock", "01810"),
    ("cn", "cash", "999999"),      # 现金腿 → 固定 'cash'
]

FETCH_MODULES = [quote_fetch, fund_fetch, crypto_fetch, futures_fetch]


@pytest.mark.parametrize("market,type_,symbol", CASES)
def test_stable_key_is_single_source_of_truth(market, type_, symbol):
    """★ Item / Investor / Switch 三处 key 必须与 stable_key 完全一致。"""
    want = stable_key(market, type_, symbol)
    it = Item(name="x", symbol=symbol, market=market, type=type_)
    assert it.key == want
    inv = Investor(nickname="n", symbol=symbol, market=market, type=type_)
    assert inv.key == want
    sw = Switch(date="2026-10-08", nickname="n", symbol=symbol,
                market=market, type=type_)
    assert sw.key == want


def test_cash_key_is_constant():
    for sym in ("999999", "", "CASH", "510300"):
        assert stable_key("cn", "cash", sym) == CASH_KEY == "cash"


def test_symptom_gspc_key_no_longer_contains_caret():
    """★ 事故的直接症状：key 里不能残留 `^`。"""
    k = stable_key("us", "index", "^GSPC")
    assert k == "us_index_GSPC"
    assert "^" not in k


@pytest.mark.parametrize("mod", FETCH_MODULES, ids=lambda m: m.__name__.split(".")[-1])
def test_fetch_modules_do_not_build_key_by_hand(mod):
    """★ 四个抓取模块必须都调用 stable_key，不许自己拼 key。"""
    src = Path(mod.__file__).read_text(encoding="utf-8")
    assert "stable_key" in src, f"{mod.__name__} 没有引用 stable_key"
    # 这些手拼写法都是隐患（允许出现在注释里，所以只查代码形态）
    for bad in ('f"{it.market}_{it.type}_{it.symbol}"',
                'f"{item.market}_{item.type}_{item.symbol}"'):
        assert bad not in src, f"{mod.__name__} 仍在手拼 key：{bad}"


def test_snapshot_keys_match_config_keys(monkeypatch):
    """★ 端到端：snapshot() 产出的 key 必须能被 daily_close 按 Item.key 查到。"""
    items = [quote_fetch.Item("标普500", "^GSPC", "us", "index"),
             quote_fetch.Item("苹果", "AAPL", "us", "stock")]

    def fake_sina(items_):
        return {
            "gb_$inx": {"name": "标普500指数", "close": 7757.1699,
                        "prev_close": 7712.5699, "date": "2026-10-22",
                        "source": "sina"},
            "gb_aapl": {"name": "苹果", "close": 343.99,
                        "prev_close": 339.5, "date": "2026-10-22",
                        "source": "sina"},
        }

    monkeypatch.setattr(quote_fetch, "sina_quotes", fake_sina)
    snap = quote_fetch.snapshot(items)

    quotes = {r["key"]: r for _, r in snap.iterrows()}
    for it in items:
        cfg_item = CfgItem(name=it.name, symbol=it.symbol,
                           market=it.market, type=it.type)
        hit = quotes.get(cfg_item.key)
        assert hit is not None, f"按 {cfg_item.key} 查不到 —— 就是事故的重演"
        assert float(hit["close"]) > 0


# --------------------------------------------------------------------- 腾讯指数代码
@pytest.mark.parametrize("symbol,expect", [
    ("^GSPC", "us.INX"),      # 实测：us^GSPC 返回空，us.INX 才有数据
    ("^IXIC", "us.IXIC"),
    ("^DJI", "us.DJI"),
    ("^NDX", "us.NDX"),
])
def test_tencent_code_for_us_index(symbol, expect):
    """★ 腾讯的美股指数代码与新浪不同：`us^GSPC` 取不到，`us.INX` 才行。"""
    it = quote_fetch.Item("x", symbol, "us", "index")
    assert quote_fetch.tencent_code(it) == expect


def test_tencent_code_for_us_stock_unchanged():
    it = quote_fetch.Item("x", "AAPL", "us", "stock")
    assert quote_fetch.tencent_code(it) == "usAAPL"


def test_sina_code_for_us_index_unchanged():
    """新浪侧仍然用 $inx 系列（不要被腾讯的改法带偏）。"""
    assert quote_fetch.sina_code(quote_fetch.Item("x", "^GSPC", "us", "index")) == "gb_$inx"
