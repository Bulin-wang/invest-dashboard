"""自定义指数：把 holdings.yaml 的持仓**等权**合成一条指数（基准日 = 100）。

- 成分：`holdings.yaml` 的持仓（不含 benchmarks），等权。
- 口径：**价格变化**（未复权），**忽略汇率**（各标的按本币收益等权，不做 CNY 折算）——与看板一致。
- 再平衡：**买入持有**（基准日等权买入，之后权重随涨跌漂移）：
      index(t) = 100 × mean_i( P_i(t) / P_i(基准日) )
- 基准日：默认取持仓最早的 `start_date`（可 `--base` 覆盖）；基准日归一为 **100**。
  保留基准日之前的历史（回推），以便立刻看到曲线。
- 输出：`data/index/equal_weight.csv`（date, index）。

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

from src.config import INDEX_PATH, PRICES_DIR, ROOT, load_holdings  # noqa: E402


def _load_prices(key: str) -> pd.Series | None:
    p = PRICES_DIR / f"{key}.csv"
    if not p.exists():
        return None
    return pd.read_csv(p, parse_dates=["date"]).set_index("date")["close"].rename(key)


def build(base: str | None = None) -> tuple[pd.DataFrame, list[str], pd.Timestamp]:
    holdings = load_holdings()
    series = {}
    for it in holdings:
        s = _load_prices(it.key)
        if s is None or s.dropna().empty:
            print(f"[warn] 缺 {it.key}（{it.name}）的价格数据，跳过")
            continue
        series[it.key] = s
    if not series:
        raise SystemExit("没有可用价格数据，请先跑 prototype.backfill / daily_close")

    px = pd.DataFrame(series).sort_index().ffill().dropna()   # 对齐交易日，取共同区间
    if px.empty:
        raise SystemExit("成分之间没有重叠的历史区间")

    starts = [pd.Timestamp(it.start_date) for it in holdings if it.start_date]
    base_ts = pd.Timestamp(base) if base else (min(starts) if starts else px.index[0])
    after = px.index[px.index >= base_ts]
    if len(after) == 0:
        raise SystemExit(f"基准日 {base_ts.date()} 之后没有数据")
    d0 = after[0]

    index = 100.0 * (px / px.loc[d0]).mean(axis=1)   # 等权 · 买入持有
    out = pd.DataFrame({"date": px.index, "index": index.round(4).values})
    return out, list(series), d0


def run(base: str | None = None, out: str | Path = INDEX_PATH) -> pd.DataFrame:
    out = Path(out)
    df, keys, d0 = build(base)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    last = df.iloc[-1]
    print(f"[index] {len(keys)} 个等权 ｜ 基准日 {d0.date()} = 100 ｜ "
          f"{df['date'].iloc[0].date()} ~ {last['date'].date()} ｜ 最新 {last['index']:.2f}")
    return df


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=None, help="基准日 YYYY-MM-DD（默认取持仓最早 start_date）")
    ap.add_argument("--out", default=str(INDEX_PATH))
    args = ap.parse_args()
    run(args.base, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
