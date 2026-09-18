"""094_invite_code_ciphertext: invite_codes.code_ciphertext

[PANJI-BIZ-FIX-20260918 Commit B2] 新邀请码允许管理后台长期查看/复制。

背景：
- 旧邀请码只存 ``code_hash = SHA256(明文)``，SHA256 不可反推，历史明文无法恢复。
- 修复后新邀请码额外保存 Fernet 密文 ``code_ciphertext``，admin 列表可解密回显。

约束（不得违反）：
- **不修改** ``code_hash``，它仍是注册/续期/验证的唯一真源（SSOT）。
- 只新增 **可空** 的 TEXT 列；历史行为 100% 不变（历史行 = NULL，不伪造明文）。
- 明文严禁直接入库（``code_ciphertext`` 只存 ``encrypt_secret(raw_code)``）。
- 本迁移只改 schema，不 UPDATE/DELETE 任何 invite_codes 历史行。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "094_invite_code_ciphertext"
down_revision: str | None = "093_invite_grant_days_default"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "invite_codes",
        sa.Column("code_ciphertext", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("invite_codes", "code_ciphertext")
