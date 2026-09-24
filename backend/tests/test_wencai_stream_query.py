"""[WENCAI-STREAM-QUERY-MIGRATION-01] 共享 Wencai 客户端 stream-query SSE 合同测试。

纯单元 + 录制/合成 SSE fixture，**不依赖真实问财**（不联网、不用真实 cookie）。

覆盖合同 A–P：SSE 解析、全表完整性 fail-closed、应用层/传输层错误、
请求头与 payload 合同、`fetch_query_table` 仍返回 list[dict]。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.services import wencai_client as wc

_COOKIE = "sess_tk=fake; userid=1; v=fake-v"


# =============================================================================
# fixture 构造（形态与浏览器实测一致）
# =============================================================================


def _frame(obj: dict[str, Any]) -> str:
    return "data:" + json.dumps(obj, ensure_ascii=False) + "\n\n"


def _rows(n: int) -> list[dict]:
    return [
        {
            "股票代码": f"{600000 + i:06d}.SH",
            "股票简称": f"股票{i}",
            "所属概念": ["概念A", "概念B"],
            "所属同花顺行业": ["一级", "二级"],
        }
        for i in range(n)
    ]


def _open_answer(datas: Any, row_count: int, *, has_more: bool = True) -> dict[str, Any]:
    return {
        "answer_path": "other/openAnswer",
        "section": {
            "id": 1001,
            "result_page": {
                "components": [
                    {
                        "uuid": "u1",
                        "data": {
                            "columns": [{"key": "股票代码"}, {"key": "股票简称"},
                                        {"key": "所属概念"}, {"key": "所属同花顺行业"}],
                            "datas": datas,
                            "code_count": row_count,
                            "meta": {
                                "extra": {
                                    "row_count": row_count,
                                    "per_page": 10,
                                    "has_more_data": has_more,
                                    "token": "tok",
                                }
                            },
                        },
                    }
                ]
            },
        },
        "logs": [],
        "extra": {},
    }


def _success_sse(n: int = 3, *, has_more: bool = True) -> str:
    frames = [
        _frame({"answer_path": "logs/baseInfo", "logs": [], "base_info": {}, "extra": {}}),
        _frame(_open_answer(_rows(n), n, has_more=has_more)),
        _frame({"answer_path": "extraInfo/sourceLink", "logs": [], "extra": {}}),
        _frame({"answer_path": "logs/trace_debug", "logs": [], "extra": {}, "is_last": True}),
    ]
    return "".join(frames)


class _FakeResponse:
    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text
        self.headers = {"Content-Type": "text/event-stream;charset=UTF-8"}


def _install_post(monkeypatch, status_code: int, text: str) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def _post(url, json=None, headers=None, timeout=None):  # noqa: A002 - 对齐 requests 参数名
        calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return _FakeResponse(status_code, text)

    monkeypatch.setattr(wc.requests, "post", _post)
    return calls


# =============================================================================
# A–D: 成功路径
# =============================================================================


def test_A_parses_all_four_frame_types() -> None:
    """SSE 四类帧都能被解析，openAnswer 之外不影响取数。"""
    rows, row_count = wc._parse_stream_query_frames(_success_sse(3))
    assert row_count == 3
    assert len(rows) == 3


def test_B_extracts_datas_from_open_answer_component() -> None:
    rows, _ = wc._parse_stream_query_frames(_success_sse(2))
    assert rows == _rows(2)
    assert rows[0]["股票代码"] == "600000.SH"


def test_C_row_count_equals_len_datas_succeeds(monkeypatch) -> None:
    _install_post(monkeypatch, 200, _success_sse(4))
    rows = wc.fetch_query_table("同花顺概念，行业分类", cookie=_COOKIE)
    assert isinstance(rows, list)
    assert len(rows) == 4


def test_D_has_more_data_true_still_succeeds_when_complete(monkeypatch) -> None:
    """实测：整表返回时 has_more_data 仍为 true —— 不得用它判完整。"""
    _install_post(monkeypatch, 200, _success_sse(5, has_more=True))
    rows = wc.fetch_query_table("同花顺概念，行业分类", cookie=_COOKIE)
    assert len(rows) == 5


def test_zero_row_count_returns_empty(monkeypatch) -> None:
    _install_post(monkeypatch, 200, _frame(_open_answer([], 0, has_more=False)))
    assert wc.fetch_query_table("q", cookie=_COOKIE) == []


# =============================================================================
# E–J: 错误 / 结构 fail-closed
# =============================================================================


def test_E_access_forbidden_includes_extra_message() -> None:
    sse = _frame({
        "answer_path": "access/forbidden",
        "logs": [],
        "extra": {"message": "invalid-source: "},
    })
    with pytest.raises(wc.WencaiTransportError) as exc:
        wc._parse_stream_query_frames(sse)
    assert "access/forbidden" in str(exc.value)
    assert "invalid-source" in str(exc.value)


def test_F_http_403_raises_transport_error(monkeypatch) -> None:
    _install_post(monkeypatch, 403, "<html>403 Forbidden</html>")
    with pytest.raises(wc.WencaiTransportError) as exc:
        wc.fetch_query_table("q", cookie=_COOKIE)
    assert "403" in str(exc.value)


def test_G_missing_open_answer_fails_closed() -> None:
    sse = _frame({"answer_path": "logs/baseInfo", "logs": [], "extra": {}})
    with pytest.raises(wc.WencaiTransportError) as exc:
        wc._parse_stream_query_frames(sse)
    assert "openAnswer" in str(exc.value)


def test_H_malformed_data_json_fails_closed() -> None:
    sse = "data:{not-json\n\n" + _frame(_open_answer(_rows(1), 1))
    with pytest.raises(wc.WencaiTransportError):
        wc._parse_stream_query_frames(sse)


def test_I_datas_not_list_fails_closed() -> None:
    sse = _frame(_open_answer("not-a-list", 1))
    with pytest.raises(wc.WencaiTransportError) as exc:
        wc._parse_stream_query_frames(sse)
    assert "不是列表" in str(exc.value)


def test_J_row_item_not_dict_fails_closed() -> None:
    sse = _frame(_open_answer(["not-a-dict"], 1))
    with pytest.raises(wc.WencaiTransportError) as exc:
        wc._parse_stream_query_frames(sse)
    assert "不是 dict" in str(exc.value)


def test_missing_row_count_fails_closed() -> None:
    bad = _open_answer(_rows(1), 1)
    (bad["section"]["result_page"]["components"][0]["data"]["meta"]["extra"]).pop("row_count")
    with pytest.raises(wc.WencaiTransportError):
        wc._parse_stream_query_frames(_frame(bad))


# =============================================================================
# K–L: 全表完整性 fail-closed
# =============================================================================


def test_K_row_count_over_capacity_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(
        wc, "_stream_query", lambda cookie, query: ([], wc._STREAM_QUERY_MAX_ROWS + 1)
    )
    with pytest.raises(wc.WencaiIncompleteResultError) as exc:
        wc.fetch_query_table("q", cookie=_COOKIE)
    assert "超过已验证" in str(exc.value)


def test_L_len_datas_less_than_row_count_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(
        wc, "_stream_query", lambda cookie, query: (_rows(2), 5)
    )
    with pytest.raises(wc.WencaiIncompleteResultError) as exc:
        wc.fetch_query_table("q", cookie=_COOKIE)
    assert "不完整" in str(exc.value)


def test_completeness_is_not_judged_by_has_more_data(monkeypatch) -> None:
    """完整性判据为 row_count vs len(datas)，与 has_more_data 无关。"""
    monkeypatch.setattr(wc, "_stream_query", lambda cookie, query: (_rows(3), 3))
    assert len(wc.fetch_query_table("q", cookie=_COOKIE)) == 3


# =============================================================================
# M: cookie 缺失行为保持
# =============================================================================


def test_M_missing_cookie_preserves_existing_error(monkeypatch) -> None:
    monkeypatch.setattr(wc, "load_cookie", lambda: None)
    with pytest.raises(RuntimeError) as exc:
        wc.fetch_query_table("q")
    assert "未配置 WENCAI_COOKIE" in str(exc.value)


# =============================================================================
# N–O: 请求头 / payload 合同
# =============================================================================


def test_N_request_contract_headers_and_endpoint(monkeypatch) -> None:
    calls = _install_post(monkeypatch, 200, _success_sse(1))
    wc.fetch_query_table("同花顺概念，行业分类", cookie=_COOKIE)

    call = calls[0]
    assert call["url"] == wc._STREAM_QUERY_URL
    assert call["url"].startswith("https://")
    headers = call["headers"]
    assert headers["Content-Type"] == "application/json"
    assert headers["Accept"] == "text/event-stream"
    assert headers["Cookie"] == _COOKIE
    assert headers["User-Agent"]
    # 实测非必需：不依赖 Referer / Origin / hexin-v / X-Source / Sec-Fetch-*
    for forbidden in ("Referer", "Origin", "hexin-v", "X-Source", "Sec-Fetch-Mode"):
        assert forbidden not in headers, f"{forbidden} 不应被依赖"
    assert call["timeout"] == wc._STREAM_QUERY_TIMEOUT_SECONDS


def test_O_payload_contract(monkeypatch) -> None:
    calls = _install_post(monkeypatch, 200, _success_sse(1))
    wc.fetch_query_table("同花顺概念，行业分类", cookie=_COOKIE)

    body = calls[0]["json"]
    assert body["question"] == "同花顺概念，行业分类"
    assert body["source"] == wc._STREAM_QUERY_SOURCE
    assert body["dialog_model"] == wc._STREAM_QUERY_DIALOG_MODEL
    assert body["version"] == wc._STREAM_QUERY_VERSION
    assert body["agent_id"] == wc._STREAM_QUERY_AGENT_ID
    assert body["entity_info"]["device_type"] == wc._STREAM_QUERY_DEVICE_TYPE
    assert body["agent_tools"][0]["tool_id"] == wc._STREAM_QUERY_TOOL_ID
    assert body["agent_tools"][0]["tool_param"]["domain"] == wc._STREAM_QUERY_DOMAIN
    assert body["agent_tools"][0]["tool_param"]["perpage"] == wc._STREAM_QUERY_MAX_ROWS
    # 冻结结构保持完整（不得精简）
    for key in ("default_fallback", "input_type", "events", "add_info", "agent_name"):
        assert key in body


def test_frozen_constants_are_exact() -> None:
    assert wc._STREAM_QUERY_URL == "https://www.iwencai.com/gateway/aime/stream-query"
    assert wc._STREAM_QUERY_SOURCE == "ths_iwencai_pc_xuangu"
    assert wc._STREAM_QUERY_DIALOG_MODEL == "CUSTOMER_AGENT"
    assert wc._STREAM_QUERY_VERSION == "3.4.1"
    assert wc._STREAM_QUERY_AGENT_ID == "MaSzyUwyyl"
    assert wc._STREAM_QUERY_TOOL_ID == "FinQuery"
    assert wc._STREAM_QUERY_DOMAIN == "stock"
    assert wc._STREAM_QUERY_DEVICE_TYPE == "android"
    assert wc._STREAM_QUERY_MAX_ROWS == 6000


def test_old_protocol_is_retired() -> None:
    """旧两跳协议不得保留为回退。"""
    assert not hasattr(wc, "_ROBOT_URL")
    assert not hasattr(wc, "_DATALIST_URL")
    assert not hasattr(wc, "_get_footer_url")
    assert not hasattr(wc, "_fetch_page")


# =============================================================================
# P: 公开契约保持
# =============================================================================


def test_P_fetch_query_table_returns_list_of_dict(monkeypatch) -> None:
    _install_post(monkeypatch, 200, _success_sse(3))
    rows = wc.fetch_query_table(
        "同花顺概念，行业分类", cookie=_COOKIE, perpage=100,
        max_pages=None, page_delay_range=wc.PAGE_DELAY_RANGE,
    )
    assert isinstance(rows, list)
    assert all(isinstance(r, dict) for r in rows)
    # 兼容参数不得导致截断
    assert len(rows) == 3


def test_signature_keeps_compatibility_arguments() -> None:
    import inspect

    sig = inspect.signature(wc.fetch_query_table)
    for name in ("query", "cookie", "perpage", "max_pages", "page_delay_range"):
        assert name in sig.parameters, f"签名必须保留 {name}"
