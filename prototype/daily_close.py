"""日更脚本（报价端点版）：每天 append 当日收盘价 → 算累计收益。

设计（契合「每天一次、低时效、~70 个标的」）：
- 数据源：新浪/腾讯报价端点（复用 prototype.quote_fetch），**无 key**；
- 清单：项目根 holdings.yaml（holdings + benchmarks）；
- 存储：**增量 append** 当日收盘价到 data/prices/{key}.csv（不是每天全量重取）；
- 收益：cum_return(t) = close(t)/close(start) - 1，起点取 start_date（无则取首个记录日）；
- 输出：data/prices/*.csv、data/returns/*.csv、data/meta.json —— 与现有 Streamlit 看板兼容。

⚠️ 口径：**价格收益**（未复权）。当日报价未含分红/除权；若要「含分红再投」，
   需另配除权修正（后续再做）。

用法：
    python -m prototype.daily_close
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:  # 避免重复包装 sys.stdout（会触发底层 buffer 被关闭）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from prototype import quote_fetch as qf  # noqa: E402
from src.config import (  # noqa: E402
    META_PATH,
    PRICES_DIR,
    RETURNS_DIR,
    load_benchmarks,
    load_holdings,
)
from src.returns import (  # noqa: E402
    annualized_return,
    calmar_ratio,
    cumulative_return,
    max_drawdown,
)

CALIBER = "price_return"   # 价格收益（不含分红再投）


def _load_history(path: Path) -> pd.DataFrame:
    if path.exists():
        df = pd.read_csv(path, parse_dates=["date"])
        return df[["date", "close"]].sort_values("date").reset_index(drop=True)
    return pd.DataFrame({"date": pd.to_datetime([]), "close": pd.Series(dtype=float)})


def _append(hist: pd.DataFrame, date, close) -> tuple[pd.DataFrame, bool]:
    """追加一行（同一交易日去重）。返回 (新历史, 是否真的新增了行)。"""
    d = pd.Timestamp(date)
    if (hist["date"] == d).any():
        return hist, False
    row = pd.DataFrame({"date": [d], "close": [float(close)]})
    out = pd.concat([hist, row], ignore_index=True).sort_values("date").reset_index(drop=True)
    return out, True


def _safe(x) -> float | None:
    try:
        return None if x != x else float(x)   # NaN -> None
    except (TypeError, ValueError):
        return None


def run() -> dict:
    PRICES_DIR.mkdir(parents=True, exist_ok=True)
    RETURNS_DIR.mkdir(parents=True, exist_ok=True)

    items = load_holdings() + load_benchmarks()
    snap = qf.snapshot(items)                  # 一次批量拿全部标的的当日收盘价
    quotes = {(r["market"], r["symbol"]): r for _, r in snap.iterrows()}

    meta: dict = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "base_currency": "CNY",
        "caliber": CALIBER,
        "source": "sina/tencent",
        "items": {},
    }

    for it in items:
        key = it.key
        q = quotes.get((it.market, it.symbol))
        try:
            if q is None or pd.isna(q.get("close")) or not q.get("date"):
                raise ValueError("本次未取到报价")
            path = PRICES_DIR / f"{key}.csv"
            hist = _load_history(path)
            hist, added = _append(hist, q["date"], q["close"])
            hist.to_csv(path, index=False)

            start = it.start_date or str(hist["date"].min().date())
            ret = cumulative_return(hist, start)
            ret.to_csv(RETURNS_DIR / f"{key}.csv", index=False)

            meta["items"][key] = {
                "name": it.name, "symbol": it.symbol,
                "market": it.market, "type": it.type,
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
                "appended": bool(added),
                "caliber": CALIBER,
                "status": "ok",
            }
            print(f"[ok ] {key:12s} {it.name:8s} {'+1 新行' if added else '无新行(已是最新)'} "
                  f"close={q['close']} n={len(ret)} cum={ret['cum_return'].iloc[-1]:+.2%}")
        except Exception as e:  # noqa: BLE001
            meta["items"][key] = {
                "name": it.name, "symbol": it.symbol,
                "market": it.market, "type": it.type,
                "status": "error", "error": f"{type(e).__name__}: {e}",
            }
            print(f"[ERR] {key:12s} {it.name:8s} -> {type(e).__name__}: {e}")

    META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    # 自定义等权指数（依赖刚更新的 data/prices）
    try:
        from prototype import index_build
        index_build.run()
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 自定义指数构建失败：{e}")

    ok = sum(1 for v in meta["items"].values() if v.get("status") == "ok")
    print(f"\n完成：{ok}/{len(items)} 更新成功 -> {META_PATH.relative_to(META_PATH.parent.parent)}")
    return meta


if __name__ == "__main__":
    run()
