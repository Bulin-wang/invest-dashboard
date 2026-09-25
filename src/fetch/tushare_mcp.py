"""A股个股 / 指数行情抓取（Tushare MCP Server）。

数据来自 Tushare 官方 MCP Server（JSON-RPC 2.0 over HTTP + SSE 流式响应）。
本次只接 A股个股与指数；A股 ETF/债券 与 美股 仍走原数据源（见 cn.py / us.py）。

口径
----
- stock : ``daily`` 的原始 ``close`` × ``adj_factor`` = **后复权价**（含分红再投/送转），
          与 akshare 的 ``adjust="hfq"`` 等价。累计收益 = 后复权(末)/后复权(首) - 1。
- index : ``index_daily`` 的 ``close``（指数不需复权）。

限速提醒（免费 token）
----------------------
该账户对 ``adj_factor`` / ``index_daily`` 限速为 **1 次/分钟 + 1 次/小时**。
因此本模块对个股的复权因子做「批量预热 + 内存缓存」：整条流水线每个交易日对
``adj_factor`` 只调用一次（一次传入所有 A股个股代码），避免触发小时级限速。
指数同理——一天一次 ``index_daily`` 调用即可。
"""
from __future__ import annotations

import json
import time

import pandas as pd
import requests

from ..config import TUSHARE_MCP_URL
from .base import normalize


class TushareError(RuntimeError):
    """Tushare MCP 调用失败（含无权限、限速、网络错误）。"""


# --------------------------------------------------------------------------- SSE / JSON-RPC
def parse_sse(resp: "requests.Response") -> dict:
    """把 HTTP 响应解析成单个 JSON-RPC 对象（兼容 text/event-stream 与 application/json）。"""
    ct = resp.headers.get("Content-Type", "")
    text = resp.content.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")
    if "text/event-stream" not in ct:
        return json.loads(text)
    events, cur = [], []
    for line in text.split("\n"):
        if line == "":
            if cur:
                events.append("\n".join(cur))
                cur = []
        elif line.startswith("data:"):
            cur.append(line[len("data:"):].lstrip())
    if cur:
        events.append("\n".join(cur))
    if not events:
        raise TushareError("空响应")
    return json.loads(events[-1])


def extract_rows(result: dict) -> list[dict]:
    """从 tools/call 的 result 中取出数据行（content[0].text 是 JSON 数组字符串）。"""
    if result.get("isError"):
        raise TushareError(_content_text(result) or "unknown MCP error")
    txt = _content_text(result)
    if not txt or not txt.strip():
        return []
    try:
        data = json.loads(txt)
    except json.JSONDecodeError as e:
        raise TushareError(f"接口返回非 JSON：{txt[:200]}") from e
    return data if isinstance(data, list) else [data]


def _content_text(result: dict) -> str:
    return "\n".join(c.get("text", "") for c in result.get("content", []) if isinstance(c, dict))


def _throttle_seconds(msg: str) -> float | None:
    """从「频率超限(1次/分钟|小时)」错误里推断应等待的秒数；小时级返回 None（不傻等）。"""
    if "频率超限" not in msg:
        return None
    if "小时" in msg:
        return None          # 小时级限速：交给上层缓存/下次运行，不做长睡眠
    if "分钟" in msg:
        return 62.0
    return 61.0


# --------------------------------------------------------------------------- 客户端
class TushareMCPClient:
    """极简 MCP 客户端：initialize 一次，之后复用（服务端无状态，可不带 session）。"""

    def __init__(self, url: str | None):
        if not url:
            raise TushareError(
                "未配置 Tushare MCP。请设置环境变量 TUSHARE_TOKEN（或 TUSHARE_MCP_URL），"
                "或在项目根目录放一个 .tushare_token 文件。")
        self.url = url
        self.session_id: str | None = None
        self._ready = False

    def _post(self, payload: dict) -> "requests.Response":
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        return requests.post(self.url, headers=headers,
                             data=json.dumps(payload), timeout=60)

    def _ensure_ready(self) -> None:
        if self._ready:
            return
        r = self._post({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "invest-dashboard", "version": "1.0"}},
        })
        if r.status_code != 200:
            raise TushareError(f"initialize 失败：HTTP {r.status_code} {r.text[:200]}")
        self.session_id = r.headers.get("Mcp-Session-Id")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        self._ready = True

    def call_tool(self, name: str, arguments: dict,
                  tries: int = 3, base_wait: float = 1.5) -> list[dict]:
        """调用一个 MCP 工具，返回数据行列表。

        对「分钟级限速」与网络错误做有界重试；「小时级限速」/无权限 立即抛出。
        """
        self._ensure_ready()
        last: Exception | None = None
        for i in range(tries):
            try:
                r = self._post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                "params": {"name": name, "arguments": arguments}})
                if r.status_code >= 500:
                    raise TushareError(f"{name}: HTTP {r.status_code}")
                obj = parse_sse(r)
                if "error" in obj:                       # JSON-RPC 协议层错误
                    raise TushareError(f"{name}: {obj['error']}")
                result = obj.get("result", {})
                if result.get("isError"):
                    msg = _content_text(result) or "unknown MCP error"
                    secs = _throttle_seconds(msg)
                    if secs is None:                     # 无权限 / 小时级限速 -> 不重试
                        raise TushareError(f"{name}: {msg}")
                    if i < tries - 1:                    # 分钟级限速 -> 等待重试
                        time.sleep(secs)
                        continue
                    raise TushareError(f"{name}: {msg}")
                return extract_rows(result)
            except requests.RequestException as e:       # 网络错误 -> 退避重试
                last = e
                if i < tries - 1:
                    time.sleep(base_wait * (i + 1))
        raise last if last else TushareError(f"{name}: 调用失败")  # type: ignore[misc]


_client: TushareMCPClient | None = None
_ADJ_CACHE: dict[str, pd.DataFrame] = {}


def _get_client() -> TushareMCPClient:
    global _client
    if _client is None:
        _client = TushareMCPClient(TUSHARE_MCP_URL)
    return _client


def reset() -> None:
    """清空客户端与缓存（测试用）。"""
    global _client
    _client = None
    _ADJ_CACHE.clear()


# --------------------------------------------------------------------------- 代码 / 日期
def guess_ts_code(symbol: str, kind: str) -> str:
    """把裸代码转成 Tushare 的 ``ts_code``（带交易所后缀）。已是 ts_code 则原样返回。"""
    s = str(symbol).strip()
    if "." in s:                      # 600519.SH / 000300.SH
        return s
    if kind == "index":
        return s + (".SH" if s.startswith(("000", "999")) else ".SZ")
    if s.startswith(("6", "9")):      # 沪市 A股 / B股
        return s + ".SH"
    if s.startswith(("0", "2", "3")):  # 深市 A股 / B股 / 创业板
        return s + ".SZ"
    if s.startswith(("4", "8")):      # 北交所
        return s + ".BJ"
    return s + ".SH"


def _ymd(d) -> str:
    return pd.Timestamp(d).strftime("%Y%m%d")


def _today_ymd() -> str:
    return pd.Timestamp.today().strftime("%Y%m%d")


# --------------------------------------------------------------------------- 拉取
def prefetch_adj_factors(codes, start, end=None) -> None:
    """一次调用批量拉取多只个股的复权因子并缓存（每日每接口仅 1 次调用）。"""
    codes = [c for c in codes if c not in _ADJ_CACHE]
    if not codes:
        return
    rows = _get_client().call_tool("adj_factor", {
        "ts_code": ",".join(codes),
        "start_date": _ymd(start),
        "end_date": _ymd(end) if end else _today_ymd(),
    })
    df = pd.DataFrame(rows)
    if df.empty:
        return
    for code, g in df.groupby("ts_code"):
        _ADJ_CACHE[code] = g[["trade_date", "adj_factor"]].copy()


def _apply_hfq(px: pd.DataFrame, code: str, start, end) -> pd.DataFrame:
    """把原始收盘价 × 复权因子，得到后复权价。

    复权因子取不到时**直接报错**，不静默回退未复权价：宁可该标的记为 error、
    由 pipeline 保留上一版正确的 CSV，也不要把「价格收益」冒充「分红再投收益」。
    """
    px = px.rename(columns={"trade_date": "date"})[["date", "close"]].copy()
    if code not in _ADJ_CACHE:
        prefetch_adj_factors([code], start, end)   # 失败则抛出 TushareError
    adj = _ADJ_CACHE.get(code)
    if adj is None:
        raise TushareError(f"{code}: 未取到复权因子，无法计算后复权价")
    adj = adj.rename(columns={"trade_date": "date"})
    px = px.merge(adj, on="date", how="left")
    px["adj_factor"] = px["adj_factor"].ffill().bfill()
    px["close"] = px["close"] * px["adj_factor"]
    return px[["date", "close"]]


def fetch_stock(symbol: str, start, end=None) -> pd.DataFrame:
    """A股个股：原始收盘价 × 复权因子 -> 后复权价。"""
    code = guess_ts_code(symbol, "stock")
    rows = _get_client().call_tool("daily", {
        "ts_code": code, "start_date": _ymd(start),
        "end_date": _ymd(end) if end else _today_ymd()})
    if not rows:
        raise TushareError(f"Tushare 未返回 {code} 的日线")
    px = _apply_hfq(pd.DataFrame(rows), code, start, end)
    return normalize(px)


def fetch_index(symbol: str, start, end=None) -> pd.DataFrame:
    """A股指数：index_daily（未复权）。"""
    code = guess_ts_code(symbol, "index")
    rows = _get_client().call_tool("index_daily", {
        "ts_code": code, "start_date": _ymd(start),
        "end_date": _ymd(end) if end else _today_ymd()})
    if not rows:
        raise TushareError(f"Tushare 未返回指数 {code}")
    px = pd.DataFrame(rows).rename(columns={"trade_date": "date"})[["date", "close"]]
    return normalize(px)


def fetch_daily(symbol: str, kind: str, start: str, end: str | None = None) -> pd.DataFrame:
    """cn 层统一入口（仅 stock / index）。"""
    if kind == "stock":
        return fetch_stock(symbol, start, end)
    if kind == "index":
        return fetch_index(symbol, start, end)
    raise ValueError(f"tushare_mcp 不支持 kind={kind!r}（仅 stock/index）")
