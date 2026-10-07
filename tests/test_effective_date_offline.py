"""调仓生效日判定的离线测试（不联网）。运行： python -m pytest tests/ -q

核心规则：
  - **crypto（7×24）**：取提交后最近的 UTC 00:00，成交在**收盘于该时刻**的日线上
    → 生效日标签 = 该 UTC 00:00 所在日 − 1 天
  - **其它品种**：取提交后（含当日）最近的市场收盘
    → 交易日收盘前提交 = 当日；否则 = 下一交易日

最关键的性质（★★）：**成交时刻必须严格晚于提交时刻**。
这是"投资人不可能用到提交时还看不到的价"的形式化保证。
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from src.effective_date import (EffectiveDateError, TradingCalendar,  # noqa: E402
                                effective_date, fill_instant_utc)

UTC = timezone.utc
# 2026-10-05 是周一；下面这份日历标记了几个"交易日"
KNOWN = [date(2026, 10, 1), date(2026, 10, 2),          # 周四、周五
         date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7),   # 周一~周三
         date(2026, 10, 8), date(2026, 10, 9)]          # 周四、周五
CAL = TradingCalendar(KNOWN)


def utc(y, m, d, hh=0, mm=0, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=UTC)


def bj(y, m, d, hh=0, mm=0, ss=0):
    """北京时间 → 带时区的 datetime（UTC+8）。"""
    return datetime(y, m, d, hh, mm, ss,
                    tzinfo=timezone(timedelta(hours=8)))


# --------------------------------------------------------------------- crypto
@pytest.mark.parametrize("submitted,label,why", [
    # 提交在 UTC 周一之内 → 最近零点是周二 00:00 → 收盘于该时刻的是**周一**那根
    (utc(2026, 10, 5, 0, 0, 1), "2026-10-05", "刚过零点"),
    (utc(2026, 10, 5, 10, 0), "2026-10-05", "UTC 白天"),
    (utc(2026, 10, 5, 23, 59), "2026-10-05", "差 1 分钟到零点"),
    # 恰好整点 → 取下一个零点 → 成交在 10-05 那根（收盘于 10-06 00:00）
    (utc(2026, 10, 5, 0, 0, 0), "2026-10-05", "★ 恰好 UTC 零点"),
    # 周末照常（crypto 不休息）
    (utc(2026, 10, 3, 12, 0), "2026-10-03", "周六"),
    (utc(2026, 10, 4, 12, 0), "2026-10-04", "周日"),
    # 跨月
    (utc(2026, 10, 31, 23, 30), "2026-10-31", "跨月：10-31 深夜"),
    (utc(2026, 12, 31, 23, 30), "2026-12-31", "跨年：12-31 深夜"),
])
def test_crypto_effective_date(submitted, label, why):
    assert effective_date(submitted, "us", "crypto", CAL) == label, why


def test_crypto_ignores_trading_calendar():
    """crypto 不受交易日历影响：周末/节假日照样次日零点成交。"""
    empty = TradingCalendar([])          # 空日历
    assert effective_date(utc(2026, 10, 3, 12, 0), "us", "crypto", empty) == "2026-10-03"
    assert effective_date(utc(2026, 10, 4, 12, 0), "us", "crypto", empty) == "2026-10-04"


def test_crypto_beijing_timezone_boundary():
    """北京 08:00 正好是 UTC 00:00 —— 日切两侧必须给出不同结果。"""
    # 北京 10-05 07:59 = UTC 10-04 23:59 → 最近零点是 10-05 00:00 → 标签 10-04
    assert effective_date(bj(2026, 10, 5, 7, 59), "us", "crypto", CAL) == "2026-10-04"
    # 北京 10-05 08:00 = UTC 10-05 00:00 整点 → 下一个零点 10-06 → 标签 10-05
    assert effective_date(bj(2026, 10, 5, 8, 0), "us", "crypto", CAL) == "2026-10-05"
    # 北京 10-05 08:01 = UTC 10-05 00:01 → 标签 10-05
    assert effective_date(bj(2026, 10, 5, 8, 1), "us", "crypto", CAL) == "2026-10-05"


# --------------------------------------------------------------------- 股票等
@pytest.mark.parametrize("submitted,market,label,why", [
    # A股：收盘 15:00 北京时间
    (bj(2026, 10, 5, 10, 30), "cn", "2026-10-05", "盘中提交 → 当日收盘"),
    (bj(2026, 10, 5, 14, 59), "cn", "2026-10-05", "收盘前 1 分钟"),
    (bj(2026, 10, 5, 15, 0), "cn", "2026-10-06", "★ 恰好收盘 → 顺延"),
    (bj(2026, 10, 5, 16, 0), "cn", "2026-10-06", "盘后 → 次一交易日"),
    (bj(2026, 10, 9, 20, 0), "cn", "2026-10-12", "周五盘后 → 下周一(兜底)"),
    (bj(2026, 10, 3, 12, 0), "cn", "2026-10-05", "周六 → 下周一"),
    # 港股：收盘 16:00 香港时间
    (bj(2026, 10, 5, 15, 30), "hk", "2026-10-05", "港股 15:30 仍在盘中"),
    (bj(2026, 10, 5, 16, 30), "hk", "2026-10-06", "港股盘后"),
    # 美股：收盘 16:00 美东。北京白天提交时，纽约还是**前一天晚上**，
    #       前一天（10-04 周日）非交易日 → 顺延到 10-05 收盘（= 北京 10-06 04:00）
    (bj(2026, 10, 5, 10, 0), "us", "2026-10-05", "北京白天=纽约前夜，顺延到 10-05"),
])
def test_equity_effective_date(submitted, market, label, why):
    assert effective_date(submitted, market, "stock", CAL) == label, why


def test_fund_uses_same_equity_rule():
    """场外基金走 A股口径（净值 T 日盘后公布，滞后一天已由数据层处理）。"""
    assert effective_date(bj(2026, 10, 5, 10, 0), "cn", "fund", CAL) == "2026-10-05"
    assert effective_date(bj(2026, 10, 5, 20, 0), "cn", "fund", CAL) == "2026-10-06"


def test_index_and_etf_and_futures_use_equity_rule():
    for t in ("etf", "bond", "index", "futures"):
        assert effective_date(bj(2026, 10, 5, 10, 0), "cn", t, CAL) == "2026-10-05"
        assert effective_date(bj(2026, 10, 5, 20, 0), "cn", t, CAL) == "2026-10-06"


def test_calendar_interior_gap_is_holiday():
    """已知范围内、但不在日历里的工作日 → 视为非交易日（节假日）。

    构造：已知 10-05(一) / 10-06(二) / 10-08(四)，**缺 10-07(三)** → 那天是节假日。
    """
    cal = TradingCalendar([date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 8)])
    assert cal.is_trading_day(date(2026, 10, 7)) is False        # 内部空洞 = 节假日
    assert cal.next_trading_day(date(2026, 10, 6)) == date(2026, 10, 8)


def test_calendar_unknown_future_falls_back_to_weekday():
    """超过已知范围（数据还没抓到）→ 工作日兜底，周末仍算休市。"""
    cal = TradingCalendar([date(2026, 10, 5)])
    assert cal.is_trading_day(date(2026, 10, 20)) is True        # 周二
    assert cal.is_trading_day(date(2026, 10, 24)) is False       # 周六
    assert cal.is_trading_day(date(2026, 10, 25)) is False       # 周日


def test_unknown_market_raises():
    with pytest.raises(EffectiveDateError, match="未知 market"):
        effective_date(bj(2026, 10, 5, 10, 0), "jp", "stock", CAL)


def test_naive_datetime_rejected():
    with pytest.raises(EffectiveDateError, match="必须带时区"):
        effective_date(datetime(2026, 10, 5, 10, 0), "cn", "stock", CAL)


# --------------------------------------------------------------------- ★★ 属性
def test_fill_is_strictly_after_submission_crypto():
    """★★ crypto：**任何**提交时刻，成交时刻都必须严格晚于它（后门为零）。"""
    base = utc(2026, 10, 5)
    for minutes in range(0, 24 * 60, 7):          # 全天每 7 分钟取一个
        t = base + timedelta(minutes=minutes)
        fill = fill_instant_utc(t, "us", "crypto")
        assert fill > t, f"提交 {t} 的成交时刻 {fill} 不晚于提交时刻！"


def test_fill_is_strictly_after_submission_equity():
    """★★ 股票类：成交时刻同样必须严格晚于提交时刻。"""
    for market, hours in (("cn", 8), ("hk", 8), ("us", 8)):
        base = datetime(2026, 10, 5, tzinfo=timezone(timedelta(hours=hours)))
        for minutes in range(0, 24 * 60, 11):
            t = base + timedelta(minutes=minutes)
            fill = fill_instant_utc(t, market, "stock", CAL)
            assert fill > t, f"{market}: 提交 {t} 的成交 {fill} 不晚于提交"


def test_crypto_fill_instant_is_utc_midnight():
    fill = fill_instant_utc(utc(2026, 10, 5, 10, 0), "us", "crypto")
    assert fill == utc(2026, 10, 6, 0, 0)              # 标签 10-05 的收盘 = 10-06 00:00 UTC


def test_equity_fill_instant_is_market_close():
    # A股标签 10-05 的收盘 = 北京 10-05 15:00 = UTC 07:00
    fill = fill_instant_utc(bj(2026, 10, 5, 10, 0), "cn", "stock", CAL)
    assert fill == utc(2026, 10, 5, 7, 0)
    # 港股标签 10-05 的收盘 = 香港 16:00 = UTC 08:00
    fill = fill_instant_utc(bj(2026, 10, 5, 15, 30), "hk", "stock", CAL)
    assert fill == utc(2026, 10, 5, 8, 0)


def test_effective_date_accepts_various_input_types():
    """兼容 pandas Timestamp / ISO 字符串（collector 会从 JSON 读时间）。"""
    import pandas as pd
    x = pd.Timestamp("2026-10-05T10:00:00Z")
    assert effective_date(x, "us", "crypto", CAL) == effective_date(
        utc(2026, 10, 5, 10, 0), "us", "crypto", CAL)
