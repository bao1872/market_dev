"""Worker pickup admission owner — E2.1 P1-C。

设计契约见 docs/maps/93-e21-admission-bootstrap-contract.md。

## Linearization boundary

worker claim 的 canonical owner 是 `worker.py::_after_close_poll_once`：在同一个
PostgreSQL `AsyncSessionLocal()` 事务内做
`SELECT ... FOR UPDATE SKIP LOCKED → running → commit`。`claim_next_job_run`
是另一条（Chip-consensus）claim 路径，**不是** after-close 生产 pickup owner。

因此 admission 判定**必须**发生在同一个事务内，并以 `FOR UPDATE` 锁住本服务的
singleton 行。谁先拿到行锁，谁就 linearize 在先：

    PAUSE 先赢锁  → worker 随后读到 paused=True → 不能 claim
    WORKER 先赢锁 → job commit 为 running → pause 随后 commit
                  → secondary pre-mutation gate 看到 running → 部署被阻止

明令禁止（E2.1 §14）：

    transaction A: read paused
    transaction B: claim

这留下 check→claim 的 TOCTOU。

## 缺行语义（admission-aware runtime）

`migration 093` 在 upgrade 时**插入** singleton 行 `after_close_orchestrator`
（paused=false）。因此 admission-aware worker 启动时该行已存在。

admission-aware worker 中：
- ROW ACTIVE  → 允许 claim
- ROW PAUSED  → 不允许 claim（job 保持 queued/resume_queued）
- ROW MISSING → **FAIL CLOSED**（不允许 claim）
- DB ERROR    → **FAIL CLOSED**（不允许 claim）

缺行 / 查询错误**绝不**再视为 admitted：否则 "PAUSE 成功后不得有新 claim" 的
linearization 在缺行场景下根本不持有行锁（SELECT ... FOR UPDATE 返回 0 行），
整个不变量失效。

旧（legacy）worker 不读取本表，其 pickup 由既有 drain 机制在 first-deploy 时隔离，
与本服务语义无关（见 deploy runbook）。

## PAUSE 业务语义

PAUSE ACTIVE 时：
- running：继续自然运行，不 kill / 不 cancel / 不 reset
- queued / resume_queued：保持原状态，不被 claim
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.worker_pickup_admission import WorkerPickupAdmission

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AdmissionStatus:
    """admission 当前状态（供 operator status / deploy gate 使用）。"""

    installed: bool
    paused: bool
    pause_token: str | None
    paused_by: str | None
    reason: str | None
    paused_at: datetime | None


def new_pause_token() -> str:
    """生成一次 pause 的 ownership token。

    deploy 只能释放 token 匹配的 pause，避免把他人 / 先前设置的 pause 解掉
    （E2.1 §17 / §20）。
    """
    return uuid.uuid4().hex


async def _get_or_create_row_locked(
    db: AsyncSession,
    scope: str,
) -> WorkerPickupAdmission:
    """取到该 scope 的 admission 行并**持行锁**；不存在则以未暂停状态创建。

    行锁是 pause 与 claim 的唯一 linearization point，因此这里必须是
    `FOR UPDATE`（非 SKIP LOCKED）：pause 不能跳过锁，否则无法与 claim 互斥。
    """
    row = (
        await db.execute(
            select(WorkerPickupAdmission).where(
                WorkerPickupAdmission.scope == scope
            ).with_for_update()
        )
    ).scalar_one_or_none()
    if row is not None:
        return row

    try:
        async with db.begin_nested():
            created = WorkerPickupAdmission(scope=scope, paused=False)
            db.add(created)
        await db.flush()
    except IntegrityError:
        # 并发创建：回退到持锁读取，保证仍然拿到同一行且持锁。
        await db.rollback()
        row = (
            await db.execute(
                select(WorkerPickupAdmission).where(
                    WorkerPickupAdmission.scope == scope
                ).with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            raise
        return row

    row = (
        await db.execute(
            select(WorkerPickupAdmission).where(
                WorkerPickupAdmission.scope == scope
            ).with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:  # pragma: no cover - 理论上不可达
        raise RuntimeError(f"admission row missing after create: {scope}")
    return row


async def is_pickup_admitted(db: AsyncSession, scope: str) -> bool:
    """worker claim 前调用：是否允许领取新的 queued / resume_queued。

    必须在与 claim **同一个事务**内调用，否则不具 linearization 意义。
    admission-aware runtime 下（migration 093 已插入 singleton 行）：
    - ROW ACTIVE → True（允许 claim）
    - ROW PAUSED → False（job 保持 queued/resume_queued 原状态）
    - ROW MISSING / DB ERROR → False（**FAIL CLOSED**，绝不视为 admitted）
    """
    try:
        row = (
            await db.execute(
                select(WorkerPickupAdmission).where(
                    WorkerPickupAdmission.scope == scope
                ).with_for_update()
            )
        ).scalar_one_or_none()
    except Exception:  # noqa: BLE001 - admission 读取失败必须 fail-closed
        logger.exception(
            "[admission] pickup admission 读取失败，fail-closed 拒绝 claim: scope=%s",
            scope,
        )
        return False
    if row is None:
        # 该行应由 migration 093 upgrade 创建；admission-aware worker 下缺行
        # 视为异常状态，FAIL CLOSED 而非 admitted（否则不持有行锁，linearization 失效）。
        return False
    return not row.paused


async def acquire_pause(
    db: AsyncSession,
    *,
    scope: str,
    token: str,
    actor: str,
    reason: str | None = None,
) -> bool:
    """设置 PAUSE。返回 True 表示 pause 已生效。

    幂等：若已 paused 且 token 相同，返回 True（不覆盖原因/时间）。
    若已 paused 但 token 不同（他人持有的 pause），返回 False ——
    调用方不得认为自己拥有了这次 pause，结束时也绝不能释放它。
    """
    row = await _get_or_create_row_locked(db, scope)
    if row.paused and row.pause_token != token:
        return False

    row.paused = True
    row.pause_token = token
    row.paused_by = actor
    row.reason = reason
    row.paused_at = datetime.utcnow()
    await db.flush()
    return True


async def release_pause(
    db: AsyncSession,
    *,
    scope: str,
    token: str,
) -> bool:
    """释放 pause，**仅当** token 与当前持有者匹配。

    未安装 / 未暂停 → False（无所有权可释放）。
    已暂停但 token 不匹配 → False（那是别人的 pause，不得解掉）。
    """
    row = (
        await db.execute(
            select(WorkerPickupAdmission).where(
                WorkerPickupAdmission.scope == scope
            ).with_for_update()
        )
    ).scalar_one_or_none()
    if row is None or not row.paused:
        return False
    if row.pause_token != token:
        return False

    row.paused = False
    row.pause_token = None
    row.paused_by = None
    row.reason = None
    row.paused_at = None
    await db.flush()
    return True


async def get_status(db: AsyncSession, scope: str) -> AdmissionStatus:
    """读取 admission 状态（只读，不加锁；需要互斥语义请用 acquire/release）。"""
    row = (
        await db.execute(
            select(WorkerPickupAdmission).where(
                WorkerPickupAdmission.scope == scope
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return AdmissionStatus(
            installed=False,
            paused=False,
            pause_token=None,
            paused_by=None,
            reason=None,
            paused_at=None,
        )
    return AdmissionStatus(
        installed=True,
        paused=bool(row.paused),
        pause_token=row.pause_token,
        paused_by=row.paused_by,
        reason=row.reason,
        paused_at=row.paused_at,
    )
