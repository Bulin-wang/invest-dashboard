"""本地管理台的纯文件操作（无 streamlit 依赖，便于单测）。

职责：安全地往 `private/roster.csv`、`private/investors.private.yaml`、`switches.yaml` **追加**内容，
尽量保留既有文件的结构与注释（用文本插入，而不是整份重写）。

成员 / 调仓都支持两种写法：
- 单标的（旧）：`append_member` / `append_switch`（行为与以前一致）
- 多标的权重：`append_member_multi` / `append_switch_multi`（写 `holdings:` 列表）
"""
from __future__ import annotations

import csv
import re
from pathlib import Path


def legs_from_rows(rows) -> list[tuple[str, str, str, float]]:
    """把管理台表格的行解析成 legs = [(symbol, market, type, weight), ...]。

    - 代码为空的行忽略；`市场`/`类型` 非法时回落为 cn/stock；权重缺省 1.0（NaN 也算缺省）
    - **现金腿**：`类型=cash`，代码可留空（自动填 CASH）
    - 同一标的重复出现 → 权重相加（保序）
    """
    out: dict[tuple[str, str, str], float] = {}
    order: list[tuple[str, str, str]] = []
    for r in rows:
        typ = str(r.get("类型") or r.get("type") or "stock").strip().lower()
        if typ not in ("stock", "etf", "bond", "index", "cash"):
            typ = "stock"
        mkt = str(r.get("市场") or r.get("market") or "cn").strip().lower()
        if mkt not in ("cn", "us", "hk"):
            mkt = "cn"
        sym = str(r.get("代码") or r.get("symbol") or "").strip()
        if typ == "cash":
            sym = sym or "CASH"                     # 现金腿：代码可留空
        if not sym or sym.lower() in ("nan", "none"):
            continue
        raw = r.get("权重", r.get("weight", 1.0))
        try:
            w = float(raw)
        except (TypeError, ValueError):
            w = 1.0
        if w != w:                      # NaN
            w = 1.0
        if w <= 0:
            continue
        key = (sym, mkt, typ)
        if key not in out:
            order.append(key)
            out[key] = 0.0
        out[key] += w
    return [(s, m, t, out[(s, m, t)]) for s, m, t in order]


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


def _member_lines(real_name: str, legs) -> list[str]:
    """多标的成员的多行写法。legs = [(symbol, market, type, weight), ...]。"""
    out = [f"  - real_name: {real_name}", "    holdings:"]
    for symbol, market, type_, weight in legs:
        out.append(f'      - {{symbol: "{symbol}", market: {market}, '
                   f'type: {type_}, weight: {weight}}}')
    return out


def _insert_member_block(path: Path, new_lines: list[str]) -> None:
    """把若干行插到 investors.private.yaml 的 members: 块末尾（保留其余内容/注释）。"""
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
    lines[last + 1:last + 1] = new_lines
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def append_member(path: Path, real_name: str, symbol: str, market: str, type_: str) -> None:
    """插入一条**单标的**成员（等价于 weight=1）。"""
    _insert_member_block(path, [_member_line(real_name, symbol, market, type_)])


def append_member_multi(path: Path, real_name: str, legs) -> None:
    """插入一条**多标的**成员（写 `holdings:` 列表）。

    入参 legs = [(symbol, market, type, weight), ...]。
    """
    _insert_member_block(path, _member_lines(real_name, legs))


def _switch_line(date: str, nickname: str, symbol: str, market: str, type_: str) -> str:
    return (f'  - {{date: {date}, nickname: "{nickname}", symbol: "{symbol}", '
            f'market: {market}, type: {type_}}}')


def _switch_lines(date: str, nickname: str, legs) -> list[str]:
    """多标的调仓（= 提交新的目标权重向量）的多行写法。"""
    out = [f"  - date: {date}", f'    nickname: "{nickname}"', "    holdings:"]
    for symbol, market, type_, weight in legs:
        out.append(f'      - {{symbol: "{symbol}", market: {market}, '
                   f'type: {type_}, weight: {weight}}}')
    return out


def _append_switch_lines(path: Path, lines: list[str]) -> None:
    """把一组调仓记录追加到 switches.yaml 末尾（append-only）。"""
    body = "\n".join(lines)
    if not path.exists():
        path.write_text("switches:\n" + body + "\n", encoding="utf-8", newline="\n")
        return
    text = path.read_text(encoding="utf-8")
    if "switches:" not in text:
        text = text.rstrip("\n") + "\nswitches:\n"
    if not text.endswith("\n"):
        text += "\n"
    path.write_text(text + body + "\n", encoding="utf-8", newline="\n")


def append_switch(path: Path, date: str, nickname: str, symbol: str, market: str, type_: str) -> None:
    """追加一行**单标的**调仓（= 全仓切换到该标的；append-only）。"""
    _append_switch_lines(path, [_switch_line(date, nickname, symbol, market, type_)])


def append_switch_multi(path: Path, date: str, nickname: str, legs) -> None:
    """追加一条**按目标权重**调仓（写 `holdings:` 列表；append-only）。

    入参 legs = [(symbol, market, type, weight), ...]（权重内部归一化）。
    """
    _append_switch_lines(path, _switch_lines(date, nickname, legs))
