"""期货合约日线抓取 —— 新浪期货接口，免 key。**目前仅支持上期能源原油 SC。**

与 `prototype/quote_fetch.py`（A股/美股/港股）、`fund_fetch.py`（场外基金）、
`crypto_fetch.py`（加密货币）并列，专治**期货合约**。

数据源
------
- 日线：``stock2.finance.sina.com.cn/futures/api/jsonp.php/.../InnerFuturesNewService.getDailyKLine?symbol=SC2611``
  字段：``d`` 日期、``o/h/l`` 开高低、``c`` **收盘价（= 当日结算价）**、``v`` 成交量、``p`` 持仓量、``s`` **前一交易日结算价**
- 实时：``hq.sinajs.cn/list=nf_SC2611``（可选，仅用于诊断，见 :func:`quote`）

⚠️ **落盘口径 = 日线的 ``c``（结算价）**
------------------------------------------------
实测：SC2611 在 2026-09-30 的日线 ``c`` = 711.000，与实时报价的"今结算"一致；
而实时报价的"最新价"是 711.500（收盘那一刻的价）。本项目所有标的都按日线收盘价落盘，
所以这里**统一取日线的 ``c``**，保证是价格**序列**而不是"序列 + 实时点"混在一起。
实时报价只作为诊断接口暴露（:func:`quote`），不进落盘路径。

⚠️ **期货有到期日**
------------------
合约到期后就没有价格了。到期未调仓 → 余额应全部变现金，这由
`src.config.Item.expires` + `src.aggregate._clip_expiry` 实现（**不是**本模块的职责）。
本模块只负责如实抓取价格；一旦合约下架，接口自然就不再返回新数据。

实测：**已到期的合约仍能取到历史**（如 SC2312 取回 435 条、2020-12-02 ~ 2023-11-30），
所以期货的历史**可以事后重建**，不像我最初担心的那样必须当时落盘。

⚠️ 接口在 CI 环境可达
--------------------
实测**无需 Referer**、任意 User-Agent（含 `github-actions`）均正常返回，
所以 GitHub Actions（Ubuntu）可以直连。

用法
----
    python -m prototype.futures_fetch --symbol SC2611
    python -m prototype.futures_fetch --symbol SC2611 --tail 5 --quote
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:  # 避免重复包装 sys.stdout
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from src.config import FUTURES_CACHE_DIR  # noqa: E402

KLINE_URL = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/"
             "var%20t=/InnerFuturesNewService.getDailyKLine?symbol={sym}")
QUOTE_URL = "https://hq.sinajs.cn/list=nf_{sym}"
SOURCE = "sina"
CALIBER = "settlement_price"       # 日线 c（= 当日结算价）

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://finance.sina.com.cn",
}
TIMEOUT = 30
CACHE_MAX_AGE_DAYS = 10            # 到期合约不再更新，别因"不新鲜"就反复重抓

# 目前只放通上期能源原油 SC：SC + 4 位年月（SC2611 = 2026 年 11 月合约）
SUPPORTED_EXCHANGES = ("SC",)
# 合约代码的**通用**格式：2~3 位交易所字母 + 4 位年月。
# ⚠️ 不要写成只匹配 SC —— 那样其它交易所会掉进"格式不合法"，
# 而不是更有用的"暂不支持 XX 品种"。
_SYMBOL_RE = re.compile(r"^([A-Z]{2,3})(\d{4})$")


class FuturesError(RuntimeError):
    """期货行情抓取/解析失败。"""


# --------------------------------------------------------------------------- 代码
def normalize_symbol(symbol) -> str:
    """合约代码规范化：去 ``nf_`` 前缀、统一大写、去空白 → ``SC2611``。"""
    s = str(symbol).strip().upper()
    if s.startswith("NF_"):
        s = s[3:]
    s = re.sub(r"[^A-Z0-9]", "", s)
    if not s:
        raise FuturesError(f"合约代码为空：{symbol!r}")
    return s


def _check_symbol(sym: str) -> str:
    m = _SYMBOL_RE.match(sym)
    if not m:
        raise FuturesError(
            f"合约代码不合法：{sym!r}（应为 交易所字母+4位年月，如 SC2611）")
    if m.group(1) not in SUPPORTED_EXCHANGES:
        raise FuturesError(
            f"暂不支持 {m.group(1)} 品种（当前仅支持 {', '.join(SUPPORTED_EXCHANGES)}）")
    return sym


# --------------------------------------------------------------------------- 解析
def _num(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def parse_klines(payload: str) -> pd.DataFrame:
    """解析新浪期货日线响应（JSONP 包一层 ``var t=(...)``）。

    返回 ``DataFrame[date, close, open, high, low, volume, open_interest, prev_settle]``，
    日期升序去重；``close`` = 日线 ``c``（当日结算价）。
    """
    m = (re.search(r"var\s+t=\((.*)\)", payload, re.S)
         or re.search(r"=\((\[.*\])\)", payload, re.S))
    if not m:
        raise FuturesError(f"未取到日线数据（响应异常）：{payload[:160]!r}")
    try:
        rows = json.loads(m.group(1))
    except ValueError as e:
        raise FuturesError(f"日线 JSON 解析失败：{e}") from e
    if not isinstance(rows, list) or not rows:
        raise FuturesError("日线为空（合约代码可能不存在或已下架）")

    recs: list[dict] = []
    for k in rows:
        if not isinstance(k, dict):
            continue
        d, close = k.get("d"), _num(k.get("c"))
        if not d or close is None:
            continue
        recs.append({
            "date": str(d),
            "close": close,
            "open": _num(k.get("o")),
            "high": _num(k.get("h")),
            "low": _num(k.get("l")),
            "volume": _num(k.get("v")),
            "open_interest": _num(k.get("p")),
            "prev_settle": _num(k.get("s")),
        })
    if not recs:
        raise FuturesError("日线无有效记录")
    df = (pd.DataFrame(recs)
          .drop_duplicates("date", keep="last")
          .sort_values("date")
          .reset_index(drop=True))
    return df


def parse_quote(payload: str) -> dict:
    """解析 ``hq.sinajs.cn`` 期货报价（``var hq_str_nf_SC2611="...";``）。

    字段实测含义：``[0]``名称 ``[2]``开 ``[3]``高 ``[4]``低 ``[5]``买价 ``[6]``卖价
    ``[7]``最新价 ``[8]``今结算 ``[9]``昨结算 ``[13]``持仓量 ``[14]``成交量 ``[17]``日期
    """
    m = re.search(r'var hq_str_\w+="(.*)";', payload, re.S)
    if not m or not m.group(1).strip():
        raise FuturesError(f"报价为空：{payload[:120]!r}")
    f = m.group(1).split(",")
    if len(f) < 18:
        raise FuturesError(f"报价字段不足（{len(f)}）：{payload[:120]!r}")

    def g(i):
        return f[i] if i < len(f) else ""

    return {
        "name": g(0),
        "open": _num(g(2)), "high": _num(g(3)), "low": _num(g(4)),
        "bid": _num(g(5)), "ask": _num(g(6)),
        "last": _num(g(7)), "settle": _num(g(8)), "prev_settle": _num(g(9)),
        "open_interest": _num(g(13)), "volume": _num(g(14)),
        "date": g(17),
    }


# --------------------------------------------------------------------------- 抓取
def _get(url: str) -> str:
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    except Exception as e:  # noqa: BLE001
        raise FuturesError(f"请求失败 {url}：{type(e).__name__}: {e}") from e
    if r.status_code != 200:
        raise FuturesError(f"{url} 返回 HTTP {r.status_code}")
    return r.content.decode("gbk", "replace")


def fetch_detail(item) -> tuple[str, pd.DataFrame]:
    """抓某合约的**全量**日线。返回 ``(合约代码, detail)``。"""
    sym = _check_symbol(normalize_symbol(item.symbol))
    return sym, parse_klines(_get(KLINE_URL.format(sym=sym)))


def fetch_history(item) -> tuple[str, pd.DataFrame]:
    """同 :func:`fetch_detail`，但只返回 ``[date, close]``（落盘用）。"""
    sym, detail = fetch_detail(item)
    out = detail[["date", "close"]].copy()
    out["date"] = pd.to_datetime(out["date"])
    return sym, out.reset_index(drop=True)


def quote(item) -> dict:
    """抓实时报价（**仅诊断用**，不参与落盘）。

    注意与落盘口径的差别：``last`` 是"最新价"，``settle`` 才对应日线的 ``c``。
    """
    sym = _check_symbol(normalize_symbol(item.symbol))
    return {"symbol": sym, "source": SOURCE, **parse_quote(_get(QUOTE_URL.format(sym=sym)))}


# --------------------------------------------------------------------------- 缓存
def _cache_path(key: str) -> Path:
    return FUTURES_CACHE_DIR / f"{key}.json"


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
    if not cache or not cache.get("bars"):
        return False
    last = cache["bars"][-1].get("date")
    if not last:
        return False
    try:
        lag = (pd.Timestamp.today().normalize() - pd.Timestamp(last)).days
    except (ValueError, TypeError):
        return False
    return lag <= max_age_days


def save_cache(key: str, symbol: str, detail: pd.DataFrame) -> dict:
    FUTURES_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    d = detail.copy()
    d["date"] = pd.to_datetime(d["date"]).dt.strftime("%Y-%m-%d")
    data = {
        "key": key,
        "symbol": symbol,
        "source": SOURCE,
        "caliber": CALIBER,
        "first_date": str(d["date"].iloc[0]),
        "last_date": str(d["date"].iloc[-1]),
        "n": int(len(d)),
        "bars": [
            {
                "date": r["date"], "close": float(r["close"]),
                "open": None if pd.isna(r["open"]) else float(r["open"]),
                "high": None if pd.isna(r["high"]) else float(r["high"]),
                "low": None if pd.isna(r["low"]) else float(r["low"]),
                "volume": None if pd.isna(r["volume"]) else float(r["volume"]),
                "open_interest": (None if pd.isna(r["open_interest"])
                                  else float(r["open_interest"])),
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
            return str(cache.get("symbol") or normalize_symbol(item.symbol)), cache_history(cache)
    sym, detail = fetch_detail(item)
    save_cache(key, sym, detail)
    return sym, detail[["date", "close"]].reset_index(drop=True)


# --------------------------------------------------------------------------- 当日
def daily_snapshot(item) -> dict:
    """取最新一根日线，返回与 ``quote_fetch.snapshot()`` **同构**的一行。

    注意：**已下架的合约**（到期后）会取不到新数据 —— 这时抛错即可，
    "到期转现金"由 `Item.expires` 在组合层处理，不靠本函数。
    """
    sym, detail = fetch_detail(item)
    row = detail.iloc[-1]
    prev = float(detail["close"].iloc[-2]) if len(detail) >= 2 else None
    close = float(row["close"])
    pct = (close / prev - 1.0) if prev else None

    return {
        # key 必须与 quote_fetch.snapshot() 同构：daily_close 统一按
        # `{market}_{type}_{symbol}` 取报价（见 test_quote_key_collision_offline.py）
        "key": f"{item.market}_{item.type}_{item.symbol}",
        "symbol": str(item.symbol),
        "name": sym,
        "market": item.market,
        "type": item.type,                      # "futures"
        "close": close,                          # 日线 c = 当日结算价
        "prev_close": prev,
        "pct_chg": pct,
        "date": str(row["date"]),
        "source": SOURCE,
        "open_interest": (None if pd.isna(row["open_interest"])
                          else float(row["open_interest"])),
    }


# --------------------------------------------------------------------------- CLI
def main() -> int:
    ap = argparse.ArgumentParser(description="抓取期货合约日线（新浪期货，结算价口径；目前仅 SC）")
    ap.add_argument("--symbol", required=True, help="合约代码，如 SC2611")
    ap.add_argument("--tail", type=int, default=8, help="打印最近 N 个交易日（默认 8）")
    ap.add_argument("--quote", action="store_true", help="同时打印实时报价（诊断用）")
    args = ap.parse_args()

    class _Item:
        symbol = args.symbol
        name = args.symbol
        market = "cn"
        type = "futures"
        key = f"cn_futures_{normalize_symbol(args.symbol)}"   # 与 src.config.Item.key 同构

    sym, detail = fetch_detail(_Item)
    print(f"{sym} 共 {len(detail)} 个交易日  "
          f"{detail['date'].iloc[0]} ~ {detail['date'].iloc[-1]}  （{CALIBER}）\n")
    show = detail.tail(args.tail).copy()
    with pd.option_context("display.width", 200):
        print(show[["date", "open", "high", "low", "close", "volume",
                    "open_interest", "prev_settle"]].to_string(index=False))
    if args.quote:
        try:
            q = quote(_Item)
            print(f"\n实时报价（诊断用，注意 last≠settle）：")
            print(f"  {q['name']}  日期={q['date']}  最新价={q['last']}  "
                  f"今结算={q['settle']}  昨结算={q['prev_settle']}  持仓={q['open_interest']}")
            print(f"  → 落盘口径取日线 c（结算价），与上面的『今结算』一致")
        except FuturesError as e:
            print(f"\n实时报价失败：{e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
