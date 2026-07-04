"""WorkerHeartbeat schema - Worker 心跳只读 admin DTO。

对应 backend/app/models/worker_heartbeat.py 的 ORM 模型。
本 schema 用于 admin API 响应，附加后端计算的 heartbeat_age_seconds 和 health_state，
避免前端复制业务规则（AGENTS.md 第 10 条：前端不得重新实现后端业务规则）。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

# 阈值显式定义在此处，避免跨模块导入耦合；值变更时需同步上游：
# - WORKER_FRESH_WINDOW_SECONDS 同 system_overview_service.WORKER_HEALTH_WINDOW
# - WORKER_STALE_THRESHOLD_SECONDS 同 worker.STALE_HEARTBEAT_THRESHOLD_SECONDS
WORKER_FRESH_WINDOW_SECONDS = 120
WORKER_STALE_THRESHOLD_SECONDS = 600


class WorkerHeartbeatItem(BaseModel):
    """单个 Worker 心跳记录（admin 只读视图）。

    health_state 由后端计算：
    - fresh:   status=running 且 heartbeat_age < WORKER_FRESH_WINDOW_SECONDS
    - stale:   status=running 且 WORKER_FRESH_WINDOW_SECONDS ≤ age < WORKER_STALE_THRESHOLD_SECONDS
    - stopped: status=stopped 或 age ≥ WORKER_STALE_THRESHOLD_SECONDS
    """

    model_config = ConfigDict(from_attributes=True)

    worker_name: str
    instance_id: str
    started_at: datetime
    heartbeat_at: datetime
    status: str  # running/idle/stopped
    current_job_id: str | None = None
    build_sha: str | None = None
    metadata_json: str | None = None
    updated_at: datetime
    # 后端计算字段
    heartbeat_age_seconds: int
    health_state: str  # fresh/stale/stopped


class WorkerHeartbeatListResponse(BaseModel):
    """Worker 心跳列表响应。"""

    items: list[WorkerHeartbeatItem]
    total: int
    limit: int
    offset: int


def classify_health_state(status: str, heartbeat_age_seconds: int) -> str:
    """根据 status 和心跳年龄计算 health_state。

    Args:
        status: worker_heartbeats.status，running/idle/stopped
        heartbeat_age_seconds: now - heartbeat_at 的秒数

    Returns:
        fresh/stale/stopped
    """
    if status == "running" and heartbeat_age_seconds < WORKER_FRESH_WINDOW_SECONDS:
        return "fresh"
    if status == "running" and heartbeat_age_seconds < WORKER_STALE_THRESHOLD_SECONDS:
        return "stale"
    return "stopped"


if __name__ == "__main__":
    # 自测入口：验证 schema 字段和分类逻辑（无副作用，不连接数据库）
    print(f"WORKER_FRESH_WINDOW_SECONDS={WORKER_FRESH_WINDOW_SECONDS}")
    print(f"WORKER_STALE_THRESHOLD_SECONDS={WORKER_STALE_THRESHOLD_SECONDS}")

    fields = list(WorkerHeartbeatItem.model_fields.keys())
    print(f"WorkerHeartbeatItem fields={fields}")
    assert "health_state" in fields
    assert "heartbeat_age_seconds" in fields
    assert "worker_name" in fields
    assert "build_sha" in fields

    # 验证分类逻辑
    assert classify_health_state("running", 30) == "fresh"
    assert classify_health_state("running", 120) == "stale"
    assert classify_health_state("running", 599) == "stale"
    assert classify_health_state("running", 600) == "stopped"
    assert classify_health_state("stopped", 10) == "stopped"
    assert classify_health_state("idle", 10) == "stopped"
    print("OK")
