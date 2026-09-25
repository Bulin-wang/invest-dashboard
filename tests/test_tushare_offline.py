"""Tushare MCP 抓取层离线单测（不联网）。

运行： python -m pytest tests/ -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.fetch import tushare_mcp  # noqa: E402
from src.fetch.tushare_mcp import (  # noqa: E402
    TushareError,
    _apply_hfq,
    _throttle_seconds,
    extract_rows,
    guess_ts_code,
    parse_sse,
)


class _Resp:
    """伪造成 requests.Response 的最小对象。"""

    def __init__(self, body: str, content_type: str = "text/event-stream"):
        self.content = body.encode("utf-8")
        self.headers = {"Content-Type": content_type}


@pytest.fixture(autouse=True)
def _clean_cache():
    tushare_mcp.reset()
    yield
    tushare_mcp.reset()


# --------------------------------------------------------------------- 代码映射
def test_guess_ts_code_stock():
    assert guess_ts_code("600519", "stock") == "600519.SH"
    assert guess_ts_code("000001", "stock") == "000001.SZ"
    assert guess_ts_code("300750", "stock") == "300750.SZ"


def test_guess_ts_code_index():
    assert guess_ts_code("000300", "index") == "000300.SH"
    assert guess_ts_code("399006", "index") == "399006.SZ"


def test_guess_ts_code_passthrough():
    assert guess_ts_code("600519.SH", "stock") == "600519.SH"


# --------------------------------------------------------------------- SSE / JSON-RPC 解析
def test_parse_sse_event_stream():
    body = 'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"ok":1}}\n\n'
    obj = parse_sse(_Resp(body))
    assert obj["result"] == {"ok": 1}


def test_parse_sse_plain_json():
    obj = parse_sse(_Resp('{"result":{"ok":2}}', content_type="application/json"))
    assert obj["result"] == {"ok": 2}


def test_extract_rows_ok():
    res = {"content": [{"type": "text", "text": '[{"a":1},{"a":2}]'}]}
    rows = extract_rows(res)
    assert rows == [{"a": 1}, {"a": 2}]


def test_extract_rows_is_error():
    res = {"isError": True, "content": [{"type": "text", "text": "频率超限(1次/小时)"}]}
    with pytest.raises(TushareError):
        extract_rows(res)


def test_throttle_seconds():
    assert _throttle_seconds("频率超限(1次/分钟)") == 62.0
    assert _throttle_seconds("频率超限(1次/小时)") is None  # 小时级不傻等
    assert _throttle_seconds("没有接口访问权限") is None


# --------------------------------------------------------------------- 后复权合并
def test_apply_hfq_multiplies_factor():
    tushare_mcp._ADJ_CACHE["600519.SH"] = pd.DataFrame(
        {"trade_date": ["20240102", "20240103", "20240104"],
         "adj_factor": [2.0, 2.0, 2.0]})
    px = pd.DataFrame(
        {"trade_date": ["20240102", "20240103", "20240104"],
         "close": [10.0, 11.0, 12.0]})
    out = _apply_hfq(px, "600519.SH", "20240102", "20240104")
    assert out["close"].tolist() == [20.0, 22.0, 24.0]


def test_apply_hfq_forwardfills_missing_factor():
    tushare_mcp._ADJ_CACHE["600519.SH"] = pd.DataFrame(
        {"trade_date": ["20240102"], "adj_factor": [3.0]})
    px = pd.DataFrame(
        {"trade_date": ["20240102", "20240103"], "close": [10.0, 11.0]})
    out = _apply_hfq(px, "600519.SH", "20240102", "20240103")
    assert out["close"].tolist() == [30.0, 33.0]  # 20240103 用前值 3.0 回填


def test_apply_hfq_missing_factor_raises(monkeypatch):
    """取不到复权因子必须报错，绝不静默用未复权价冒充后复权价。"""
    def _boom(*args, **kwargs):
        raise TushareError("adj_factor: 频率超限(1次/小时)")

    monkeypatch.setattr(tushare_mcp, "prefetch_adj_factors", _boom)
    px = pd.DataFrame({"trade_date": ["20240102"], "close": [10.0]})
    with pytest.raises(TushareError):
        _apply_hfq(px, "600519.SH", "20240102", "20240102")


# --------------------------------------------------------------------- 批量预热
def test_prefetch_adj_factors_batches_one_call(monkeypatch):
    seen = {}

    class _FakeClient:
        def call_tool(self, name, arguments):
            seen["name"] = name
            seen["args"] = arguments
            return [
                {"ts_code": "600519.SH", "trade_date": "20240102", "adj_factor": 2.0},
                {"ts_code": "000001.SZ", "trade_date": "20240102", "adj_factor": 1.5},
            ]

    monkeypatch.setattr(tushare_mcp, "_get_client", lambda: _FakeClient())
    tushare_mcp.prefetch_adj_factors(["600519.SH", "000001.SZ"], "20240101")

    assert seen["name"] == "adj_factor"
    assert seen["args"]["ts_code"] == "600519.SH,000001.SZ"  # 一次调用覆盖多只
    assert set(tushare_mcp._ADJ_CACHE) == {"600519.SH", "000001.SZ"}

    # 第二次调用应命中缓存、不再触发网络
    seen.clear()
    tushare_mcp.prefetch_adj_factors(["600519.SH", "000001.SZ"], "20240101")
    assert seen == {}
