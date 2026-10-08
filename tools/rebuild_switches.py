"""按「陆续建仓」模式重建 `switches.yaml`：把每条调仓的**起点改为现金**。

背景
----
目标语义：所有投资者在 `start_date` 都持有 100 万**现金**，之后**按各自提交日**
陆续建仓；建仓记录写在 `switches.yaml`（目标 = 真实标的）。

而原先的 `switches.yaml` 是「目标 == 初始持仓」的恒等调仓 —— 从 start_date 起就
等效于一直持有该标的，所以现金段只存在一天，看不出"陆续建仓"。

本脚本把原 `switches.yaml` 里的「真实标的 + 日期」**原样保留**，因为它本身就是
权威的「谁在哪天建了什么仓」的记录：

    python -m tools.rebuild_switches --from switches.yaml.bak --write

⚠️ 两个用法陷阱（都源于自噬：输出会把输入改掉）
------------------------------------------------
1. 默认从 `switches.yaml` 自己读 —— 所以**必须先把原件备份改名**，否则第二次运行
   会读到已被改写的输出（全部变成"没有真实标的"）。脚本对"读到的记录里一个真实
   标的都没有"会直接报错退出，不再默默产出空文件。
2. 也可以改用 `--planned-from investors.yaml` 从公开清单取计划持仓，但那必须发生在
   `make_investors --initial-cash` **之前**（之后清单里全是现金）。

用法
----
    python -m tools.rebuild_switches --from switches.yaml.bak          # 预览
    python -m tools.rebuild_switches --from switches.yaml.bak --write  # 备份后写入
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_SWITCHES = ROOT / "switches.yaml"
CASH_MARK = "999999"

HEADER = """\
# ==========================================================================
# 建仓流水（append-only）—— 每人从 start_date 起持有现金，按各自提交日建仓
# --------------------------------------------------------------------------
# 语义：`date` 收盘价买入 `symbol`；**该日算旧状态（现金），次日起按标的算收益**。
#       即：date 之前收益 0%，date 之后跟随标的涨跌。
#   {date: 2026-10-08, nickname: "investor01", symbol: "510300", market: cn, type: etf}
# 多标的（目标权重组合）用 holdings 写法：
#   - date: 2026-10-08
#     nickname: "investor10"
#     holdings:
#       - {symbol: "01810", market: hk, type: stock, weight: 0.7}
#       - {symbol: "ONDO", market: us, type: crypto, weight: 0.3}
# 口径：调仓日收盘价切换；不计费用 / 汇率。维护：有新记录就往这里追加，不改历史。
# ==========================================================================
switches:
"""


class RebuildError(SystemExit):
    """输入不可用（例如读到了已被改写的空壳）。"""


def _fmt_flow(d: dict) -> str:
    """把一条腿渲染成 YAML 行内映射（与 investors.yaml 的写法保持一致）。"""
    order = ["date", "nickname", "symbol", "market", "type", "weight", "expires"]
    keys = [k for k in order if k in d] + [k for k in d if k not in order]
    parts = []
    for k in keys:
        v = d[k]
        if isinstance(v, str):
            parts.append(f'{k}: "{v}"')
        else:
            parts.append(f"{k}: {v}")
    return "{" + ", ".join(parts) + "}"


def parse_records(path: Path) -> list[dict]:
    """把 switches.yaml 解析成**记录列表**（行内写法与 holdings 多行写法都支持）。

    直接用 yaml.safe_load —— 顶层 `switches:` 是个列表，两种写法天然都能解析；
    文档字符串里的示例会被当成"孤儿键/行"忽略，不会混进来。
    """
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw = cfg.get("switches") or []
    records: list[dict] = []
    for r in raw:
        if not isinstance(r, dict):
            continue
        nick = str(r.get("nickname") or "").strip()
        date = r.get("date")
        if not nick or date is None:
            continue
        if r.get("holdings"):
            legs = [dict(l) for l in r["holdings"]]
        else:
            leg = {k: r[k] for k in ("symbol", "market", "type") if k in r}
            if "weight" in r:
                leg["weight"] = r["weight"]
            if "expires" in r:
                leg["expires"] = r["expires"]
            legs = [leg] if leg.get("symbol") else []
        if legs:
            records.append({"date": str(date), "nickname": nick, "legs": legs})
    return records


def load_planned_from_investors(investors_path: Path) -> list[dict]:
    """从 investors.yaml 取计划持仓（顺序即记录顺序）。"""
    cfg = yaml.safe_load(investors_path.read_text(encoding="utf-8")) or {}
    out: list[dict] = []
    for rec in cfg.get("investors") or []:
        nick = rec["nickname"]
        if rec.get("holdings"):
            legs = [dict(l) for l in rec["holdings"]]
        else:
            sym = str(rec.get("symbol") or "")
            if not sym or sym == CASH_MARK:
                continue
            leg = {"symbol": sym, "market": str(rec.get("market", "cn")),
                   "type": str(rec.get("type", "stock"))}
            if rec.get("expires"):
                leg["expires"] = str(rec["expires"])
            legs = [leg]
        out.append({"date": None, "nickname": nick, "legs": legs})
    return out


def _start_date(investors_path: Path) -> str:
    if not investors_path.exists():
        return "2026-09-24"
    cfg = yaml.safe_load(investors_path.read_text(encoding="utf-8")) or {}
    return str(cfg.get("start_date") or "2026-09-24")


def build(src_records: list[dict], date_of: dict[str, str], order: list[str],
          fallback_date: str) -> tuple[list[str], list[str]]:
    """生成新 switches.yaml 正文；返回 (行列表, 警告列表)。"""
    # 每位投资者只保留一条建仓记录：目标取该人的计划持仓
    planned: dict[str, list[dict]] = {}
    for r in src_records:
        planned.setdefault(r["nickname"], r["legs"])       # 取第一条的真实标的
    for r in src_records:
        if not date_of.get(r["nickname"]):
            date_of[r["nickname"]] = r["date"]

    lines: list[str] = []
    warns: list[str] = []
    nicks = [n for n in order if n in planned] + \
            [n for n in planned if n not in order]

    for nick in nicks:
        legs = planned[nick]
        date = date_of.get(nick) or fallback_date
        if not date_of.get(nick):
            warns.append(f"{nick}: 没有建仓日期，已按 start_date({fallback_date}) 立即建仓")
        if len(legs) == 1:
            leg = legs[0]
            rec = {"date": date, "nickname": nick, "symbol": str(leg["symbol"]),
                   "market": str(leg["market"]), "type": str(leg["type"])}
            if leg.get("expires"):
                rec["expires"] = str(leg["expires"])
            lines.append("  - " + _fmt_flow(rec))
        else:
            lines.append(f"  - date: {date}")
            lines.append(f'    nickname: "{nick}"')
            lines.append("    holdings:")
            for leg in legs:
                d = {"symbol": str(leg["symbol"]), "market": str(leg["market"]),
                     "type": str(leg["type"])}
                if leg.get("weight") is not None:
                    d["weight"] = leg["weight"]
                if leg.get("expires"):
                    d["expires"] = str(leg["expires"])
                lines.append("      - " + _fmt_flow(d))
    return lines, warns


def main() -> int:
    ap = argparse.ArgumentParser(description="把 switches.yaml 重建为「现金 → 建仓」")
    ap.add_argument("--from", dest="src", default=str(DEFAULT_SWITCHES),
                    help="计划持仓 + 日期的来源（建议用备份的原件）")
    ap.add_argument("--planned-from", dest="planned_from", default=None,
                    help="改为从 investors.yaml 取计划持仓（需在 --initial-cash 之前）")
    ap.add_argument("--switches", dest="out", default=str(DEFAULT_SWITCHES))
    ap.add_argument("--start-date", dest="start_date", default=None)
    ap.add_argument("--write", action="store_true", help="写入（默认只预览）")
    ap.add_argument("--emit", dest="emit", default=None,
                    help="把结果写到这个文件（绕开控制台编码问题）")
    args = ap.parse_args()

    src_p, out_p = Path(args.src), Path(args.out)
    fallback = args.start_date or _start_date(ROOT / "investors.yaml")

    date_of: dict[str, str] = {}
    order: list[str] = []
    if args.planned_from:
        src_records = load_planned_from_investors(Path(args.planned_from))
    else:
        src_records = parse_records(src_p)
    for r in src_records:
        date_of.setdefault(r["nickname"], r["date"])
        if r["nickname"] not in order:
            order.append(r["nickname"])

    # ---- 自噬防护：一个真实标的都没有 → 一定是读错了输入
    if not src_records:
        raise RebuildError(
            f"从 {src_p} 里没有解析出任何记录（含真实标的）。\n"
            f"  这通常是因为它已经是本脚本的输出（起点为现金、没有真实标的）。\n"
            f"  正确做法：用**原始备份**作为输入，例如\n"
            f"      python -m tools.rebuild_switches --from switches.yaml.bak --write")

    lines, warns = build(src_records, date_of, order, fallback)
    if not lines:
        raise RebuildError("重建结果为空，拒绝写入（请检查输入）")

    nicks_with = len({r["nickname"] for r in src_records})
    print(f"来源 {src_p.name}: {len(src_records)} 条原始记录，{nicks_with} 位投资者")
    print(f"重建后: {sum(1 for l in lines if l.startswith('  - '))} 条建仓记录"
          f"（其中 {sum(1 for l in lines if l.startswith('    nickname:'))} 条为多标的）")
    for w in warns:
        print(f"  [warn] {w}")

    text = head_text() + "\n".join(lines) + "\n"
    if args.emit:
        Path(args.emit).write_text(text, encoding="utf-8", newline="\n")
        print(f"已写出 -> {args.emit}（{len(lines)} 行）")
        return 0

    if not args.write:
        print()
        print(head_text())
        print("\n".join(lines))
        print("\n（预览模式，未写入；加 --write 才会写）")
        return 0

    if out_p.exists():
        bak = out_p.with_suffix(out_p.suffix + ".bak")
        shutil.copy2(out_p, bak)
        print(f"已备份原文件 -> {bak.name}")
    try:
        out_p.write_text(text, encoding="utf-8", newline="\n")
    except PermissionError:
        raise RebuildError(
            f"没有写权限：{out_p}\n"
            f"  沙箱下这个文件对 shell 只读。请改用 `--emit` 输出到临时文件，\n"
            f"  再用编辑工具落盘。")
    print(f"已写入 {out_p}（{len(lines)} 行）")
    return 0


def head_text() -> str:
    return HEADER


if __name__ == "__main__":
    sys.exit(main())

