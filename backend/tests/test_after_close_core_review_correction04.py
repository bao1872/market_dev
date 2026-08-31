"""[CORRECTION-04] AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01-CORRECTION-04 行为测试。

锁定四个 P0 修复后的生产 routing：

- P0-1 core_ready 生命周期：gate PASS 后不得被二次声明清零（唯一初始化点）。
- P0-2 mandatory Core gate 无条件化：snapshot_run_id=None 生产路径立即 fail-closed。
- P0-3 DSA publish_run 真实合同：publish_run(db, run_id)（只 flush）→ 显式 commit
  → 提交后复核 status=="published" 且 published_at 非空。
- P0-4 DSA step_summary 合同：统一执行器标准 summary（step 键）落库，
  failed 进 optional_failures → parent partial_success。

全部为纯单元（fake/injected session/service），关键生产方法一律 autospec /
显式匹配真实签名（禁止自定义假签名 mock 冒充）。
"""

import asyncio
import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch

import pytest

import app.services.after_close_orchestrator as orch
from app.services.after_close_orchestrator import (
    AfterCloseCancelledError,
    AfterCloseCoreNotReadyError,
    _run_dsa_compatibility_projection,
)
from app.services.strategy_batch_service import StrategyBatchService

T_DATE = __import__("datetime").date(2026, 8, 25)


# ---------------------------------------------------------------------------
# Part 0 · 公共 fake 设施（签名严格匹配生产 callable）
# ---------------------------------------------------------------------------


class FakeResult:
    """execute(...) 的通用空结果（scalars().all()/scalar_one_or_none 等链）。"""

    def __init__(self, rows=None, single=None):
        self._rows = rows or []
        self._single = single

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def unique(self):
        return self

    def first(self):
        return self._single

    def scalar_one_or_none(self):
        return self._single


class FakeSessionCtx:
    """极简 async session：.get 按键查表；支持多次 async with（模拟新会话）。"""

    def __init__(self, store):
        self._store = store  # {(model_name, key): row}
        self.commits = 0
        self.flushes = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, model, key):
        name = getattr(model, "__name__", str(model))
        return self._store.get((name, key))

    async def execute(self, *a, **k):
        return FakeResult()

    async def commit(self):
        self.commits += 1

    async def flush(self):
        self.flushes += 1

    async def rollback(self):
        return None

    async def refresh(self, obj):
        return None

    def add(self, obj):
        return None


def _core_run(status="succeeded", run_id=None, trade_date=T_DATE):
    from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun

    row = StockFeatureSnapshotRun()
    row.id = run_id or uuid.uuid4()
    row.trade_date = trade_date
    row.status = status
    row.run_type = "after_close"
    return row


def _strategy_run(status="completed", succeeded_count=95):
    from app.models.strategy_run import StrategyRun

    row = StrategyRun()
    row.id = uuid.uuid4()
    row.status = status
    row.succeeded_count = succeeded_count
    row.total_instruments = 100
    row.failed_count = 100 - succeeded_count
    row.strategy_version_id = uuid.uuid4()
    row.published_at = None
    row.error_message = None
    row.trade_date = T_DATE
    # [CORRECTION-04-PG-GATE] lineage 以 input_overrides 承载（与生产写入同构）
    row.input_overrides = {
        "source_core_run_id": "CORE-X",
        "requirement": "required_compatibility",
    }
    return row


def _job_row(trade_date=T_DATE):
    from app.models.scheduler_job_run import SchedulerJobRun

    now = datetime.now(timezone.utc)
    row = SchedulerJobRun()
    row.id = uuid.uuid4()
    row.job_name = "after_close_orchestrator"
    row.business_date = trade_date.isoformat()
    row.run_key = f"cor04:{row.id}"
    row.status = "running"
    row.metadata_json = "{}"
    row.scheduled_at = now
    row.started_at = now
    row.heartbeat_at = now
    row.lease_expires_at = now
    row.finished_at = None
    row.error_message = None
    row.worker_instance_id = None
    return row


# ---------------------------------------------------------------------------
# Part 1 · DSA helper 合同（P0-3/KPI-6/7/8）：autospec 真实签名
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dsa_publish_uses_real_signature_and_commits_case_F_G():
    """Case F/G: publish_run 以真实签名 (db, run_id) 调用；commit 后复核已持久化状态。

    - create_autospec 绑定生产 StrategyBatchService.publish_run：
      参数名/顺序偏离真实签名会直接 TypeError。
    - side_effect 按 publish_run 真实 mutating 语义写 published 行（仅 flush；
      commit 由调用方/helper 负责 —— fake session 记录两者计数以便区分）。
    - helper 提交后用"第二个会话"复核同一行：status=published、published_at!=None。
    """
    dsa_row = _strategy_run(status="completed")
    store = {
        ("SchedulerJobRun", "JOB"): _job_row(),
        ("StrategyRun", dsa_row.id): dsa_row,
    }

    sessions_opened = []

    def factory():
        ctx = FakeSessionCtx(store)
        sessions_opened.append(ctx)
        return ctx

    spec_publish = create_autospec(StrategyBatchService.publish_run)
    created_dsa_rows: list = []

    async def publish_side_effect(_self, db, run_id):
        # 真实语义：completed+succeeded>0 → published/published_at；仅 flush。
        target = next(r for r in created_dsa_rows if r.id == run_id)
        assert run_id == target.id
        db.flushes += 1
        target.status = "published"
        target.published_at = datetime.now(timezone.utc)
        return target

    spec_publish.side_effect = publish_side_effect

    class StubRepo:
        instances: list["StubRepo"] = []

        def __init__(self, *a, **k):
            type(self).instances.append(self)
            self.calls = []

        async def project_dsa_batch(self, **kw):
            # 忠实模拟生产协作语义：persist_precomputed_dsa_results 完成后
            # 将 DSA run 推进为 completed（真实实现位于 strategy_batch_service:1913）。
            run_row = store.get(("StrategyRun", kw.get("dsa_run_id"))) or store.get(
                kw.get("dsa_run_id")
            )
            if run_row is not None:
                run_row.status = "completed"
                run_row.succeeded_count = 95
            self.calls.append(kw)

    spec_create = create_autospec(StrategyBatchService.create_batch_run)

    async def create_side_effect(_self, **kw):
        # 签名合同：兼容性投影必须 source_core_run_id 显式绑定 X
        assert kw["source_core_run_id"] == "CORE-X"
        assert kw.get("requirement") == "required_compatibility"
        new_row = _strategy_run(status="running", succeeded_count=0)
        new_row.id = uuid.uuid4()
        store[("StrategyRun", new_row.id)] = new_row
        store[new_row.id] = new_row
        created_dsa_rows.append(new_row)
        return new_row

    spec_create.side_effect = create_side_effect

    spec_quality = create_autospec(StrategyBatchService._check_quality_gates)
    spec_quality.side_effect = lambda _self, *a, **k: True

    import app.services.core_artifact_repository as car_mod

    with (
        patch_session_local(orch, factory),
        patch_publish_stack(spec_publish, spec_create, spec_quality),
        patch_publish_repo(car_mod, StubRepo),
        patch.object(
            orch.strategy_result_repository, "count_by_run",
            new=_count_by_run_async(95),
        ),
        patch_publish_result_row(store, store),
    ):
        result = await _run_dsa_compatibility_projection(
            job_run_id="JOB",
            worker_id="w1",
            lease_epoch=1,
            trade_date=T_DATE,
            snapshot_run_id="CORE-X",
            dsa_run_id=None,
            instrument_ids=["600000"],
        )

    # Case F：真实签名命中 —— autospec 下类方法调用带 self：
    # call_args.args == (service_instance, db, run_id)，且不得以 dsa_run_id= 关键字形式调用
    assert spec_publish.call_count == 1
    p_args, p_kwargs = spec_publish.call_args
    assert len(p_args) == 3, f"publish_run 必须以位置参数 (db, run_id) 调用: {p_args}"
    assert not any(k.startswith("run_id") or k == "dsa_run_id" for k in p_kwargs), (
        "禁止 publish_run(run_id=...) 关键字假签名"
    )
    assert isinstance(result, dict) and result["status"] == "succeeded"

    # Case G：commit 之后，用"第二次打开的会话"复核同一行的持久化事实
    assert any(ctx.commits > 0 for ctx in sessions_opened)
    probe_ctx = FakeSessionCtx(store)
    target_row = created_dsa_rows[-1]
    fresh_row = await probe_ctx.get(type(target_row), target_row.id)
    assert fresh_row.status == "published"
    assert fresh_row.published_at is not None
    # producer/repo 只消费持久化 Core artifact（source_core_run_id=X），无 kernel 重算
    assert StubRepo.instances and StubRepo.instances[-1].calls
    assert StubRepo.instances[-1].calls[-1]["source_core_run_id"] == "CORE-X"


@pytest.mark.asyncio
async def test_dsa_projection_failure_marks_run_failed_then_raises_case_E_helper():
    """Case E(helper 面): project 抛异常 → 先如实标 run failed，再向 executor re-raise。"""
    dsa_row = _strategy_run(status="running")
    store = {
        ("SchedulerJobRun", "JOB"): _job_row(),
        ("StrategyRun", dsa_row.id): dsa_row,
        dsa_row.id: dsa_row,
    }

    def factory():
        return FakeSessionCtx(store)

    class ExplodingRepo:
        def __init__(self, *a, **k):
            pass

        async def project_dsa_batch(self, **k):
            raise RuntimeError("projection boom")

    spec_create = create_autospec(StrategyBatchService.create_batch_run)

    async def _no_create(_self, **kw):
        raise AssertionError("已有 dsa_run_id 时不得新建")

    spec_create.side_effect = _no_create

    import app.services.core_artifact_repository as car_mod

    with (
        patch_session_local(orch, factory),
        patch_publish_stack(None, spec_create, None),
        patch_publish_repo(car_mod, ExplodingRepo),
    ):
        with pytest.raises(RuntimeError, match="projection boom"):
            await _run_dsa_compatibility_projection(
                job_run_id="JOB",
                worker_id="w1",
                lease_epoch=1,
                trade_date=T_DATE,
                snapshot_run_id="CORE-X",
                dsa_run_id=dsa_row.id,
                instrument_ids=["600000"],
            )

    # 异常路径必须先把 run 如实标记 failed（而非吞掉异常伪造状态）
    assert dsa_row.status == "failed"
    assert dsa_row.error_message and "projection boom" in dsa_row.error_message


@pytest.mark.asyncio
async def test_dsa_quality_gate_failure_raises_not_fake_success():
    """质量门禁未通过 → RuntimeError（绝不以 succeeded 伪造 required compatibility）。"""
    dsa_row = _strategy_run(status="completed")
    store = {
        ("SchedulerJobRun", "JOB"): _job_row(),
        ("StrategyRun", dsa_row.id): dsa_row,
        dsa_row.id: dsa_row,
    }
    spec_create = create_autospec(StrategyBatchService.create_batch_run)

    async def _no_create(_self, **kw):
        raise AssertionError("不得新建")

    spec_create.side_effect = _no_create

    class NoOpRepo:
        def __init__(self, *a, **k):
            pass

        async def project_dsa_batch(self, **k):
            return None

    spec_quality = create_autospec(StrategyBatchService._check_quality_gates)
    spec_quality.side_effect = lambda _self, *a, **k: False  # 门禁未通过

    with (
        patch_session_local(orch, lambda: FakeSessionCtx(store)),
        patch_publish_stack(None, spec_create, spec_quality),
        patch_publish_repo(__import__(
            "app.services.core_artifact_repository", fromlist=["x"]
        ), NoOpRepo),
        patch.object(
            orch.strategy_result_repository, "count_by_run",
            new=_count_by_run_async(95),
        ),
    ):
        with pytest.raises(RuntimeError, match="质量门禁"):
            await _run_dsa_compatibility_projection(
                job_run_id="JOB",
                worker_id="w1",
                lease_epoch=1,
                trade_date=T_DATE,
                snapshot_run_id="CORE-X",
                dsa_run_id=dsa_row.id,
                instrument_ids=["600000"],
            )


def _count_by_run_async(value):
    async def _f(*a, **k):
        return value

    return _f


def patch_session_local(module, factory):
    return __import__("unittest").mock.patch.object(
        module, "AsyncSessionLocal", new=factory
    )


def patch_publish_stack(spec_publish, spec_create, spec_quality):
    """对生产类的三类方法做类级 autospec 替换（publish 侧可为 None=保持原样不触达）。"""
    from unittest import mock

    stack = []
    if spec_publish is not None:
        stack.append(mock.patch.object(
            StrategyBatchService, "publish_run", spec_publish))
    if spec_create is not None:
        stack.append(mock.patch.object(
            StrategyBatchService, "create_batch_run", spec_create))
    if spec_quality is not None:
        stack.append(mock.patch.object(
            StrategyBatchService, "_check_quality_gates", spec_quality))

    class _MultiCM:
        def __enter__(self):
            for cm in stack:
                cm.__enter__()
            return self

        def __exit__(self, *exc):
            for cm in reversed(stack):
                cm.__exit__(*exc)
            return False

    return _MultiCM()


def patch_publish_repo(car_mod, repo_cls):
    return __import__("unittest").mock.patch.object(
        car_mod, "CoreArtifactRepository", repo_cls
    )


def patch_publish_result_row(_unused_marker, store):
    """占位（保留 hook 供后续扩展）；当前返回 no-op CM。"""
    class _Noop:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    return _Noop()


# ---------------------------------------------------------------------------
# Part 2 · 全链 routing 行为（Cases A/B/C/D/E/H/I/J）
# ---------------------------------------------------------------------------


def build_harness(
    monkeypatch,
    *,
    core_status="succeeded",
    core_row_missing=False,
    review_summary=None,
    dsa_project_fail=False,
    cancel_at=None,
):
    """搭建 execute_after_close_run 的纯单元 harness。

    - 路由真实性保障：execute_orchestrator_step 以 wrapper 包装原实现（保真，
      仅记录 step 名）；mandatory Core gate / terminal short-circuit /
      协作式取消等决策点全部走真实生产代码。
    - 业务体隔离：review/history 各步的业务闭包替换为受控替身（routing 判定
      仍由生产代码完成 —— 这是被测对象）。
    - cancel_at='computing_review'：在该步骤边界把 job 行置 cancelled，
      生产 cancellation_check/executor 自然产生真实的 cancelled summary
      （等价于管理员恰在此刻取消的语义），随后驱动 Review 终态短路。
    """
    snap_id = uuid.uuid4()
    history_id = uuid.uuid4()

    core_row = None if core_row_missing else _core_run(status=core_status, run_id=snap_id)
    # create_snapshot_run 的替身返回对象：missing 场景创建成功（合法）但 store 中
    # 不存在该行 —— canonical owner 校验时仍判定 CoreRun 缺失。
    created_core_stub = _core_run(
        status="running", run_id=snap_id,
    ) if core_row_missing else None
    job_row = _job_row()
    if core_row is not None:
        job_row.metadata_json = json.dumps({
            "last_completed_step": "syncing_boards",
        })
    store = {("SchedulerJobRun", job_row.id): job_row}
    if core_row is not None:
        store[("StockFeatureSnapshotRun", snap_id)] = core_row
        store[snap_id] = core_row

    rec = {"steps": [], "events": [], "chip": [], "sessions": []}

    real_executor = orch.execute_orchestrator_step

    async def recording_executor(step, operation, **kwargs):
        if cancel_at is not None:
            if step == cancel_at:
                job_row.status = "cancelled"  # 管理员取消恰好发生在该步骤边界
            elif step != cancel_at and step in ("syncing_boards", "checking_coverage"):
                job_row.status = "running"  # 保证更早步骤不被误取消
        rec["steps"].append(step)
        return await real_executor(step, operation, **kwargs)

    def factory():
        ctx = FakeSessionCtx(store)
        rec["sessions"].append(ctx)
        return ctx

    batch_stub = __import__("types").SimpleNamespace(
        dsa_run_id=None, skip_reason="OK", daily_coverage=1.0,
    )

    review_result_stub = __import__("types").SimpleNamespace(id=uuid.uuid4())
    hist_stub = __import__("types").SimpleNamespace(id=history_id)

    review_cancelled = review_summary is not None and (
        review_summary.get("status") == "cancelled"
    )

    async def fake_review_op(**kw):
        if review_cancelled:
            # 与生产等价的取消形态：业务体抛 CancelledError → 统一执行器
            # _run_with_cancellation/except 链生成 status="cancelled" 的真实 summary
            # （不伪造 summary dict）。
            raise asyncio.CancelledError()
        if core_status != "succeeded":
            return (None, {"status": "skipped"})
        return review_result_stub, dict(review_summary)

    async def make_history(**kw):
        async def _op():
            return {"history_run_id": hist_stub.id, "ready": True}

        return _op

    async def fake_create_chip_consensus_job(*args, **kwargs):
        # [CHIP-RETIRE 2026-09-01] 自动 chip 已退役：服务函数本身仍保留（历史兼容），
        # 因此此处仍在**真实服务模块**上安装录制替身 —— rec["chip"] 为空是
        # 「主链未调用」的有效证据，而非「符号不存在」的空断言。
        rec["chip"].append(kwargs or args)
        return hist_stub, True

    chip_mod = __import__(
        "app.services.after_close_chip_consensus_service", fromlist=["x"],
    )
    state_events_mod = __import__("app.services.state_event_service", fromlist=["x"])
    fss_mod = __import__("app.services.feature_snapshot_service", fromlist=["x"])

    class StubRepo:
        def __init__(self, *a, **k):
            pass

        async def project_dsa_batch(self, **kw):
            if dsa_project_fail:
                raise RuntimeError("projection boom(harness)")
            return None

    spec_create_sbrun = create_autospec(StrategyBatchService.create_batch_run)

    async def create_sbrun_side(**kw):
        from app.models.strategy_run import StrategyRun as SR

        row = SR()
        row.id = uuid.uuid4()
        row.status = "running"
        row.succeeded_count = 0
        row.strategy_version_id = uuid.uuid4()
        store[("StrategyRun", row.id)] = row
        store[row.id] = row
        return row

    spec_create_sbrun.side_effect = create_sbrun_side

    car_mod = __import__("app.services.core_artifact_repository", fromlist=["x"])

    patches = [
        __import__("unittest").mock.patch.object(
            orch, "AsyncSessionLocal", new=factory),
        __import__("unittest").mock.patch.object(
            orch, "execute_orchestrator_step", recording_executor),
        __import__("unittest").mock.patch.object(
            orch.BarsSchedulerService, "refresh_all_instruments",
            AsyncMock(return_value=batch_stub),
        ),
        __import__("unittest").mock.patch.object(
            orch, "_execute_syncing_boards", AsyncMock(return_value=(
                {"status": "skipped"}, {"status": "skipped"}))),
        __import__("unittest").mock.patch.object(
            orch, "repair_stale_after_close_snapshot_runs",
            AsyncMock(return_value=[]),
        ),
        __import__("unittest").mock.patch.object(
            orch, "get_active_a_share_instruments",
            AsyncMock(return_value=["600000"]),
        ),
        __import__("unittest").mock.patch.object(
            orch, "create_snapshot_run",
            AsyncMock(return_value=created_core_stub or core_row),
        ),
        __import__("unittest").mock.patch.object(
            orch, "finalize_snapshot_run_compute_complete",
            AsyncMock(return_value=core_row),
        ),
        __import__("unittest").mock.patch.object(
            orch, "finish_snapshot_run", AsyncMock(),
        ),
        __import__("unittest").mock.patch.object(
            orch, "append_event", AsyncMock(return_value=None),
        ),
        __import__("unittest").mock.patch.object(
            orch, "list_events", AsyncMock(return_value=[]),
        ),
        # 事件服务自身命名空间持有独立 AsyncSessionLocal 引用（纯单元不得触库）
        __import__("unittest").mock.patch.object(
            __import__("app.services.job_run_event_service", fromlist=["x"]),
            "append_event", AsyncMock(return_value=None),
        ),
        __import__("unittest").mock.patch.object(
            __import__("app.services.job_run_event_service", fromlist=["x"]),
            "list_events", AsyncMock(return_value=[]),
        ),
        __import__("unittest").mock.patch.object(
            fss_mod, "compute_review_core_with_run_items",
            AsyncMock(return_value={"snapshot_count": 5, "failed_count": 0}),
        ),
        __import__("unittest").mock.patch.object(
            orch, "_execute_review_step",
            fake_review_op,
        ),
        __import__("unittest").mock.patch.object(
            orch, "_make_history_step", make_history),
        __import__("unittest").mock.patch.object(
            chip_mod, "create_after_close_chip_consensus_job",
            fake_create_chip_consensus_job,
        ),
        __import__("unittest").mock.patch.object(
            state_events_mod, "generate_events_for_run",
            _spy_async(rec, "events", 1),
        ),
        __import__("unittest").mock.patch.object(
            state_events_mod, "cleanup_old_events", AsyncMock(return_value={})
        ),
        __import__("unittest").mock.patch.object(
            car_mod, "CoreArtifactRepository", StubRepo),
        __import__("unittest").mock.patch.object(
            StrategyBatchService, "create_batch_run", spec_create_sbrun),
        # publication 域零交互 spy（I/J）
        *[p for p in _spy_feature_snapshot(fss_mod)],
        __import__("unittest").mock.patch.dict(
            "os.environ", {}, clear=False),
    ]

    return {
        "patches": patches,
        "store": store,
        "job_row": job_row,
        "snap_id": snap_id,
        "core_row": core_row,
        "rec": rec,
        "run_id": job_row.id,
    }


def _spy_async(rec, key, arg_index):
    async def _f(*args, **kwargs):
        target = args[arg_index] if len(args) > arg_index else kwargs
        rec[key].append(args[1] if len(args) > 1 else kwargs)
        return {"event_count": 0}

    return _f


def _spy_feature_snapshot(mod):
    """feature_snapshot_service 公开函数全量 wrap-spy（I/J 行为零交互证明）。

    排除集合：这些符号已由 harness 以业务替身接管（同模块同名），
    若再 wrap 会以后入栈覆盖替身，导致 routing 用例误触真实实现。
    """
    import inspect
    from unittest import mock

    excluded = {
        "compute_review_core_with_run_items",
        "repair_stale_after_close_snapshot_runs",
        "create_snapshot_run",
        "finalize_snapshot_run_compute_complete",
        "finish_snapshot_run",
        "get_active_a_share_instruments",
    }
    out = []
    for name in dir(mod):
        if name.startswith("_") or name in excluded:
            continue
        try:
            obj = getattr(mod, name)
        except Exception:
            continue
        if inspect.isfunction(obj):
            out.append(mock.patch.object(mod, name, wraps=obj))
    return out


async def _run_main(job_id, trade_date=T_DATE):
    await orch.execute_after_close_run(job_id, trade_date)


@pytest.mark.asyncio
async def test_case_A_core_ready_full_chain_executes(monkeypatch):
    """Case A: Core succeeded → gate PASS → Review/History/DSA/events 全部被调用。

    [CHIP-RETIRE 2026-09-01] chip 自动入队已退役：同一条成功主链中
    chip 步骤不得出现、chip create 服务不得被调用（rec["chip"] 由真实服务
    模块上的录制替身供证，见 build_harness）。
    """
    h = build_harness(monkeypatch)
    from contextlib import ExitStack

    with ExitStack() as st:
        for p in h["patches"]:
            st.enter_context(p)
        await _run_main(h["run_id"])

    steps = h["rec"]["steps"]
    assert "computing_review" in steps
    assert "computing_history" in steps
    assert "dsa_compatibility" in steps
    # KPI-1：整条链没有任何环节重新清零 core_ready —— state_events 已产生副作用
    assert h["rec"]["events"], "state_events 必须为 X 执行"
    # CHIP-RETIRE：成功主链不得再产生 chip 步骤或 chip job
    assert "enqueue_chip_job" not in steps, "chip 步骤已退役，不得出现在主链"
    assert h["rec"]["chip"] == [], (
        "自动 chip 已退役：正常成功主链不得调用 create_after_close_chip_consensus_job"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_status", ["failed", "running"])
async def test_case_B_C_core_not_success_fail_closed(monkeypatch, bad_status):
    """Case B/C: Core failed/running → 下游调用数全部 0 → parent mandatory failed。"""
    h = build_harness(monkeypatch, core_status=bad_status)
    from contextlib import ExitStack

    with ExitStack() as st:
        for p in h["patches"]:
            st.enter_context(p)
        with pytest.raises((AfterCloseCoreNotReadyError, RuntimeError)):
            await _run_main(h["run_id"])

    steps = h["rec"]["steps"]
    for forbidden in (
        "computing_review", "computing_history", "dsa_compatibility",
        "enqueue_chip_job",
    ):
        assert forbidden not in steps, f"{forbidden} 不得在 Core 未就绪时执行"
    assert h["rec"]["events"] == []
    assert h["rec"]["chip"] == []
    assert h["job_row"].status == "failed"


@pytest.mark.asyncio
async def test_case_D_core_missing_fail_closed(monkeypatch):
    """Case D: snapshot_run_id/CoreRun 缺失 → fail-closed，下游 0 调用。"""
    h = build_harness(monkeypatch, core_row_missing=True)
    from contextlib import ExitStack

    with ExitStack() as st:
        for p in h["patches"]:
            st.enter_context(p)
        with pytest.raises(AfterCloseCoreNotReadyError):
            await _run_main(h["run_id"])

    assert "computing_review" not in h["rec"]["steps"]
    assert "computing_history" not in h["rec"]["steps"]
    assert h["rec"]["events"] == [] and h["rec"]["chip"] == []


@pytest.mark.asyncio
async def test_case_E_dsa_failure_partial_success_and_summary(monkeypatch):
    """Case E: DSA projection 异常 → step_summary.dsa_compatibility failed
    → parent partial_success；Core 保持 succeeded、Review 已有效。"""
    h = build_harness(monkeypatch, dsa_project_fail=True)
    from contextlib import ExitStack

    with ExitStack() as st:
        for p in h["patches"]:
            st.enter_context(p)
        await _run_main(h["run_id"])  # optional executor 吸收 → 不 raise

    steps = h["rec"]["steps"]
    # Review 已先于 DSA 完成（有效 lineage 不受影响）
    assert steps.index("computing_review") < steps.index("dsa_compatibility")
    assert h["job_row"].status == "partial_success"

    meta = json.loads(h["job_row"].metadata_json or "{}")
    summary = (meta.get("step_summary") or {}).get("dsa_compatibility") or {}
    assert summary.get("status") == "failed", summary
    assert summary.get("optional") is True
    assert "dsa_compatibility" in (meta.get("optional_failures") or [])
    # Core 事实源未被撤销
    assert h["core_row"].status == "succeeded"


@pytest.mark.asyncio
async def test_case_H_review_cancelled_stops_all_side_effects(monkeypatch):
    """Case H: Review cancelled（生产协作取消路径真实命中）→
    History/DSA/events/chip 副作用调用次数 0。"""
    h = build_harness(monkeypatch, cancel_at="computing_review")
    from contextlib import ExitStack

    with ExitStack() as st:
        for p in h["patches"]:
            st.enter_context(p)
        # 生产语义：Review 终态短路 raise AfterCloseCancelledError →
        # 编排器外层按"取消不是失败"吸收并正常返回（不得覆盖终端状态）。
        await _run_main(h["run_id"])

    steps = h["rec"]["steps"]
    assert "computing_review" in steps  # Review 步骤本身已启动并被取消收尾
    for forbidden in ("computing_history", "dsa_compatibility"):
        assert forbidden not in steps, (
            f"Review cancelled 后不得继续 {forbidden}"
        )
    assert h["rec"]["events"] == [] and h["rec"]["chip"] == []
    assert h["job_row"].status in ("cancelled", "interrupted")


@pytest.mark.asyncio
async def test_case_I_J_no_publication_interaction_in_normal_chain(monkeypatch):
    """Case I/J: 正常主链 publish_stock_core_atomically 结构性缺席 +
    feature_snapshot_service publication/read 函数零调用。"""
    import app.services.feature_snapshot_service as fss
    import app.services.stock_core_publication_service as scps

    h = build_harness(monkeypatch)
    from contextlib import ExitStack

    spy_started = []
    spies = {}
    for mod in (fss, scps):
        for name in dir(mod):
            if name.startswith("_"):
                continue
            try:
                obj = getattr(mod, name)
            except Exception:
                continue
            import inspect
            if inspect.isfunction(obj):
                pm = patch.object(mod, name, wraps=obj)
                spies[name] = pm

    with ExitStack() as st:
        for name, pm in spies.items():
            started = st.enter_context(pm)
            spy_started.append((name, started))
        for p in h["patches"]:
            st.enter_context(p)
        await _run_main(h["run_id"])

    touched = [n for n, m in spy_started if m.called]
    # publish_stock_core_atomically 本身允许存在（legacy service 保留），
    # 但正常主链全程不得触碰它或任何 stock_core publication 域函数。
    assert touched == [], f"正常主链不得触碰 stock_core publication 域: {touched}"


def test_publish_stock_core_not_referenced_by_orchestrator_structural_I():
    """Case I(结构面): 编排器不再持有 publish_stock_core_atomically 符号/引用。"""
    assert not hasattr(orch, "publish_stock_core_atomically"), (
        "after_close_orchestrator 不应再存在 publish_stock_core_atomically"
    )
    import inspect

    src = inspect.getsource(orch.execute_after_close_run)
    assert "publish_stock_core_atomically(" not in src, (
        "主链源码不得调用 publish_stock_core_atomically"
    )


# ---------------------------------------------------------------------------
# DSA already-published resume（CORRECTION-04-PG-GATE Case R1/R2/R3）
# ---------------------------------------------------------------------------


_PUBLISHED_AT_AUTO = object()  # 仅在未显式指定时自动生成时间戳


def _published_dsa_row(
    *,
    status="published",
    published_at=_PUBLISHED_AT_AUTO,
    source_core="CORE-X",
    requirement="required_compatibility",
):
    from app.models.strategy_run import StrategyRun
    from datetime import datetime, timezone

    row = StrategyRun()
    row.id = uuid.uuid4()
    row.status = status
    if published_at is _PUBLISHED_AT_AUTO:
        row.published_at = (
            datetime.now(timezone.utc) if status == "published" else None
        )
    else:
        row.published_at = published_at  # 允许显式 None（Case R3）
    row.trade_date = T_DATE
    row.succeeded_count = 95
    row.total_instruments = 100
    row.strategy_version_id = uuid.uuid4()
    # lineage 以 input_overrides JSONB 承载（与 create_batch_run 写入路径同构）
    row.input_overrides = {
        "source_core_run_id": source_core,
        "requirement": requirement,
    }
    return row


@pytest.mark.asyncio
async def test_dsa_resume_published_idempotent_R1():
    """Case R1: 已 published + lineage 正确 → resume 幂等返回 succeeded；
    projection call_count=0、create_batch_run call_count=0。"""
    dsa_row = _published_dsa_row(published_at=datetime.now(timezone.utc))
    store = {
        ("SchedulerJobRun", "JOB"): _job_row(),
        ("StrategyRun", dsa_row.id): dsa_row,
        dsa_row.id: dsa_row,
    }

    spec_create = create_autospec(StrategyBatchService.create_batch_run)

    async def _must_not_create(_self, **kw):
        raise AssertionError("已发布 run 恢复不得新建 compatibility run")

    spec_create.side_effect = _must_not_create

    class RepoMustNotRun:
        def __init__(self, *a, **k):
            self.calls = []

        async def project_dsa_batch(self, **kw):
            raise AssertionError("已发布 run 恢复不得重新投影")

    spec_publish = create_autospec(StrategyBatchService.publish_run)

    async def _must_not_publish(_self, db, run_id):
        raise AssertionError("已发布 run 恢复不得重复 publish")

    spec_publish.side_effect = _must_not_publish

    import app.services.core_artifact_repository as car_mod

    with (
        patch_session_local(orch, lambda: FakeSessionCtx(store)),
        patch_publish_stack(spec_publish, spec_create, None),
        patch_publish_repo(car_mod, RepoMustNotRun),
    ):
        result = await _run_dsa_compatibility_projection(
            job_run_id="JOB",
            worker_id="w1",
            lease_epoch=1,
            trade_date=T_DATE,
            snapshot_run_id="CORE-X",
            dsa_run_id=dsa_row.id,
            instrument_ids=["600000"],
        )

    assert result["status"] == "succeeded"
    assert result.get("resumed") is True
    assert result["dsa_run_id"] == str(dsa_row.id)
    assert spec_create.call_count == 0, "create_batch_run call_count 必须为 0"
    assert spec_publish.call_count == 0


class _NoopRepo:
    def __init__(self, *a, **k):
        pass

    async def project_dsa_batch(self, **kw):
        raise AssertionError("lineage 校验失败路径不得触发投影")


@pytest.mark.asyncio
async def test_dsa_resume_lineage_mismatch_fail_closed_R2():
    """Case R2: source_core_run_id != X → fail-closed，禁止复用他人兼容输出。"""
    dsa_row = _published_dsa_row(source_core="OTHER-CORE")
    store = {
        ("SchedulerJobRun", "JOB"): _job_row(),
        ("StrategyRun", dsa_row.id): dsa_row,
        dsa_row.id: dsa_row,
    }
    import app.services.core_artifact_repository as car_mod

    with (
        patch_session_local(orch, lambda: FakeSessionCtx(store)),
        patch_publish_stack(None, None, None),
        patch_publish_repo(car_mod, _NoopRepo),
    ):
        with pytest.raises(RuntimeError, match="lineage"):
            await _run_dsa_compatibility_projection(
                job_run_id="JOB",
                worker_id="w1",
                lease_epoch=1,
                trade_date=T_DATE,
                snapshot_run_id="CORE-X",
                dsa_run_id=dsa_row.id,
                instrument_ids=["600000"],
            )


@pytest.mark.asyncio
async def test_dsa_resume_published_without_timestamp_fail_closed_R3():
    """Case R3: published 但 published_at=None → fail-closed，禁止冒充成功。"""
    dsa_row = _published_dsa_row(status="published", published_at=None)
    store = {
        ("SchedulerJobRun", "JOB"): _job_row(),
        ("StrategyRun", dsa_row.id): dsa_row,
        dsa_row.id: dsa_row,
    }
    import app.services.core_artifact_repository as car_mod

    with (
        patch_session_local(orch, lambda: FakeSessionCtx(store)),
        patch_publish_stack(None, None, None),
        patch_publish_repo(car_mod, _NoopRepo),
    ):
        with pytest.raises(RuntimeError, match="published_at"):
            await _run_dsa_compatibility_projection(
                job_run_id="JOB",
                worker_id="w1",
                lease_epoch=1,
                trade_date=T_DATE,
                snapshot_run_id="CORE-X",
                dsa_run_id=dsa_row.id,
                instrument_ids=["600000"],
            )
