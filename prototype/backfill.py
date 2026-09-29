"""历史回填：一次性把各标的的日线历史（未复权）补进 data/prices/*.csv。

之后照常 `python -m prototype.daily_close` 每天增量 append。

数据源（与当日报价同口径 —— 都是**未复权**，保证序列连续）：
- cn（个股/ETF/指数）+ hk（港股）：腾讯 `fqkline` 的 `day`（= 不复权；港股用 `hk` 前缀代码）
- us（个股/ETF/指数）：新浪 `US_MinKService.getDailyK`（指数用 `.INX`/`.IXIC`/`.DJI`）

⚠️ 未复权：跨**拆股**（美股）或**除权除息**（A股）会有跳空 → 适合近期窗口；
   长历史需要复权（另做）。start_date 仍是累计收益的基准。

用法：
    python -m prototype.backfill                 # 默认回填最近 400 个交易日
    python -m prototype.backfill --days 800
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from prototype import daily_close  # noqa: E402
from prototype.quote_fetch import Item, tencent_code  # noqa: E402
from src.config import (  # noqa: E402
    PRICES_DIR,
    investor_segments,
    load_investor_benchmarks,
    load_investors,
    load_switches,
)

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
TX_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{n},"
SINA_US_K = ("https://stock.finance.sina.com.cn/usstock/api/jsonp.php/var%20t=/"
             "US_MinKService.getDailyK?symbol={sym}&___qn=3")
US_INDEX = {"^GSPC": ".INX", "^IXIC": ".IXIC", "^DJI": ".DJI", "^NDX": ".NDX"}


def _tencent_cn_daily(code: str, n: int) -> pd.DataFrame:
    r = requests.get(TX_KLINE.format(code=code, n=n),
                     headers=dict(UA, Referer="https://gu.qq.com"), timeout=30)
    data = json.loads(r.content.decode("utf-8", "replace"))["data"][code]
    rows = data.get("day") or data.get("qfqday") or []
    # 每条: [date, open, close, high, low, volume, ...]
    recs = [{"date": x[0], "close": float(x[2])} for x in rows if len(x) >= 3]
    return pd.DataFrame(recs)


def _sina_us_daily(symbol: str, n: int) -> pd.DataFrame:
    sym = US_INDEX.get(symbol, symbol)
    r = requests.get(SINA_US_K.format(sym=sym),
                     headers=dict(UA, Referer="https://finance.sina.com.cn"), timeout=30)
    m = re.search(r"var t=\((.*)\)", r.content.decode("utf-8", "replace"), re.S)
    if not m:
        raise ValueError(f"sina US 未返回 {sym}")
    arr = json.loads(m.group(1))
    recs = [{"date": x["d"], "close": float(x["c"])} for x in arr[-n:]]
    return pd.DataFrame(recs)


def fetch_history(it: Item, n: int) -> pd.DataFrame:
    if it.market == "us":
        return _sina_us_daily(it.symbol, n)
    # cn 个股/ETF/指数 与 hk 港股：都走腾讯 fqkline（未复权 day）
    return _tencent_cn_daily(tencent_code(it), n)


def _merge(existing: pd.DataFrame, fetched: pd.DataFrame) -> pd.DataFrame:
    """并集后按日去重（保留已有的当日报价优先），再排序。"""
    cols = ["date", "close"]
    if existing is not None and len(existing):
        existing = existing[cols]
        part = pd.concat([fetched[cols], existing], ignore_index=True)
    else:
        part = fetched[cols]
    part["date"] = pd.to_datetime(part["date"])
    part = (part.drop_duplicates("date", keep="last")
                .sort_values("date").reset_index(drop=True))
    return part


def seed_missing(items, days: int = 400, min_rows: int = 5) -> int:
    """给**历史过短/缺失**的标的补一份历史（新加入标的用）。

    daily_close 每次会先调用它：这样"往 investors.yaml 加个标的 → 跑 daily_close"
    就能自动补全历史，不会因为某标只有 1 天数据把指数/收益带偏。返回补种数量。
    """
    seeded = 0
    for it in items:
        path = PRICES_DIR / f"{it.key}.csv"
        rows = 0
        if path.exists():
            try:
                rows = len(pd.read_csv(path))
            except Exception:  # noqa: BLE001
                rows = 0
        if rows >= min_rows:
            continue
        try:
            fetched = fetch_history(it, days)
            existing = pd.read_csv(path, parse_dates=["date"]) if path.exists() else None
            merged = _merge(existing, fetched)
            merged.to_csv(path, index=False)
            print(f"[seed] {it.key:12s} {it.name:8s} 补种 {len(fetched)} 行 -> 共 {len(merged)} 行")
            seeded += 1
        except Exception as e:  # noqa: BLE001
            print(f"[warn] {it.key:12s} {it.name:8s} 补种失败：{type(e).__name__}: {e}")
    return seeded


def run(days: int) -> None:
    PRICES_DIR.mkdir(parents=True, exist_ok=True)
    investors = load_investors()
    segments_by_inv = investor_segments(investors, load_switches())
    items = daily_close._instruments(segments_by_inv, load_investor_benchmarks())
    print(f"回填 {len(items)} 个标的，各取最近 {days} 个交易日（未复权）…\n")
    for it in items:
        key = it.key
        path = PRICES_DIR / f"{key}.csv"
        try:
            fetched = fetch_history(it, days)
            existing = pd.read_csv(path, parse_dates=["date"]) if path.exists() else None
            merged = _merge(existing, fetched)
            merged.to_csv(path, index=False)
            print(f"[ok ] {key:12s} {it.name:8s} 回填 {len(fetched):4d} 行 -> 共 {len(merged)} 行 "
                  f"({merged['date'].iloc[0].date()} ~ {merged['date'].iloc[-1].date()})")
        except Exception as e:  # noqa: BLE001
            print(f"[ERR] {key:12s} {it.name:8s} -> {type(e).__name__}: {e}")

    print("\n重算收益（基准 = 各标的 start_date）…\n")
    daily_close.run()   # 复用日更逻辑：补当日 + 算累计收益 + 写 meta.json


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=400, help="回填的交易日数（默认 400）")
    args = ap.parse_args()
    run(args.days)
    return 0


if __name__ == "__main__":
    sys.exit(main())
