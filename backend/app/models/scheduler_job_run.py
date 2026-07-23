"""SchedulerJobRun ORM 模型 - 定时任务执行记录。

对应迁移 027_job_runs_and_heartbeats + 067 lease_epoch/attempt_no：
- scheduler_job_runs: 定时任务执行记录（job_name 区分任务类型，status 状态机）

字段说明：
- job_name: 任务名称，如 bars_daily/strategy_scheduler/monitor_cycle
- business_date: 业务日期 YYYY-MM-DD
- status: queued/running/succeeded/failed/skipped/interrupted/resume_queued
- heartbeat_at/lease_expires_at: Worker 心跳与租约
- lease_epoch: [PRD §4.3 JOB-02] 租约代际，Worker 领取时递增，写操作校验防 fencing
- attempt_no: [PRD §4.3 JOB-01] 尝试次数，首次 0，自动 resume 递增
- total_count/succeeded_count/failed_count/progress: 执行进度

状态闭环（PRD §4.3）：
    queued → running → interrupted → resume_queued → running → succeeded/failed
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models._table_meta import table_constraints, table_indexes
from app.models.base import Base


class SchedulerJobRun(Base):
    """定时任务执行记录。

    状态机（PRD §4.3）：
        queued → running → succeeded/failed
        running → interrupted（watchdog 检测 lease 过期/heartbeat 超时）
        interrupted → resume_queued（auto-resume 自动转换）
        resume_queued → running（Worker 领取，递增 lease_epoch + attempt_no）
    """

    __tablename__ = "scheduler_job_runs"

    # [Idempotency] - run_key 部分唯一索引：约束 queued/running/resume_queued 活跃记录
    # [PRD §4.3 JOB-01] resume_queued 也算活跃状态，防止 interrupted 自动恢复期间出现重复任务
    # 配合迁移 067（更新 038 的部分唯一索引，新增 resume_queued）
    __table_args__ = (
        Index(
            "uq_scheduler_job_runs_active_run_key",
            "run_key",
            unique=True,
            postgresql_where=text(
                "run_key IS NOT NULL AND status IN ('queued', 'running', 'resume_queued')"
            ),
        ),
        Index("ix_scheduler_job_runs_job_bd", "job_name", "business_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    job_name: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="任务名称，如 bars_daily/strategy_scheduler/monitor_cycle",
    )
    business_date: Mapped[str | None] = mapped_column(
        String(10), nullable=True, comment="业务日期 YYYY-MM-DD",
    )
    run_key: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="业务幂等键，如 bars_scheduler:2026-06-25",
    )
    scheduled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="计划执行时间",
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="实际开始时间",
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="完成时间",
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="running",
        comment="queued/running/succeeded/failed/skipped/interrupted/resume_queued",
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="心跳时间",
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="租约过期时间",
    )
    worker_instance_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="Worker 实例标识 hostname:pid",
    )
    # [PRD §4.3 JOB-02] - lease_epoch: 租约代际，Worker 领取时递增，写操作校验防 fencing
    # 旧 Worker（lease 已过期）写入时 WHERE lease_epoch = :current_epoch 失败，防止僵尸写
    lease_epoch: Mapped[int] = mapped_column(
        Integer(), nullable=False, server_default=text("0"),
        comment="租约代际：Worker 领取时递增，写操作校验防 fencing",
    )
    # [PRD §4.3 JOB-01] - attempt_no: 尝试次数，首次 0，自动 resume 递增
    attempt_no: Mapped[int] = mapped_column(
        Integer(), nullable=False, server_default=text("0"),
        comment="尝试次数：首次 0，自动 resume 递增",
    )
    last_cycle_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="最近一个周期执行时间",
    )
    total_count: Mapped[int | None] = mapped_column(
        Integer(), nullable=True, comment="总数",
    )
    succeeded_count: Mapped[int | None] = mapped_column(
        Integer(), nullable=True, comment="成功数",
    )
    failed_count: Mapped[int | None] = mapped_column(
        Integer(), nullable=True, comment="失败数",
    )
    progress: Mapped[float | None] = mapped_column(
        Float(), nullable=True, comment="进度 0.0-1.0",
    )
    error_code: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="错误码",
    )
    error_message: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="错误信息",
    )
    metadata_json: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="额外元数据 JSON",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<SchedulerJobRun(job_name={self.job_name!r}, "
            f"status={self.status!r}, business_date={self.business_date})>"
        )


if __name__ == "__main__":
    # 自测入口：验证 ORM 模型映射（无副作用，不连接数据库）
    print(f"SchedulerJobRun.__tablename__={SchedulerJobRun.__tablename__}")
    cols = [c.name for c in SchedulerJobRun.__table__.columns]
    print(f"SchedulerJobRun columns={cols}")
    assert "job_name" in cols
    assert "business_date" in cols
    assert "run_key" in cols
    assert "status" in cols
    assert "heartbeat_at" in cols
    assert "lease_expires_at" in cols
    assert "progress" in cols
    assert "error_code" in cols
    assert "error_message" in cols
    # [PRD §4.3 JOB-01/JOB-02] 验证新增字段
    assert "lease_epoch" in cols, f"缺少 lease_epoch 列: {cols}"
    assert "attempt_no" in cols, f"缺少 attempt_no 列: {cols}"
    assert "worker_instance_id" in cols, f"缺少 worker_instance_id 列: {cols}"
    # 验证 __table_args__ 中的约束与索引
    constraint_names = {c.name for c in table_constraints(SchedulerJobRun) if hasattr(c, "name") and c.name}
    index_names = {idx.name for idx in table_indexes(SchedulerJobRun) if idx.name}
    assert "uq_scheduler_job_runs_run_key" not in constraint_names, (
        f"应已移除全局唯一约束: {constraint_names}"
    )
    assert "uq_scheduler_job_runs_active_run_key" in index_names, (
        f"缺少部分唯一索引: {index_names}"
    )
    assert "ix_scheduler_job_runs_job_bd" in index_names, f"缺少复合索引: {index_names}"
    print(f"constraints={constraint_names}")
    print(f"indexes={index_names}")
    print("OK")
