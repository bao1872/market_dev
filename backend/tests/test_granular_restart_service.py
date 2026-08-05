"""Granular restart service 单测（PURE_UNIT_TEST）。

验证 [PRD 31 §6] 门禁：所有 10 个 boundary 均已实现，不再返回 not_implemented/501；
主链 boundary 设置正确的续跑起点；子产品 boundary 创建 child SchedulerJobRun 并真实调用 publisher。
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
        self.metadata_json = json.dumps(metadata or {"trade_date": business_date})

    def __getitem__(self, key):
        return self.__dict__[key]


class _FakeDB:
    """最小 fake AsyncSession：记录 add/commit，execute 默认空。"""

    def __init__(self):
        self.added = []
        self.committed = 0
        self.events = []

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


def test_all_ten_boundaries_implemented():
    """门禁：10 个正式 boundary 全部实现，无 not_implemented。"""
    assert len(ALL_BOUNDARIES) == 10
    for b in ALL_BOUNDARIES:
        assert is_implemented_boundary(b) is True


def test_unknown_boundary_rejected():
    import pytest

    db = _FakeDB()
    job = _FakeJobRun()
    with pytest.raises(ValueError):
        import asyncio

        asyncio.get_event_loop().run_until_complete(
            dispatch_restart(db, job, "nope", actor="tester", request_id="r1")
        )


async def test_child_boundary_dispatches_publisher(patch_event):
    """子产品 boundary：创建 child SchedulerJobRun 并调用注入 publisher。"""
    db = _FakeDB()
    job = _FakeJobRun()
    called = {}

    async def fake_publisher(db, *, trade_date, source_run_id, actor):
        called["trade_date"] = trade_date
        called["source_run_id"] = source_run_id
        called["actor"] = actor
        return source_run_id or uuid.uuid4()

    child = await dispatch_restart(
        db,
        job,
        "review",
        actor="tester",
        request_id="r1",
        publishers={"review": fake_publisher},
    )
    assert child.job_name == "granular_restart_review"
    assert child.metadata_json
    md = json.loads(child.metadata_json)
    assert md["parent_job_run_id"] == str(job.id)
    assert md["operation"] == "review"
    assert called.get("trade_date") == "2026-08-05"
    assert child.status == "queued"
    # 写入了 manual_restart 事件
    assert any(e["step"] == "manual_restart" for e in db.events)


async def test_child_boundary_publisher_failure_not_501(patch_event):
    """子产品 publisher 抛错：child 标记 failed + 错误事件，不返回 501、不伪造成功。"""
    db = _FakeDB()
    job = _FakeJobRun()

    async def boom(db, *, trade_date, source_run_id, actor):
        raise RuntimeError("lineage mismatch: source core run 不匹配")

    child = await dispatch_restart(
        db,
        job,
        "dsa_projection",
        actor="tester",
        request_id="r1",
        publishers={"dsa_projection": boom},
    )
    assert child.status == "failed"
    assert child.error_code == "granular_restart_publish_failed"
    assert "lineage mismatch" in (child.error_message or "")
    assert any(e["level"] == "error" for e in db.events)


async def test_mainchain_boundary_sets_resume_step(patch_event, monkeypatch):
    """主链 boundary：设置正确的 last_completed_step 续跑（不返回 501）。"""
    import app.api.admin_after_close as _aao_module
    import app.services.granular_restart_service as svc

    captured = {}

    async def fake_update(db, job_run, status, message, extra=None, payload=None):
        captured["extra"] = extra
        captured["status"] = status

    monkeypatch.setattr(_aao_module, "_update_orchestrator_status", fake_update)
    # 确保 after_close_orchestrator 的 AfterCloseRunStatus 可被 import（避免断点恢复依赖）
    monkeypatch.setattr(svc, "_update_orchestrator_status", fake_update)  # 兼容直接引用
    db = _FakeDB()
    job = _FakeJobRun()

    result = await dispatch_restart(db, job, "core", actor="tester", request_id="r1")
    assert result is job
    assert captured["extra"]["last_completed_step"] == "checking_coverage"
    assert captured["extra"]["restart_from"] == "core"
