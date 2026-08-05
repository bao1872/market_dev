"""Granular restart service 单测（PURE_UNIT_TEST）。

[Corrective Pass 2] 验证：真实 handler registry 为唯一权威（state_events 诚实未实现）；
子产品 child 成功后置 succeeded+finished_at；幂等复用 run_key；失败记错误事件不伪造成功。
"""

import json
import uuid

import pytest

from app.services.granular_restart_service import (
    ALL_BOUNDARIES,
    is_implemented_boundary,
    dispatch_restart,
)


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar_one_or_none(self):
        return self._rows[0][0] if self._rows else None


class _FakeJobRun:
    def __init__(self, job_name="after_close_orchestrator", business_date="2026-08-05", metadata=None):
        self.id = uuid.uuid4()
        self.job_name = job_name
        self.business_date = business_date
        self.status = "succeeded"
        self.error_code = None
        self.error_message = None
        self.started_at = None
        self.finished_at = None
        self.metadata_json = json.dumps(metadata or {"trade_date": business_date})


class _FakeDB:
    def __init__(self):
        self.added = []
        self.committed = 0
        self.events = []
        self._children = []

    async def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass

    async def commit(self):
        self.committed += 1

    async def execute(self, stmt):
        return _FakeResult([])

    async def get(self, model, pk):
        return None


async def _fake_append_event(db, job_run_id, step, level, message, payload=None):
    db.events.append({"job_run_id": job_run_id, "step": step, "level": level, "message": message})


@pytest.fixture
def patch_event(monkeypatch):
    import app.services.granular_restart_service as svc

    monkeypatch.setattr(svc, "append_event", _fake_append_event)


def test_implemented_registry_not_just_enum():
    """[Corrective Pass 2] is_implemented_boundary 以真实 handler 为权威，state_events 诚实未实现。"""
    for b in ("daily_ready", "board_facts", "core", "stock_core_published",
              "dsa_projection", "chip", "auction", "board_aggregation", "review"):
        assert is_implemented_boundary(b) is True
    # state_events 无真实领域级 handler → 明确未实现
    assert is_implemented_boundary("state_events") is False


def test_unknown_boundary_rejected():
    import asyncio

    db = _FakeDB()
    job = _FakeJobRun()
    with pytest.raises(ValueError):
        asyncio.get_event_loop().run_until_complete(
            dispatch_restart(db, job, "nope", actor="tester", request_id="r1")
        )


async def test_state_events_not_implemented_raises(patch_event):
    """state_events 无真实 handler：dispatch_restart 抛 NotImplementedError，不伪造成功。"""
    import asyncio

    db = _FakeDB()
    job = _FakeJobRun()
    with pytest.raises(NotImplementedError):
        await dispatch_restart(db, job, "state_events", actor="tester", request_id="r1")


async def test_child_boundary_success_sets_succeeded(patch_event):
    """子产品 boundary：创建 child，publisher 成功后 child.status=succeeded 且 finished_at 有值。"""
    db = _FakeDB()
    job = _FakeJobRun()
    called = {}

    async def fake_publisher(db, *, trade_date, source_run_id, actor):
        called["trade_date"] = trade_date
        return source_run_id or uuid.uuid4()

    child = await dispatch_restart(
        db, job, "review", actor="tester", request_id="r1",
        publishers={"review": fake_publisher},
    )
    assert child.job_name == "granular_restart_review"
    assert child.status == "succeeded"
    assert child.finished_at is not None
    assert child.started_at is not None
    md = json.loads(child.metadata_json)
    assert md["parent_job_run_id"] == str(job.id)
    assert called.get("trade_date") == "2026-08-05"
    assert any(e["step"] == "manual_restart" for e in db.events)


async def test_child_boundary_idempotent_reuse(patch_event):
    """重复调用同 boundary：复用已有 child（run_key），不新建重复任务。"""
    db = _FakeDB()
    job = _FakeJobRun()

    async def pub(db, *, trade_date, source_run_id, actor):
        return uuid.uuid4()

    c1 = await dispatch_restart(db, job, "chip", actor="t", request_id="r1", publishers={"chip": pub})
    # 模拟已存在 child 被复用：第二次调用应使用同 run_key
    # 由于 fake db 不持久化，这里验证 run_key 幂等键生成规则一致
    c2 = await dispatch_restart(db, job, "chip", actor="t", request_id="r2", publishers={"chip": pub})
    assert c1.run_key == c2.run_key == f"granular_restart:2026-08-05:chip"


async def test_child_boundary_publisher_failure_not_501(patch_event):
    """子产品 publisher 抛错：child 标记 failed + 错误事件，不返回 501、不伪造成功。"""
    db = _FakeDB()
    job = _FakeJobRun()

    async def boom(db, *, trade_date, source_run_id, actor):
        raise RuntimeError("lineage mismatch: source core run 不匹配")

    child = await dispatch_restart(
        db, job, "dsa_projection", actor="tester", request_id="r1",
        publishers={"dsa_projection": boom},
    )
    assert child.status == "failed"
    assert child.error_code == "granular_restart_publish_failed"
    assert "lineage mismatch" in (child.error_message or "")
    assert child.finished_at is not None
    assert any(e["level"] == "error" for e in db.events)


async def test_mainchain_boundary_sets_resume_step(patch_event, monkeypatch):
    """主链 boundary：设置正确的 last_completed_step 续跑。"""
    import app.api.admin_after_close as _aao_module

    captured = {}

    async def fake_update(db, job_run, status, message, extra=None, payload=None):
        captured["extra"] = extra

    monkeypatch.setattr(_aao_module, "_update_orchestrator_status", fake_update)
    db = _FakeDB()
    job = _FakeJobRun()

    result = await dispatch_restart(db, job, "core", actor="tester", request_id="r1")
    assert result is job
    assert captured["extra"]["last_completed_step"] == "checking_coverage"
    assert captured["extra"]["restart_from"] == "core"
