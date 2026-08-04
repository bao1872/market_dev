"""long_task_budget 工具单元测试（PURE_UNIT_TEST=1 纯单元模式，不连库）。"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
# 使 backend/ 可导入 app.*（与 backend 测试保持一致；纯单元不连库）
_BACKEND_DIR = ROOT / "backend"
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from app.utils.long_task_budget import (  # noqa: E402
    LongTaskBudgetState,
    LongTaskStopReason,
    current_rss_mb,
)


def _make_state(total: int = 100, chunk_size: int = 5, budget_mb: int = 1024):
    return LongTaskBudgetState(
        chunk_size=chunk_size,
        concurrency=1,
        memory_budget_mb=budget_mb,
        total=total,
    )


def test_progress_and_remaining():
    state = _make_state(total=100)
    assert state.progress == 0.0
    assert state.remaining == 100
    state.record_chunk_done(25)
    assert state.processed == 25
    assert state.remaining == 75
    assert state.progress == pytest.approx(0.25)


def test_total_zero_progress_is_zero():
    state = _make_state(total=0)
    assert state.progress == 0.0
    assert state.remaining == 0


def test_should_stop_above_budget():
    state = _make_state(budget_mb=100)
    assert state.should_stop(rss=150) is True
    assert state.should_stop(rss=50) is False
    # RSS 未知时不应误停
    assert state.should_stop(rss=None) is False


def test_peak_rss_accumulates_max():
    state = _make_state()
    state.peak_rss_mb = None
    state.sample()  # rss 可能为 None（非 Linux /proc 缺失），不 assert 具体值
    # 手动累计峰值
    state.peak_rss_mb = 100.0
    rss = state.sample()
    if rss is not None and rss > 100.0:
        assert state.peak_rss_mb == rss
    else:
        assert state.peak_rss_mb == 100.0


def test_stop_reason_mark_and_value():
    state = _make_state()
    assert state.stop_reason is None
    state.mark_stopped(LongTaskStopReason.MEMORY_BUDGET_EXCEEDED)
    assert state.stop_reason == LongTaskStopReason.MEMORY_BUDGET_EXCEEDED
    assert state.to_status()["stop_reason"] == "memory_budget_exceeded"


def test_make_checkpoint_roundtrip():
    state = _make_state(total=100, budget_mb=256)
    state.record_chunk_done(10)
    state.peak_rss_mb = 99.5
    state.mark_stopped(LongTaskStopReason.CANCELLED, extra="note")
    cp = state.make_checkpoint()
    restored = LongTaskBudgetState.restore_from_checkpoint(cp)
    assert restored.processed == 10
    assert restored.total == 100
    assert restored.chunk_size == 5
    assert restored.memory_budget_mb == 256
    assert restored.peak_rss_mb == pytest.approx(99.5)
    assert restored.stop_reason == LongTaskStopReason.CANCELLED
    assert restored.metadata.get("extra") == "note"


def test_restore_from_dict():
    data = {
        "chunk_size": 5,
        "memory_budget_mb": 512,
        "total": 80,
        "processed": 40,
        "stop_reason": "memory_budget_exceeded",
        "metadata": {},
    }
    state = LongTaskBudgetState.from_dict(data)
    assert state.processed == 40
    assert state.memory_budget_mb == 512
    assert state.stop_reason == LongTaskStopReason.MEMORY_BUDGET_EXCEEDED


def test_should_sample_every_step():
    state = _make_state()
    state.sample_every = 10
    # processed 整除步长时应当采样
    state.processed = 10
    assert state.should_sample(processed_delta=1) is True
    state.processed = 11
    assert state.should_sample(processed_delta=1) is False


def test_current_rss_never_raises():
    # 纯单元环境（非 Linux /proc 缺失）必须返回 None 或 float，绝不抛异常。
    rss = current_rss_mb()
    assert rss is None or isinstance(rss, float)


def test_to_status_contains_all_contract_keys():
    state = _make_state(total=10)
    payload = state.to_status()
    for key in (
        "chunk_size", "concurrency", "memory_budget_mb", "sample_every",
        "total", "processed", "remaining", "progress", "peak_rss_mb",
        "heartbeat_at", "stop_reason", "resume_token", "metadata",
    ):
        assert key in payload, f"缺少契约字段: {key}"
    json.dumps(payload, ensure_ascii=False)  # 必须可 JSON 序列化
