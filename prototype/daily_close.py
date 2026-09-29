"""日更脚本（报价端点版）：每天 append 当日收盘价 → 算标的收益 + 投资者组合收益。

设计（契合「~60 位投资者、每天一次、低时效」）：
- 数据源：新浪/腾讯报价端点（复用 prototype.quote_fetch），**无 key**；
- 清单：项目根 investors.yaml（每人**初始持仓** + 基准）；调仓流水 switches.yaml（append-only）；
- 组合：按每位投资者的**持仓路径**（全仓切换）合成投资者的累计收益
  （`src.portfolio.portfolio_nav`；调仓日收盘切换，当日算旧标的，无费用 / 不计汇率）；
- 存储：增量 append 当日收盘价到 `data/prices/{key}.csv`；
  投资者组合收益写到 `data/portfolios/{Investor}.csv`；
- 输出：data/prices/*.csv、data/returns/*.csv、data/portfolios/*.csv、data/index/*.csv、data/meta.json。

⚠️ 口径：**价格收益**（未复权），不含分红/除权，不计汇率。

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
    PORTFOLIOS_DIR,
    PRICES_DIR,
    RETURNS_DIR,
    Item,
    investor_segments,
    load_investor_benchmarks,
    load_investor_config,
    load_investors,
    load_switches,
    portfolio_id,
)
from src.portfolio import portfolio_nav  # noqa: E402
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


def _price_series(key: str) -> pd.Series | None:
    """读已落盘的收盘价序列（日期索引）。"""
    path = PRICES_DIR / f"{key}.csv"
    if not path.exists():
        return None
    s = pd.read_csv(path, parse_dates=["date"]).set_index("date")["close"].sort_index()
    return s if not s.dropna().empty else None


def _instruments(segments_by_inv, benchmarks) -> list[Item]:
    """所有持仓路径里的标的 + 基准 → **去重后的标的列表**（同一标的多位持有者只抓一次）。

    键用 `market_type_symbol`（如 cn_stock_600519、cn_index_000001）。
    """
    uniq: dict[str, Item] = {}
    for segs in segments_by_inv.values():
        for _date, it in segs:
            uniq.setdefault(it.key, it)
    for b in benchmarks:
        uniq.setdefault(b.key, b)
    return list(uniq.values())


def run() -> dict:
    PRICES_DIR.mkdir(parents=True, exist_ok=True)
    RETURNS_DIR.mkdir(parents=True, exist_ok=True)
    PORTFOLIOS_DIR.mkdir(parents=True, exist_ok=True)

    investors = load_investors()
    switches = load_switches()
    benchmarks = load_investor_benchmarks()
    cfg = load_investor_config()
    principal = cfg["principal"]

    segments_by_inv = investor_segments(investors, switches)
    items = _instruments(segments_by_inv, benchmarks)
    # 新增标的（本地尚无/过短历史）自动补种，避免其只有单日数据把收益/指数带偏
    try:
        from prototype import backfill
        if backfill.seed_missing(items):
            print()
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 历史补种跳过：{e}")
    snap = qf.snapshot(items)                  # 一次批量拿全部标的的当日收盘价
    quotes = {(r["market"], r["symbol"]): r for _, r in snap.iterrows()}

    meta: dict = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "base_currency": cfg["base_currency"],
        "caliber": CALIBER,
        "source": "sina/tencent",
        "principal": principal,
        "base_date": cfg["start_date"],
        "n_investors": len(investors),
        "n_switches": len(switches),
        "items": {},
        "portfolios": [],
    }

    # --- 标的层：抓当日收盘 + 增量 append + 单标的收益 ---
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

            base_close = float(ret["close"].iloc[0])
            last_close = float(ret["close"].iloc[-1])
            meta["items"][key] = {
                "name": it.name, "symbol": it.symbol,
                "market": it.market, "type": it.type,
                "start_date": start,
                "first_date": str(ret["date"].iloc[0].date()),
                "last_date": str(ret["date"].iloc[-1].date()),
                "base_close": base_close,
                "last_close": last_close,
                "cum_return": float(ret["cum_return"].iloc[-1]),
                "market_value": last_close / base_close * principal,
                "annualized": _safe(annualized_return(ret)),
                "max_drawdown": _safe(max_drawdown(ret["nav"])),
                "calmar": _safe(calmar_ratio(ret)),
                "n_obs": int(len(ret)),
                "appended": bool(added),
                "caliber": CALIBER,
                "status": "ok",
            }
            print(f"[ok ] {key:14s} {it.name:8s} {'+1 新行' if added else '无新行(已是最新)'} "
                  f"close={q['close']} n={len(ret)} cum={ret['cum_return'].iloc[-1]:+.2%}")
        except Exception as e:  # noqa: BLE001
            meta["items"][key] = {
                "name": it.name, "symbol": it.symbol,
                "market": it.market, "type": it.type,
                "status": "error", "error": f"{type(e).__name__}: {e}",
            }
            print(f"[ERR] {key:14s} {it.name:8s} -> {type(e).__name__}: {e}")

    # --- 投资者层：按持仓路径（全仓切换）合成组合收益 ---
    for inv in investors:
        nick = inv.nickname
        segs = segments_by_inv[nick]
        try:
            series: dict[str, pd.Series] = {}
            for _d, it in segs:
                s = _price_series(it.key)
                if s is None:
                    raise ValueError(f"缺 {it.key} 的价格数据")
                series[it.key] = s
            df = portfolio_nav(series, [(d, it.key) for d, it in segs], principal)
            df.to_csv(PORTFOLIOS_DIR / f"{portfolio_id(nick)}.csv", index=False)

            current = segs[-1][1]
            meta["portfolios"].append({
                "nickname": nick,
                "principal": inv.principal,
                "holdings": [
                    {"date": d, "key": it.key, "symbol": it.symbol,
                     "market": it.market, "type": it.type}
                    for d, it in segs
                ],
                "n_switches": len(segs) - 1,
                "current": {"symbol": current.symbol, "market": current.market, "type": current.type},
                "first_date": str(df["date"].iloc[0].date()),
                "last_date": str(df["date"].iloc[-1].date()),
                "cum_return": float(df["cum_return"].iloc[-1]),
                "market_value": float(df["value"].iloc[-1]),
                "annualized": _safe(annualized_return(df)),
                "max_drawdown": _safe(max_drawdown(df["nav"])),
                "status": "ok",
            })
            print(f"[inv] {nick:8s} {len(segs) - 1} 次调仓 · 当前 {current.symbol:>7s} "
                  f"· cum={df['cum_return'].iloc[-1]:+.2%}")
        except Exception as e:  # noqa: BLE001
            meta["portfolios"].append({
                "nickname": nick, "status": "error",
                "error": f"{type(e).__name__}: {e}",
            })
            print(f"[ERR] {nick:8s} 组合 -> {type(e).__name__}: {e}")

    META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    # 群体平均指数（= 投资者组合收益按人平均，依赖刚生成的 data/portfolios）
    try:
        from prototype import index_build
        index_build.run()
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 群体平均指数构建失败：{e}")

    ok = sum(1 for v in meta["items"].values() if v.get("status") == "ok")
    pok = sum(1 for v in meta["portfolios"] if v.get("status") == "ok")
    print(f"\n完成：{ok}/{len(items)} 标的、{pok}/{len(investors)} 投资者"
          f" -> {META_PATH.relative_to(META_PATH.parent.parent)}")
    return meta


if __name__ == "__main__":
    run()
