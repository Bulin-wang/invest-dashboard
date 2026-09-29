"""配置与路径：读取 investors.yaml（投资组合）/ holdings.yaml（备用后端），提供全局路径常量。"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

# 项目根目录（src/ 的上一级）
ROOT = Path(__file__).resolve().parent.parent

HOLDINGS_PATH = ROOT / "holdings.yaml"
INVESTORS_PATH = ROOT / "investors.yaml"      # 投资组合清单（公开版，仅昵称）
SWITCHES_PATH = ROOT / "switches.yaml"        # 调仓流水（append-only，公开版）
PRIVATE_DIR = ROOT / "private"                # 本地私密目录（真实名单，gitignore）
DATA_DIR = ROOT / "data"
PRICES_DIR = DATA_DIR / "prices"
RETURNS_DIR = DATA_DIR / "returns"
PORTFOLIOS_DIR = DATA_DIR / "portfolios"      # 每个投资者的组合收益序列
INDEX_DIR = DATA_DIR / "index"
INDEX_PATH = INDEX_DIR / "equal_weight.csv"
META_PATH = DATA_DIR / "meta.json"

# 组合默认参数（可在 investors.yaml 顶层覆盖）
DEFAULT_PRINCIPAL = 1_000_000.0   # 每人本金（元）
DEFAULT_START_DATE = "2026-09-24"  # 统一起始/基准日

# --- Tushare MCP（A股个股 / 指数 数据源）-------------------------------------
# Token 属于敏感信息，不写进代码/仓库。按优先级取值：
#   1) 环境变量 TUSHARE_MCP_URL（完整地址，最高优先级）
#   2) 环境变量 TUSHARE_TOKEN（只给 token，自动拼地址）
#   3) 项目根目录下的本地文件 .tushare_token（已 gitignore，便于本地开发）
# 都取不到时返回 None；只有真正要用 Tushare 时才会报错提示如何配置。
_TUSHARE_MCP_BASE = "https://api.tushare.pro/mcp/"


def _load_tushare_url() -> str | None:
    url = os.environ.get("TUSHARE_MCP_URL")
    if url:
        return url.strip()
    token = os.environ.get("TUSHARE_TOKEN")
    if not token:
        token_file = ROOT / ".tushare_token"
        if token_file.exists():
            token = token_file.read_text(encoding="utf-8").strip()
    if token:
        return f"{_TUSHARE_MCP_BASE}?token={token}"
    return None


TUSHARE_MCP_URL = _load_tushare_url()

VALID_MARKETS = {"cn", "us", "hk"}
VALID_TYPES = {"stock", "etf", "bond", "index"}


@dataclass
class Item:
    """一个标的（持仓或基准）。"""

    name: str
    symbol: str
    market: str          # cn / us / hk
    type: str            # stock / etf / bond / index
    start_date: str | None = None   # 基准可为空
    weight: float | None = None     # 组合权重，预留

    @property
    def key(self) -> str:
        """用于文件名 / 列名的稳定键，如 cn_stock_600519、us_index_GSPC。

        含 ``type`` 是为了消歧：同一 market 下不同品种可有相同代码
        （如 ``cn_stock_000001`` 平安银行 vs ``cn_index_000001`` 上证指数）。
        """
        safe = self.symbol.replace("^", "").replace("=", "_")
        return f"{self.market}_{self.type}_{safe}"

    @property
    def is_benchmark(self) -> bool:
        return self.type == "index"


def _parse(entry: dict) -> Item:
    market = str(entry["market"]).lower()
    typ = str(entry.get("type", "stock")).lower()
    if market not in VALID_MARKETS:
        raise ValueError(f"未知 market={market!r}（应为 cn/us/hk）")
    if typ not in VALID_TYPES:
        raise ValueError(f"未知 type={typ!r}（应为 {sorted(VALID_TYPES)}）")
    start = entry.get("start_date")
    return Item(
        name=str(entry["name"]),
        symbol=str(entry["symbol"]),
        market=market,
        type=typ,
        start_date=str(start) if start else None,
        weight=entry.get("weight"),
    )


def _load(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"找不到 {path}")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_holdings(path: Path = HOLDINGS_PATH) -> list[Item]:
    cfg = _load(path)
    items = [_parse(h) for h in cfg.get("holdings", [])]
    for it in items:
        if not it.start_date:
            raise ValueError(f"持仓 {it.name}({it.symbol}) 缺少 start_date")
    return items


def load_benchmarks(path: Path = HOLDINGS_PATH) -> list[Item]:
    cfg = _load(path)
    return [_parse(b) for b in cfg.get("benchmarks", [])]


def load_base_currency(path: Path = HOLDINGS_PATH) -> str:
    return str(_load(path).get("base_currency", "CNY"))


# --------------------------------------------------------------------------- 投资组合（investors.yaml）
@dataclass
class Investor:
    """一位投资者：持有**单个**标的，本金 principal（元），自 start_date 起计。"""

    nickname: str          # 展示用昵称（真实姓名映射在本地，不入库）
    symbol: str
    market: str            # cn / us / hk
    type: str = "stock"    # stock / etf / bond / index
    start_date: str | None = None
    principal: float = DEFAULT_PRINCIPAL

    @property
    def key(self) -> str:
        """用于文件名 / 列名的稳定键，如 cn_stock_600519、us_index_GSPC（与 Item.key 同构）。

        含 ``type`` 以消歧：同一 market 下不同品种可有相同代码。
        """
        safe = self.symbol.replace("^", "").replace("=", "_")
        return f"{self.market}_{self.type}_{safe}"

    @property
    def name(self) -> str:
        """显示名 = 昵称（便于复用按 `name` 取标签的通用逻辑）。"""
        return self.nickname

    @property
    def is_benchmark(self) -> bool:
        return False


def load_investor_config(path: Path = INVESTORS_PATH) -> dict:
    """读 investors.yaml 的顶层参数：{start_date, principal, base_currency}。"""
    cfg = _load(path)
    start = cfg.get("start_date", DEFAULT_START_DATE)
    return {
        "start_date": str(start) if start else None,
        "principal": float(cfg.get("principal", DEFAULT_PRINCIPAL)),
        "base_currency": str(cfg.get("base_currency", "CNY")),
    }


def load_investors(path: Path = INVESTORS_PATH) -> list[Investor]:
    """读投资者清单。start_date / principal 可逐条覆盖顶层默认值。"""
    cfg = _load(path)
    default_start = cfg.get("start_date", DEFAULT_START_DATE)
    default_principal = float(cfg.get("principal", DEFAULT_PRINCIPAL))
    out: list[Investor] = []
    for h in cfg.get("investors", []):
        market = str(h["market"]).lower()
        typ = str(h.get("type", "stock")).lower()
        if market not in VALID_MARKETS:
            raise ValueError(f"未知 market={market!r}（应为 cn/us/hk）")
        if typ not in VALID_TYPES:
            raise ValueError(f"未知 type={typ!r}（应为 {sorted(VALID_TYPES)}）")
        start = h.get("start_date", default_start)
        out.append(Investor(
            nickname=str(h["nickname"]),
            symbol=str(h["symbol"]),
            market=market,
            type=typ,
            start_date=str(start) if start else None,
            principal=float(h.get("principal", default_principal)),
        ))
    if not out:
        raise ValueError(f"{path} 里没有 investors 条目")
    return out


def load_investor_benchmarks(path: Path = INVESTORS_PATH) -> list[Item]:
    """读 investors.yaml 的 benchmarks（基准指数，不参与组合收益）。"""
    cfg = _load(path)
    return [_parse(b) for b in cfg.get("benchmarks", [])]


# --------------------------------------------------------------------------- 调仓流水（switches.yaml）
@dataclass
class Switch:
    """一次**全仓切换**：某投资者自 date（收盘）从当前标的整体换到该标的。"""

    date: str
    nickname: str
    symbol: str
    market: str            # cn / us / hk
    type: str = "stock"

    @property
    def key(self) -> str:
        safe = self.symbol.replace("^", "").replace("=", "_")
        return f"{self.market}_{self.type}_{safe}"

    def item(self, name: str | None = None) -> Item:
        return Item(name=name or self.nickname, symbol=self.symbol,
                    market=self.market, type=self.type)


def portfolio_id(nickname: str) -> str:
    """昵称 → 组合文件名（去掉不适合做文件名的字符）。"""
    return re.sub(r"[^0-9A-Za-z_\-]", "_", nickname) or "investor"


def load_switches(path: Path = SWITCHES_PATH) -> list[Switch]:
    """读 switches.yaml 的调仓流水（append-only）。文件不存在时返回空表（= 每人单段）。"""
    if not path.exists():
        return []
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: list[Switch] = []
    for s in cfg.get("switches", []):
        market = str(s["market"]).lower()
        typ = str(s.get("type", "stock")).lower()
        if market not in VALID_MARKETS:
            raise ValueError(f"未知 market={market!r}（应为 cn/us/hk）")
        if typ not in VALID_TYPES:
            raise ValueError(f"未知 type={typ!r}（应为 {sorted(VALID_TYPES)}）")
        out.append(Switch(date=str(s["date"]), nickname=str(s["nickname"]),
                          symbol=str(s["symbol"]), market=market, type=typ))
    return out


def investor_segments(investors: list[Investor],
                      switches: list[Switch]) -> dict[str, list[tuple[str, Item]]]:
    """把「初始持仓 + 调仓流水」合成为每位投资者的持仓路径（segments）。

    返回 {nickname: [(起始日, Item), (调仓日, Item), ...]}，按日期升序；
    首段 = investors.yaml 里的初始持仓（date = 该投资者 start_date）。
    """
    by_nick: dict[str, list[Switch]] = {}
    for s in switches:
        by_nick.setdefault(s.nickname, []).append(s)
    out: dict[str, list[tuple[str, Item]]] = {}
    for inv in investors:
        start = inv.start_date
        segs: list[tuple[str, Item]] = [(start, Item(
            name=inv.nickname, symbol=inv.symbol, market=inv.market,
            type=inv.type, start_date=start))]
        for s in sorted(by_nick.get(inv.nickname, []), key=lambda x: x.date):
            segs.append((s.date, s.item(inv.nickname)))
        out[inv.nickname] = segs
    return out
