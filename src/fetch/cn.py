"""A股 / 境内行情抓取。

- stock  : 个股日线，Tushare ``daily`` × ``adj_factor`` = **后复权**（含分红/送转）
- index  : 指数日线，Tushare ``index_daily``（不需复权）
- etf    : 场内基金/ETF 日线，akshare 后复权 adjust="hfq"
- bond   : 债券优先用代表性 ETF（走 etf 接口，后复权）

注：Tushare 免费 token 对基金接口无权限，故 ETF/债券仍用 akshare；
个股/指数迁到 Tushare 以规避 akshare 偶发的连接重置。
"""
from __future__ import annotations

import time

import akshare as ak
import pandas as pd

from .base import normalize

_RENAME = {"日期": "date", "收盘": "close"}


def _retry(fn, tries: int = 3, wait: float = 2.0):
    """简单指数退避重试，应对公开接口偶发失败。"""
    last: Exception | None = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(wait * (i + 1))
    raise last  # type: ignore[misc]


def _fetch_akshare(symbol: str, kind: str, start_ymd: str, end_ymd: str) -> pd.DataFrame:
    """ETF / 债券：akshare 场内基金接口（含 ETF 型债券），后复权。"""
    df = _retry(lambda: ak.fund_etf_hist_em(
        symbol=symbol, period="daily",
        start_date=start_ymd, end_date=end_ymd, adjust="hfq"))
    return normalize(df.rename(columns=_RENAME))


def fetch_daily(symbol: str, kind: str, start: str, end: str | None = None) -> pd.DataFrame:
    start_ymd = pd.Timestamp(start).strftime("%Y%m%d")
    end_ymd = (
        pd.Timestamp(end).strftime("%Y%m%d") if end
        else pd.Timestamp.today().strftime("%Y%m%d")
    )

    if kind in ("stock", "index"):
        from . import tushare_mcp
        return tushare_mcp.fetch_daily(symbol, kind, start, end)

    # etf / bond —— 场内基金接口（含 ETF 型债券），后复权
    return _fetch_akshare(symbol, kind, start_ymd, end_ymd)

