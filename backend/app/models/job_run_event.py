"""JobRunEvent ORM 模型 - 任务运行事件时间线。

对应迁移 037_job_run_events：
- job_run_events: 任务关键步骤事件记录（START/DAILY_DONE/DSA_CREATED 等）

字段说明：
- job_run_id: 关联 scheduler_job_runs.id（ON DELETE CASCADE，任务删除时事件级联清除）
- step: 步骤标识（如 START / DAILY_DONE / DSA_CREATED / ERROR）
- level: 事件级别（info / warn / error）
- message: 人类可读消息
- payload: 结构化附加数据（JSONB，覆盖率/run_id/成功失败数等）
- created_at: 事件创建时间

索引：
- ix_job_run_events_job_run_id_created_at (job_run_id, created_at)
  加速按任务查询时间线（任务详情抽屉按时间倒序展示）
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class JobRunEvent(Base):
    """任务执行事件时间线 - 每个 SchedulerJobRun 的关键步骤记录。

    用途：
    - 任务详情抽屉展示执行时间线，定位任务卡在哪一步
    - 盘后编排（after_close_orchestrator）串联全流程事件
    - 写入由 job_run_event_service.append_event() 统一负责（flush 不 commit）
    """

    __tablename__ = "job_run_events"

    # [JobRunEvent] - 复合索引加速按任务查询时间线（job_run_id + created_at）
    __table_args__ = (
        Index(
            "ix_job_run_events_job_run_id_created_at",
            "job_run_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    job_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("scheduler_job_runs.id", ondelete="CASCADE"),
        nullable=False,
        comment="关联 scheduler_job_runs.id",
    )
    step: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="步骤名，如 START/DAILY_DONE/ERROR",
    )
    level: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="info", comment="级别：info/warn/error",
    )
    message: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="人类可读消息",
    )
    payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(astext_type=Text()),
        nullable=True,
        comment="详细数据 JSON（覆盖率、run_id 等）",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<JobRunEvent(job_run_id={self.job_run_id!r}, "
            f"step={self.step!r}, level={self.level!r})>"
        )


if __name__ == "__main__":
    # 自测入口：验证 ORM 模型映射（无副作用，不连接数据库）
    print(f"JobRunEvent.__tablename__={JobRunEvent.__tablename__}")
    cols = [c.name for c in JobRunEvent.__table__.columns]
    print(f"JobRunEvent columns={cols}")
    assert "id" in cols
    assert "job_run_id" in cols
    assert "step" in cols
    assert "level" in cols
    assert "message" in cols
    assert "payload" in cols
    assert "created_at" in cols

    # 验证 __table_args__ 中的索引
    index_names = {idx.name for idx in JobRunEvent.__table__.indexes if idx.name}
    assert "ix_job_run_events_job_run_id_created_at" in index_names, (
        f"缺少复合索引: {index_names}"
    )
    print(f"indexes={index_names}")

    # 验证外键 ON DELETE CASCADE
    fks = []
    for c in JobRunEvent.__table__.columns:
        for fk in c.foreign_keys:
            fks.append((c.name, fk.target_fullname, fk.ondelete))
    print(f"foreign_keys={fks}")
    assert any(fk[0] == "job_run_id" and fk[2] == "CASCADE" for fk in fks), (
        f"job_run_id 外键应 ON DELETE CASCADE: {fks}"
    )

    # 验证 level 默认值
    level_col = JobRunEvent.__table__.c.level
    assert level_col.server_default is not None, "level 应有 server_default"
    print(f"level server_default={level_col.server_default.arg}")

    print("OK")
