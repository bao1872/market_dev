"""board definition identity contract version

[Corrective-2 2026-08-05] board_sync_service 必须把 provider 输出的
identity_contract_version 落实到 BoardDefinitionVersion（正式存储），
禁止只写入 BoardFactsRun 后丢弃。数据库已有行无法回填，采用 nullable
列 + 回填占位，新写入行由服务层强制非空。

Revision ID: 085_board_definition_identity_contract
Revises: 084_domain_runs_publication_kinds
Create Date: 2026-08-05
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "085_board_definition_identity_contract"
down_revision: str | None = "084_domain_runs_publication_kinds"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "board_definition_versions",
        sa.Column("identity_contract_version", sa.Text(), nullable=True),
    )
    # 历史行无法回填，用明文占位标记"未声明合同版本"；新写入行由服务层强制非空
    op.execute(
        "UPDATE board_definition_versions "
        "SET identity_contract_version = 'unversioned' "
        "WHERE identity_contract_version IS NULL"
    )
    op.alter_column(
        "board_definition_versions",
        "identity_contract_version",
        existing_type=sa.Text(),
        nullable=False,
    )


def downgrade() -> None:
    op.drop_column("board_definition_versions", "identity_contract_version")
