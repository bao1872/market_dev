"""021 drop (strategy_version_id, trade_date, instrument_id) unique constraint

Revision ID: 021_drop_vdi_uq
Revises: 020_remove_secret_ref
Create Date: 2026-06-23

strategy_results 表唯一约束调整：
- 删除 (strategy_version_id, trade_date, instrument_id) 唯一约束
  该约束允许新 run 覆盖旧 run 的结果，语义错误
- 保留 (run_id, instrument_id) 唯一约束（uq_strategy_results_run_instrument）
  确保结果属于唯一 run，语义正确

约束名说明：
- 迁移 005 创建该约束时未指定 name，PostgreSQL 自动命名为
  strategy_results_strategy_version_id_trade_date_instrument_id_key
- 但 PostgreSQL 标识符最大 63 字符，实际被截断为
  strategy_results_strategy_version_id_trade_date_instrument__key
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "021_drop_vdi_uq"
down_revision: str | None = "020_remove_secret_ref"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# PostgreSQL 截断后的实际约束名（63 字符上限）
_TRUNCATED_CONSTRAINT_NAME = (
    "strategy_results_strategy_version_id_trade_date_instrument__key"
)


def upgrade() -> None:
    op.drop_constraint(
        _TRUNCATED_CONSTRAINT_NAME,
        "strategy_results",
        type_="unique",
    )


def downgrade() -> None:
    op.create_unique_constraint(
        _TRUNCATED_CONSTRAINT_NAME,
        "strategy_results",
        ["strategy_version_id", "trade_date", "instrument_id"],
    )


if __name__ == "__main__":
    # 自测入口：验证 revision 链与函数定义（不连接数据库）
    assert revision == "021_drop_vdi_uq"
    assert down_revision == "020_remove_secret_ref"
    assert callable(upgrade)
    assert callable(downgrade)
    print(f"revision={revision}")
    print(f"down_revision={down_revision}")
    print("OK: 迁移文件验证通过")
