"""093 worker pickup admission - E2.1 P1-C admission control owner

新增 worker_pickup_admission 表：worker pickup 的 admission 控制记录
（每 scope 一行的 singleton）。

背景（见 docs/maps/93-e21-admission-bootstrap-contract.md）：

    worker claim 的 canonical owner 在 PostgreSQL 事务内
    （SELECT ... FOR UPDATE SKIP LOCKED → running → commit）。
    要让 "PAUSE 成功返回后不得再有新的 claim commit" 机器可证明，
    pause 判定必须与 claim 共享同一 ownership boundary ——
    即被同一事务以 FOR UPDATE 锁住的 singleton 行。

    scheduler_job_runs 是每行一个 job 的表，无法表达全局暂停，
    也无法在 claim 之前被原子锁定；Redis / 进程内 flag 与 PostgreSQL claim
    分属两个系统，无法提供单一 linearization point。

字段只取完成 invariant 所需的最小集合：
    scope / paused / pause_token / paused_by / reason / paused_at / updated_at

upgrade：
1. CREATE TABLE worker_pickup_admission（幂等：已存在则跳过）。
2. 不预置任何行：无行 == 未安装 admission control，属合法 bootstrap 前状态。
   由 MODE A bootstrap 流程在 drain 完成后显式创建并置 PAUSED。

downgrade：
- DROP TABLE。仅在确认无部署进行中时执行；本 migration 不自动判断运行态。

PRODUCTION_DB_MIGRATION = NO（只在 verification DB 执行）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "093_worker_pickup_admission"
down_revision: str | None = "092_review_core_only_identity"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "worker_pickup_admission" in inspector.get_table_names():
        return

    op.create_table(
        "worker_pickup_admission",
        sa.Column("scope", sa.String(length=128), nullable=False),
        sa.Column(
            "paused",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("pause_token", sa.String(length=128), nullable=True),
        sa.Column("paused_by", sa.String(length=128), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("paused_at", sa.DateTime(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("scope"),
    )


def downgrade() -> None:
    op.drop_table("worker_pickup_admission")
