"""CaptureRequest.focus_event 跨服务数据合同测试。

根因（API_CONTRACT_MISMATCH P0）：
    旧 CaptureRequest.focus_event 声明为 dict[str, str] | None，
    但 SMC 结构事件（BOS/CHoCH/OB/EQH/EQL）会在 focus_event 中携带
    float/int/bool 字段（level/bar_high/bar_low/bias/internal/bullish），
    导致 POST /capture 触发 Pydantic v2 422 → 飞书收不到结构图。
    筹码共识事件 node_cluster_touch 只含字符串字段，故始终 PASS。

本测试直接针对 CaptureRequest 模型做合同断言，不启动 HTTP 服务。
纯单元可跑（PURE_UNIT_TEST=1）。
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.capture_main import CaptureRequest


def _base_kwargs() -> dict:
    return {
        "symbol": "600519",
        "event_id": str(uuid4()),
        "token": "short-lived-token",
        "frontend_base_url": "http://localhost:5173",
    }


def test_capture_request_node_cluster_touch_focus_event_passes() -> None:
    """筹码共识事件：只有字符串字段，必须 PASS（回归基线）。"""
    req = CaptureRequest(
        **_base_kwargs(),
        focus_event={
            "focus_event_id": str(uuid4()),
            "focus_event_type": "node_cluster_touch",
        },
    )
    assert req.focus_event["focus_event_type"] == "node_cluster_touch"


def test_capture_request_smc_bos_focus_event_passes() -> None:
    """SMC BOS retest 携带 float/int/bool，修复后必须 PASS（原 422 根因）。"""
    req = CaptureRequest(
        **_base_kwargs(),
        focus_event={
            "focus_event_id": str(uuid4()),
            "focus_event_type": "smc_bos_retest",
            "level": 18.26,
            "bias": 1,
            "internal": True,
            "bullish": True,
        },
    )
    assert req.focus_event["level"] == 18.26
    assert req.focus_event["bias"] == 1
    assert req.focus_event["internal"] is True
    assert req.focus_event["bullish"] is True


def test_capture_request_smc_choch_focus_event_passes() -> None:
    req = CaptureRequest(
        **_base_kwargs(),
        focus_event={
            "focus_event_id": str(uuid4()),
            "focus_event_type": "smc_choch_retest",
            "level": 12.34,
            "bias": -1,
            "internal": False,
        },
    )
    assert req.focus_event["level"] == 12.34


def test_capture_request_smc_ob_focus_event_passes() -> None:
    req = CaptureRequest(
        **_base_kwargs(),
        focus_event={
            "focus_event_id": str(uuid4()),
            "focus_event_type": "smc_ob_first_touch",
            "bar_high": 15.6,
            "bar_low": 14.2,
            "bias": 1,
            "internal": True,
        },
    )
    assert req.focus_event["bar_high"] == 15.6
    assert req.focus_event["bar_low"] == 14.2


def test_capture_request_smc_eqhl_focus_event_passes() -> None:
    for et in ("smc_eqh_retest", "smc_eql_retest"):
        req = CaptureRequest(
            **_base_kwargs(),
            focus_event={
                "focus_event_id": str(uuid4()),
                "focus_event_type": et,
                "level": 20.0,
            },
        )
        assert req.focus_event["level"] == 20.0


def test_capture_request_focus_event_optional_none() -> None:
    """无 focus_event 时请求仍有效。"""
    req = CaptureRequest(**_base_kwargs())
    assert req.focus_event is None


def test_legacy_dict_str_focus_event_still_accepted() -> None:
    """向后兼容：纯字符串 focus_event 仍被接受（不破坏旧调用方）。"""
    req = CaptureRequest(
        **_base_kwargs(),
        focus_event={
            "focus_event_id": "abc",
            "focus_event_type": "node_cluster_touch",
        },
    )
    assert isinstance(req.focus_event["focus_event_id"], str)


def test_contract_regression_422_reproduction_is_fixed() -> None:
    """回归测试：直接复现原 422 问题的 payload，新代码必须 PASS。

    原行为（修复前）：
        CaptureRequest.focus_event: dict[str, str]
        -> 收到 level=18.26 (float) 抛 ValidationError
    修复后：dict[str, Any]，应正常解析。
    """
    smc_payload = {
        **_base_kwargs(),
        "focus_event": {
            "focus_event_id": str(uuid4()),
            "focus_event_type": "smc_bos_retest",
            "level": 18.26,
            "bias": 1,
            "internal": True,
            "bullish": True,
        },
    }
    try:
        req = CaptureRequest(**smc_payload)
    except ValidationError as exc:  # pragma: no cover - 不应再发生
        raise AssertionError(
            f"CaptureRequest 仍拒绝 SMC 结构事件 focus_event（根因未修复）: {exc}"
        ) from exc
    assert req.focus_event["level"] == 18.26
