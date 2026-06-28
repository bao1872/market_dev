"""043 rename dsa_selector display_name to 趋势选股

Revision ID: 043_rename_dsa_selector_display_name
Revises: 042_drop_dsa_backfill
Create Date: 2026-06-28

变更内容：
- 更新 strategy_definitions 表中 strategy_key='dsa_selector' 的 display_name 为 '趋势选股'
- 业务背景：前端/后端 manifest 已统一改名为"趋势选股"，DB 存量数据同步更新
- 幂等 UPDATE：多次执行结果一致；downgrade 不回滚（数据更新类迁移）
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "043_rename_dsa_selector_display_name"
down_revision: str | None = "042_drop_dsa_backfill"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # [Display Name] - 描述: 幂等更新 dsa_selector 展示名为"趋势选股"（与 manifest/yaml 前端统一）
    op.execute(
        "UPDATE strategy_definitions "
        "SET display_name = '趋势选股' "
        "WHERE strategy_key = 'dsa_selector'"
    )


def downgrade() -> None:
    # 数据更新类迁移，不回滚（旧 display_name 已废弃，无需恢复）
    pass


if __name__ == "__main__":
    # 自测入口：验证 revision 链与函数定义（不连接数据库）
    assert revision == "043_rename_dsa_selector_display_name"
    assert down_revision == "042_drop_dsa_backfill"
    assert callable(upgrade)
    assert callable(downgrade)
    print(f"revision={revision}")
    print(f"down_revision={down_revision}")
    print("OK: 迁移文件验证通过")
