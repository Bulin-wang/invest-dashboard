"""日更脚本（报价端点版）：每天 append 当日收盘价 → 算标的收益 + 投资者组合收益。

设计（契合「~60 位投资者、每天一次、低时效」）：
- 数据源：新浪/腾讯报价端点（复用 prototype.quote_fetch），**无 key**；
  其中 `type: fund`（**场外开放式基金**）走东方财富净值接口（`prototype.fund_fetch`），
  `type: crypto`（**加密货币**）走币安公开镜像（`prototype.crypto_fetch`）；
- 清单：项目根 investors.yaml（每人**初始持仓**，单标的或 `holdings` 多标的权重 + 基准）；
  调仓流水 switches.yaml（append-only）；
- 组合：按每位投资者的**权重路径**合成累计收益（**buy & hold**）
  （`src.aggregate.weighted_path_nav`；调仓日收盘切换，当日算旧组合，无费用 / 不计汇率）；
- 存储：增量 append 当日收盘价到 `data/prices/{key}.csv`；
  投资者组合收益写到 `data/portfolios/{Investor}.csv`；
- 输出：data/prices/*.csv、data/returns/*.csv、data/portfolios/*.csv、data/index/*.csv、data/meta.json。

⚠️ 口径：**价格收益**（未复权），不含分红/除权，不计汇率。
   例外 1：`type: fund`（场外基金）用**累计净值**（含分红再投）——场外基金没有
   "未复权原始价"这个概念（单位净值本身就是除权后的价格）。
   例外 2：`type: crypto`（加密货币）用**UTC 日线收盘价**，且**周末也有数据**
   （7×24）——组合日历取并集，因此只要有人持有 crypto，看板就会出现周末点位。
   逐项口径记录在 `meta.json` 的 `items[].caliber`
   （`price_return` / `cumulative_nav` / `utc_daily_close`）。

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
    investor_weight_segments,
    load_investor_benchmarks,
    load_investor_config,
    load_investors,
    load_switches,
    portfolio_id,
)
from src.aggregate import holding_spec, weighted_path_nav  # noqa: E402
from src.returns import (  # noqa: E402
    annualized_return,
    calmar_ratio,
    cumulative_return,
    max_drawdown,
)

CALIBER = "price_return"   # 价格收益（不含分红再投）
CALIBER_FUND = "cumulative_nav"   # 场外基金：累计净值（含分红再投）；与 CALIBER 不同，逐项标注
CALIBER_CRYPTO = "utc_daily_close"  # 加密货币：UTC 日线收盘价（含周末）；逐项标注
CALIBER_FUTURES = "settlement_price"  # 期货：当日结算价（日线 c）；逐项标注
# 逐 type 的口径标签（写进 meta.json 的 items[].caliber）
_CALIBER_BY_TYPE = {
    "fund": CALIBER_FUND,
    "crypto": CALIBER_CRYPTO,
    "futures": CALIBER_FUTURES,
}


def _expired_holdings(df: pd.DataFrame, segs: list, expires_by_key: dict) -> list[tuple]:
    """到期后未调仓、已按现金处理的持仓 → 供 `daily_close` 提醒主人去滚仓。

    返回 ``[(到期日, 腿key), ...]``。判定依据：该腿有 `expires`，
    且组合序列里在到期日**之后**仍有日期（即已按现金延续）。
    """
    if not expires_by_key or df.empty:
        return []
    last_date = pd.Timestamp(df["date"].iloc[-1])
    last_legs = segs[-1][1]
    out: list[tuple] = []
    for it, _w in last_legs:
        exp = expires_by_key.get(it.key)
        if exp and last_date > pd.Timestamp(exp):
            out.append((str(exp), it.key))
    return out


def _snapshot_module(kind: str):
    """按 type 取「专用快照 + 全量缓存」型抓取模块（fund / crypto）。

    这两个品种都不在 `quote_fetch` 的行情端点里，但都提供
    `daily_snapshot(item)`（返回与 `quote_fetch.snapshot()` 同构的一行）
    与 `fetch_history(item)`，因此 daily_close / backfill 可以走同一条通路。
    """
    if kind == "fund":
        from prototype import fund_fetch
        return fund_fetch
    if kind == "crypto":
        from prototype import crypto_fetch
        return crypto_fetch
    if kind == "futures":
        from prototype import futures_fetch
        return futures_fetch
    return None


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
        for _date, legs in segs:
            for it, _w in legs:
                if it.type == "cash":
                    continue                       # 现金腿不需要行情
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

    segments_by_inv = investor_weight_segments(investors, switches)
    items = _instruments(segments_by_inv, benchmarks)
    # 新增标的（本地尚无/过短历史）自动补种，避免其只有单日数据把收益/指数带偏
    try:
        from prototype import backfill
        if backfill.seed_missing(items):
            print()
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 历史补种跳过：{e}")
    snap = qf.snapshot(items)                  # 一次批量拿全部标的的当日收盘价

    # 不在行情端点里的品种（场外基金 / 加密货币）：各自走专用接口，
    # 再把结果并进同一张 snap —— 后续逻辑完全不用区分来源
    extra_rows = []
    for it in items:
        mod = _snapshot_module(it.type)
        if mod is None:
            continue
        try:
            extra_rows.append(mod.daily_snapshot(it))
        except Exception as e:  # noqa: BLE001
            print(f"[warn] {it.key:16s} {it.name:12s} 快照失败：{type(e).__name__}: {e}")
            extra_rows.append({"symbol": it.symbol, "name": it.name, "market": it.market,
                               "type": it.type, "close": None, "prev_close": None,
                               "pct_chg": None, "date": None, "source": None})
    if extra_rows:
        snap = pd.concat([snap, pd.DataFrame(extra_rows)], ignore_index=True)

    # ⚠️ 键必须带上 **type**，即 `{market}_{type}_{symbol}`（= Item.key）：
    # 光用 (market, symbol) 会让**同号不同品种**互相覆盖 —— 例如平安银行
    # （cn_stock_000001，11 元）与上证指数（cn_index_000001，3800 点）同号，
    # 后写的那条会把前一条挤掉，于是股票价格被写成指数点位。
    # 详见回归用例 tests/test_quote_key_collision_offline.py
    quotes = {f'{r["market"]}_{r["type"]}_{r["symbol"]}': r for _, r in snap.iterrows()}

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
        q = quotes.get(key)                      # 键含 type，同号不同品种不会串
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
                # 口径逐项标注：基金=累计净值（含分红再投）、crypto=UTC 日线收盘，
                # 其余品种 = price_return（未复权、不含分红）
                "caliber": _CALIBER_BY_TYPE.get(it.type, CALIBER),
                "status": "ok",
            }
            if it.type == "fund":
                fund_name = q.get("name")
                if fund_name:
                    meta["items"][key]["name"] = fund_name
                meta["items"][key]["unit_nav"] = _safe(q.get("unit_nav"))
            elif it.type == "crypto":
                if q.get("name"):
                    meta["items"][key]["name"] = q["name"]      # 交易对，如 BTCUSDT
                if q.get("pair"):
                    meta["items"][key]["pair"] = q["pair"]
            print(f"[ok ] {key:16s} {it.name:12s} {'+1 新行' if added else '无新行(已是最新)'} "
                  f"close={q['close']} n={len(ret)} cum={ret['cum_return'].iloc[-1]:+.2%}")
        except Exception as e:  # noqa: BLE001
            meta["items"][key] = {
                "name": it.name, "symbol": it.symbol,
                "market": it.market, "type": it.type,
                "status": "error", "error": f"{type(e).__name__}: {e}",
            }
            print(f"[ERR] {key:16s} {it.name:12s} -> {type(e).__name__}: {e}")

    # --- 投资者层：按权重路径（buy & hold，支持现金腿）合成组合收益 ---
    # 组合日历 = 「该组合各腿价格日期」∪ cal_hint。cal_hint 取**所有标的**（含基准）
    # 的日期并集，作用是：
    #   (a) 全现金组合需要一根日期轴；
    #   (b) 某条腿已到期（期货合约结束等）后，组合曲线仍继续延伸成平线（该腿变现金），
    #       否则整条曲线会随该腿一起断掉、在看板上"消失"。
    cal_hint = None
    _all_dates: list[pd.DatetimeIndex] = []
    for it in items:
        s = _price_series(it.key)
        if s is not None and not s.empty:
            _all_dates.append(pd.DatetimeIndex(s.index))
    if _all_dates:
        cal_hint = pd.DatetimeIndex(sorted(set().union(*_all_dates)))

    # 到期日映射：{key: expires}（期货 = 最后交易日；当日仍有价，次日起视为现金）
    expires_by_key = {}
    for _d, legs in (seg for segs in segments_by_inv.values() for seg in segs):
        for it, _w in legs:
            if it.expires:
                expires_by_key[it.key] = it.expires
    warn_expired: list[tuple] = []

    for inv in investors:
        nick = inv.nickname
        segs = segments_by_inv[nick]
        try:
            series: dict[str, pd.Series] = {}
            weight_segs: list[tuple[str, dict[str, float]]] = []
            for d, legs in segs:
                wd: dict[str, float] = {}
                for it, w in legs:
                    if it.type != "cash":          # 现金腿不需要行情
                        s = _price_series(it.key)
                        if s is None:
                            raise ValueError(f"缺 {it.key} 的价格数据")
                        series[it.key] = s
                    wd[it.key] = w
                weight_segs.append((d, wd))
            df = weighted_path_nav(series, weight_segs, principal,
                                   calendar_hint=cal_hint, expires_by_key=expires_by_key)
            df.to_csv(PORTFOLIOS_DIR / f"{portfolio_id(nick)}.csv", index=False)

            # 到期后未调仓（已按现金延续）→ 提醒去滚仓
            for exp_date, leg_key in _expired_holdings(df, segs, expires_by_key):
                warn_expired.append((nick, leg_key, exp_date))

            last_legs = segs[-1][1]
            cur_spec = holding_spec({it.key: w for it, w in last_legs})
            meta["portfolios"].append({
                "nickname": nick,
                "principal": inv.principal,
                "holdings": [
                    {
                        "date": d,
                        "key": holding_spec({it.key: w for it, w in legs}),
                        "symbol": "+".join(it.symbol for it, _ in legs),
                        "market": legs[0][0].market,
                        "type": legs[0][0].type if len(legs) == 1 else "multi",
                        "legs": [
                            {"key": it.key, "symbol": it.symbol, "market": it.market,
                             "type": it.type, "weight": round(float(w), 6)}
                            for it, w in legs
                        ],
                    }
                    for d, legs in segs
                ],
                "n_switches": len(segs) - 1,
                "current": {
                    "holding": cur_spec,
                    "symbol": "+".join(it.symbol for it, _ in last_legs),
                    "market": last_legs[0][0].market,
                    "type": last_legs[0][0].type if len(last_legs) == 1 else "multi",
                },
                "first_date": str(df["date"].iloc[0].date()),
                "last_date": str(df["date"].iloc[-1].date()),
                "cum_return": float(df["cum_return"].iloc[-1]),
                "market_value": float(df["value"].iloc[-1]),
                "annualized": _safe(annualized_return(df)),
                "max_drawdown": _safe(max_drawdown(df["nav"])),
                "status": "ok",
            })
            print(f"[inv] {nick:8s} {len(segs) - 1} 次调仓 · 当前 {cur_spec} "
                  f"· cum={df['cum_return'].iloc[-1]:+.2%}")
        except Exception as e:  # noqa: BLE001
            # ⚠️ 必须删掉这个投资者的**旧组合文件**：看板（app/streamlit_app.py）
            # 只判断"文件是否存在"就读它，残留的旧文件会被当成最新数据展示
            # —— 名字是新持仓、数值却停在几天前，而且毫无提示。
            # 删掉之后看板会跳过该投资者（数据缺失是显式的），比展示陈旧数据安全。
            stale = PORTFOLIOS_DIR / f"{portfolio_id(nick)}.csv"
            removed = False
            if stale.exists():
                try:
                    stale.unlink()
                    removed = True
                except OSError as oe:  # noqa: BLE001
                    print(f"[warn] {nick:8s} 旧组合文件删除失败：{oe}")
            meta["portfolios"].append({
                "nickname": nick, "status": "error",
                "error": f"{type(e).__name__}: {e}",
                "stale_file_removed": removed,
            })
            print(f"[ERR] {nick:8s} 组合 -> {type(e).__name__}: {e}"
                  + ("（已删除过期组合文件，看板将跳过该投资者）" if removed else ""))

    META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    # 群体平均指数（= 投资者组合收益按人平均，依赖刚生成的 data/portfolios）
    try:
        from prototype import index_build
        index_build.run()
    except KeyboardInterrupt:
        raise
    except BaseException as e:  # noqa: BLE001
        # 注意要连 SystemExit 一起接住：index_build 在"没有可用组合收益"时是
        # `raise SystemExit(...)`，而 SystemExit 继承自 BaseException，
        # 只写 `except Exception` 会让它**穿透出去中断整个管线**。
        print(f"[warn] 群体平均指数构建失败：{type(e).__name__}: {e}")

    ok = sum(1 for v in meta["items"].values() if v.get("status") == "ok")
    pok = sum(1 for p in meta["portfolios"] if p.get("status") == "ok")
    if warn_expired:
        print(f"\n[提醒] 以下持仓的标的**已到期但未调仓**，余额已按现金处理"
              f"（持仓冻结不涨不跌）：")
        for nick, leg_key, exp_date in warn_expired:
            print(f"        - {nick:12s} {leg_key:22s} 到期日 {exp_date}")
        print("        若要继续持有，请在 switches.yaml 追加一条滚仓（换到下个合约）。")
    print(f"\n完成：{ok}/{len(items)} 标的、{pok}/{len(investors)} 投资者"
          f" -> {META_PATH.relative_to(META_PATH.parent.parent)}")
    return meta


if __name__ == "__main__":
    run()
