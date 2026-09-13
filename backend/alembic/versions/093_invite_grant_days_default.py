"""093 invite grant_days server default 30 -> 1

周期单位合同收口：邀请码 grant_days 的数据库列 server_default 由 30 改为 1，
与 ORM default（1 单位 = 1 天）对齐。

仅修改 schema default：
- 旧邀请码数据不动
- 旧 expires_at 不动
- 历史 grant_months 不动
- server_default: 30 -> 1

不执行 UPDATE invite_codes（不改动任何历史记录）。
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "093_invite_grant_days_default"
down_revision: str | None = "092_review_core_only_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "invite_codes",
        "grant_days",
        existing_type=sa.Integer(),
        existing_nullable=False,
        server_default=sa.text("1"),
    )


def downgrade() -> None:
    op.alter_column(
        "invite_codes",
        "grant_days",
        existing_type=sa.Integer(),
        existing_nullable=False,
        server_default=sa.text("30"),
    )
