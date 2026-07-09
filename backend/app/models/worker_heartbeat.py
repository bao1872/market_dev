"""WorkerHeartbeat ORM 模型 - Worker 心跳表。

对应迁移 027_job_runs_and_heartbeats：
- worker_heartbeats: Worker 心跳记录（复合主键 worker_name + instance_id）

字段说明：
- worker_name: Worker 名称，如 bars_scheduler/strategy_batch
- instance_id: 实例标识，hostname:pid
- status: running/idle/stopped
- current_job_id: 当前执行的任务 ID
- build_sha: 构建版本 SHA
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, PrimaryKeyConstraint, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class WorkerHeartbeat(Base):
    """Worker 心跳记录。

    复合主键 (worker_name, instance_id) 标识唯一 Worker 实例。
    """

    __tablename__ = "worker_heartbeats"
    __table_args__ = (
        PrimaryKeyConstraint("worker_name", "instance_id"),
    )

    worker_name: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="Worker 名称，如 bars_scheduler/strategy_batch",
    )
    instance_id: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="实例标识，hostname:pid",
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="启动时间",
    )
    heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="最近心跳时间",
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="running", comment="running/idle/stopped",
    )
    current_job_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, comment="当前执行的任务 ID",
    )
    build_sha: Mapped[str | None] = mapped_column(
        String(40), nullable=True, comment="构建版本 SHA",
    )
    metadata_json: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="额外元数据 JSON",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<WorkerHeartbeat(worker_name={self.worker_name!r}, "
            f"instance_id={self.instance_id!r}, status={self.status!r})>"
        )


if __name__ == "__main__":
    # 自测入口：验证 ORM 模型映射（无副作用，不连接数据库）
    print(f"WorkerHeartbeat.__tablename__={WorkerHeartbeat.__tablename__}")
    cols = [c.name for c in WorkerHeartbeat.__table__.columns]
    print(f"WorkerHeartbeat columns={cols}")
    assert "worker_name" in cols
    assert "instance_id" in cols
    assert "started_at" in cols
    assert "heartbeat_at" in cols
    assert "status" in cols
    assert "current_job_id" in cols
    assert "build_sha" in cols
    # 验证复合主键
    pk_cols = [c.name for c in WorkerHeartbeat.__table__.primary_key]
    print(f"WorkerHeartbeat PK={pk_cols}")
    assert pk_cols == ["worker_name", "instance_id"]
    print("OK")
