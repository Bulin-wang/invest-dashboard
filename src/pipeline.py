"""主流程：读持仓 → 抓行情 → 算收益 → 落盘 data/。

默认「全量重取」（每天从 start_date 重抓一遍）：
因为 A股后复权价会随除权除息改写历史，增量 append 会失真；
标的数量不大时全量重取最简单也最正确。

用法：  python -m src.pipeline
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

import pandas as pd

from .config import (
    DATA_DIR,
    META_PATH,
    PRICES_DIR,
    RETURNS_DIR,
    Item,
    load_benchmarks,
    load_holdings,
)
from .fetch.base import fetch_daily
from .returns import annualized_return, calmar_ratio, cumulative_return, max_drawdown


def _process(item: Item) -> dict:
    """抓取单个标的并落盘，返回其 meta 记录。"""
    key = item.key
    start = item.start_date or "2015-01-01"

    prices = fetch_daily(item.symbol, item.market, item.type, start)
    prices.to_csv(PRICES_DIR / f"{key}.csv", index=False)

    ret = cumulative_return(prices, start)
    ret.to_csv(RETURNS_DIR / f"{key}.csv", index=False)

    return {
        "name": item.name,
        "symbol": item.symbol,
        "market": item.market,
        "type": item.type,
        "weight": item.weight,
        "start_date": start,
        "first_date": str(ret["date"].iloc[0].date()),
        "last_date": str(ret["date"].iloc[-1].date()),
        "base_close": float(ret["close"].iloc[0]),
        "last_close": float(ret["close"].iloc[-1]),
        "cum_return": float(ret["cum_return"].iloc[-1]),
        "annualized": _safe(annualized_return(ret)),
        "max_drawdown": _safe(max_drawdown(ret["nav"])),
        "calmar": _safe(calmar_ratio(ret)),
        "n_obs": int(len(ret)),
        "status": "ok",
    }


def _safe(x: float) -> float | None:
    try:
        return None if x != x else float(x)  # NaN -> None（便于 JSON）
    except (TypeError, ValueError):
        return None


def _warm_tushare_adj(items: list[Item]) -> None:
    """把 A股个股的复权因子一次性批量取回并缓存。

    Tushare 免费 token 对 adj_factor 限速 1 次/分钟 + 1 次/小时，
    逐个标的分开取会触发小时级限速；这里在跑主循环前用一次调用覆盖全部个股。
    """
    stocks = [it for it in items if it.market == "cn" and it.type == "stock"]
    if not stocks:
        return
    try:
        from .fetch import tushare_mcp
        codes = [tushare_mcp.guess_ts_code(it.symbol, "stock") for it in stocks]
        start = min(it.start_date or "2015-01-01" for it in stocks)
        tushare_mcp.prefetch_adj_factors(codes, start)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] adj_factor 批量预热失败（将按需单独获取）：{e}")


def run() -> dict:
    DATA_DIR.mkdir(exist_ok=True)
    PRICES_DIR.mkdir(exist_ok=True)
    RETURNS_DIR.mkdir(exist_ok=True)

    items = load_holdings() + load_benchmarks()
    _warm_tushare_adj(items)
    meta: dict = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "base_currency": "CNY",
        "items": {},
    }

    for item in items:
        try:
            meta["items"][item.key] = _process(item)
            rec = meta["items"][item.key]
            print(f"[ok ] {item.key:12s} {item.name:10s} "
                  f"last={rec['last_date']} cum={rec['cum_return']:+.2%} "
                  f"mdd={rec['max_drawdown']:.2%}")
        except Exception as e:  # noqa: BLE001
            meta["items"][item.key] = {
                "name": item.name, "symbol": item.symbol,
                "market": item.market, "type": item.type,
                "status": "error", "error": f"{type(e).__name__}: {e}",
            }
            print(f"[ERR] {item.key:12s} {item.name:10s} -> {type(e).__name__}: {e}")

    META_PATH.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    ok = sum(1 for v in meta["items"].values() if v.get("status") == "ok")
    print(f"\n完成：{ok}/{len(items)} 个标的更新成功 -> {META_PATH.relative_to(DATA_DIR.parent)}")
    return meta


def main() -> int:
    meta = run()
    # 全部失败时返回非 0，便于 CI 报警（部分成功仍算通过）
    ok = any(v.get("status") == "ok" for v in meta["items"].values())
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
