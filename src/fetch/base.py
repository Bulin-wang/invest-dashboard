"""抓取层统一接口与数据规整。"""
from __future__ import annotations

import pandas as pd


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    """统一为 [date, close]，去重、排序、去空、去时区。"""
    if df is None or df.empty:
        raise ValueError("抓取结果为空")
    out = df[["date", "close"]].copy()
    s = pd.to_datetime(out["date"])
    try:  # 去掉时区（yfinance 可能返回 tz-aware）
        s = s.dt.tz_localize(None)
    except TypeError:
        pass
    out["date"] = s
    out["close"] = pd.to_numeric(out["close"], errors="coerce")
    out = (
        out.dropna(subset=["close"])
        .drop_duplicates("date")
        .sort_values("date")
        .reset_index(drop=True)
    )
    if out.empty:
        raise ValueError("规整后无有效收盘价")
    return out


def fetch_daily(symbol: str, market: str, kind: str,
                start: str, end: str | None = None) -> pd.DataFrame:
    """统一入口：返回 [date, close]。

    market: cn / us；kind: stock / etf / bond / index
    """
    market = market.lower()
    if market == "cn":
        from . import cn
        return cn.fetch_daily(symbol, kind, start, end)
    if market == "us":
        from . import us
        return us.fetch_daily(symbol, kind, start, end)
    raise ValueError(f"未知 market={market!r}")
