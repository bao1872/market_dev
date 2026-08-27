"""[CORRECTION-03] AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01-CORRECTION-03 行为合同测试。

本模块锁定 P0-1（canonical CORE_READY owner）与 P0-2（DSA 兼容性移出 mandatory
Core 路径）的生产 owner/路由行为。全部使用 fake/injected session/service，
无需真实 PG（T3 focused business-chain behavior）。

覆盖审计要求的 Case A–K：

- _validate_core_ready：canonical Core readiness owner（Case B/C/D 行为）。
- _run_dsa_compatibility_projection：OPTIONAL（Case E/K 行为）——
  失败返回 failed 且绝不向调用方抛异常（Core 不被标记 failed）；
  成功调用 publish_run 并返回 succeeded。
- _enqueue_chip_job_step：chip 失败返回 failed（Case I），Review 不受影响。
- Case J/K：DSA 兼容性 / chip 路径与 stock_core publication 零交互
  （spy 整个 feature_snapshot_service），publish_stock_core_atomically 符号已不存在。
"""

import contextlib
import inspect
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import app.services.feature_snapshot_service as feature_snapshot_service
from app.services import after_close_orchestrator as orch
from app.services.after_close_orchestrator import (
    AfterCloseCoreNotReadyError,
    _run_dsa_compatibility_projection,
    _validate_core_ready,
)

T = date(2026, 7, 31)


def _fake_core_run(status: str, run_id="CORE-RUN-ID", trade_date=T):
    return SimpleNamespace(id=run_id, trade_date=trade_date, status=status)


class _FakeSessionCtx:
    """极简 async DB session：支持 .get / .commit，按需配置 CoreRun 行。"""

    def __init__(self, get_results=None):
        self._get_results = get_results or {}
        self.get_calls: list = []
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, model, key):
        self.get_calls.append((model.__name__ if hasattr(model, "__name__") else model, key))
        return self._get_results.get(key)

    async def commit(self):
        self.commits += 1


class _DsaRepoStub:
    """project_dsa_batch 替身；failure=True 时抛异常模拟 DSA 投影失败。"""

    instance = None

    def __init__(self, *args, **kwargs):
        type(self).instance = self
        self.calls: list[dict] = []

    async def project_dsa_batch(self, **kwargs):
        if getattr(self, "raise_on_project", False):
            raise RuntimeError("DSA kernel boom")
        self.calls.append(kwargs)


# ---------------------------------------------------------------------------
# P0-1: canonical CORE_READY owner —— 真实生产函数行为（Case B/C/D）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_core_ready_succeeded_passes():
    """Core status==succeeded + trade_date 匹配 → 返回真实 CoreRun 行（可进入下游）。"""
    run = _fake_core_run("succeeded")
    sess = _FakeSessionCtx({"CORE-RUN-ID": run})
    got = await _validate_core_ready(sess, "CORE-RUN-ID", T)
    assert got is run
    # 必须读取真实 DB 行（行为证据：确实发生了 get）
    assert ("StockFeatureSnapshotRun", "CORE-RUN-ID") in [
        (m, k) for m, k in sess.get_calls
    ]


@pytest.mark.asyncio
async def test_core_failed_fail_closed():
    """Case C: Core status==failed → fail-closed（禁止 Review/History/events/chip）。"""
    run = _fake_core_run("failed")
    with pytest.raises(AfterCloseCoreNotReadyError) as ei:
        await _validate_core_ready(_FakeSessionCtx({"CORE-RUN-ID": run}), "CORE-RUN-ID", T)
    assert "failed" in str(ei.value)


@pytest.mark.asyncio
async def test_core_running_fail_closed():
    """Case B: Core status==running → fail-closed。

    compute-complete 合同允许 pending/running items → status=running；
    snapshot_run_id 非空不等于 Core Ready，必须 fail-closed。
    """
    run = _fake_core_run("running")
    with pytest.raises(AfterCloseCoreNotReadyError):
        await _validate_core_ready(_FakeSessionCtx({"CORE-RUN-ID": run}), "CORE-RUN-ID", T)


@pytest.mark.asyncio
async def test_core_missing_fail_closed():
    """Case D: CoreRun 不存在（None）→ fail-closed。"""
    with pytest.raises(AfterCloseCoreNotReadyError):
        await _validate_core_ready(_FakeSessionCtx({}), "MISSING", T)


@pytest.mark.asyncio
async def test_core_trade_date_mismatch_fail_closed():
    """Core trade_date 与 T 不匹配 → 即使 succeeded 也 fail-closed。"""
    run = _fake_core_run("succeeded", trade_date=date(2026, 7, 30))
    with pytest.raises(AfterCloseCoreNotReadyError):
        await _validate_core_ready(_FakeSessionCtx({"CORE-RUN-ID": run}), "CORE-RUN-ID", T)


@pytest.mark.asyncio
async def test_core_none_id_fail_closed():
    """snapshot_run_id 为 None → fail-closed（不得以空 id 推断就绪）。"""
    with pytest.raises(AfterCloseCoreNotReadyError):
        await _validate_core_ready(_FakeSessionCtx(), None, T)


# ---------------------------------------------------------------------------
# P0-2: DSA compatibility is OPTIONAL（Case E/K）—— 生产函数行为
# ---------------------------------------------------------------------------


# [AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01-CORRECTION-04] DSA 兼容性行为合同已升级为
# 统一执行器 owner / 真实 publish_run(db, run_id) 合同，行为测试整体迁移至
# tests/test_after_close_core_review_correction04.py（autospec 真实签名 +
# execute_orchestrator_step(optional=True) 标准摘要）。本文件的
# canonical CORE_READY owner / state_events / chip 行为测试保持有效。


def test_publish_stock_core_atomically_symbol_absent():
    """Case J(补充): publish_stock_core_atomically 符号已从两个模块移除（call_count==0 结构保证）。"""
    import app.services.strategy_batch_service as sbs_mod

    for mod_name, mod in (
        ("after_close_orchestrator", orch),
        ("feature_snapshot_service", feature_snapshot_service),
        ("strategy_batch_service", sbs_mod),
    ):
        assert not hasattr(mod, "publish_stock_core_atomically"), (
            f"{mod_name} 仍存在 publish_stock_core_atomically"
        )


# ---------------------------------------------------------------------------
# Case I: chip failure is optional & truthful
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chip_failure_returns_failed_not_raise():
    """Case I: chip 入队抛异常 → 返回 ('failed', None)，不阻断 Review/partial_success 判定。"""
    summary_recorder = []

    async def _record(run_id, steps):
        summary_recorder.append((run_id, steps))

    with (
        patch.object(
            orch, "create_after_close_chip_consensus_job",
            new=_async_raise(RuntimeError("db down")),
        ),
        patch.object(orch, "AsyncSessionLocal"),
        patch.object(orch, "_persist_step_summary", new=_record),
    ):
        status, jid = await orch._enqueue_chip_job_step(
            job_run_id=object(),
            worker_id="w1",
            lease_epoch=1,
            trade_date=T,
            snapshot_run_id="CORE-RUN-ID",
            expected_count=100,
        )
    assert status == "failed"


def _async_raise(exc):
    async def _f(*a, **k):
        raise exc

    return _f


# ---------------------------------------------------------------------------
# Case F / H: state_events 以 Core X 执行；其失败为 optional（不阻断 Review）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_events_generate_for_run_x_case_F():
    """Case F: 真实生产 generate_events_for_run 以 X 调用。

    注入的 session 中 run 不存在 → 生产函数以 run_id=X 返回统计，
    证明 state_events 入口按 X 消费而非 publication。
    """
    from uuid import uuid4

    from app.services.state_event_service import generate_events_for_run

    core_id = uuid4()
    sess = _FakeSessionCtx({})  # .get 返回 None → run 不存在早退分支

    stats = await generate_events_for_run(sess, core_id)

    assert stats["run_id"] == str(core_id)
    assert ("StockFeatureSnapshotRun", core_id) in [
        (m, k) for m, k in sess.get_calls
    ]
    assert stats["event_count"] == 0


@pytest.mark.asyncio
async def test_state_events_failure_surface_is_exception_case_H():
    """Case H: state_events 执行失败以异常呈现（由编排层 try/except 吸收为 optional）。

    编排层在 if core_ready 块内 try/except 包裹 generate_events_for_run，
    此处锁定失败面的真实形态：抛异常而非静默伪成功。
    """
    from uuid import uuid4

    from app.services.state_event_service import generate_events_for_run

    class _ExplodingSession:
        async def get(self, *a, **k):
            raise RuntimeError("db down during events")

        async def commit(self):
            return None

    with pytest.raises(RuntimeError, match="db down"):
        await generate_events_for_run(_ExplodingSession(), uuid4())


def test_state_events_routed_under_core_ready_gate_supplementary():
    """补充源码守卫（非唯一证据）：state_events/chip 位于 if core_ready 门控下。"""
    src = inspect.getsource(orch.execute_after_close_run)
    idx = src.find("state events（non-blocking post-core）")
    assert idx != -1
    block = src[idx: idx + 600]
    assert "\n        if core_ready:" in block, (
        "state_events 必须被 if core_ready 门控包住"
    )
