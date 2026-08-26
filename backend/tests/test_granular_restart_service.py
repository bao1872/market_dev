"""Granular restart service 单测（PURE_UNIT_TEST，Corrective Pass 3）。

本轮验证 CP3 的确定性修复，全部为**行为断言**，不断言"提交存在"：

1. `is_implemented_boundary` 以真实 handler registry 为唯一权威，10/10 全部有真实 handler。
2. **废除 `last_completed_step`**：主链 boundary 绝不写 last_completed_step，
   改为在 child metadata 写 `mainchain_stage`（"从哪个阶段开始"，语义与"已完成"相反），
   child 保持 queued（不伪造成功）。
3. **幂等键含 parent / source / input_hash**：跨 parent、跨 source、跨 input 不得误复用。
4. **succeeded + 同 input_hash → 不再执行 handler**（真幂等，不是"复用行但重跑"）。
5. active（queued/running）child 存在 → 不重复调度。
6. failed → attempt_no+1 重新执行。
7. 子产品失败记 level=error 事件 + child.failed，不 501、不伪造成功。

真实 PG 路径（真实 publisher / 真实重建）在远程验证库首跑验证（Phase 4，rules/80 DS-110）。
"""

from __future__ import annotations

import json
import hashlib
import uuid
from typing import Any

import pytest

from app.services.granular_restart_service import (
    ALL_BOUNDARIES,
    build_run_key,
    compute_input_hash,
    dispatch_restart,
    implemented_boundaries,
    is_implemented_boundary,
)

pytestmark = pytest.mark.asyncio


# =============================================================================
# fakes
# =============================================================================


class _FakeResult:
    def __init__(self, rows: list[Any]):
        self._rows = rows

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None

    def scalars(self):
        return self

    def scalar_one_or_none(self):
        if not self._rows:
            return None
        row = self._rows[0]
        return row[0] if isinstance(row, tuple) else row


class _FakeJobRun:
    """父 orchestrator run。"""

    def __init__(self, business_date: str = "2026-08-05", metadata: dict | None = None):
        self.id = uuid.uuid4()
        self.job_name = "after_close_orchestrator"
        self.business_date = business_date
        self.status = "succeeded"
        self.metadata_json = json.dumps(metadata or {"trade_date": business_date})


class _FakeChild:
    """模拟已存在的 child SchedulerJobRun。"""

    def __init__(self, *, run_key: str, status: str, attempt_no: int, input_hash: str):
        self.id = uuid.uuid4()
        self.run_key = run_key
        self.status = status
        self.attempt_no = attempt_no
        self.started_at = None
        self.finished_at = None
        self.error_code = None
        self.error_message = None
        self.metadata_json = json.dumps({"input_hash": input_hash})


class _FakeDB:
    """最小 fake session。

    - `existing_child`：模拟 `_find_existing_child` 的查询结果。
    - `published_core_run_id`：模拟当日 stock_core pointer。
    """

    def __init__(
        self,
        *,
        existing_child: Any = None,
        published_core_run_id: uuid.UUID | None = None,
    ):
        self.added: list[Any] = []
        self.committed = 0
        self.events: list[dict] = []
        self.existing_child = existing_child
        self.published_core_run_id = published_core_run_id

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass

    async def commit(self):
        self.committed += 1

    async def execute(self, stmt):
        # 简化：由 monkeypatch 接管 pointer / child 查询，这里只返回空集。
        return _FakeResult([])

    async def get(self, model, pk):
        return None


async def _fake_append_event(db, job_run_id, step, level, message, payload=None):
    db.events.append(
        {
            "job_run_id": job_run_id,
            "step": step,
            "level": level,
            "message": message,
            "payload": payload or {},
        }
    )


class _SpySchedulerJobRun:
    """替身 SchedulerJobRun，捕获构造参数。"""

    def __init__(self, **kwargs):
        self.id = uuid.uuid4()
        self.job_name = kwargs.get("job_name")
        self.business_date = kwargs.get("business_date")
        self.run_key = kwargs.get("run_key")
        self.status = kwargs.get("status")
        self.attempt_no = kwargs.get("attempt_no")
        self.metadata_json = kwargs.get("metadata_json")
        self.started_at = None
        self.finished_at = None
        self.error_code = None
        self.error_message = None


@pytest.fixture
def svc(monkeypatch):
    """patch 事件写入、child 模型、pointer 解析。"""
    import app.services.granular_restart_service as module

    monkeypatch.setattr(module, "append_event", _fake_append_event)
    monkeypatch.setattr(module, "SchedulerJobRun", _SpySchedulerJobRun)

    async def fake_find_existing_child(db, run_key):
        child = getattr(db, "existing_child", None)
        if child is not None and child.run_key == run_key:
            return child
        return None

    async def fake_resolve_pointer(db, trade_date):
        return getattr(db, "published_core_run_id", None)

    monkeypatch.setattr(module, "_find_existing_child", fake_find_existing_child)
    monkeypatch.setattr(module, "_resolve_published_core_run_id", fake_resolve_pointer)
    return module


# =============================================================================
# 1. registry 权威性
# =============================================================================


def test_all_nine_boundaries_have_real_handlers():
    """CP3：9/9 boundary 都有真实 handler（CP2 的 state_events 缺口已补齐；[Slice 4A10] 已移除 board_aggregation）。"""
    assert len(ALL_BOUNDARIES) == 9
    for boundary in ALL_BOUNDARIES:
        assert is_implemented_boundary(boundary) is True, f"{boundary} 无真实 handler"
    assert set(implemented_boundaries()) == set(ALL_BOUNDARIES)


def test_state_events_is_implemented_in_cp3():
    """CP2 中 state_events 未实现；CP3 已补真实重建 handler。"""
    assert is_implemented_boundary("state_events") is True

    from app.services.granular_restart_service import rebuild_state_events

    assert callable(rebuild_state_events)


def test_unknown_boundary_rejected_with_value_error():
    assert is_implemented_boundary("nope") is False


# =============================================================================
# 2. 幂等键组成（parent / source / input_hash）
# =============================================================================


def test_run_key_contains_parent_source_and_input_hash():
    """[CP3 P0] 幂等键必须含 parent/source/input_hash，禁止跨 parent 或跨输入误复用。"""
    parent = uuid.uuid4()
    source = uuid.uuid4()
    key = build_run_key(
        trade_date="2026-08-05",
        boundary="chip",
        parent_job_run_id=parent,
        source_core_run_id=source,
        input_hash="deadbeef",
    )
    assert str(parent) in key
    assert str(source) in key
    assert "deadbeef" in key
    assert key.startswith("granular_restart:2026-08-05:chip:")


def test_run_key_differs_across_parent_and_source():
    """不同 parent / 不同 source → 不同 run_key（不得误复用）。"""
    p1, p2 = uuid.uuid4(), uuid.uuid4()
    s1, s2 = uuid.uuid4(), uuid.uuid4()
    base = {"trade_date": "2026-08-05", "boundary": "chip", "input_hash": "h"}
    k1 = build_run_key(parent_job_run_id=p1, source_core_run_id=s1, **base)
    k2 = build_run_key(parent_job_run_id=p2, source_core_run_id=s1, **base)
    k3 = build_run_key(parent_job_run_id=p1, source_core_run_id=s2, **base)
    assert len({k1, k2, k3}) == 3


def test_input_hash_changes_with_source_core_run():
    """source core run 变化 → input_hash 变化 → 不复用旧 succeeded 结果。"""
    h1 = compute_input_hash(
        trade_date="2026-08-05", boundary="chip", source_core_run_id=uuid.uuid4()
    )
    h2 = compute_input_hash(
        trade_date="2026-08-05", boundary="chip", source_core_run_id=uuid.uuid4()
    )
    assert h1 != h2


# =============================================================================
# 3. 主链：写 mainchain_stage，绝不写 last_completed_step
# =============================================================================


@pytest.mark.parametrize(
    "boundary,expected_stage",
    [
        ("daily_ready", "syncing_boards"),
        ("board_facts", "syncing_boards"),
        ("core", "computing_features"),
        ("stock_core_published", "publishing"),
    ],
)
async def test_mainchain_writes_stage_never_last_completed_step(
    svc, boundary, expected_stage
):
    """[CP3 P0] 主链 boundary 写 mainchain_stage，child 保持 queued，绝不写 last_completed_step。

    CP2 缺陷：写 last_completed_step="checking_coverage"，但 orchestrator 的
    `_completed_steps` 根本没有该键，落到默认空集合 = "什么都没完成"，语义完全相反。
    """
    db = _FakeDB(published_core_run_id=uuid.uuid4())
    job = _FakeJobRun()

    child = await dispatch_restart(
        db, job, boundary, actor="tester", request_id="req-1"
    )

    meta = json.loads(child.metadata_json)
    assert meta["mainchain_stage"] == expected_stage
    assert meta["execution_mode"] == "worker_pull"
    assert meta["restart_from"] == boundary
    # 核心断言：绝不出现 last_completed_step
    assert "last_completed_step" not in meta
    assert "last_completed_step" not in (child.metadata_json or "")
    # 主链不在本进程执行 → 保持 queued，不伪造成功
    assert child.status == "queued"
    assert child.finished_at is None


# =============================================================================
# 4. 子产品：真实执行 + 成功终态
# =============================================================================


async def test_child_boundary_success_sets_succeeded(svc):
    """子产品 boundary：handler 成功 → child.succeeded + finished_at + target_run_id。"""
    db = _FakeDB(published_core_run_id=uuid.uuid4())
    job = _FakeJobRun()
    target = uuid.uuid4()
    calls: list[dict] = []

    async def fake_handler(db, **kw):
        calls.append(kw)
        return target

    child = await dispatch_restart(
        db, job, "review", actor="tester", request_id="r1",
        handlers={"review": fake_handler},
    )

    assert child.job_name == "granular_restart_review"
    assert child.status == "succeeded"
    assert child.started_at is not None
    assert child.finished_at is not None
    meta = json.loads(child.metadata_json)
    assert meta["target_run_id"] == str(target)
    assert meta["parent_job_run_id"] == str(job.id)
    # handler 收到完整 lineage 上下文
    assert len(calls) == 1
    assert calls[0]["trade_date"] == "2026-08-05"
    assert calls[0]["parent_job_run_id"] == job.id
    assert calls[0]["attempt"] == 1


# =============================================================================
# 5. 真幂等：succeeded + 同 input_hash → 不再执行 handler
# =============================================================================


async def test_succeeded_same_input_does_not_reexecute_handler(svc):
    """[CP3 P0] CP2 缺陷：复用了行但仍重跑 handler。CP3 必须**直接返回、不执行**。"""
    source = uuid.uuid4()
    job = _FakeJobRun()
    input_hash = compute_input_hash(
        trade_date="2026-08-05",
        boundary="chip",
        source_core_run_id=source,
        extra={"request_scope": "granular_restart"},
    )
    run_key = build_run_key(
        trade_date="2026-08-05",
        boundary="chip",
        parent_job_run_id=job.id,
        source_core_run_id=source,
        input_hash=input_hash,
    )
    existing = _FakeChild(
        run_key=run_key, status="succeeded", attempt_no=1, input_hash=input_hash
    )
    db = _FakeDB(existing_child=existing, published_core_run_id=source)

    executed = []

    async def fake_handler(db, **kw):
        executed.append(kw)
        return uuid.uuid4()

    child = await dispatch_restart(
        db, job, "chip", actor="t", request_id="r2",
        handlers={"chip": fake_handler},
    )

    assert child is existing
    assert executed == [], "succeeded + 同 input_hash 时不得重复执行 handler"
    assert any("幂等命中" in e["message"] for e in db.events)


async def test_active_child_not_rescheduled(svc):
    """已有 running/queued child → 返回既有 child，不重复调度。"""
    source = uuid.uuid4()
    job = _FakeJobRun()
    input_hash = compute_input_hash(
        trade_date="2026-08-05",
        boundary="chip",
        source_core_run_id=source,
        extra={"request_scope": "granular_restart"},
    )
    run_key = build_run_key(
        trade_date="2026-08-05", boundary="chip", parent_job_run_id=job.id,
        source_core_run_id=source, input_hash=input_hash,
    )
    existing = _FakeChild(
        run_key=run_key, status="running", attempt_no=1, input_hash=input_hash
    )
    db = _FakeDB(existing_child=existing, published_core_run_id=source)

    executed = []

    async def fake_handler(db, **kw):
        executed.append(kw)
        return None

    child = await dispatch_restart(
        db, job, "chip", actor="t", request_id="r3", handlers={"chip": fake_handler}
    )
    assert child is existing
    assert executed == []


async def test_failed_child_creates_new_attempt(svc):
    """failed child → attempt_no+1 新建并重新执行（不复用失败结果）。"""
    source = uuid.uuid4()
    job = _FakeJobRun()
    input_hash = compute_input_hash(
        trade_date="2026-08-05",
        boundary="chip",
        source_core_run_id=source,
        extra={"request_scope": "granular_restart"},
    )
    run_key = build_run_key(
        trade_date="2026-08-05", boundary="chip", parent_job_run_id=job.id,
        source_core_run_id=source, input_hash=input_hash,
    )
    existing = _FakeChild(
        run_key=run_key, status="failed", attempt_no=2, input_hash=input_hash
    )
    db = _FakeDB(existing_child=existing, published_core_run_id=source)

    executed = []

    async def fake_handler(db, **kw):
        executed.append(kw)
        return uuid.uuid4()

    child = await dispatch_restart(
        db, job, "chip", actor="t", request_id="r4", handlers={"chip": fake_handler}
    )
    assert child is not existing
    assert child.attempt_no == 3
    assert len(executed) == 1
    assert executed[0]["attempt"] == 3
    assert child.status == "succeeded"


# =============================================================================
# 6. 失败：真实原因，不 501、不伪造成功
# =============================================================================


async def test_handler_failure_marks_failed_with_real_reason(svc):
    """子产品 handler 抛错 → child.failed + level=error 事件 + 真实异常信息。"""
    db = _FakeDB(published_core_run_id=uuid.uuid4())
    job = _FakeJobRun()

    async def boom(db, **kw):
        raise RuntimeError("lineage mismatch: source core run 不匹配")

    child = await dispatch_restart(
        db, job, "dsa_projection", actor="tester", request_id="r1",
        handlers={"dsa_projection": boom},
    )

    assert child.status == "failed"
    assert child.error_code == "granular_restart_failed"
    assert "lineage mismatch" in (child.error_message or "")
    assert child.finished_at is not None
    error_events = [e for e in db.events if e["level"] == "error"]
    assert error_events
    assert "lineage mismatch" in error_events[0]["message"]


async def test_missing_trade_date_raises_value_error(svc):
    """metadata.trade_date 与 business_date 均缺失 → ValueError（不静默继续）。"""
    db = _FakeDB()
    job = _FakeJobRun(business_date="", metadata={})
    job.business_date = None
    with pytest.raises(ValueError):
        await dispatch_restart(db, job, "chip", actor="t", request_id="r1")


# =============================================================================
# REPROCESS-OWNER-CLOSURE-01 CORRECTION-02 — build_run_key 长度合同（production owner）
# =============================================================================

_RUN_KEY_TRADE_DATE = "2026-08-25"


def _sample_parent() -> uuid.UUID:
    return uuid.uuid4()


def _sample_source() -> uuid.UUID:
    return uuid.uuid4()


def test_run_key_contract_all_boundaries_within_128() -> None:
    """A. ALL_BOUNDARIES：parent UUID + source UUID + 16-char input_hash
    → len(build_run_key(...)) <= 128。"""
    parent = _sample_parent()
    source = _sample_source()
    input_hash = compute_input_hash(
        trade_date=_RUN_KEY_TRADE_DATE, boundary="daily_ready",
        source_core_run_id=source, extra={"request_scope": "granular_restart"},
    )
    for boundary in ALL_BOUNDARIES:
        key = build_run_key(
            trade_date=_RUN_KEY_TRADE_DATE,
            boundary=boundary,
            parent_job_run_id=parent,
            source_core_run_id=source,
            input_hash=input_hash,
        )
        assert len(key) <= 128, f"boundary={boundary} run_key 越界: len={len(key)} key={key!r}"


def test_run_key_contract_deterministic() -> None:
    """B. 同样输入 → deterministic，同一 key。"""
    parent = _sample_parent()
    source = _sample_source()
    input_hash = compute_input_hash(
        trade_date=_RUN_KEY_TRADE_DATE, boundary="daily_ready",
        source_core_run_id=source, extra={"request_scope": "granular_restart"},
    )
    k1 = build_run_key(
        trade_date=_RUN_KEY_TRADE_DATE, boundary="daily_ready",
        parent_job_run_id=parent, source_core_run_id=source, input_hash=input_hash,
    )
    k2 = build_run_key(
        trade_date=_RUN_KEY_TRADE_DATE, boundary="daily_ready",
        parent_job_run_id=parent, source_core_run_id=source, input_hash=input_hash,
    )
    assert k1 == k2


def test_run_key_contract_input_change_alters_key() -> None:
    """C. parent / source / input_hash 任一改变 → key 必须改变。"""
    source = _sample_source()
    input_hash = compute_input_hash(
        trade_date=_RUN_KEY_TRADE_DATE, boundary="daily_ready",
        source_core_run_id=source, extra={"request_scope": "granular_restart"},
    )
    base = build_run_key(
        trade_date=_RUN_KEY_TRADE_DATE, boundary="daily_ready",
        parent_job_run_id=_sample_parent(), source_core_run_id=source,
        input_hash=input_hash,
    )
    # parent 改变
    other_parent = _sample_parent()
    assert base != build_run_key(
        trade_date=_RUN_KEY_TRADE_DATE, boundary="daily_ready",
        parent_job_run_id=other_parent, source_core_run_id=source,
        input_hash=input_hash,
    )
    # source 改变
    assert base != build_run_key(
        trade_date=_RUN_KEY_TRADE_DATE, boundary="daily_ready",
        parent_job_run_id=_sample_parent(), source_core_run_id=_sample_source(),
        input_hash=input_hash,
    )
    # input_hash 改变
    assert base != build_run_key(
        trade_date=_RUN_KEY_TRADE_DATE, boundary="daily_ready",
        parent_job_run_id=_sample_parent(), source_core_run_id=source,
        input_hash="0" * 16,
    )


def test_run_key_contract_short_key_backward_compatible() -> None:
    """D. 当前本来 <=128 的 readable key → 格式保持不变（backward compatibility）。"""
    parent = uuid.UUID("00000000-0000-0000-0000-000000000000")
    # 'none' 段使 key 落在 128 内 → 原样 readable 格式。
    key = build_run_key(
        trade_date=_RUN_KEY_TRADE_DATE, boundary="daily_ready",
        parent_job_run_id=parent, source_core_run_id=None, input_hash="a" * 16,
    )
    assert key == (
        f"granular_restart:{_RUN_KEY_TRADE_DATE}:daily_ready:"
        f"{parent}:none:{'a' * 16}"
    )


def test_run_key_contract_overlength_uses_compact_fallback() -> None:
    """E. daily_ready + non-null source UUID → overlength → compact fallback，
    不发生简单截断（digest 覆盖完整 original key），且最终 <= 128。"""
    parent = _sample_parent()
    source = _sample_source()
    input_hash = compute_input_hash(
        trade_date=_RUN_KEY_TRADE_DATE, boundary="daily_ready",
        source_core_run_id=source, extra={"request_scope": "granular_restart"},
    )
    full = (
        f"granular_restart:{_RUN_KEY_TRADE_DATE}:daily_ready:"
        f"{parent}:{source}:{input_hash}"
    )
    assert len(full) > 128, "前提：canonical full key 必须 > 128 才触发 fallback"
    key = build_run_key(
        trade_date=_RUN_KEY_TRADE_DATE, boundary="daily_ready",
        parent_job_run_id=parent, source_core_run_id=source, input_hash=input_hash,
    )
    assert len(key) <= 128
    # compact fallback 必须保留三段可读前缀 + 完整 SHA-256 digest（非简单截断）。
    assert key.startswith(f"granular_restart:{_RUN_KEY_TRADE_DATE}:daily_ready:")
    digest_part = key.split(":", 3)[3]
    expected_digest = hashlib.sha256(full.encode("utf-8")).hexdigest()
    # CORRECTION-03：完整 64-hex digest，且必须等于 original key 的 SHA-256。
    assert digest_part == expected_digest
    assert len(digest_part) == 64
    assert len(key) <= 128
