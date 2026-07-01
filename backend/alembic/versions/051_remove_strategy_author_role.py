"""051 remove strategy_author role - 删除 strategy_author 角色并迁移用户。

Revision ID: 051_remove_strategy_author_role
Revises: 050_drop_memberships_table
Create Date: 2026-07-01

变更内容：
- 删除项目不再使用的 strategy_author 角色
- 角色迁移规则：
  * 同时持有 member 和 strategy_author 的用户：保留 member，删除 strategy_author
  * 仅持有 strategy_author 的用户：转换为 member
- 若 member 角色不存在则自动创建，保证转换路径自洽

downgrade 行为：
- 重新创建 strategy_author 角色（不恢复用户角色分配，因 upgrade 未保留历史映射）
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "051_remove_strategy_author_role"
down_revision: str | None = "050_drop_memberships_table"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # [Migration] - 描述: 确保 member 角色存在，作为 strategy_author 的唯一转换目标
    op.execute(
        """
        INSERT INTO roles (id, name, description)
        SELECT gen_random_uuid(), 'member', '普通会员'
        WHERE NOT EXISTS (SELECT 1 FROM roles WHERE name = 'member')
        """
    )

    # [Migration] - 描述: 仅持有 strategy_author 的用户补充 member 角色
    op.execute(
        """
        INSERT INTO user_roles (user_id, role_id)
        SELECT sa_users.user_id, member_role.id
        FROM (
            SELECT ur.user_id
            FROM user_roles ur
            JOIN roles r ON ur.role_id = r.id
            WHERE r.name = 'strategy_author'
              AND NOT EXISTS (
                  SELECT 1 FROM user_roles ur2
                  JOIN roles r2 ON ur2.role_id = r2.id
                  WHERE ur2.user_id = ur.user_id AND r2.name = 'member'
              )
        ) sa_users
        CROSS JOIN (SELECT id FROM roles WHERE name = 'member') member_role
        """
    )

    # [Migration] - 描述: 删除所有 strategy_author 角色关联
    op.execute(
        """
        DELETE FROM user_roles
        WHERE role_id = (SELECT id FROM roles WHERE name = 'strategy_author')
        """
    )

    # [Migration] - 描述: 删除 strategy_author 角色本身
    op.execute(
        """
        DELETE FROM roles WHERE name = 'strategy_author'
        """
    )


def downgrade() -> None:
    # [Migration] - 描述: 重新创建 strategy_author 角色（用户分配不恢复）
    op.execute(
        """
        INSERT INTO roles (id, name, description)
        SELECT gen_random_uuid(), 'strategy_author', '策略作者'
        WHERE NOT EXISTS (SELECT 1 FROM roles WHERE name = 'strategy_author')
        """
    )


if __name__ == "__main__":
    # 自测入口：验证 revision 链与函数定义（不连接数据库）
    assert revision == "051_remove_strategy_author_role"
    assert down_revision == "050_drop_memberships_table"
    assert callable(upgrade)
    assert callable(downgrade)
    print(f"revision={revision}")
    print(f"down_revision={down_revision}")
    print("OK: 迁移文件验证通过")
