"""加密货币（crypto）日线抓取 —— 币安公开数据镜像，免 key。

与 `prototype/quote_fetch.py`（A股/美股/港股）、`prototype/fund_fetch.py`（场外基金）并列，
专治**加密现货交易对**。

数据源
------
``data-api.binance.vision/api/v3/klines`` —— 币安**官方公开数据镜像**，免 key。

⚠️ 为什么不直连 ``api.binance.com``：实测该域名在本机**连接超时**
   （OKX / CoinGecko / Coinbase 同样超时），而 ``data-api.binance.vision``
   可正常访问。若某天镜像也不通，只需改本模块的 ``BASE_URL``。

⚠️ 日期语义：时间戳是 **UTC 00:00**，必须按 UTC 解析
--------------------------------------------------
与基金（本地零点）不同，币安日线 ``openTime`` 是 UTC 零点。
若误用本地时间解析，日期会整体**后移**一天（UTC+8 下 2026-10-05Z 变成 10-06）。
所以本模块用 ``datetime.fromtimestamp(ms/1000, timezone.utc)``。

⚠️ 7×24：**周末也有行情**
------------------------
加密每天（含周六日）都有一根日线。你已选择「保持并集」口径：只要组合里有人持有
crypto，看板就会出现周末点位；当天股票/基金前填不动，显示为平线。
详见 README「加密货币」一节。

⚠️ 最后一根可能**未走完**
------------------------
UTC 当天尚未结束时，当天那根日线的 close 只是**当前价**而非收盘价。
本模块一律**丢弃未完成的当日 K 线**（``openTime + 1d > now``），保证序列里全是完整日线。

⚠️ 计价货币：默认 USDT
--------------------
``symbol: BTC`` → 自动补成 ``BTCUSDT``；也可显式写 ``BTCUSDT`` / ``BTC/USDT`` / ``BTC-USDT``。
跨市场比较时请注意：**crypto 是 USDT 计价，本项目不做汇率折算**（与美股同样的已知局限）。

用法
----
    python -m prototype.crypto_fetch --symbol BTCUSDT
    python -m prototype.crypto_fetch --symbol ETH --tail 5
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:  # 避免重复包装 sys.stdout
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from src.config import CRYPTO_CACHE_DIR  # noqa: E402

BASE_URL = "https://data-api.binance.vision/api/v3/klines"
SOURCE = "binance"
CALIBER = "utc_daily_close"        # UTC 日线收盘价（含周末）

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
TIMEOUT = 30
DAY_MS = 86_400_000
MAX_LIMIT = 1000                   # 币安单次上限（实测 limit=1500 也只返回 1000）
MAX_PAGES = 60                     # 翻页保险丝（60×1000 天 ≈ 164 年，足够）
CACHE_MAX_AGE_DAYS = 3             # 缓存最新日期超过这么多天 → 重取（crypto 每天都有）

# symbol 缺计价货币时的补全优先级
QUOTES = ("USDT", "USDC", "FDUSD", "TUSD", "BTC", "ETH", "BNB", "EUR", "TRY")
MIN_BASE_LEN = 2                   # 基础币最少字符数（避免把 "BTC" 切成 "B"+"TC"）

_PAIR_RE = re.compile(r"^[A-Z0-9]{5,20}$")


class CryptoError(RuntimeError):
    """加密行情抓取/解析失败。"""


# --------------------------------------------------------------------------- 交易对
def _split_pair(s: str) -> tuple[str, str] | None:
    """从字母数字串里切出 ``(基础币, 计价货币)``。

    计价货币必须排在``基础币``**之后**，且基础币至少 2 个字符
    （否则 ``BTC`` 会被误判成"基础币=B、计价=BTC"）。
    """
    for q in QUOTES:
        if (s.endswith(q) and len(s) > len(q)
                and len(s) - len(q) >= MIN_BASE_LEN and s[:-len(q)].isalnum()):
            return s[:-len(q)], q
    return None


def normalize_pair(symbol) -> str:
    """交易对规范化：``BTC/USDT``、``btc-usdt``、``BTC`` → ``BTCUSDT``（统一大写、去分隔符）。

    只给基础币（如 ``BTC``）时补上计价货币，默认 ``USDT``（按 :data:`QUOTES` 顺序匹配）。

    ⚠️ 不能简单用 ``s.endswith(tuple(QUOTES))`` 判断"已带计价货币"：
    ``"BTC".endswith("BTC")`` 为真，会把基础币 ``BTC`` 误判成交易对而**不补** ``USDT``。
    所以必须要求计价货币前面还剩至少 :data:`MIN_BASE_LEN` 个字符。
    """
    s = str(symbol).strip().upper().replace("/", "").replace("-", "").replace("_", "")
    s = re.sub(r"[^A-Z0-9]", "", s)
    if not s:
        raise CryptoError(f"交易对为空：{symbol!r}")
    if _split_pair(s) is not None:
        return s
    return s + "USDT"


def _check_pair(pair: str) -> str:
    if not _PAIR_RE.match(pair):
        raise CryptoError(f"交易对不合法：{pair!r}（应为如 BTCUSDT，基础币至少 2 个字符）")
    if _split_pair(pair) is None:
        raise CryptoError(
            f"无法识别计价货币：{pair!r}（支持的计价：{', '.join(QUOTES)}）")
    return pair


# --------------------------------------------------------------------------- 解析
def _to_date(ms) -> str:
    """毫秒时间戳 → ``YYYY-MM-DD``。

    ⚠️ 币安是 **UTC** 零点，不能用本地时间（会把日期推后一天）。
    """
    return datetime.fromtimestamp(float(ms) / 1000.0, timezone.utc).strftime("%Y-%m-%d")


def _now_ms() -> int:
    return int(time.time() * 1000)


def parse_klines(rows, now_ms: int | None = None) -> pd.DataFrame:
    """币安 klines 数组 → ``DataFrame[date, close, open, high, low, volume]``。

    - 丢弃**未完成**的当日 K 线（``openTime + 1d > now``）
    - 丢弃字段不全 / 价格非法的行
    - 按日期去重（保留最后一条）并升序
    """
    now = _now_ms() if now_ms is None else now_ms
    recs: list[dict] = []
    for k in rows or []:
        if not isinstance(k, (list, tuple)) or len(k) < 6:
            continue
        try:
            open_ms = int(k[0])
            close = float(k[4])
        except (TypeError, ValueError):
            continue
        if open_ms + DAY_MS > now:            # 当日未走完 → 丢弃
            continue
        recs.append({
            "date": _to_date(open_ms),
            "close": close,
            "open": _num(k[1]),
            "high": _num(k[2]),
            "low": _num(k[3]),
            "volume": _num(k[5]),
        })
    if not recs:
        raise CryptoError("没有可用的完整日线（kline 为空或全部未走完）")
    df = (pd.DataFrame(recs)
          .drop_duplicates("date", keep="last")
          .sort_values("date")
          .reset_index(drop=True))
    return df


def _num(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- 抓取
def _request(symbol: str, *, start_ms=None, end_ms=None, limit: int = MAX_LIMIT) -> list:
    params = {"symbol": symbol, "interval": "1d", "limit": min(int(limit), MAX_LIMIT)}
    if start_ms is not None:
        params["startTime"] = int(start_ms)
    if end_ms is not None:
        params["endTime"] = int(end_ms)
    try:
        r = requests.get(BASE_URL, params=params, headers=HEADERS, timeout=TIMEOUT)
    except Exception as e:  # noqa: BLE001
        raise CryptoError(
            f"请求失败 {BASE_URL}：{type(e).__name__}: {e}"
            "（若持续超时，说明该镜像在当前网络下不可达，需换数据源）") from e
    if r.status_code != 200:
        body = r.content.decode("utf-8", "replace")[:200]
        raise CryptoError(f"{symbol} 返回 HTTP {r.status_code}：{body}")
    try:
        data = r.json()
    except ValueError as e:
        raise CryptoError(f"{symbol} 返回非 JSON：{r.content[:200]!r}") from e
    if isinstance(data, dict):               # 币安错误形如 {"code": -1121, "msg": "Invalid symbol."}
        raise CryptoError(f"{symbol} 接口报错：{data}")
    return data


def _recent(pair: str, n: int = 7) -> list:
    """取最近 ``n`` 根日线。"""
    return _request(pair, end_ms=_now_ms(), limit=n)


def fetch_detail(item, max_pages: int = MAX_PAGES) -> tuple[str, pd.DataFrame]:
    """抓某交易对的**全量**日线（向后翻页，币安单次最多 1000 根）。

    返回 ``(交易对, DataFrame[date, close, open, high, low, volume])``。
    """
    pair = _check_pair(normalize_pair(item.symbol))
    cursor = _now_ms()
    chunks: list[list] = []
    for _ in range(max_pages):
        batch = _request(pair, end_ms=cursor, limit=MAX_LIMIT)
        if not batch:
            break
        chunks.append(batch)
        if len(batch) < MAX_LIMIT:
            break
        cursor = int(batch[0][0]) - 1          # 往前挪（避免重复含边界那根）
    rows: list = []
    for c in reversed(chunks):
        rows.extend(c)
    if not rows:
        raise CryptoError(f"{pair} 没有任何日线数据（交易对可能不存在或已下架）")
    return pair, parse_klines(rows)


def fetch_history(item) -> tuple[str, pd.DataFrame]:
    """同 :func:`fetch_detail`，但只返回 ``[date, close]``（落盘用，date 已转 datetime）。"""
    pair, detail = fetch_detail(item)
    out = detail[["date", "close"]].copy()
    out["date"] = pd.to_datetime(out["date"])
    return pair, out.reset_index(drop=True)


# --------------------------------------------------------------------------- 缓存
def _cache_path(key: str) -> Path:
    return CRYPTO_CACHE_DIR / f"{key}.json"


def load_cache(key: str) -> dict | None:
    path = _cache_path(key)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    if not isinstance(data, dict) or not data.get("bars"):
        return None
    return data


def cache_is_fresh(cache: dict | None, max_age_days: int = CACHE_MAX_AGE_DAYS) -> bool:
    """缓存最新日期是否足够新（crypto 每天都有数据，滞后超过几天就该重取）。"""
    if not cache or not cache.get("bars"):
        return False
    last = cache["bars"][-1].get("date")
    if not last:
        return False
    try:
        lag = (pd.Timestamp.now(tz="UTC").normalize().tz_localize(None)
               - pd.Timestamp(last)).days
    except (ValueError, TypeError):
        return False
    return lag <= max_age_days


def save_cache(key: str, pair: str, detail: pd.DataFrame) -> dict:
    """全量日线写入 ``data/crypto_cache/{key}.json``。"""
    CRYPTO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    d = detail.copy()
    d["date"] = pd.to_datetime(d["date"]).dt.strftime("%Y-%m-%d")
    data = {
        "key": key,
        "pair": pair,
        "source": SOURCE,
        "caliber": CALIBER,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "first_date": str(d["date"].iloc[0]),
        "last_date": str(d["date"].iloc[-1]),
        "n": int(len(d)),
        "bars": [
            {
                "date": r["date"],
                "close": float(r["close"]),
                "open": None if pd.isna(r["open"]) else float(r["open"]),
                "high": None if pd.isna(r["high"]) else float(r["high"]),
                "low": None if pd.isna(r["low"]) else float(r["low"]),
                "volume": None if pd.isna(r["volume"]) else float(r["volume"]),
            }
            for r in d.to_dict("records")
        ],
    }
    _cache_path(key).write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                encoding="utf-8")
    return data


def cache_history(cache: dict) -> pd.DataFrame:
    df = pd.DataFrame([{"date": b["date"], "close": b["close"]} for b in cache["bars"]])
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def load_or_fetch(item, force: bool = False) -> tuple[str, pd.DataFrame]:
    """缓存优先：命中且新鲜 → 读缓存；否则抓全量并写缓存。"""
    key = item.key
    if not force:
        cache = load_cache(key)
        if cache_is_fresh(cache):
            return str(cache.get("pair") or normalize_pair(item.symbol)), cache_history(cache)
    pair, detail = fetch_detail(item)
    save_cache(key, pair, detail)
    return pair, detail[["date", "close"]].reset_index(drop=True)


# --------------------------------------------------------------------------- 当日
def _merge_recent(cache: dict, pair: str, recent: pd.DataFrame) -> None:
    """把最近几根日线并进缓存末尾（按日期去重，新值优先），并改写元信息。

    用于**增量**维护缓存：避免每天抓当日时都重取一遍全量历史
    （BTCUSDT 全量约 3400 根 = 4 次请求；有 N 个币对就是 4N 次）。
    """
    bars = {b["date"]: dict(b) for b in cache.get("bars") or []}
    for r in recent.to_dict("records"):
        d = str(r["date"])
        if d in bars:
            continue                      # 已收盘的 K 线不会变，无需覆盖
        bars[d] = {
            "date": d, "close": float(r["close"]),
            "open": None if pd.isna(r["open"]) else float(r["open"]),
            "high": None if pd.isna(r["high"]) else float(r["high"]),
            "low": None if pd.isna(r["low"]) else float(r["low"]),
            "volume": None if pd.isna(r["volume"]) else float(r["volume"]),
        }
    dates = sorted(bars)
    cache.update({
        "pair": pair, "source": SOURCE, "caliber": CALIBER,
        "first_date": dates[0], "last_date": dates[-1], "n": len(dates),
        "bars": [bars[d] for d in dates],
    })


def daily_snapshot(item) -> dict:
    """取最新**完整**日线，返回与 ``quote_fetch.snapshot()`` **同构**的一行。

    - 只请求最近 7 根（1 次请求），内部丢弃未走完的当日 K 线；
    - 缓存**只做增量维护**：**已有**缓存才往里追加；没有缓存则**不写**，
      留给 `backfill.seed_missing()` 去抓全量（避免缓存里只有 7 行、
      被误当成"完整历史"而跳过补种）。
      契约：`{key}.json` 一旦存在，其 `bars` 就覆盖该交易对的全量历史
      （`fund_fetch`/`futures_fetch` 遵循同一契约）。
    """
    pair = _check_pair(normalize_pair(item.symbol))
    recent = parse_klines(_recent(pair, 7))        # 丢弃未完成的当日 K 线
    row = recent.iloc[-1]
    prev = float(recent["close"].iloc[-2]) if len(recent) >= 2 else None
    close = float(row["close"])
    pct = (close / prev - 1.0) if prev else None

    # 增量维护缓存（已有才追加；没有就等 seed_missing 抓全量）
    try:
        cache = load_cache(item.key)
        if cache is not None and (cache.get("bars") or []):
            _merge_recent(cache, pair, recent)
            _cache_path(item.key).write_text(
                json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except (CryptoError, OSError, ValueError):
        pass

    return {
        "symbol": str(item.symbol),
        "name": pair,
        "market": item.market,
        "type": item.type,                     # "crypto"
        "close": close,
        "prev_close": prev,
        "pct_chg": pct,
        "date": str(row["date"]),
        "source": SOURCE,
        "pair": pair,
    }


# --------------------------------------------------------------------------- CLI
def main() -> int:
    ap = argparse.ArgumentParser(description="抓取加密货币日线（币安公开镜像，UTC 日线口径）")
    ap.add_argument("--symbol", required=True, help="交易对，如 BTCUSDT 或简写 BTC")
    ap.add_argument("--tail", type=int, default=8, help="打印最近 N 天（默认 8）")
    args = ap.parse_args()

    class _Item:
        symbol = args.symbol
        name = args.symbol
        market = "us"
        type = "crypto"
        key = f"us_crypto_{normalize_pair(args.symbol)}"   # 与 src.config.Item.key 同构

    pair, detail = fetch_detail(_Item)
    print(f"{pair} 共 {len(detail)} 根完整日线  "
          f"{detail['date'].iloc[0]} ~ {detail['date'].iloc[-1]}  （{CALIBER}，含周末）\n")
    show = detail.tail(args.tail).copy()
    with pd.option_context("display.width", 200):
        print(show[["date", "open", "high", "low", "close", "volume"]].to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
