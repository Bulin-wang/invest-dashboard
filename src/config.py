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
FUND_CACHE_DIR = DATA_DIR / "fund_cache"      # 开放式基金全量净值缓存（累计净值口径）
CRYPTO_CACHE_DIR = DATA_DIR / "crypto_cache"  # 加密货币全量日线缓存（UTC 日线收盘价）
FUTURES_CACHE_DIR = DATA_DIR / "futures_cache"  # 期货全量日线缓存（结算价口径）

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
# type 的取值。注意：它同时是**数据源路由**与**文件名 key** 的一部分
# （key = {market}_{type}_{symbol}），所以新增类型意味着两件事：
#   1) 抓取层要给它一个取数分支（见 prototype/fund_fetch.py、prototype/crypto_fetch.py）；
#   2) 同一标的不同人必须写**同一个** type，否则会各抓一份、各算一套收益。
# fund   = **场外开放式基金**（无交易所前缀，走东方财富净值接口，累计净值口径）；
#          场内 ETF/LOF 仍写 etf。
# crypto = **加密货币现货交易对**（走币安公开镜像，UTC 日线收盘价，**含周末**）。
#          symbol 写 BTCUSDT 或简写 BTC（自动补 USDT）；key 形如 crypto_BTCUSDT。
# futures= **期货合约**（走新浪期货接口，结算价口径；**有到期日**，见 `expires`）。
#          symbol 写合约代码，如 SC2611；目前仅支持上期能源原油 SC。
VALID_TYPES = {"stock", "etf", "bond", "index", "fund", "crypto", "futures", "cash"}
CASH_KEY = "cash"        # 现金腿的固定 key：不抓行情、价格恒为 1


def _parse_date(v):
    """可选日期字段：空串 / None → None，否则规范成 ``YYYY-MM-DD`` 字符串。"""
    if v is None or v == "":
        return None
    return str(v)


@dataclass
class Item:
    """一个标的（持仓或基准）。

    **``expires``（可选）**：该标的的到期日（期货 = 最后交易日）。
    规则：**当日仍有价格，次日起视为现金**——持有到期而未调仓，余额自动变现金。
    语义上只对"到期日事先可知、且到期后不再有价格"的标的成立（期货/期权等衍生品）；
    股票/ETF/基金/加密通常不需要填。留空 = 永不到期。
    """

    name: str
    symbol: str
    market: str          # cn / us / hk
    type: str            # stock / etf / bond / index / fund / crypto / futures / cash
    start_date: str | None = None   # 基准可为空
    weight: float | None = None     # 组合权重，预留
    expires: str | None = None      # 到期日 YYYY-MM-DD（期货的最后交易日）；None = 不到期

    @property
    def key(self) -> str:
        """用于文件名 / 列名的稳定键，如 cn_stock_600519、us_index_GSPC、cn_fund_002910。

        含 ``type`` 是为了消歧：同一 market 下不同品种可有相同代码
        （如 ``cn_stock_000001`` 平安银行 vs ``cn_index_000001`` 上证指数）。
        **现金腿**固定为 ``cash``；**场外基金**为 ``cn_fund_{6位代码}``；
        **期货**为 ``cn_futures_SC2611``。
        """
        if self.type == "cash":
            return CASH_KEY
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
        expires=_parse_date(entry.get("expires")),
    )


def _parse_leg(entry: dict, default_name: str) -> Item:
    """解析一条「持仓腿」（`holdings` 列表的元素，或单标的的扁平写法）。

    **现金腿**：`type: cash`（`symbol` 可省略，`market` 无意义，一律记 cn）。
    """
    typ = str(entry.get("type", "stock")).lower()
    market = str(entry.get("market") or "cn").lower()
    if typ == "cash":
        symbol, market = str(entry.get("symbol") or "CASH"), "cn"
    else:
        if market not in VALID_MARKETS:
            raise ValueError(f"未知 market={market!r}（应为 cn/us/hk）")
        if not entry.get("symbol"):
            raise ValueError("持仓腿缺少 symbol")
        symbol = str(entry["symbol"])
    if typ not in VALID_TYPES:
        raise ValueError(f"未知 type={typ!r}（应为 {sorted(VALID_TYPES)}）")
    start = entry.get("start_date")
    weight = entry.get("weight")
    return Item(
        name=str(entry.get("name") or default_name),
        symbol=symbol,
        market=market,
        type=typ,
        start_date=str(start) if start else None,
        weight=float(weight) if weight is not None else None,
        expires=_parse_date(entry.get("expires")),
    )


def _parse_legs(entry: dict, owner: str) -> list[Item]:
    """一条 entry 的持仓腿：`holdings: [...]` = 多标的；否则按扁平 symbol/market/type = 单标的。

    兼容旧写法：单标的 entry 不写 holdings，等价于一条 ``weight=1.0`` 的腿。
    """
    raw = entry.get("holdings")
    if raw:
        legs = [_parse_leg(h, owner) for h in raw]
        keys = [it.key for it in legs]
        if len(set(keys)) != len(keys):
            raise ValueError(f"{owner} 的 holdings 有重复标的：{keys}")
    else:
        legs = [_parse_leg(entry, owner)]
    if not legs:
        raise ValueError(f"{owner} 的 holdings 为空")
    return legs


def normalized_weights(legs: list[Item]) -> dict[str, float]:
    """腿列表 → {key: 权重}，归一化到和为 1。

    权重缺失 = 等权：全部未给 → 等分；部分给出 → 剩余份额由未给的腿等分。
    """
    if not legs:
        raise ValueError("没有持仓腿")
    given = {it.key: float(it.weight) for it in legs if it.weight is not None}
    rest = [it.key for it in legs if it.weight is None]
    if any(v < 0 for v in given.values()):
        raise ValueError(f"权重不能为负：{given!r}")
    if rest:
        share = max(0.0, 1.0 - sum(given.values())) / len(rest)
        raw = {it.key: (float(it.weight) if it.weight is not None else share) for it in legs}
    else:
        raw = given
    total = sum(raw.values())
    if total <= 0:
        raise ValueError(f"权重非法（需要至少一个正权重）：{raw!r}")
    return {k: v / total for k, v in raw.items()}


def holding_spec(legs: list[Item]) -> str:
    """腿列表 → `holding` 列字符串（单腿 = 裸 key，兼容旧格式）。"""
    from src.aggregate import holding_spec as _spec
    return _spec(normalized_weights(legs))


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
    market: str            # cn / us / hk（crypto 统一记 us，仅用于分组，不影响取数）
    type: str = "stock"    # stock / etf / bond / index / fund / crypto / futures / cash
    start_date: str | None = None
    principal: float = DEFAULT_PRINCIPAL
    holdings: list[Item] | None = None   # 多标的持仓腿（单标的时为空，见 .legs）
    expires: str | None = None           # 到期日（期货 = 最后交易日）；None = 不到期

    @property
    def key(self) -> str:
        """用于文件名 / 列名的稳定键，如 cn_stock_600519、us_index_GSPC（与 Item.key 同构）。

        含 ``type`` 以消歧：同一 market 下不同品种可有相同代码；**现金腿**固定为 ``cash``。
        ``fund`` 的 symbol 会补足 6 位并去掉 sh/sz 前缀（见 prototype.fund_fetch.normalize_code），
        以保证同一只基金只对应一份价格文件。
        """
        if self.type == "cash":
            return CASH_KEY
        safe = self.symbol.replace("^", "").replace("=", "_")
        return f"{self.market}_{self.type}_{safe}"

    @property
    def name(self) -> str:
        """显示名 = 昵称（便于复用按 `name` 取标签的通用逻辑）。"""
        return self.nickname

    @property
    def is_benchmark(self) -> bool:
        return False

    @property
    def legs(self) -> list[Item]:
        """持仓腿列表：多标的取 holdings；单标的视为一条 weight=1 的腿。"""
        if self.holdings:
            return list(self.holdings)
        return [Item(name=self.nickname, symbol=self.symbol, market=self.market,
                     type=self.type, start_date=self.start_date, weight=1.0,
                     expires=self.expires)]

    @property
    def weights(self) -> dict[str, float]:
        """{key: 归一化权重}。"""
        return normalized_weights(self.legs)

    @property
    def holding_spec(self) -> str:
        """`holding` 列字符串（单标的 = 裸 key）。"""
        return holding_spec(self.legs)


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
        nick = str(h["nickname"])
        legs = _parse_legs(h, nick)
        first = legs[0]
        start = h.get("start_date", default_start)
        out.append(Investor(
            nickname=nick,
            symbol=first.symbol,
            market=first.market,
            type=first.type,
            start_date=str(start) if start else None,
            principal=float(h.get("principal", default_principal)),
            holdings=legs if len(legs) > 1 else None,
            expires=first.expires,
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
    holdings: list[Item] | None = None   # 多标的调仓（单标的时为 None，见 .legs）
    expires: str | None = None           # 新标的的到期日（期货 = 最后交易日）

    @property
    def key(self) -> str:
        if self.type == "cash":          # 现金腿固定 key
            return CASH_KEY
        safe = self.symbol.replace("^", "").replace("=", "_")
        return f"{self.market}_{self.type}_{safe}"

    def item(self, name: str | None = None) -> Item:
        return Item(name=name or self.nickname, symbol=self.symbol,
                    market=self.market, type=self.type, expires=self.expires)

    @property
    def legs(self) -> list[Item]:
        """调仓后的持仓腿（单标的 = 一条 weight=1 的腿）。"""
        if self.holdings:
            return list(self.holdings)
        return [self.item()]

    @property
    def weights(self) -> dict[str, float]:
        return normalized_weights(self.legs)

    @property
    def holding_spec(self) -> str:
        return holding_spec(self.legs)


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
        nick = str(s["nickname"])
        legs = _parse_legs(s, nick)
        first = legs[0]
        out.append(Switch(date=str(s["date"]), nickname=nick,
                          symbol=first.symbol, market=first.market, type=first.type,
                          holdings=legs if len(legs) > 1 else None,
                          expires=first.expires))
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
            type=inv.type, start_date=start, expires=inv.expires))]
        for s in sorted(by_nick.get(inv.nickname, []), key=lambda x: x.date):
            segs.append((s.date, s.item(inv.nickname)))
        out[inv.nickname] = segs
    return out


def investor_weight_segments(
        investors: list[Investor],
        switches: list[Switch]) -> dict[str, list[tuple[str, list[tuple[Item, float]]]]]:
    """把「初始持仓 + 调仓流水」合成为每位投资者的**权重路径**。

    返回 {nickname: [(日期, [(Item, weight), ...]), ...]}，按日期升序；
    首段 = investors.yaml 的初始持仓（date = 该投资者 start_date）；
    每段权重已归一化到和为 1。单标的投资者的结果与 `investor_segments` 一一对应。
    """
    by_nick: dict[str, list[Switch]] = {}
    for s in switches:
        by_nick.setdefault(s.nickname, []).append(s)
    out: dict[str, list[tuple[str, list[tuple[Item, float]]]]] = {}
    for inv in investors:
        segs: list[tuple[str, list[tuple[Item, float]]]] = [
            (inv.start_date, _legs_weighted(inv.legs))]
        for s in sorted(by_nick.get(inv.nickname, []), key=lambda x: x.date):
            segs.append((s.date, _legs_weighted(s.legs)))
        out[inv.nickname] = segs
    return out


def _legs_weighted(legs: list[Item]) -> list[tuple[Item, float]]:
    """[(Item, 归一化权重), ...]（顺序与传入腿一致）。"""
    w = normalized_weights(legs)
    return [(it, w[it.key]) for it in legs]
