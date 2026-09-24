"""[BOARD-LOCAL-OWNERSHIP-01] 管理员板块同步状态读模型 + 端点合同（任务 §18 G）。

覆盖：
- counts / last_success_at 从 DB 派生；无有效 board → available=False, last_success_at=None
- recent_attempt 可选；Redis 缺失/异常仍返回 DB 状态
- 旧的 last_success_at **不**导致 available=False（无 age/SLA 语义）
- 最近一次尝试失败但库中仍有有效数据 → available 仍为 True
- 端点 admin-only；**无**任何服务端「立即同步」POST 端点
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.api import admin_board_sync as api_mod
from app.services import board_sync_status_service as svc

_COUNTS = {
    "board_count": 767,
    "membership_count": 191750,
    "industry_count": 257,
    "concept_count": 510,
    "stock_count": 5400,
}


class _FakeDB:
    """最小 session 替身：只提供读模型使用的 scalar()。"""

    def __init__(self, max_updated_at) -> None:
        self._max = max_updated_at
        self.scalar_calls = 0

    async def scalar(self, *args, **kwargs):
        self.scalar_calls += 1
        return self._max


def _patch(monkeypatch, *, counts, redis_result=None, redis_exc=None):
    async def _counts(db):
        return dict(counts)

    async def _status():
        if redis_exc is not None:
            raise redis_exc
        return redis_result

    monkeypatch.setattr(svc, "get_current_detailed_counts", _counts)
    monkeypatch.setattr(svc, "get_sync_status", _status)


@pytest.mark.asyncio
async def test_status_derives_counts_and_last_success(monkeypatch) -> None:
    ts = datetime(2026, 9, 20, 7, 30, tzinfo=UTC)
    _patch(
        monkeypatch,
        counts=_COUNTS,
        redis_result={"status": "succeeded", "source": "wencai", "mode": "local_manual"},
    )
    db = _FakeDB(ts)

    status = await svc.get_board_sync_status(db)

    assert status["mode"] == "local_manual"
    assert status["source"] == "wencai"
    assert status["available"] is True
    assert status["last_success_at"] == ts.isoformat()
    assert status["board_count"] == 767
    assert status["industry_count"] == 257
    assert status["concept_count"] == 510
    assert status["membership_count"] == 191750
    assert status["stock_count"] == 5400
    assert status["recent_attempt"]["status"] == "succeeded"


@pytest.mark.asyncio
async def test_no_active_boards_unavailable(monkeypatch) -> None:
    _patch(monkeypatch, counts={**_COUNTS, "board_count": 0}, redis_result=None)
    db = _FakeDB(datetime(2026, 9, 20, tzinfo=UTC))

    status = await svc.get_board_sync_status(db)

    assert status["available"] is False
    assert status["last_success_at"] is None


@pytest.mark.asyncio
async def test_redis_absent_still_returns_db_status(monkeypatch) -> None:
    """Redis 不可用不得使 DB 状态不可用。"""
    ts = datetime(2026, 9, 20, tzinfo=UTC)
    _patch(monkeypatch, counts=_COUNTS, redis_exc=RuntimeError("redis down"))
    db = _FakeDB(ts)

    status = await svc.get_board_sync_status(db)

    assert status["available"] is True
    assert status["last_success_at"] == ts.isoformat()
    assert status["recent_attempt"] is None


@pytest.mark.asyncio
async def test_old_last_success_does_not_make_unavailable(monkeypatch) -> None:
    """年龄仅信息展示：旧的最后成功时间**不**导致 available=False（无 stale 语义）。"""
    old = datetime(2025, 1, 1, tzinfo=UTC)
    _patch(monkeypatch, counts=_COUNTS, redis_result=None)
    db = _FakeDB(old)

    status = await svc.get_board_sync_status(db)

    assert status["available"] is True
    assert status["last_success_at"] == old.isoformat()
    # 无任何频率/SLA 字段
    for forbidden in ("is_stale", "stale_after_days", "next_due_at", "overdue", "required_frequency"):
        assert forbidden not in status


@pytest.mark.asyncio
async def test_failed_recent_attempt_keeps_available(monkeypatch) -> None:
    """最近一次尝试失败但库中仍有有效数据 → available 仍为 True。"""
    ts = datetime(2026, 9, 20, tzinfo=UTC)
    _patch(
        monkeypatch,
        counts=_COUNTS,
        redis_result={
            "status": "failed",
            "source": "wencai",
            "mode": "local_manual",
            "error_code": "StagingValidationError",
            "reused_previous_snapshot": True,
        },
    )
    db = _FakeDB(ts)

    status = await svc.get_board_sync_status(db)

    assert status["available"] is True
    assert status["recent_attempt"]["status"] == "failed"


def test_endpoint_is_get_admin_only() -> None:
    """端点存在、方法为 GET，且依赖 admin 角色（RBAC）。"""
    paths = {r.path: r for r in api_mod.router.routes}
    route = paths["/v1/admin/board-sync/status"]
    assert "GET" in route.methods
    # 依赖中包含 require_roles("admin")
    dep_src = str(getattr(route, "dependant", None))
    assert "admin" in dep_src, "端点必须为 admin-only"


def test_no_server_side_sync_post_endpoint() -> None:
    """不得存在任何服务端触发问财抓取/同步的 POST 端点。"""
    for route in api_mod.router.routes:
        methods = getattr(route, "methods", set())
        assert "POST" not in methods, (
            f"admin board-sync 不得暴露服务端同步 POST 端点: {route.path}"
        )
