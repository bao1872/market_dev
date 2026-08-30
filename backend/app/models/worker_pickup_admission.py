"""WorkerPickupAdmission ORM 模型 — E2.1 P1-C pickup admission 控制记录。

对应迁移 093_worker_pickup_admission。

## 为什么需要独立控制表

worker claim 的 canonical owner 在 PostgreSQL 事务内
（`claim_next_job_run`：SELECT ... FOR UPDATE SKIP LOCKED → running → commit）。
要让 "PAUSE 成功返回后不得再有新的 claim commit" 成为**机器可证明**的不变量，
pause 判定必须与 claim 共享同一个 ownership boundary —— 即被同一事务以
`FOR UPDATE` 锁住的 **singleton 行**。

`scheduler_job_runs` 是每行一个 job 的表，无法表达"全局暂停"这一状态，
也无法在 claim 之前被原子锁定。Redis / 进程内 flag 与 PostgreSQL claim
分属两个系统，无法提供单一 linearization point。因此需要这张表。

## 字段只取完成 invariant 所需的最小集合

- `scope`：singleton identity（如 after_close_orchestrator）
- `paused`：是否暂停新的 pickup
- `pause_token`：当前 pause 的 ownership token（deploy 只能释放自己创建的 pause）
- `paused_by` / `reason` / `paused_at`：observability
- `updated_at`：观测与排障

## 语义

PAUSE ACTIVE 时：
- `running`：继续自然运行，不 kill / 不 cancel / 不 reset
- `queued` / `resume_queued`：保持原状态，不被 claim
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class WorkerPickupAdmission(Base):
    """Worker pickup admission 控制记录（每 scope 一行的 singleton）。"""

    __tablename__ = "worker_pickup_admission"

    # singleton identity：一个 scope 一行，例如 after_close_orchestrator。
    scope: Mapped[str] = mapped_column(String(128), primary_key=True)

    # 暂停开关：True 时不得再 claim 新的 queued / resume_queued。
    paused: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )

    # ownership token：deploy attempt 只能释放 token 匹配的 pause，
    # 避免把他人/先前设置的 pause 解掉（E2.1 §17 / §20）。
    pause_token: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # observability：谁、为什么、何时设置的 pause。
    paused_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    paused_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
