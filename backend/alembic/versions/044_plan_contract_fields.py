"""044 plan_contract fields on invite_codes and memberships

Revision ID: 044_plan_contract_fields
Revises: 043_rename_dsa_selector_display_name
Create Date: 2026-06-28

变更内容：
- invite_codes 新增 plan_code(String)/monitor_limit(Integer)/grant_months(Integer) 列
- memberships 新增 plan_code(String)/monitor_limit(Integer) 列
- 旧数据回填：invite_codes → plan_code='observe_20', monitor_limit=20, grant_months=1
- 旧数据回填：memberships → plan_code='observe_20', monitor_limit=20
- grant_days 保留兼容性（新代码优先使用 grant_months 按自然月计算）

业务背景：
- advice.md 第四节要求建立单一 plan_contract：observe_20 监控 20 只、research_50 监控 50 只
- 邀请码新增 plan_code/monitor_limit 快照/grant_months（替代 grant_days 固定 30 天）
- 会员记录保存当前套餐与监控上限
- 旧邀请码和旧会员默认映射 observe_20（30天≈1月 → grant_months=1）
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "044_plan_contract_fields"
down_revision: str | None = "043_rename_dsa_selector_display_name"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # [plan_contract] - 描述: invite_codes 新增套餐字段（plan_code/monitor_limit/grant_months）
    op.add_column(
        "invite_codes",
        sa.Column("plan_code", sa.String(length=32), nullable=True, comment="套餐代码 observe_20/research_50"),
    )
    op.add_column(
        "invite_codes",
        sa.Column("monitor_limit", sa.Integer(), nullable=True, comment="监控数量上限快照"),
    )
    op.add_column(
        "invite_codes",
        sa.Column("grant_months", sa.Integer(), nullable=True, comment="兑换后增加的自然月数"),
    )

    # [plan_contract] - 描述: memberships 新增套餐字段（plan_code/monitor_limit）
    op.add_column(
        "memberships",
        sa.Column("plan_code", sa.String(length=32), nullable=True, comment="当前套餐代码 observe_20/research_50"),
    )
    op.add_column(
        "memberships",
        sa.Column("monitor_limit", sa.Integer(), nullable=True, comment="当前套餐监控数量上限"),
    )

    # 回填旧 invite_codes：默认映射 observe_20 / monitor_limit=20 / grant_months=1（30天≈1月）
    op.execute(
        "UPDATE invite_codes SET plan_code='observe_20', monitor_limit=20, grant_months=1 "
        "WHERE plan_code IS NULL"
    )

    # 回填旧 memberships：默认映射 observe_20 / monitor_limit=20
    op.execute(
        "UPDATE memberships SET plan_code='observe_20', monitor_limit=20 "
        "WHERE plan_code IS NULL"
    )


def downgrade() -> None:
    # 回滚：删除 plan_contract 相关列
    op.drop_column("memberships", "monitor_limit")
    op.drop_column("memberships", "plan_code")
    op.drop_column("invite_codes", "grant_months")
    op.drop_column("invite_codes", "monitor_limit")
    op.drop_column("invite_codes", "plan_code")


if __name__ == "__main__":
    # 自测入口：验证 revision 链与函数定义（不连接数据库）
    assert revision == "044_plan_contract_fields"
    assert down_revision == "043_rename_dsa_selector_display_name"
    assert callable(upgrade)
    assert callable(downgrade)
    print(f"revision={revision}")
    print(f"down_revision={down_revision}")
    print("OK: 迁移文件验证通过")
