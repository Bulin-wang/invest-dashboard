"""本地匿名化：把「成员名单（真名+标的）」+「真名↔昵称 映射」合成为公开的 `investors.yaml`。

输入（都在 private/，已 gitignore）：
- `private/roster.csv`            —— **权威身份映射**：`real_name,nickname`（手动维护、稳定不重排）
- `private/investors.private.yaml` —— 两种成员写法（可混用）+ 顶层参数 + benchmarks：
    * 单标的：`{real_name, symbol, market, type}`
    * 多标的：`{real_name, holdings: [{symbol, market, type, weight}, ...]}`（weight 省略 = 等权）
输出（公开）：
- `investors.yaml` —— 只含 `{nickname, symbol, market, type}` 或
  `{nickname, holdings: [...]}` + benchmarks，**不含真实姓名**

隐私：真名只留在 private/；`investors.yaml` 对外只出现昵称。
维护：加人 = 往 roster.csv 追加一行（分配一个没用过的昵称）+ 往 members 追加一行（真名 + 初始标的）；
两者靠 `real_name` 关联。

用法：
    python -m tools.make_investors
    python -m tools.make_investors --members private/investors.private.yaml --roster private/roster.csv --out investors.yaml
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MEMBERS = ROOT / "private" / "investors.private.yaml"
DEFAULT_ROSTER = ROOT / "private" / "roster.csv"
DEFAULT_OUT = ROOT / "investors.yaml"

HEADER = """\
# ==========================================================================
# 投资组合清单（公开版）—— 只有昵称，**不含真实姓名**
# --------------------------------------------------------------------------
# 由 `python -m tools.make_investors` 从 private/roster.csv（真名↔昵称）
# 与 private/investors.private.yaml（真名+标的）合成，请勿手改（会被覆盖）。
#
# 口径：每人有一份**初始持仓**（单个标的，或 `holdings:` 多标的权重组合），本金 principal（元），
#       自 start_date 起按未复权价格收益折算市值；调仓见 switches.yaml。
# ==========================================================================
"""


def _rel(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


def _fmt_flow(d: dict) -> str:
    parts = []
    for k, v in d.items():
        parts.append(f'{k}: "{v}"' if isinstance(v, str) else f"{k}: {v}")
    return "{" + ", ".join(parts) + "}"


def load_roster(path: Path) -> dict[str, str]:
    """读 real_name → nickname 映射。"""
    if not path.exists():
        raise SystemExit(f"找不到映射文件 {path}（格式：real_name,nickname）")
    rows = list(csv.DictReader(path.read_text(encoding="utf-8-sig").splitlines()))
    out: dict[str, str] = {}
    for r in rows:
        rn = (r.get("real_name") or "").strip()
        nk = (r.get("nickname") or "").strip()
        if not rn or not nk:
            continue
        if rn in out:
            raise SystemExit(f"{path} 里 real_name 重复：{rn}")
        out[rn] = nk
    if not out:
        raise SystemExit(f"{path} 里没有有效行（real_name,nickname）")
    return out


def build(members_path: Path = DEFAULT_MEMBERS,
          roster_path: Path = DEFAULT_ROSTER,
          out: Path = DEFAULT_OUT) -> int:
    if not members_path.exists():
        raise SystemExit(f"找不到成员名单 {members_path}")
    cfg = yaml.safe_load(members_path.read_text(encoding="utf-8")) or {}
    members = cfg.get("members", [])
    if not members:
        raise SystemExit(f"{members_path} 里没有 members 条目")

    roster = load_roster(roster_path)

    investors, seen = [], set()
    for m in members:
        rn = str(m.get("real_name", "")).strip()
        if rn not in roster:
            raise SystemExit(f"成员 {rn!r} 在 {_rel(roster_path)} 里没有昵称映射")
        nk = roster[rn]
        if nk in seen:
            raise SystemExit(f"昵称重复：{nk}（real_name={rn}）")
        seen.add(nk)
        if m.get("holdings"):
            legs = []
            for h in m["holdings"]:
                typ = str(h.get("type", "stock"))
                # **现金腿**没有 symbol（与 src.config._parse_leg 的兜底保持一致）；
                # 不做这个兜底会在初始持仓里用 holdings+cash 时 KeyError。
                sym = str(h.get("symbol") or ("CASH" if typ.lower() == "cash" else ""))
                if not sym:
                    raise SystemExit(
                        f"成员 {rn!r} 的 holdings 有一条既没有 symbol 也不是 cash：{h}")
                leg = {"symbol": sym,
                       "market": str(h.get("market") or "cn").lower(),
                       "type": typ}
                if h.get("weight") is not None:
                    leg["weight"] = float(h["weight"])
                if h.get("expires"):
                    leg["expires"] = str(h["expires"])
                legs.append(leg)
            investors.append({"nickname": nk, "holdings": legs})
        else:
            rec = {
                "nickname": nk,
                "symbol": str(m["symbol"]),
                "market": str(m["market"]).lower(),
                "type": str(m.get("type", "stock")),
            }
            if m.get("expires"):
                rec["expires"] = str(m["expires"])
            investors.append(rec)

    lines = [HEADER.rstrip("\n")]
    lines.append(f"base_currency: {cfg.get('base_currency', 'CNY')}")
    lines.append(f"start_date: {cfg.get('start_date', '2026-09-24')}")
    lines.append(f"principal: {cfg.get('principal', 1000000)}")
    lines.append("")
    lines.append("investors:")
    for rec in investors:
        if "holdings" in rec:
            lines.append('  - nickname: "%s"' % rec["nickname"])
            lines.append("    holdings:")
            for leg in rec["holdings"]:
                lines.append("      - " + _fmt_flow(leg))
        else:
            lines.append("  - " + _fmt_flow(rec))
    benches = cfg.get("benchmarks", [])
    if benches:
        lines.append("")
        lines.append("# 基准指数（看板可叠加对比，不参与组合收益）")
        lines.append("benchmarks:")
        for b in benches:
            lines.append("  - " + _fmt_flow({k: str(v) for k, v in b.items()}))
    out.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    print(f"[ok] {len(investors)} 位投资者 -> {_rel(out)}（仅昵称）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="从私有名单生成公开的 investors.yaml")
    ap.add_argument("--members", dest="members", default=str(DEFAULT_MEMBERS))
    ap.add_argument("--roster", dest="roster", default=str(DEFAULT_ROSTER))
    ap.add_argument("--out", dest="out", default=str(DEFAULT_OUT))
    args = ap.parse_args()
    return build(Path(args.members), Path(args.roster), Path(args.out))


if __name__ == "__main__":
    sys.exit(main())
