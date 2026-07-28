"""[Gate4] Worker 心跳 stopped_at 字段与状态判定测试。

覆盖：
1. WorkerHeartbeat ORM 模型包含 stopped_at 字段
2. WorkerHeartbeatItem schema 包含 stopped_at 字段
3. classify_health_state 状态判定逻辑（fresh/stale/stopped 边界）
4. _heartbeat_loop 退出时设置 stopped_at（不覆盖 heartbeat_at）—— 源码级验证
5. mark_stale_worker_heartbeats 同步写入 stopped_at —— 源码级验证
6. admin API 返回 stopped_at 字段 —— 源码级验证
7. 历史重复实例折叠：每个 worker_name 默认只显示最新实例（前端逻辑由 TSC 验证类型一致）

测试环境：纯单元测试 + 源码级验证（不依赖 DB；DB 集成已在 test_worker_heartbeat_stale_cleanup.py 覆盖）
设计要点：
- 不修改生产代码，仅验证字段与逻辑
- 使用 inspect.getsource 做源码级验证（避免 mock 自证）
- 阈值常量显式定义在 schemas/worker_heartbeat.py，不重复
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime

import pytest

from app.models.worker_heartbeat import WorkerHeartbeat
from app.schemas.worker_heartbeat import (
    WORKER_FRESH_WINDOW_SECONDS,
    WORKER_STALE_THRESHOLD_SECONDS,
    WorkerHeartbeatItem,
    classify_health_state,
)

# =============================================================================
# 1. ORM 模型字段验证
# =============================================================================


class TestWorkerHeartbeatModelStoppedAt:
    """[Gate4] WorkerHeartbeat ORM 模型包含 stopped_at 字段。"""

    def test_model_has_stopped_at_field(self) -> None:
        """WorkerHeartbeat 必须包含 stopped_at 列。"""
        cols = [c.name for c in WorkerHeartbeat.__table__.columns]
        assert "stopped_at" in cols, (
            "WorkerHeartbeat 模型缺少 stopped_at 列（Gate4 要求）"
        )

    def test_stopped_at_is_nullable(self) -> None:
        """stopped_at 必须是 nullable（运行中或历史记录无此字段时为 NULL）。"""
        col = WorkerHeartbeat.__table__.columns.get("stopped_at")
        assert col is not None, "stopped_at 列不存在"
        assert col.nullable is True, (
            "stopped_at 必须是 nullable；运行中 Worker 此字段为 NULL"
        )

    def test_stopped_at_is_timezone_aware(self) -> None:
        """stopped_at 必须是 timezone-aware（与 started_at/heartbeat_at 一致）。"""
        from sqlalchemy import DateTime

        col = WorkerHeartbeat.__table__.columns.get("stopped_at")
        assert col is not None
        assert isinstance(col.type, DateTime), "stopped_at 必须是 DateTime 类型"
        assert col.type.timezone is True, "stopped_at 必须是 timezone=True"


# =============================================================================
# 2. Schema 字段验证
# =============================================================================


class TestWorkerHeartbeatItemSchemaStoppedAt:
    """[Gate4] WorkerHeartbeatItem schema 包含 stopped_at 字段。"""

    def test_schema_has_stopped_at_field(self) -> None:
        """WorkerHeartbeatItem schema 必须包含 stopped_at 字段。"""
        fields = list(WorkerHeartbeatItem.model_fields.keys())
        assert "stopped_at" in fields, (
            "WorkerHeartbeatItem schema 缺少 stopped_at 字段（Gate4 要求）"
        )

    def test_stopped_at_field_is_optional(self) -> None:
        """stopped_at 必须是 Optional（None=运行中或历史记录无此字段）。"""
        field_info = WorkerHeartbeatItem.model_fields.get("stopped_at")
        assert field_info is not None
        # Pydantic 中 Optional 字段默认值应为 None 或允许 None
        # 检查字段是否允许 None（union 类型包含 NoneType）
        assert field_info.is_required() is False, (
            "stopped_at 必须是可选字段（默认 None）"
        )

    def test_schema_serialization_includes_stopped_at(self) -> None:
        """[Gate4] 序列化时 stopped_at 字段必须出现在输出中。"""
        now = datetime.now(UTC)
        item = WorkerHeartbeatItem(
            worker_name="test_worker",
            instance_id="host:12345",
            started_at=now,
            heartbeat_at=now,
            status="stopped",
            stopped_at=now,
            current_job_id=None,
            build_sha="abc123",
            metadata_json=None,
            updated_at=now,
            heartbeat_age_seconds=0,
            health_state="stopped",
        )
        dumped = item.model_dump()
        assert "stopped_at" in dumped, "序列化输出缺少 stopped_at"
        assert dumped["stopped_at"] == now

    def test_schema_stopped_at_default_none(self) -> None:
        """[Gate4] 不传 stopped_at 时默认为 None（运行中 Worker）。"""
        now = datetime.now(UTC)
        item = WorkerHeartbeatItem(
            worker_name="test_worker",
            instance_id="host:12345",
            started_at=now,
            heartbeat_at=now,
            status="running",
            current_job_id=None,
            build_sha="abc123",
            metadata_json=None,
            updated_at=now,
            heartbeat_age_seconds=10,
            health_state="fresh",
        )
        assert item.stopped_at is None


# =============================================================================
# 3. classify_health_state 状态判定逻辑
# =============================================================================


class TestClassifyHealthState:
    """[Gate4] classify_health_state 状态判定逻辑测试。"""

    def test_fresh_running_under_threshold(self) -> None:
        """running + age < 120s → fresh。"""
        assert classify_health_state("running", 0) == "fresh"
        assert classify_health_state("running", 60) == "fresh"
        assert classify_health_state("running", 119) == "fresh"

    def test_stale_running_between_thresholds(self) -> None:
        """running + 120 ≤ age < 600 → stale。"""
        assert classify_health_state("running", 120) == "stale"
        assert classify_health_state("running", 300) == "stale"
        assert classify_health_state("running", 599) == "stale"

    def test_stopped_running_over_threshold(self) -> None:
        """running + age ≥ 600 → stopped（僵尸心跳）。"""
        assert classify_health_state("running", 600) == "stopped"
        assert classify_health_state("running", 3600) == "stopped"

    def test_stopped_status_any_age(self) -> None:
        """status=stopped 不论 age 都返回 stopped。"""
        assert classify_health_state("stopped", 0) == "stopped"
        assert classify_health_state("stopped", 600) == "stopped"
        assert classify_health_state("stopped", 99999) == "stopped"

    def test_idle_status_treated_as_stopped(self) -> None:
        """status=idle（不在 fresh 判定范围）应返回 stopped。

        设计说明：idle 状态在 classify_health_state 中不在 running 分支，
        因此落入默认 return 'stopped' 分支。这是预期行为。
        """
        assert classify_health_state("idle", 10) == "stopped"

    def test_threshold_constants(self) -> None:
        """[Gate4] 阈值常量符合预期。"""
        assert WORKER_FRESH_WINDOW_SECONDS == 120
        assert WORKER_STALE_THRESHOLD_SECONDS == 600


# =============================================================================
# 4. _heartbeat_loop 退出逻辑（源码级验证）
# =============================================================================


class TestHeartbeatLoopExitLogic:
    """[Gate4] _heartbeat_loop 退出时设置 stopped_at，不覆盖 heartbeat_at。"""

    def test_exit_sets_stopped_at(self) -> None:
        """退出代码块必须设置 hb.stopped_at = ..."""
        from app.worker import _heartbeat_loop

        source = inspect.getsource(_heartbeat_loop)
        # 验证退出时设置 stopped_at
        assert "stopped_at" in source, (
            "_heartbeat_loop 退出时未设置 stopped_at（Gate4 要求）"
        )
        # 验证退出代码块（在 # 退出时标记 stopped 注释之后）
        exit_section = source[source.find("退出时标记 stopped"):]
        assert "stopped_at" in exit_section, "退出代码块缺少 stopped_at 赋值"

    def test_exit_does_not_overwrite_heartbeat_at(self) -> None:
        """[Gate4] 退出时不应该把 heartbeat_at 更新为 now（保留最后一次心跳时间）。

        验证退出代码块中不包含 hb.heartbeat_at = datetime.now(UTC)。
        """
        from app.worker import _heartbeat_loop

        source = inspect.getsource(_heartbeat_loop)
        exit_section = source[source.find("退出时标记 stopped"):]
        # 退出代码块不应再设置 heartbeat_at = now
        assert "hb.heartbeat_at = datetime.now(UTC)" not in exit_section, (
            "退出代码块不应覆盖 heartbeat_at（应使用 stopped_at 替代）"
        )

    def test_exit_sets_status_stopped(self) -> None:
        """退出时必须设置 hb.status = 'stopped'。"""
        from app.worker import _heartbeat_loop

        source = inspect.getsource(_heartbeat_loop)
        exit_section = source[source.find("退出时标记 stopped"):]
        assert 'hb.status = "stopped"' in exit_section, (
            "退出代码块未设置 status='stopped'"
        )


# =============================================================================
# 5. mark_stale_worker_heartbeats 同步写入 stopped_at（源码级验证）
# =============================================================================


class TestMarkStaleWorkerHeartbeatsStoppedAt:
    """[Gate4] mark_stale_worker_heartbeats 标记僵尸心跳时同步写入 stopped_at。"""

    def test_update_sql_sets_stopped_at(self) -> None:
        """UPDATE SQL 必须包含 SET stopped_at = :now。"""
        from app.worker import mark_stale_worker_heartbeats

        source = inspect.getsource(mark_stale_worker_heartbeats)
        # 验证 SQL 中包含 stopped_at = :now
        assert "stopped_at = :now" in source, (
            "mark_stale_worker_heartbeats UPDATE SQL 未设置 stopped_at = :now（Gate4 要求）"
        )

    def test_now_parameter_passed_to_execute(self) -> None:
        """db.execute 调用必须传递 now 参数。"""
        from app.worker import mark_stale_worker_heartbeats

        source = inspect.getsource(mark_stale_worker_heartbeats)
        # 验证参数字典包含 "now": now
        assert '"now": now' in source or "'now': now" in source, (
            "db.execute 调用未传递 now 参数（用于 stopped_at 赋值）"
        )

    def test_does_not_delete_history(self) -> None:
        """[Gate4] 不删除历史记录，保留审计数据。"""
        from app.worker import mark_stale_worker_heartbeats

        source = inspect.getsource(mark_stale_worker_heartbeats)
        # 不应有 DELETE
        assert "DELETE" not in source.upper(), (
            "mark_stale_worker_heartbeats 不应包含 DELETE（保留审计数据）"
        )


# =============================================================================
# 6. admin API 返回 stopped_at（源码级验证）
# =============================================================================


class TestAdminApiReturnsStoppedAt:
    """[Gate4] admin API 返回 stopped_at 字段。"""

    def test_api_includes_stopped_at_in_response(self) -> None:
        """GET /admin/worker-heartbeats 响应包含 stopped_at 字段。"""
        from app.api.admin_subscription import get_worker_heartbeats

        source = inspect.getsource(get_worker_heartbeats)
        # 验证 API 构造 WorkerHeartbeatItem 时传入 stopped_at
        assert "stopped_at" in source, (
            "admin API 未在 WorkerHeartbeatItem 构造中包含 stopped_at（Gate4 要求）"
        )


# =============================================================================
# 7. 前端类型一致性（源码级验证）
# =============================================================================


class TestFrontendTypeConsistency:
    """[Gate4] 前端 WorkerHeartbeatItem 类型与后端一致。

    通过检查 endpoints.ts 文件验证前端类型定义包含 stopped_at。
    """

    def test_frontend_endpoints_has_stopped_at(self) -> None:
        """前端 endpoints.ts WorkerHeartbeatItem 接口必须包含 stopped_at。"""
        from pathlib import Path

        endpoints_path = Path(__file__).parent.parent.parent / "frontend" / "src" / "api" / "endpoints.ts"
        if not endpoints_path.exists():
            pytest.skip("frontend/src/api/endpoints.ts 不存在（非 monorepo 环境）")

        content = endpoints_path.read_text(encoding="utf-8")
        # 找到 WorkerHeartbeatItem 接口定义
        assert "interface WorkerHeartbeatItem" in content, (
            "endpoints.ts 缺少 WorkerHeartbeatItem 接口定义"
        )
        # 提取接口体
        start = content.find("interface WorkerHeartbeatItem")
        end = content.find("}", start)
        interface_body = content[start:end]
        assert "stopped_at" in interface_body, (
            "前端 WorkerHeartbeatItem 接口缺少 stopped_at 字段（Gate4 要求）"
        )

    def test_admin_jobs_page_uses_time_display(self) -> None:
        """[Gate4] AdminJobsPage 必须包含 time_display 智能时间显示逻辑。"""
        from pathlib import Path

        page_path = Path(__file__).parent.parent.parent / "frontend" / "src" / "pages" / "AdminJobsPage.tsx"
        if not page_path.exists():
            pytest.skip("frontend/src/pages/AdminJobsPage.tsx 不存在")

        content = page_path.read_text(encoding="utf-8")
        # 验证包含 Gate4 标识
        assert "Gate4" in content, "AdminJobsPage 缺少 Gate4 标识"
        # 验证包含 time_display 字段
        assert "time_display" in content, "AdminJobsPage 缺少 time_display 字段"
        # 验证包含"距上次心跳"和"已停止于"显示逻辑
        assert "距上次心跳" in content, "AdminJobsPage 缺少 '距上次心跳' 显示逻辑"
        assert "已停止于" in content, "AdminJobsPage 缺少 '已停止于' 显示逻辑"
        # 验证包含历史实例折叠切换
        assert "showAllWorkerInstances" in content, "AdminJobsPage 缺少历史实例折叠切换"


if __name__ == "__main__":
    # 自测入口：不依赖 pytest，仅验证核心逻辑
    print(f"WORKER_FRESH_WINDOW_SECONDS={WORKER_FRESH_WINDOW_SECONDS}")
    print(f"WORKER_STALE_THRESHOLD_SECONDS={WORKER_STALE_THRESHOLD_SECONDS}")
    print(f"classify('running', 30) = {classify_health_state('running', 30)}")
    print(f"classify('running', 300) = {classify_health_state('running', 300)}")
    print(f"classify('stopped', 10) = {classify_health_state('stopped', 10)}")
    print("OK")
