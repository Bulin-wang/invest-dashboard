"""群体平均指数：把 investors.yaml 的投资者**按人平均**合成一条指数（基准日 = 100）。

- 成分：`investors.yaml` 的每一位投资者（不含 benchmarks）。**按人**等权：
  同一标的被多人持有时，权重按持有者人数累加（等价于对「每人的归一化曲线」取平均）。
- 口径：**价格变化**（未复权），**忽略汇率** —— 与看板一致。
- 再平衡：**买入持有**（基准日按人等权买入，之后权重随涨跌漂移）：
      index(t) = 100 × weighted_mean_i( P_i(t) / P_i(基准日) )
- 基准日：默认取 investors.yaml 顶层 `start_date`（可 `--base` 覆盖）；基准日归一为 **100**。
- **只输出基准日及之后的点**（不展示起始日之前的历史）。
- 只有**所有成分都有数据**的区间才纳入指数：若某标的起始晚于基准日，
  会打印告警并把基准日顺延到它们的共同起点（正常应先跑 `prototype.backfill`）。
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
    PRICES_DIR,
    load_investor_config,
    load_investors,
)


def _load_prices(key: str) -> pd.Series | None:
    p = PRICES_DIR / f"{key}.csv"
    if not p.exists():
        return None
    return pd.read_csv(p, parse_dates=["date"]).set_index("date")["close"].rename(key)


def build(base: str | None = None) -> tuple[pd.DataFrame, list[str], pd.Timestamp]:
    investors = load_investors()
    cfg = load_investor_config()
    keys = [inv.key for inv in investors]

    series: dict[str, pd.Series] = {}
    for k in sorted(set(keys)):
        s = _load_prices(k)
        if s is None or s.dropna().empty:
            print(f"[warn] 缺 {k} 的价格数据，跳过")
            continue
        series[k] = s
    if not series:
        raise SystemExit("没有可用价格数据，请先跑 prototype.backfill / daily_close")

    px = pd.DataFrame(series).sort_index().ffill()   # 对齐交易日（先只前向填充，不整段丢弃）
    firsts = {k: px[k].first_valid_index() for k in px.columns}

    default_base = cfg.get("start_date")
    base_ts = (pd.Timestamp(base) if base
               else (pd.Timestamp(default_base) if default_base else px.index[0]))

    # 有的成分在基准日当天/之前还没数据 → 告警（多半是新加还没回填）
    late = {k: v for k, v in firsts.items() if v is not None and v > base_ts}
    if late:
        print(f"[warn] 以下标的在基准日 {base_ts.date()} 及之前没有数据，指数基准日将顺延到其起点：")
        for k, v in late.items():
            print(f"        - {k} 起于 {v.date()}  → 建议先跑 `python -m prototype.backfill`")

    common_start = max(v for v in firsts.values() if v is not None)
    px = px.loc[px.index >= common_start]            # 只在「所有成分都有数据」的区间内
    after = px.index[px.index >= base_ts]
    if len(after) == 0:
        raise SystemExit(f"基准日 {base_ts.date()} 之后没有共同数据")
    d0 = after[0]

    # 按人加权：某标的被 n 人持有时权重 ∝ n
    counts = pd.Series(keys).value_counts()
    weights = pd.Series({k: float(counts.get(k, 0)) for k in px.columns})
    weights = weights / weights.sum()

    index = 100.0 * (px / px.loc[d0]).mul(weights, axis=1).sum(axis=1)   # 按人平均 · 买入持有
    out = pd.DataFrame({"date": px.index, "index": index.round(4).values})
    out = out[out["date"] >= d0].reset_index(drop=True)   # 不展示基准日之前
    return out, list(series), d0


def run(base: str | None = None, out: str | Path = INDEX_PATH) -> pd.DataFrame:
    out = Path(out)
    df, keys, d0 = build(base)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    last = df.iloc[-1]
    print(f"[index] {len(keys)} 个标的 · 按人平均 ｜ 基准日 {d0.date()} = 100（= 100 万）｜ "
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
