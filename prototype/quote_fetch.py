"""原型：用新浪/腾讯「报价端点」批量抓取每日收盘价。

特点（契合「70 个标的、每天一次、低时效」的场景）：
- 无 API key、无数据库接口，就是抓行情站的小文本端点；
- 新浪 `hq.sinajs.cn/list=` 一次请求可塞多个代码，返回「最新价（收盘后即收盘价）+ 日期」；
- 覆盖 **A股个股 / ETF / 指数 + 美股 + 港股**（前缀 sh/sz、gb_、hk）；
- 腾讯 `qt.gtimg.cn/q=` 作备用源。

用法：
    python -m prototype.quote_fetch            # 默认读项目根的 holdings.yaml
    python -m prototype.quote_fetch --universe prototype/universe.yaml

口径：**未复权收盘价的价格变化**（不含分红 / 除权，不做除权修正）。
   报价端点返回当日价（收盘后即收盘价）+ 日期；每天拿一次即可。
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import requests
import yaml

try:  # Windows 控制台默认 GBK，打印中文会炸；尽量切 UTF-8（不新建 wrapper，避免关闭底层 buffer）
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

ROOT = Path(__file__).resolve().parent
SINA_URL = "https://hq.sinajs.cn/list={codes}"
TENCENT_URL = "https://qt.gtimg.cn/q={codes}"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
SINA_LINE = re.compile(r'var hq_str_([\w$]+)="(.*)";')

# 美股指数在新浪的特殊写法
US_INDEX = {"^GSPC": "$inx", "^IXIC": "$ixic", "^DJI": "$dji", "^NDX": "$ndx"}


@dataclass
class Item:
    name: str
    symbol: str
    market: str          # cn / us / hk
    type: str = "stock"  # stock / etf / bond / index


# --------------------------------------------------------------------------- 代码映射
def sina_code(it: Item) -> str:
    s = str(it.symbol).strip()
    if it.market == "us":
        if s in US_INDEX:
            return "gb_" + US_INDEX[s]
        return "gb_" + s.lower().replace(".", "$")
    if it.market == "hk":
        return "hk" + s.zfill(5)
    # cn
    if it.type == "index":
        return ("sh" if s.startswith(("000", "999")) else "sz") + s
    if s.startswith(("6", "9")):
        return "sh" + s
    if s.startswith(("0", "2", "3")):
        return "sz" + s
    if s.startswith("5"):      # 沪市 ETF/基金
        return "sh" + s
    if s.startswith("1"):      # 深市 ETF/基金
        return "sz" + s
    if s.startswith(("4", "8")):  # 北交所
        return "bj" + s
    return "sh" + s


def tencent_code(it: Item) -> str:
    s = str(it.symbol).strip()
    if it.market == "us":
        return "us" + s.upper().replace(".", "")
    if it.market == "hk":
        return "hk" + s.zfill(5)
    if it.type == "index":
        return ("sh" if s.startswith(("000", "999")) else "sz") + s
    if s.startswith(("6", "9", "5")):
        return "sh" + s
    if s.startswith(("0", "2", "3", "1")):
        return "sz" + s
    return "sh" + s


# --------------------------------------------------------------------------- 解析
def _f(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def parse_sina(code: str, payload: str) -> dict | None:
    """把 `var hq_str_XXX="...";` 的 payload 解析成 {name, close, prev_close, date}。"""
    if not payload:
        return None
    f = payload.split(",")

    def n(i):
        return f[i] if i < len(f) else ""

    if code.startswith("gb_"):            # 美股：name,price,pct,datetime,change,open,high,low,...
        close = _f(n(1))
        chg = _f(n(4))
        prev = round(close - chg, 4) if (close is not None and chg is not None) else None
        return {"name": n(0), "close": close, "prev_close": prev, "date": n(3)[:10]}
    if code.startswith("hk"):             # 港股：ename,cname,open,prev,high,low,price,...
        return {"name": n(1), "close": _f(n(6)), "prev_close": _f(n(3)),
                "date": n(17).replace("/", "-")}
    # A股/ETF/指数：name,open,prev,price,high,low,...
    close = _f(n(3)) or _f(n(2))
    return {"name": n(0), "close": close, "prev_close": _f(n(2)), "date": n(30)}


def parse_tencent(payload: str) -> dict | None:
    """腾讯 `v_xxx="a~name~code~price~prev~open~...";`。"""
    m = re.search(r'="(.*)"', payload)
    if not m or not m.group(1):
        return None
    f = m.group(1).split("~")
    if len(f) < 5:
        return None
    close = _f(f[3])
    prev = _f(f[4])
    date = ""
    for x in f:
        if re.match(r"^\d{4}[-/]\d{2}[-/]\d{2}", x):      # 2026-09-25 12:50:19（美股）
            date = x[:10].replace("/", "-")
            break
        if re.match(r"^\d{14}$", x):                       # 20260924161444（A股）
            date = f"{x[0:4]}-{x[4:6]}-{x[6:8]}"
            break
    return {"name": f[1], "close": close, "prev_close": prev, "date": date}


# --------------------------------------------------------------------------- 抓取
def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def sina_quotes(items: list[Item], chunk: int = 50) -> dict[str, dict]:
    """返回 {sina_code: 行情}。按唯一代码做键，避免不同标的同号撞键。"""
    out: dict[str, dict] = {}
    headers = dict(UA, Referer="https://finance.sina.com.cn")
    for grp in _chunks(items, chunk):
        codes = [sina_code(it) for it in grp]
        r = requests.get(SINA_URL.format(codes=",".join(codes)), headers=headers, timeout=20)
        text = r.content.decode("gbk", "replace")
        for line in text.splitlines():
            m = SINA_LINE.search(line)
            if not m:
                continue
            q = parse_sina(m.group(1), m.group(2))
            if q:
                out[m.group(1)] = {**q, "source": "sina"}
    return out


def tencent_quotes(items: list[Item]) -> dict[str, dict]:
    """返回 {tencent_code: 行情}。"""
    out: dict[str, dict] = {}
    headers = dict(UA, Referer="https://gu.qq.com")
    codes = [tencent_code(it) for it in items]
    r = requests.get(TENCENT_URL.format(codes=",".join(codes)), headers=headers, timeout=20)
    text = r.content.decode("gbk", "replace")
    for line in text.splitlines():
        m = re.match(r"v_([\w]+)=", line)
        if m:
            q = parse_tencent(line)
            if q:
                out[m.group(1)] = {**q, "source": "tencent"}
    return out


def snapshot(items: list[Item]) -> pd.DataFrame:
    """主源新浪，缺失的用腾讯补齐。"""
    sina = sina_quotes(items)
    missing = [it for it in items if not (sina.get(sina_code(it)) or {}).get("close")]
    tencent = tencent_quotes(missing) if missing else {}

    rows = []
    for it in items:
        q = sina.get(sina_code(it)) or tencent.get(tencent_code(it))
        if not q or q.get("close") is None:
            rows.append({"symbol": it.symbol, "name": it.name, "market": it.market,
                         "type": it.type, "close": None, "prev_close": None,
                         "pct_chg": None, "date": None, "source": None})
            continue
        prev = q.get("prev_close")
        pct = (q["close"] / prev - 1) if (prev and q["close"]) else None
        rows.append({"symbol": it.symbol, "name": q.get("name") or it.name,
                     "market": it.market, "type": it.type,
                     "close": q["close"], "prev_close": prev, "pct_chg": pct,
                     "date": q.get("date"), "source": q["source"]})
    return pd.DataFrame(rows)


def load_universe(path: Path) -> list[Item]:
    """支持两种清单：原型 universe.yaml 的 `universe:`，或主配置 holdings.yaml 的
    `holdings:` + `benchmarks:`（基准一并纳入，便于叠加对比）。"""
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw = cfg.get("universe")
    if raw is None:
        raw = list(cfg.get("holdings", [])) + list(cfg.get("benchmarks", []))
    items = []
    for h in raw:
        items.append(Item(name=str(h["name"]), symbol=str(h["symbol"]),
                          market=str(h["market"]).lower(), type=str(h.get("type", "stock"))))
    return items


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default=str(ROOT.parent / "holdings.yaml"))
    ap.add_argument("--out", default=str(ROOT / "out" / "snapshot.csv"))
    args = ap.parse_args()

    items = load_universe(Path(args.universe))
    print(f"共 {len(items)} 个标的，开始抓取…")
    df = snapshot(items)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False, encoding="utf-8-sig")

    ok = int(df["close"].notna().sum())
    print(f"\n成功 {ok}/{len(df)}  -> {out.relative_to(ROOT.parent)}")
    bad = df[df["close"].isna()]
    if len(bad):
        print("失败：" + ", ".join(bad["symbol"]))
    with pd.option_context("display.width", 200, "display.max_rows", 100):
        show = df.copy()
        show["pct_chg"] = show["pct_chg"].map(lambda x: f"{x:+.2%}" if pd.notna(x) else "—")
        print(show[["symbol", "name", "market", "type", "close", "pct_chg", "date", "source"]]
              .to_string(index=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
