"""群体平均指数：把**所有投资者的组合收益**按人平均合成一条指数（基准日 = 100）。

- 成分：`investors.yaml` 的每一位投资者（读 `data/portfolios/{Investor}.csv` 的 nav）。
- 口径：价格收益（未复权），**按人等权**；每个投资者 nav 在基准日归一为 100。
- 基准日：默认取 `investors.yaml` 顶层 `start_date`（可 `--base` 覆盖）。
- **只输出基准日及之后**的点。
- 输出：`data/index/equal_weight.csv`（date, index），index = 100 对应 100 万。

用法：
    python -m prototype.index_build
    python -m prototype.index_build --base 2026-09-24
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from src.config import (  # noqa: E402
    INDEX_PATH,
    PORTFOLIOS_DIR,
    load_investor_config,
    load_investors,
    portfolio_id,
)


def _load_nav(nickname: str) -> pd.Series | None:
    p = PORTFOLIOS_DIR / f"{portfolio_id(nickname)}.csv"
    if not p.exists():
        return None
    return pd.read_csv(p, parse_dates=["date"]).set_index("date")["nav"].rename(nickname)


def build(base: str | None = None) -> tuple[pd.DataFrame, list[str], pd.Timestamp]:
    investors = load_investors()
    cfg = load_investor_config()

    navs: dict[str, pd.Series] = {}
    for inv in investors:
        s = _load_nav(inv.nickname)
        if s is None or s.dropna().empty:
            print(f"[warn] 缺 {inv.nickname} 的组合收益（先跑 prototype.daily_close / backfill），跳过")
            continue
        navs[inv.nickname] = s
    if not navs:
        raise SystemExit("没有可用组合收益，请先跑 prototype.backfill / daily_close")

    px = pd.DataFrame(navs).sort_index().ffill()     # 对齐交易日
    firsts = {k: px[k].first_valid_index() for k in px.columns}

    default_base = cfg.get("start_date")
    base_ts = (pd.Timestamp(base) if base
               else (pd.Timestamp(default_base) if default_base else px.index[0]))

    late = {k: v for k, v in firsts.items() if v is not None and v > base_ts}
    if late:
        print(f"[warn] 以下投资者在基准日 {base_ts.date()} 及之前没有数据，指数基准日将顺延到其起点：")
        for k, v in late.items():
            print(f"        - {k} 起于 {v.date()}")

    common_start = max(v for v in firsts.values() if v is not None)
    px = px.loc[px.index >= common_start]
    after = px.index[px.index >= base_ts]
    if len(after) == 0:
        raise SystemExit(f"基准日 {base_ts.date()} 之后没有共同数据")
    d0 = after[0]

    index = 100.0 * (px / px.loc[d0]).mean(axis=1)   # 按人平均（每人等权）
    out = pd.DataFrame({"date": px.index, "index": index.round(4).values})
    out = out[out["date"] >= d0].reset_index(drop=True)
    return out, sorted(navs), d0


def run(base: str | None = None, out: str | Path = INDEX_PATH) -> pd.DataFrame:
    out = Path(out)
    df, names, d0 = build(base)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    last = df.iloc[-1]
    print(f"[index] {len(names)} 位投资者 · 按人平均 ｜ 基准日 {d0.date()} = 100（= 100 万）｜ "
          f"{df['date'].iloc[0].date()} ~ {last['date'].date()} ｜ 最新 {last['index']:.2f}")
    return df


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=None, help="基准日 YYYY-MM-DD（默认取 investors.yaml 顶层 start_date）")
    ap.add_argument("--out", default=str(INDEX_PATH))
    args = ap.parse_args()
    run(args.base, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
