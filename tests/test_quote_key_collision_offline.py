"""★ 回归：同号不同品种（股票 vs 指数）不能串价。

真实事故（2026-09-30，commit c7cfed5）：
    investor18 持有 cn_stock_000001（平安银行，约 11 元）
    investor61 持有 cn_index_000001（上证指数，约 3800 点）
    `daily_close` 用 (market, symbol) 当 quotes 字典的键 → 两行撞成一格，
    后写的指数把股票挤掉 → 平安银行被写成 3848.9273（= 上证指数收盘点位），
    该投资者单日"暴涨" 33,811%。

本文件用**真实数据流**（假行情 → 写 CSV → 算组合）锁死这个行为。
不使用 pytest 的 `tmp_path`（沙箱里跑不起来），改用工作区临时目录。
"""
from __future__ import annotations

import shutil
import sys
import uuid
from pathlib import Path

import pandas as pd
import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from prototype import quote_fetch  # noqa: E402
from prototype.quote_fetch import Item as QuoteItem, snapshot  # noqa: E402
from src.config import Item as ConfigItem  # noqa: E402

STOCK_CLOSE = 11.57          # 平安银行真实收盘
INDEX_CLOSE = 3848.9273      # 上证指数真实收盘（就是那个被写错的值）


@pytest.fixture()
def workdir():
    d = _ROOT / f".tmp_test_clash_{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True, exist_ok=True)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------- 1) 快照层
def test_snapshot_key_distinguishes_type(monkeypatch):
    """snapshot() 返回的 key 必须带 type，让同号两行区分得开。"""
    monkeypatch.setattr(quote_fetch, "sina_quotes", lambda items: {
        "sz000001": {"name": "平安银行", "close": STOCK_CLOSE, "prev_close": 11.35,
                     "date": "2026-09-30", "source": "sina"},
        "sh000001": {"name": "上证指数", "close": INDEX_CLOSE, "prev_close": 3830.45,
                     "date": "2026-09-30", "source": "sina"},
    })
    snap = snapshot([QuoteItem("平安银行", "000001", "cn", "stock"),
                     QuoteItem("上证指数", "000001", "cn", "index")])
    assert "key" in snap.columns
    got = dict(zip(snap["key"], snap["close"]))
    assert got == {"cn_stock_000001": STOCK_CLOSE, "cn_index_000001": INDEX_CLOSE}


# --------------------------------------------------------------------- 2) 字典层
def test_quotes_dict_keyed_by_item_key_does_not_collide():
    """★ 复现事故机制：键含 type 时两行各归各位；键只有 (market,symbol) 时撞成一格。"""
    snap = pd.DataFrame([
        {"key": "cn_stock_000001", "market": "cn", "type": "stock",
         "symbol": "000001", "close": STOCK_CLOSE},
        {"key": "cn_index_000001", "market": "cn", "type": "index",
         "symbol": "000001", "close": INDEX_CLOSE},
    ])

    # 修复后的写法：按 key 建索引
    fixed = {r["key"]: r for _, r in snap.iterrows()}
    assert len(fixed) == 2
    assert fixed["cn_stock_000001"]["close"] == STOCK_CLOSE
    assert fixed["cn_index_000001"]["close"] == INDEX_CLOSE

    # 事故当时的写法：按 (market, symbol) 建索引 → 只剩一条，且是指数那条
    broken = {(r["market"], r["symbol"]): r for _, r in snap.iterrows()}
    assert len(broken) == 1
    assert broken[("cn", "000001")]["close"] == INDEX_CLOSE      # 就是这个值污染了股票


# --------------------------------------------------------------------- 3) 端到端
def test_end_to_end_stock_and_index_do_not_swap(workdir, monkeypatch):
    """★ 真跑一遍 daily_close：股票写股票价、指数写指数价，互不污染。

    这是对事故的完整复现——事故当时这里会把 STOCK_CLOSE 写成 INDEX_CLOSE。
    """
    from src import config as cfg
    from prototype import backfill, daily_close

    prices = workdir / "prices"
    returns = workdir / "returns"
    portfolios = workdir / "portfolios"
    for d in (prices, returns, portfolios):
        d.mkdir(parents=True, exist_ok=True)

    # 造一段历史：股票 ~11 元，指数 ~3800 点
    hist_stock = pd.DataFrame({"date": pd.to_datetime(["2026-09-28", "2026-09-29"]),
                               "close": [11.30, 11.35]})
    hist_index = pd.DataFrame({"date": pd.to_datetime(["2026-09-28", "2026-09-29"]),
                               "close": [3823.62, 3830.45]})
    hist_stock.to_csv(prices / "cn_stock_000001.csv", index=False)
    hist_index.to_csv(prices / "cn_index_000001.csv", index=False)

    # 假行情：两个标的同号，值差异巨大
    def fake_snapshot(items):
        rows = []
        for it in items:
            close = STOCK_CLOSE if it.type == "stock" else INDEX_CLOSE
            prev = 11.35 if it.type == "stock" else 3830.45
            rows.append({"key": f"{it.market}_{it.type}_{it.symbol}",
                         "symbol": it.symbol, "name": it.name,
                         "market": it.market, "type": it.type,
                         "close": close, "prev_close": prev,
                         "pct_chg": close / prev - 1, "date": "2026-09-30",
                         "source": "fake"})
        return pd.DataFrame(rows)

    monkeypatch.setattr(daily_close.qf, "snapshot", fake_snapshot)
    monkeypatch.setattr(daily_close, "PRICES_DIR", prices)
    monkeypatch.setattr(daily_close, "RETURNS_DIR", returns)
    monkeypatch.setattr(daily_close, "PORTFOLIOS_DIR", portfolios)
    monkeypatch.setattr(daily_close, "META_PATH", workdir / "meta.json")
    monkeypatch.setattr(backfill, "seed_missing", lambda *a, **k: 0)
    monkeypatch.setattr(daily_close, "_snapshot_module", lambda kind: None)

    inv = [
        cfg.Investor(nickname="stock_holder", symbol="000001", market="cn",
                     type="stock", start_date="2026-09-28"),
        cfg.Investor(nickname="index_holder", symbol="000001", market="cn",
                     type="index", start_date="2026-09-28"),
    ]
    monkeypatch.setattr(daily_close, "load_investors", lambda *a, **k: inv)
    monkeypatch.setattr(daily_close, "load_switches", lambda *a, **k: [])
    monkeypatch.setattr(daily_close, "load_investor_benchmarks", lambda *a, **k: [])
    monkeypatch.setattr(daily_close, "load_investor_config", lambda *a, **k: {
        "start_date": "2026-09-28", "principal": 1_000_000.0, "base_currency": "CNY"})

    meta = daily_close.run()

    # ① 标的层：两个文件各写各的价
    got_stock = pd.read_csv(prices / "cn_stock_000001.csv", parse_dates=["date"])
    got_index = pd.read_csv(prices / "cn_index_000001.csv", parse_dates=["date"])
    assert float(got_stock["close"].iloc[-1]) == pytest.approx(STOCK_CLOSE)
    assert float(got_index["close"].iloc[-1]) == pytest.approx(INDEX_CLOSE)

    # ② meta 层
    assert meta["items"]["cn_stock_000001"]["last_close"] == pytest.approx(STOCK_CLOSE)
    assert meta["items"]["cn_index_000001"]["last_close"] == pytest.approx(INDEX_CLOSE)

    # ③ 组合层：收益率必须是各自的，不能出现 33,000% 这种
    for nick, want in (("stock_holder", STOCK_CLOSE / 11.30 - 1),
                       ("index_holder", INDEX_CLOSE / 3823.62 - 1)):
        pf = pd.read_csv(portfolios / f"{nick}.csv", parse_dates=["date"])
        assert float(pf["cum_return"].iloc[-1]) == pytest.approx(want, abs=1e-9), nick
        assert abs(float(pf["daily_return"].iloc[-1])) < 0.05, f"{nick} 单日涨跌异常"


# --------------------------------------------------------------------- 4) 契约
def test_all_snapshot_producers_emit_matching_key():
    """★ 契约：所有快照来源的行都必须带 ``key``，格式与 snapshot() 一致。

    ``daily_close`` 用**同一个** ``{market}_{type}_{symbol}`` 键去查所有品种
    （quote_fetch / fund_fetch / crypto_fetch / futures_fetch）。
    哪个模块忘了带 key，那个品种就会静默取不到报价、整类数据变成 error
    —— 这是修撞键 bug 时真实踩到的回归。
    """
    from prototype import crypto_fetch, fund_fetch, futures_fetch

    # fund：喂一个最小可用的假响应，走真实 daily_snapshot
    item = ConfigItem(name="x", symbol="002910", market="cn", type="fund")
    js = ('var fS_name = "测试基金";'
          'var Data_netWorthTrend = [{"x": 1790697600000, "y": 1.5, "equityReturn": 0.1}];'
          'var Data_ACWorthTrend = [[1790697600000, 1.5]];')
    orig = fund_fetch.requests.get
    fund_fetch.requests.get = lambda *a, **k: type(
        "R", (), {"status_code": 200, "content": js.encode("utf-8")})()
    try:
        row = fund_fetch.daily_snapshot(item)
    finally:
        fund_fetch.requests.get = orig
    assert row.get("key") == "cn_fund_002910"

    # crypto / futures：检查源码里的构造契约（不联网）
    for mod, kind in ((crypto_fetch, "crypto"), (futures_fetch, "futures")):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert '"key": f"{item.market}_{item.type}_{item.symbol}"' in src, \
            f"{kind} 的 daily_snapshot 没有输出 key"


def test_pipeline_lookup_uses_key_only():
    """``daily_close`` 必须只按 key 取报价，不能退回 (market, symbol)。"""
    src = (_ROOT / "prototype" / "daily_close.py").read_text(encoding="utf-8")
    assert "quotes.get((it.market, it.symbol))" not in src, \
        "又退回了 (market, symbol) 取报价 —— 同号不同品种会再次串价"
    assert "quotes.get(key)" in src
