"""美股 / 境外行情抓取（yfinance）。

auto_adjust=True 返回拆股+分红调整后的价格，等价于「分红再投」。
指数（如 ^GSPC）同样适用。
"""
from __future__ import annotations

import time

import pandas as pd
import yfinance as yf

from .base import normalize


def _retry(fn, tries: int = 3, wait: float = 2.0):
    last: Exception | None = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(wait * (i + 1))
    raise last  # type: ignore[misc]


def fetch_daily(symbol: str, kind: str, start: str, end: str | None = None) -> pd.DataFrame:
    def _download() -> pd.DataFrame:
        return yf.Ticker(symbol).history(
            start=str(start),
            end=str(pd.Timestamp(end) + pd.Timedelta(days=1)) if end else None,
            auto_adjust=True,
        )

    df = _retry(_download)
    if df is None or df.empty:
        raise ValueError(f"yfinance 未返回 {symbol} 的数据")

    df = df.reset_index()
    date_col = "Date" if "Date" in df.columns else "Datetime"
    df = df[[date_col, "Close"]].rename(columns={date_col: "date", "Close": "close"})
    return normalize(df)
