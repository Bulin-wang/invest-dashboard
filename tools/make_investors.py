"""本地匿名化：把**真实名单**转成可公开的 `investors.yaml`。

隐私设计
--------
- 输入：`private/investors.private.yaml`（**私密，已 gitignore**）——含真实姓名 + 标的；
- 输出：`investors.yaml`（**公开**）——只含昵称（Alice/Bob/…）+ 标的，**不含真实姓名**；
- 同时把「真实姓名 ↔ 昵称」的反向映射写到 `private/nickname_map.csv`（私密），供你自己核对。

这样别人 clone 仓库只能看到「Alice 持有 600519」，看不到真实身份；
昵称分配规则与原始名单都留在本地，不入库。

昵称规则
--------
- 按私密清单里的**顺序**依次分配 `NICKNAMES` 里的名字；人数超过名字池时追加序号（Alice2、Bob2…）。
- 想换昵称风格，改 `NICKNAMES` 即可（生成结果稳定、可复现）。

用法：
    python -m tools.make_investors                     # 默认读 private/investors.private.yaml
    python -m tools.make_investors --in xxx.yaml --out investors.yaml
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_IN = ROOT / "private" / "investors.private.yaml"
DEFAULT_OUT = ROOT / "investors.yaml"
DEFAULT_MAP = ROOT / "private" / "nickname_map.csv"

# 昵称池（够 ~80 人；不够时自动追加序号）。顺序即分配顺序。
NICKNAMES = [
    "Alice", "Bob", "Carol", "Dave", "Eve", "Frank", "Grace", "Heidi", "Ivan", "Judy",
    "Mallory", "Niaj", "Olivia", "Peggy", "Quan", "Rupert", "Sybil", "Trent", "Uma", "Victor",
    "Walter", "Xena", "Yuri", "Zoe", "Aaron", "Bella", "Caleb", "Diana", "Ethan", "Fiona",
    "George", "Hannah", "Isaac", "Julia", "Kevin", "Luna", "Mason", "Nina", "Oscar", "Paula",
    "Quinn", "Rachel", "Sam", "Tina", "Ulysses", "Vera", "Wendy", "Xavier", "Yolanda", "Zach",
    "Adam", "Bianca", "Carlos", "Denise", "Edward", "Freya", "Gabriel", "Helen", "Igor", "Janet",
    "Karl", "Leo", "Mia", "Noah", "Olga", "Pablo", "Rosa", "Simon", "Tara", "Umar",
    "Violet", "Will", "Xin", "Yara", "Zane", "Amy", "Bruno", "Cindy", "Derek", "Elena",
]

HEADER = """\
# ==========================================================================
# 投资组合清单（公开版）—— 只有昵称，**不含真实姓名**
# --------------------------------------------------------------------------
# 本文件由 `python -m tools.make_investors` 从私有名单生成，请勿手改（会被覆盖）。
# 真实姓名 ↔ 昵称 的映射保存在本地 private/（已 gitignore），不入库、不公开。
#
# 口径：每人持有 1 个标的，本金 principal（元），自 start_date 起按标的
#       **未复权价格收益**折算市值 = principal × close(t)/close(start_date)。
# ==========================================================================
"""


def _assign_nicknames(n: int) -> list[str]:
    out: list[str] = []
    total = len(NICKNAMES)
    for i in range(n):
        if i < total:
            out.append(NICKNAMES[i])
        else:  # 人数超过名字池：追加轮次序号
            out.append(f"{NICKNAMES[i % total]}{i // total + 1}")
    return out


def _rel(p: Path) -> str:
    """尽量显示相对项目根的路径；不在根下（如临时目录）时回退为原路径。"""
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


def _fmt_flow(d: dict) -> str:
    """把 dict 渲染成 `{k: v, ...}` 的行内形式，尽量贴合仓库既有清单风格。"""
    parts = []
    for k, v in d.items():
        key = k
        if isinstance(v, str):
            val = f'"{v}"'
        else:
            val = str(v)
        parts.append(f"{key}: {val}")
    return "{" + ", ".join(parts) + "}"


def build(src: Path, out: Path, map_path: Path) -> int:
    if not src.exists():
        raise SystemExit(
            f"找不到私密名单 {src}。请先创建它（例如从模板复制），"
            f"内容是 `members: [{{real_name, symbol, market, type}}]`。")
    cfg = yaml.safe_load(src.read_text(encoding="utf-8")) or {}
    members = cfg.get("members", [])
    if not members:
        raise SystemExit(f"{src} 里没有 members 条目")

    nicks = _assign_nicknames(len(members))
    investors, mapping = [], []
    for m, nick in zip(members, nicks):
        rec = {
            "nickname": nick,
            "symbol": str(m["symbol"]),
            "market": str(m["market"]).lower(),
            "type": str(m.get("type", "stock")),
        }
        investors.append(rec)
        mapping.append((str(m.get("real_name", "")), nick, rec["symbol"]))

    # 逐行手写，保留行内风格与稳定字段顺序
    lines = [HEADER.rstrip("\n")]
    lines.append(f"base_currency: {cfg.get('base_currency', 'CNY')}")
    lines.append(f"start_date: {cfg.get('start_date', '2026-09-24')}")
    lines.append(f"principal: {cfg.get('principal', 1000000)}")
    lines.append("")
    lines.append("investors:")
    for rec in investors:
        lines.append("  - " + _fmt_flow(rec))
    benches = cfg.get("benchmarks", [])
    if benches:
        lines.append("")
        lines.append("# 基准指数（看板可叠加对比，不参与组合收益）")
        lines.append("benchmarks:")
        for b in benches:
            lines.append("  - " + _fmt_flow({k: str(v) for k, v in b.items()}))
    out.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    # 私密反向映射
    map_path.parent.mkdir(parents=True, exist_ok=True)
    rows = ["real_name,nickname,symbol"] + [f"{a},{b},{c}" for a, b, c in mapping]
    map_path.write_text("\n".join(rows) + "\n", encoding="utf-8", newline="\n")

    print(f"[ok] {len(investors)} 位投资者 -> {_rel(out)}（仅昵称）")
    print(f"[ok] 私密映射 -> {_rel(map_path)}（勿上传）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="从私密名单生成公开的 investors.yaml")
    ap.add_argument("--in", dest="src", default=str(DEFAULT_IN))
    ap.add_argument("--out", dest="out", default=str(DEFAULT_OUT))
    ap.add_argument("--map", dest="map_path", default=str(DEFAULT_MAP))
    args = ap.parse_args()
    return build(Path(args.src), Path(args.out), Path(args.map_path))


if __name__ == "__main__":
    sys.exit(main())
