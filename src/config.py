"""配置与路径：读取 holdings.yaml，提供全局路径常量。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

# 项目根目录（src/ 的上一级）
ROOT = Path(__file__).resolve().parent.parent

HOLDINGS_PATH = ROOT / "holdings.yaml"
DATA_DIR = ROOT / "data"
PRICES_DIR = DATA_DIR / "prices"
RETURNS_DIR = DATA_DIR / "returns"
INDEX_DIR = DATA_DIR / "index"
INDEX_PATH = INDEX_DIR / "equal_weight.csv"
META_PATH = DATA_DIR / "meta.json"

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

VALID_MARKETS = {"cn", "us"}
VALID_TYPES = {"stock", "etf", "bond", "index"}


@dataclass
class Item:
    """一个标的（持仓或基准）。"""

    name: str
    symbol: str
    market: str          # cn / us
    type: str            # stock / etf / bond / index
    start_date: str | None = None   # 基准可为空
    weight: float | None = None     # 组合权重，预留

    @property
    def key(self) -> str:
        """用于文件名 / 列名的稳定键，如 cn_600519、us_AAPL。"""
        safe = self.symbol.replace("^", "").replace("=", "_")
        return f"{self.market}_{safe}"

    @property
    def is_benchmark(self) -> bool:
        return self.type == "index"


def _parse(entry: dict) -> Item:
    market = str(entry["market"]).lower()
    typ = str(entry.get("type", "stock")).lower()
    if market not in VALID_MARKETS:
        raise ValueError(f"未知 market={market!r}（应为 cn/us）")
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
