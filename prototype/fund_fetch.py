"""开放式基金（场外）净值抓取 —— 东方财富，免 key。

与 `prototype/quote_fetch.py`（A股/美股/港股行情）并列，专治**场外基金**：
场外基金没有交易所前缀，不能走新浪/腾讯那套 `sh`/`sz` 代码映射。

数据源
------
1. ``fund.eastmoney.com/pingzhongdata/{code}.js``   —— **全量历史，一次请求**
   含 ``Data_netWorthTrend``（单位净值）与 ``Data_ACWorthTrend``（累计净值）。
2. ``api.fund.eastmoney.com/f10/lsjz``              —— 历史净值（**每页最多 20 条**）
   留作备用；本项目全量走 (1) 即可，不需要翻页。

⚠️ 口径：**累计净值**（``Data_ACWorthTrend``）落盘为 ``close``
--------------------------------------------------------------
场外基金没有"未复权原始价"这个概念：**单位净值本身就是除权后的价格**，
每次分红/份额折算都会让它跳水。实测差异极大：

    163402 兴全趋势投资   单位净值口径 -17.53%   累计净值口径 +1014.59%
    519066 汇添富蓝筹稳健  单位净值口径 +214.00%  累计净值口径 +346.90%
    000001 华夏成长混合   单位净值口径 +22.20%   累计净值口径 +279.50%

所以 ``type: fund`` 的收益口径 = **含分红再投**，与 A股/美股/ETF 的
"未复权、不含分红"（`meta.json` 的 ``caliber: price_return``）**不同**，
看板与 `meta.json` 会据此单独标注（见 ``daily_close.CALIBER_FUND``）。

⚠️ 时间戳是**本地时间零点**，必须用 ``datetime.fromtimestamp`` 解析
----------------------------------------------------------------
``x = 1790697600000`` 表示**本地** 2026-09-30 00:00。若误用 ``utcfromtimestamp``，
在 UTC+8 下会把所有日期整体**提前一天**（09-30 变成 09-29，甚至出现"周日有净值"）。

⚠️ 净值滞后一天属正常
--------------------
场外基金净值 T 日盘后公布，当日 15:35 抓不到当日值。本项目**不做额外处理**。

用法
----
    python -m prototype.fund_fetch --code 002910
    python -m prototype.fund_fetch --code 002910 --tail 5
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:  # 避免重复包装 sys.stdout
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from src.config import FUND_CACHE_DIR  # noqa: E402

PINGZHONG_URL = "https://fund.eastmoney.com/pingzhongdata/{code}.js"
LSJZ_URL = "https://api.fund.eastmoney.com/f10/lsjz"      # 备用：每页 ≤20 条
SOURCE = "eastmoney"
CALIBER = "cumulative_nav"        # 累计净值（含分红再投）

# 东方财富对缺 Referer 的请求会拒绝，必须带上（实测）
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Referer": "https://fund.eastmoney.com/",
}
TIMEOUT = 30

# 缓存最多信任多少天（净值滞后一天正常，留足假期余量；超期则重新抓取）
CACHE_MAX_AGE_DAYS = 10

_CODE_RE = re.compile(r"^\d{6}$")


class FundError(RuntimeError):
    """基金净值抓取/解析失败。"""


# --------------------------------------------------------------------------- 代码
def normalize_code(symbol) -> str:
    """基金代码规范化：去空白、去 ``sh``/``sz`` 前缀、补足 6 位。"""
    s = str(symbol).strip().lower()
    for pre in ("sh", "sz", "of"):
        if s.startswith(pre):
            s = s[len(pre):]
            break
    s = s.strip()
    return s.zfill(6) if s.isdigit() else s


def _check_code(code: str) -> str:
    if not _CODE_RE.match(code):
        raise FundError(f"基金代码应为 6 位数字，收到 {code!r}")
    return code


# --------------------------------------------------------------------------- 解析
def _fetch_text(code: str) -> str:
    url = PINGZHONG_URL.format(code=code)
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    except Exception as e:  # noqa: BLE001
        raise FundError(f"请求失败 {url}：{type(e).__name__}: {e}") from e
    if r.status_code != 200:
        raise FundError(f"{code} 返回 HTTP {r.status_code}")
    return r.content.decode("utf-8", "replace")


# 东方财富的 JS 里变量之间夹着 JSDoc 注释（`...}];/*累计净值走势*/var X=...`），
# 注释里含 `*`、`/` 等会干扰边界判断的字符，所以先整体剥掉注释再取值。
_JS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
_JS_LINE_COMMENT_RE = re.compile(r"(?m)//[^\n]*$")


def _strip_js_comments(text: str) -> str:
    """去掉 ``/* ... */`` 与 ``//`` 注释。

    只在**已完整取到的值字符串**上使用（值里的 ``//`` 会被误伤，但净值数据里没有），
    这是为了不让注释里的 ``var*`` 之类内容干扰取值边界。
    """
    return _JS_LINE_COMMENT_RE.sub("", _JS_COMMENT_RE.sub("", text))


def _js_var(text: str, name: str) -> str | None:
    """取 ``var <name> = <值>`` 的值部分。

    两处坑（都踩过）：
    1. **不能**用 ``(.*?);`` 匹配到分号：基金名/分红说明等字符串里可能出现分号，
       会在字符串中间截断，`json.loads` 报 "Extra data" / "Unterminated string"。
    2. **不能**用 ``(?=var\\s|$)`` 作边界：变量之间夹着注释 ``...}];/*累计净值走势*/var X=``，
       而 ``\\s`` 会匹配注释里的 ``*``，于是 ``var*`` 被误判成下一个变量声明，
       取值多带一段尾注释。这里先把注释剥掉，再用 ``\\bvar\\s+标识符`` 作边界。
    """
    m = re.search(rf"var\s+{re.escape(name)}\s*=\s*(.*?)(?=\bvar\s+[A-Za-z_$]|$)",
                  _strip_js_comments(text), re.S)
    if not m:
        return None
    return m.group(1).strip().rstrip(";").strip()


def _decode_detail(raw: str) -> str:
    """``unitMoney`` 字段可能是 JSON 字符串或空串；去掉 HTML 片段。"""
    if not raw:
        return ""
    try:
        s = json.loads(raw)
    except (ValueError, TypeError):
        s = raw
    s = re.sub(r"<[^>]+>", "", str(s))
    s = s.replace("&nbsp;", " ").replace("&amp;", "&")
    return " ".join(s.split())


def parse_pingzhong(text: str) -> tuple[str, pd.DataFrame]:
    """解析 ``pingzhongdata`` 的 JS 文本。

    返回 ``(基金名称, DataFrame[date, nav_unit, nav_cum, pct_chg, detail])``，
    日期升序去重；``nav_cum`` 缺值回落为 ``nav_unit``。
    """
    raw_name = _js_var(text, "fS_name")
    name = json.loads(raw_name) if raw_name else ""

    unit_raw = _js_var(text, "Data_netWorthTrend")
    if not unit_raw or unit_raw in ("null", "[]"):
        raise FundError("未取到净值数据（基金代码可能不存在）")
    try:
        unit = json.loads(unit_raw)
    except ValueError as e:
        raise FundError(f"单位净值解析失败：{e}") from e

    cum_raw = _js_var(text, "Data_ACWorthTrend")
    cum: list = []
    if cum_raw and cum_raw not in ("null", "[]"):
        try:
            cum = json.loads(cum_raw)
        except ValueError:
            cum = []

    # 累计净值：按本地日期对齐（时间戳 = 本地 00:00）
    cum_by_date: dict[str, float] = {}
    for point in cum:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            continue
        d = _to_date(point[0])
        v = _to_float(point[1])
        if d and v is not None:
            cum_by_date[d] = v

    rows: list[dict] = []
    for point in unit:
        if not isinstance(point, dict):
            continue
        d = _to_date(point.get("x"))
        if not d:
            continue
        nav_unit = _to_float(point.get("y"))
        nav_cum = cum_by_date.get(d)
        close = nav_cum if nav_cum is not None else nav_unit
        if close is None:
            continue
        rows.append({
            "date": d,
            "nav_unit": nav_unit,
            "nav_cum": nav_cum,
            "close": close,                      # 落盘口径 = 累计净值
            "pct_chg": _to_float(point.get("equityReturn")),
            "detail": _decode_detail(point.get("unitMoney")),
        })
    if not rows:
        raise FundError("净值数据为空")
    df = (pd.DataFrame(rows)
          .drop_duplicates("date", keep="last")
          .sort_values("date")
          .reset_index(drop=True))
    return (name or ""), df


def _to_float(x) -> float | None:
    if x is None or x == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _to_date(x) -> str | None:
    """毫秒时间戳 → ``YYYY-MM-DD``（**本地时间**，不能用 utcfromtimestamp）。"""
    v = _to_float(x)
    if v is None:
        return None
    try:
        return datetime.fromtimestamp(v / 1000.0).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return None


# --------------------------------------------------------------------------- 抓取
def fetch_history(item) -> tuple[str, pd.DataFrame]:
    """抓某只基金的**全量**历史净值（一次请求）。返回 ``(名称, [date, close])``。"""
    code = _check_code(normalize_code(item.symbol))
    name, detail = parse_pingzhong(_fetch_text(code))
    out = detail[["date", "close"]].copy()
    out["date"] = pd.to_datetime(out["date"])
    return (name or item.name), out


def fetch_detail(item) -> tuple[str, pd.DataFrame]:
    """同 :func:`fetch_history`，但保留单位净值/累计净值/日涨幅/分红说明。"""
    code = _check_code(normalize_code(item.symbol))
    name, detail = parse_pingzhong(_fetch_text(code))
    detail = detail.copy()
    detail["date"] = pd.to_datetime(detail["date"])
    return (name or item.name), detail


# --------------------------------------------------------------------------- 缓存
def _cache_path(key: str) -> Path:
    return FUND_CACHE_DIR / f"{key}.json"


def _today() -> pd.Timestamp:
    return pd.Timestamp.today().normalize()


def load_cache(key: str) -> dict | None:
    """读本地缓存；不存在或损坏 → None。"""
    path = _cache_path(key)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    if not isinstance(data, dict) or not data.get("nav"):
        return None
    return data


def cache_is_fresh(cache: dict | None, max_age_days: int = CACHE_MAX_AGE_DAYS) -> bool:
    """缓存最新净值是否足够新（净值滞后一天属正常，故留出余量）。"""
    if not cache or not cache.get("nav"):
        return False
    last = cache["nav"][-1].get("date")
    if not last:
        return False
    try:
        lag = (_today() - pd.Timestamp(last)).days
    except (ValueError, TypeError):
        return False
    return lag <= max_age_days


def save_cache(key: str, name: str, detail: pd.DataFrame) -> dict:
    """把全量净值写入 ``data/fund_cache/{key}.json``。"""
    FUND_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    d = detail.copy()
    d["date"] = pd.to_datetime(d["date"]).dt.strftime("%Y-%m-%d")
    data = {
        "key": key,
        "code": key.rsplit("_", 1)[-1],
        "name": name,
        "source": SOURCE,
        "caliber": CALIBER,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "first_date": str(d["date"].iloc[0]),
        "last_date": str(d["date"].iloc[-1]),
        "n": int(len(d)),
        "nav": [
            {
                "date": r["date"],
                "close": float(r["close"]),
                "unit": None if pd.isna(r["nav_unit"]) else float(r["nav_unit"]),
                "pct": None if pd.isna(r["pct_chg"]) else float(r["pct_chg"]),
                "detail": r["detail"] or None,
            }
            for r in d.to_dict("records")
        ],
    }
    _cache_path(key).write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                encoding="utf-8")
    return data


def cache_history(cache: dict) -> pd.DataFrame:
    """缓存 → ``DataFrame[date, close]``（日期升序）。"""
    df = pd.DataFrame([{"date": r["date"], "close": r["close"]} for r in cache["nav"]])
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)


def load_or_fetch(item, force: bool = False) -> tuple[str, pd.DataFrame]:
    """缓存优先：命中且新鲜 → 读缓存；否则抓全量并写缓存。

    返回 ``(名称, DataFrame[date, close])``（``close`` = 累计净值）。
    """
    key = item.key
    if not force:
        cache = load_cache(key)
        if cache_is_fresh(cache):
            return str(cache.get("name") or item.name), cache_history(cache)
    name, detail = fetch_detail(item)
    save_cache(key, name, detail)
    return name, detail[["date", "close"]].reset_index(drop=True)


# --------------------------------------------------------------------------- 当日
def daily_snapshot(item) -> dict:
    """取某基金最新净值，返回与 ``quote_fetch.snapshot()`` **同构**的一行。

    保证 ``daily_close`` 后续逻辑（列名、日期、去重）无需改动。
    """
    name, detail = fetch_detail(item)
    row = detail.iloc[-1]
    close = float(row["close"])                      # 累计净值
    unit = None if pd.isna(row["nav_unit"]) else float(row["nav_unit"])
    pct = None if pd.isna(row["pct_chg"]) else float(row["pct_chg"])
    prev = round(unit / (1.0 + pct / 100.0), 4) if (unit and pct is not None and pct != -100) else None

    latest = {
        # key 必须与 quote_fetch.snapshot() 同构：daily_close 统一按
        # `{market}_{type}_{symbol}` 取报价（见 test_quote_key_collision_offline.py）
        "key": f"{item.market}_{item.type}_{item.symbol}",
        "symbol": str(item.symbol),
        "name": name or item.name,
        "market": item.market,
        "type": item.type,                            # "fund"
        "close": close,                               # 累计净值（落盘口径）
        "prev_close": prev,                           # 单位净值口径的昨值
        "pct_chg": None if pct is None else pct / 100.0,
        "date": row["date"].strftime("%Y-%m-%d"),
        "source": SOURCE,
        "unit_nav": unit,                             # 单位净值（仅展示/留档）
        "nav_date_lag_note": "场外基金净值 T 日盘后公布，滞后一天属正常",
    }
    # 顺手把全量写进缓存，省掉 daily_close 里的二次抓取
    try:
        save_cache(item.key, name or item.name, detail)
    except OSError:
        pass
    return latest


# --------------------------------------------------------------------------- CLI
def main() -> int:
    ap = argparse.ArgumentParser(description="抓取开放式基金净值（东方财富，累计净值口径）")
    ap.add_argument("--code", required=True, help="6 位基金代码，如 002910")
    ap.add_argument("--tail", type=int, default=8, help="打印最近 N 个交易日（默认 8）")
    args = ap.parse_args()

    class _Item:
        symbol = args.code
        name = args.code
        market = "cn"
        type = "fund"
        key = f"cn_fund_{normalize_code(args.code)}"

    name, detail = fetch_detail(_Item)
    print(f"{name}（{args.code}）共 {len(detail)} 个交易日  "
          f"{detail['date'].iloc[0].date()} ~ {detail['date'].iloc[-1].date()}")
    print(f"落盘口径（close）= {CALIBER}\n")
    show = detail.tail(args.tail).copy()
    show["date"] = show["date"].dt.strftime("%Y-%m-%d")
    with pd.option_context("display.width", 200):
        print(show[["date", "nav_unit", "close", "pct_chg", "detail"]].to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
