"""本地管理台的纯文件操作（无 streamlit 依赖，便于单测）。

职责：安全地往 `private/roster.csv`、`private/investors.private.yaml`、`switches.yaml` **追加**内容，
尽量保留既有文件的结构与注释（用文本插入，而不是整份重写）。
"""
from __future__ import annotations

import csv
import re
from pathlib import Path


def read_roster(path: Path) -> list[tuple[str, str]]:
    """读 roster.csv → [(real_name, nickname)]（保序）。"""
    if not path.exists():
        return []
    rows = csv.DictReader(path.read_text(encoding="utf-8-sig").splitlines())
    out: list[tuple[str, str]] = []
    for r in rows:
        rn = (r.get("real_name") or "").strip()
        nk = (r.get("nickname") or "").strip()
        if rn and nk:
            out.append((rn, nk))
    return out


def next_nickname(existing, prefix: str = "investor", width: int = 2) -> str:
    """在已有昵称里找 `prefix数字` 的最大编号 +1（如 investor07 → investor08）。"""
    mx = 0
    for nk in existing:
        m = re.fullmatch(rf"{re.escape(prefix)}(\d+)", str(nk))
        if m:
            mx = max(mx, int(m.group(1)))
    return f"{prefix}{mx + 1:0{width}d}"


def append_roster(path: Path, real_name: str, nickname: str) -> None:
    """追加一行到 roster.csv（不存在则先写表头）。"""
    if not path.exists():
        path.write_text("real_name,nickname\n", encoding="utf-8", newline="\n")
    with path.open("a", encoding="utf-8", newline="") as f:
        csv.writer(f).writerow([real_name, nickname])


def _member_line(real_name: str, symbol: str, market: str, type_: str) -> str:
    return (f'  - {{real_name: {real_name}, symbol: "{symbol}", '
            f'market: {market}, type: {type_}}}')


def append_member(path: Path, real_name: str, symbol: str, market: str, type_: str) -> None:
    """把一行成员插到 investors.private.yaml 的 members: 块末尾（保留其余内容/注释）。"""
    if not path.exists():
        raise FileNotFoundError(f"找不到 {path}")
    lines = path.read_text(encoding="utf-8").splitlines()
    mi = next((i for i, l in enumerate(lines) if l.strip() == "members:"), None)
    if mi is None:
        raise ValueError(f"{path} 里找不到 `members:`")
    # members 块的边界：下一个顶格（非缩进、非注释）的键，如 benchmarks:
    end = len(lines)
    for j in range(mi + 1, len(lines)):
        s = lines[j]
        if s and not s[0].isspace() and not s.lstrip().startswith("#"):
            end = j
            break
    # 最后一条成员行的位置
    last = mi
    for j in range(mi + 1, end):
        if lines[j].lstrip().startswith("- "):
            last = j
    lines.insert(last + 1, _member_line(real_name, symbol, market, type_))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def _switch_line(date: str, nickname: str, symbol: str, market: str, type_: str) -> str:
    return (f'  - {{date: {date}, nickname: "{nickname}", symbol: "{symbol}", '
            f'market: {market}, type: {type_}}}')


def append_switch(path: Path, date: str, nickname: str, symbol: str, market: str, type_: str) -> None:
    """追加一行到 switches.yaml 末尾（append-only）。"""
    line = _switch_line(date, nickname, symbol, market, type_)
    if not path.exists():
        path.write_text("switches:\n" + line + "\n", encoding="utf-8", newline="\n")
        return
    text = path.read_text(encoding="utf-8")
    if "switches:" not in text:
        text = text.rstrip("\n") + "\nswitches:\n"
    if not text.endswith("\n"):
        text += "\n"
    path.write_text(text + line + "\n", encoding="utf-8", newline="\n")
