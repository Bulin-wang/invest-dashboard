"""调仓生效日判定 —— 把「提交时刻」翻译成「在哪一天的收盘价成交」。

规则（按品种分两类）
--------------------
**A. 有明确收盘时刻的市场**（A股 / 港股 / 美股 / 指数 / 场外基金 / 债券 / ETF）
    取**提交之后（含当日）最近的那个收盘**：
      - 提交发生在交易日 T 的收盘**之前** → 生效日 = T（当日收盘成交）
      - 否则（已收盘 / 非交易日）           → 生效日 = T 之后第一个交易日

**B. crypto（7×24，没有"收盘时刻"）**
    取**提交之后最近的一个 UTC 00:00**，成交在那根**收盘于该时刻**的日线上：
      生效日标签 = (提交后最近的 UTC 00:00) 所在 UTC 日 − 1 天

    ⚠️ 为什么必须这样，而不是"提交日的 UTC 收盘"：
    币安日线的时间戳是 **UTC 00:00**，所以标签 `D` 的收盘价产生在 **UTC D 日
    23:59:59**（= 北京 D+1 日 07:59:59）。若按"提交日"取价，北京周三 14:00 提交
    会用到**北京周四 08:00** 的价 —— 一个提交时还看不到的价，等于事后诸葛。
    按"下一个 UTC 0"取，成交时刻**必然严格晚于提交时刻**，后门归零。
    详见 tests/test_effective_date_offline.py 的属性断言。

为什么 US 也归到 A 类（而不是"提交日的收盘"）
--------------------------------------------
"盘中提交→当日收盘"这个直觉隐含了「收盘就在几小时后」。A股 15:00 / 港股 16:00
成立；但美股在北京白天提交时，**当日美股收盘在十几个小时之后**（北京次日凌晨），
那个价在提交时同样不可知，且提交时刻并不在"美股盘中"。
所以对美股，A 类规则给出的也是**未来的**收盘——与 crypto 那条原则一致：
> 只用提交之后才发生的收盘价。
对 A股/港股/场外基金，这条规则与"盘中提交→当日收盘"**完全等价**。
"""
from __future__ import annotations

from datetime import date as _date
from datetime import datetime, time as _time
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

__all__ = [
    "EffectiveDateError",
    "TradingCalendar",
    "effective_date",
    "fill_instant_utc",
    "is_crypto_type",
]

# 各市场的收盘时刻（当地时区）与市场标识
# 注意：美股用美东时区，且**不考虑夏令时切换的边界精度**——收盘定在 16:00 ET，
#       由 zoneinfo 负责 EST/EDT 换算，足够本用途。
EQUITY_CLOSE = {
    "cn": (_time(15, 0), "Asia/Shanghai"),
    "hk": (_time(16, 0), "Asia/Hong_Kong"),
    "us": (_time(16, 0), "America/New_York"),
}
CRYPTO_TYPE = "crypto"
UTC = timezone.utc


class EffectiveDateError(ValueError):
    """生效日判定失败（参数非法或日历为空且无法兜底）。"""


# --------------------------------------------------------------------------- 日历
class TradingCalendar:
    """交易日历：优先用**已知交易日**（来自 data/prices 的日期并集），
    超出已知范围时按"工作日 = 交易日"兜底。

    兜底会漏掉节假日，但只会用在"数据还没抓到的未来日期"上——那些日子到了之后
    自然会被真实数据补上。判定结果最多差一个交易日，且**偏保守**（宁可晚一天）。
     """

    def __init__(self, known_dates=None, today: _date | None = None):
        self.known: set[_date] = set()
        for d in known_dates or ():
            self.known.add(d if isinstance(d, _date) else _to_date(d))
        # 已知范围之外用工作日兜底
        self.max_known = max(self.known) if self.known else None

    def is_trading_day(self, d: _date) -> bool:
        if d in self.known:
            return True
        # 落在已知范围内但不在集合里 → 确定是非交易日（如周末/节假日）
        if self.max_known is not None and d <= self.max_known:
            return False
        return d.weekday() < 5          # 未知的未来：工作日兜底

    def next_trading_day(self, d: _date) -> _date:
        """严格晚于 ``d`` 的第一个交易日（最多找 30 天）。"""
        for i in range(1, 31):
            cand = d + timedelta(days=i)
            if self.is_trading_day(cand):
                return cand
        raise EffectiveDateError(f"从 {d} 往后 30 天内找不到交易日（日历异常）")


def _to_date(x) -> _date:
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, _date):
        return x
    import pandas as pd  # 延迟导入：本模块不依赖 pandas 也能用
    return pd.Timestamp(x).date()


# --------------------------------------------------------------------------- 主体
def is_crypto_type(type_: str) -> bool:
    return str(type_).strip().lower() == CRYPTO_TYPE


def _ensure_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise EffectiveDateError(
            f"提交时刻必须带时区（收到 naive datetime: {dt!r}）；"
            "GitHub Issue 的 created_at 是带 Z 的 UTC 时间，直接用即可")
    return dt


def _crypto_boundary(submitted_at: datetime) -> datetime:
    """提交后**最近的一个 UTC 00:00**（严格晚于提交时刻）。

    注意两种情形刚好用同一个式子：``t`` 落在 00:00 整点时，"下一个零点"是次日 00:00；
    否则是当天的下一个零点。两者都等于 ``floor(t, 'D') + 1 天``。
    """
    t = submitted_at.astimezone(UTC)
    midnight = t.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight + timedelta(days=1)


def effective_date(submitted_at: datetime, market: str, type_: str,
                   calendar: TradingCalendar | None = None) -> str:
    """提交时刻 → 生效日（``YYYY-MM-DD``，即 ``switches.yaml`` 的 ``date``）。

    生效日 = **在该日收盘价成交**；调仓日收盘切换，当日算旧标的、次日起算新标的
    （沿用项目既有口径）。
    """
    t = _ensure_aware(submitted_at)
    mkt = str(market or "").strip().lower()

    # --- crypto：下一个 UTC 0 ---
    if is_crypto_type(type_):
        boundary = _crypto_boundary(t)
        return (boundary - timedelta(days=1)).date().isoformat()

    # --- 其余品种：最近的市场收盘 ---
    if mkt not in EQUITY_CLOSE:
        raise EffectiveDateError(
            f"未知 market={market!r}（已知：{sorted(EQUITY_CLOSE)}）；"
            f"type={type_!r} 需要能确定收盘时刻")
    close_t, tzname = EQUITY_CLOSE[mkt]
    close_dt = t.astimezone(ZoneInfo(tzname))
    d = close_dt.date()
    cal = calendar or TradingCalendar()

    if cal.is_trading_day(d) and close_dt.time() < close_t:
        return d.isoformat()                       # 当日收盘成交
    return cal.next_trading_day(d).isoformat()     # 下一交易日收盘成交


def fill_instant_utc(submitted_at: datetime, market: str, type_: str,
                     calendar: TradingCalendar | None = None) -> datetime:
    """生效日对应的**实际成交时刻**（UTC）。

    这是 ``effective_date`` 的"物理含义"版本，测试用它断言
    **成交时刻严格晚于提交时刻**（后头门为零）。
    """
    t = _ensure_aware(submitted_at)
    d = _to_date(effective_date(submitted_at, market, type_, calendar))

    if is_crypto_type(type_):
        # 标签 D 的 crypto 日线，收盘于 UTC D+1 00:00
        return datetime.combine(d + timedelta(days=1), _time(0, 0), tzinfo=UTC)

    close_t, tzname = EQUITY_CLOSE[str(market).strip().lower()]
    return datetime.combine(d, close_t, tzinfo=ZoneInfo(tzname)).astimezone(UTC)
