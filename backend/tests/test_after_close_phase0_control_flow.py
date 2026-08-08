"""Phase 4.2 corrective — after-close 控制流真实行为测试。

这些测试直接驱动 `execute_after_close_run`，用全 mock 的 `AsyncSessionLocal`
（不连 PG，纯单元）验证 **normal publish 专属步骤（auction anchor / board
aggregation / publishing checkpoint）** 与 **skip_publish 断点恢复路径** 的分支归属，
以及 superseded run 不被 auction/aggregation/state_events/chip 消费。

绝不依赖 inspect.getsource() 或脆弱的 AsyncSession 行为模拟；只断言业务步骤
是否真正被调用（spy on service 函数）。

运行：PURE_UNIT_TEST=1 pytest backend/tests/test_after_close_phase0_control_flow.py
"""

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.after_close_orchestrator import (
    AfterCloseRunStatus,
    execute_after_close_run,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# 全 mock 的 AsyncSessionLocal：所有 DB 操作都是 no-op，避免连 PG
# ---------------------------------------------------------------------------
class _FakeSession:
    def __init__(self, job_run: MagicMock) -> None:
        self._job_run = job_run
        self.add = MagicMock()
        self.commit = AsyncMock()
        self.flush = AsyncMock()
        self.refresh = AsyncMock()
        self.rollback = AsyncMock()
        self.execute = AsyncMock(return_value=MagicMock())
        # DSA run：status=completed 使跨 worker fencing 直接视为已完成，
        # 跳过真实 DB fencing 逻辑（不连 PG）。
        _dsa = MagicMock()
        _dsa.status = "completed"
        _dsa.worker_id = "existing"
        _dsa.attempt_count = 0
        _dsa.published = False
        self.get = AsyncMock(return_value=_dsa)
        self.begin = MagicMock()

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _make_job_run(*, dsa_run_id=None, snapshot_run_id=None, last_completed_step=None):
    meta = {}
    if dsa_run_id is not None:
        meta["dsa_run_id"] = str(dsa_run_id)
    if snapshot_run_id is not None:
        meta["feature_snapshot_run_id"] = str(snapshot_run_id)
    if last_completed_step is not None:
        meta["last_completed_step"] = last_completed_step
    job_run = MagicMock()
    job_run.id = uuid.uuid4()
    job_run.status = "running"
    job_run.metadata_json = json.dumps(meta)
    job_run.error_message = None
    job_run.finished_at = None
    return job_run


def _fake_session_factory(job_run: MagicMock):
    factory = MagicMock()
    factory.return_value = _FakeSession(job_run)
    return factory


def _install_patches(job_run, *, resolve_side_effect):
    """安装所有外部依赖的 mock，返回 spied 函数供断言。"""
    spies = {}

    # 拍卖锚点 / 板块聚合 / 状态事件 / chip 入队 / 计算 core
    spies["auction"] = AsyncMock(
        return_value={
            "publication_id": uuid.uuid4(),
            "structure_count": 1,
            "chip_count": 1,
            "composite_count": 1,
        }
    )
    spies["aggregation"] = AsyncMock(return_value={"published": 1})
    spies["events"] = AsyncMock(return_value={})
    spies["chip"] = AsyncMock(return_value=("succeeded", uuid.uuid4()))
    spies["compute_review_core"] = AsyncMock(return_value=MagicMock())

    # 内部 helper
    spies["heartbeat_step"] = AsyncMock()
    spies["get_job_run"] = AsyncMock(return_value=job_run)
    spies["create_run"] = AsyncMock(return_value=MagicMock(id=uuid.uuid4()))
    spies["compute_run"] = AsyncMock(return_value={})
    spies["finish_snapshot_run"] = AsyncMock()
    spies["repair"] = AsyncMock(return_value=[])
    spies["resolve"] = AsyncMock(side_effect=resolve_side_effect)

    # batch service
    batch = MagicMock()
    batch.publish_run = AsyncMock(return_value=MagicMock(id=uuid.uuid4()))
    batch.create_batch_run = AsyncMock(return_value=MagicMock(id=uuid.uuid4()))

    patchers = [
        patch("app.services.after_close_orchestrator.AsyncSessionLocal",
              new=_fake_session_factory(job_run)),
        patch("app.services.bars_scheduler_service.BarsSchedulerService.refresh_all_instruments",
              new=AsyncMock(return_value=MagicMock(
                  dsa_run_id=uuid.uuid4(), daily_coverage=1.0, skip_reason=None))),
        patch("app.services.after_close_orchestrator.StrategyBatchService",
              new=MagicMock(return_value=batch)),
        patch("app.services.after_close_orchestrator.resolve_stock_core_published",
              new=spies["resolve"]),
        patch("app.services.auction_anchor_service.generate_and_publish_auction_anchors",
              new=spies["auction"]),
        patch("app.services.board_analysis_service.compute_all_boards",
              new=spies["aggregation"]),
        patch("app.services.feature_snapshot_service.compute_review_core_with_run_items",
              new=spies["compute_review_core"]),
        patch("app.services.state_event_service.generate_events_for_run",
              new=spies["events"]),
        patch("app.services.after_close_orchestrator._enqueue_chip_job_step",
              new=spies["chip"]),
        patch("app.services.after_close_orchestrator._update_heartbeat_and_step",
              new=spies["heartbeat_step"]),
        patch("app.services.after_close_orchestrator._get_job_run_or_raise",
              new=spies["get_job_run"]),
        patch("app.services.review_orchestrator_service.create_run",
              new=spies["create_run"]),
        patch("app.services.review_orchestrator_service.compute_run",
              new=spies["compute_run"]),
        patch("app.services.after_close_orchestrator.finish_snapshot_run",
              new=spies["finish_snapshot_run"]),
        patch("app.services.after_close_orchestrator.repair_stale_after_close_snapshot_runs",
              new=spies["repair"]),
        patch("app.services.after_close_orchestrator.get_active_a_share_instruments",
              new=AsyncMock(return_value=[])),
    ]
    # 移除占位
    patchers = [p for p in patchers if p is not None]
    for p in patchers:
        p.start()
    return spies, patchers


def _stop_patches(patchers):
    for p in patchers:
        p.stop()


def _published_resolution(session, trade_date, snapshot_run_id):
    return (True, False)


def _superseded_resolution(session, trade_date, snapshot_run_id):
    return (False, True)


async def _run_orchestrator(*, job_run, skip_publish):
    # skip_publish 由 last_completed_step="publishing" 推导；snapshot_run_id 由
    # metadata 的 feature_snapshot_run_id 读取。二者均不通过函数参数传入。
    await execute_after_close_run(
        job_run_id=job_run.id,
        trade_date=__import__("datetime").date(2026, 8, 7),
        worker_id="test-worker",
        lease_epoch=1,
        dsa_poll_interval=0.1,
        dsa_poll_timeout=1,
    )


async def test_normal_publish_pointer_current_triggers_auction_and_aggregation():
    job_run = _make_job_run(
        dsa_run_id=uuid.uuid4(), snapshot_run_id=uuid.uuid4())
    spies, patchers = _install_patches(
        job_run, resolve_side_effect=_published_resolution)
    try:
        await _run_orchestrator(job_run=job_run, skip_publish=False)
        # normal publish + pointer=current → auction anchor 必须被调用
        assert spies["auction"].called, "normal publish 应调用 auction anchor"
        assert spies["aggregation"].called, "normal publish 应调用 board aggregation"
        # publishing checkpoint 推进：_update_heartbeat_and_step 被以 PUBLISHING 调用
        assert any(
            len(c.args) >= 3 and c.args[2] == AfterCloseRunStatus.PUBLISHING.value
            for c in spies["heartbeat_step"].call_args_list
        ), "normal publish 应推进 publishing 检查点"
        # chip 入队（pointer 已发布）
        assert spies["chip"].called, "normal publish 应入队 chip"
        # 状态事件生成（pointer 已发布、未 superseded）
        assert spies["events"].called, "normal publish 应生成 state events"
    finally:
        _stop_patches(patchers)


async def test_superseded_run_not_consumed_by_auction_aggregation_events_chip():
    snap_id = uuid.uuid4()
    job_run = _make_job_run(dsa_run_id=uuid.uuid4(), snapshot_run_id=snap_id)
    spies, patchers = _install_patches(
        job_run, resolve_side_effect=_superseded_resolution)
    try:
        await _run_orchestrator(job_run=job_run, skip_publish=False)
        # superseded run 不得触发 auction / aggregation / state events / chip
        assert not spies["auction"].called, "superseded run 不应调用 auction anchor"
        assert not spies["aggregation"].called, "superseded run 不应调用 board aggregation"
        assert not spies["events"].called, "superseded run 不应生成 state events"
        assert not spies["chip"].called, "superseded run 不应入队 chip"
    finally:
        _stop_patches(patchers)


async def test_skip_publish_pointer_current_recovers_but_no_normal_publish_steps():
    snap_id = uuid.uuid4()
    job_run = _make_job_run(dsa_run_id=uuid.uuid4(), snapshot_run_id=snap_id,
                            last_completed_step="publishing")
    spies, patchers = _install_patches(
        job_run, resolve_side_effect=_published_resolution)
    try:
        await _run_orchestrator(job_run=job_run, skip_publish=True)
        # skip_publish 断点恢复：pointer 已恢复 → chip 重新入队
        assert spies["chip"].called, "skip_publish 恢复应重新入队 chip"
        # normal publish 专属步骤（auction / publishing 检查点）不得错误翻转到 resume
        assert not spies["auction"].called, \
            "skip_publish 不得执行 auction anchor"
        assert not any(
            len(c.args) >= 3 and c.args[2] == AfterCloseRunStatus.PUBLISHING.value
            for c in spies["heartbeat_step"].call_args_list
        ), "skip_publish 不得推进 publishing 检查点"
    finally:
        _stop_patches(patchers)


# ---------------------------------------------------------------------------
# [Phase 4.4 RB-01 2026-08-07] Recovery Boundary Closure
# Board aggregation 是 mandatory 步骤，判据只依赖 stock_core pointer 是否已发布，
# 不受 skip_publish 控制。publishing 检查点上的断点恢复必须补齐 aggregation，
# 否则 review 前置条件（aggregation_status == "succeeded"）永不满足。
# ---------------------------------------------------------------------------
async def test_rb01_skip_publish_recovery_still_runs_mandatory_board_aggregation():
    """publishing 检查点断点恢复：stock_core 已发布 → 必须补齐 board aggregation。"""
    snap_id = uuid.uuid4()
    job_run = _make_job_run(dsa_run_id=uuid.uuid4(), snapshot_run_id=snap_id,
                            last_completed_step="publishing")
    spies, patchers = _install_patches(
        job_run, resolve_side_effect=_published_resolution)
    try:
        await _run_orchestrator(job_run=job_run, skip_publish=True)
        assert spies["aggregation"].called, (
            "RB-01：skip_publish 断点恢复下 mandatory board aggregation 必须执行，"
            "不得因 skip_publish 被永久跳过"
        )
    finally:
        _stop_patches(patchers)


async def test_rb01_skip_publish_recovery_runs_review_after_aggregation():
    """publishing 断点恢复：aggregation 成功后 review 前置条件满足，review 必须执行。"""
    snap_id = uuid.uuid4()
    job_run = _make_job_run(dsa_run_id=uuid.uuid4(), snapshot_run_id=snap_id,
                            last_completed_step="publishing")
    spies, patchers, record = _install_patches_with_order(
        job_run, resolve_side_effect=_published_resolution)
    try:
        await _run_orchestrator(job_run=job_run, skip_publish=True)
        assert spies["aggregation"].called, "断点恢复应执行 board aggregation"
        assert spies["create_run"].called, (
            "RB-01：aggregation 成功后 review 前置条件应满足，review 必须执行"
        )
        assert record.index("chip") < record.index("aggregation"), (
            "断点恢复下 chip 仍必须早于 board aggregation（PC-8）"
        )
    finally:
        _stop_patches(patchers)


async def test_rb01_stock_core_not_published_still_skips_aggregation():
    """stock_core 未发布（superseded）→ aggregation 仍必须跳过，修复不放宽边界。"""
    snap_id = uuid.uuid4()
    job_run = _make_job_run(dsa_run_id=uuid.uuid4(), snapshot_run_id=snap_id,
                            last_completed_step="publishing")
    spies, patchers = _install_patches(
        job_run, resolve_side_effect=_superseded_resolution)
    try:
        await _run_orchestrator(job_run=job_run, skip_publish=True)
        assert not spies["aggregation"].called, (
            "stock_core 未发布时 board aggregation 仍不得执行"
        )
    finally:
        _stop_patches(patchers)


# ---------------------------------------------------------------------------
# [P1-2 2026-08-07] post-core 依赖顺序收口（V2.1 PC-8）
# Chip / State Events 不得等待 Board Aggregation：
#   stock_core published
#     ├─ state events
#     ├─ enqueue chip
#     ├─ auction anchor
#     └─ DSA projection
#           ↓
#        board aggregation
#           ↓
#         review
# ---------------------------------------------------------------------------
def _install_patches_with_order(job_run, *, resolve_side_effect):
    """与 _install_patches 相同，但记录各 step 的真实调用顺序到 return 元组。"""
    record: list[str] = []
    spies, patchers = _install_patches(job_run, resolve_side_effect=resolve_side_effect)

    def _wrap(name, spy):
        original = spy.side_effect

        async def _side_effect(*args, **kwargs):
            record.append(name)
            if callable(original):
                return await original(*args, **kwargs)
            return spy.return_value

        spy.side_effect = _side_effect

    _wrap("auction", spies["auction"])
    _wrap("aggregation", spies["aggregation"])
    _wrap("events", spies["events"])
    _wrap("chip", spies["chip"])
    return spies, patchers, record


async def test_p1_2_chip_enqueue_before_board_aggregation():
    """normal stock_core publication：chip 入队必须发生在 board aggregation 之前。"""
    job_run = _make_job_run(dsa_run_id=uuid.uuid4(), snapshot_run_id=uuid.uuid4())
    spies, patchers, record = _install_patches_with_order(
        job_run, resolve_side_effect=_published_resolution)
    try:
        await _run_orchestrator(job_run=job_run, skip_publish=False)
        assert spies["chip"].called, "normal publish 应入队 chip"
        assert spies["aggregation"].called, "normal publish 应调用 board aggregation"
        assert record.index("chip") < record.index("aggregation"), (
            "chip 入队必须早于 board aggregation（PC-8：chip 不得等待 aggregation）"
        )
    finally:
        _stop_patches(patchers)


async def test_p1_2_state_events_before_board_aggregation():
    """normal stock_core publication：state events 必须发生在 board aggregation 之前。"""
    job_run = _make_job_run(dsa_run_id=uuid.uuid4(), snapshot_run_id=uuid.uuid4())
    spies, patchers, record = _install_patches_with_order(
        job_run, resolve_side_effect=_published_resolution)
    try:
        await _run_orchestrator(job_run=job_run, skip_publish=False)
        assert spies["events"].called, "normal publish 应生成 state events"
        assert spies["aggregation"].called, "normal publish 应调用 board aggregation"
        assert record.index("events") < record.index("aggregation"), (
            "state events 必须早于 board aggregation（PC-8：events 不得等待 aggregation）"
        )
    finally:
        _stop_patches(patchers)


async def test_p1_2_superseded_does_not_trigger_events_chip_auction_aggregation():
    """superseded run 仍然不得触发 events/chip/auction/aggregation（PC-8 / P0-1）。

    注意：review 步骤的 skip_review 仅由断点恢复 completed 步骤决定，
    与 superseded 无关；因此本断言只覆盖 P1-2 关注的 post-core 4 类副作用，
    不约束 review 是否执行。
    """
    snap_id = uuid.uuid4()
    job_run = _make_job_run(dsa_run_id=uuid.uuid4(), snapshot_run_id=snap_id)
    spies, patchers, record = _install_patches_with_order(
        job_run, resolve_side_effect=_superseded_resolution)
    try:
        await _run_orchestrator(job_run=job_run, skip_publish=False)
        assert not spies["events"].called, "superseded run 不应生成 state events"
        assert not spies["chip"].called, "superseded run 不应入队 chip"
        assert not spies["auction"].called, "superseded run 不应调用 auction anchor"
        assert not spies["aggregation"].called, "superseded run 不应调用 board aggregation"
        # 这四类 post-core 副作用都不应出现在执行序列中
        post_core = {"events", "chip", "auction", "aggregation"}
        assert post_core.isdisjoint(set(record)), (
            f"superseded run 不应执行任何 post-core 副作用，实际序列: {record}"
        )
    finally:
        _stop_patches(patchers)
