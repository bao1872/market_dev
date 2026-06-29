"""046 trading_calendar source/status/verified_at

Revision ID: 046_calendar_source_status
Revises: 045_beta_applications
Create Date: 2026-06-29

变更内容：
- 为 trading_calendar 表增加三字段（advice.md v9 Task 1）：
  - source: VARCHAR(32), nullable=False, server_default='pytdx'
    数据来源标识（pytdx/weekday/holiday），用于自愈决策
  - status: VARCHAR(32), nullable=False, server_default='unknown'
    确认状态（confirmed_trading/confirmed_closed/unknown）
    - confirmed_trading: pytdx 已生成该日 K 线，权威交易日
    - confirmed_closed: 已知节假日或周末，权威非交易日
    - unknown: 未经权威数据确认（如盘前今日 K 线未生成、历史数据缺口）
  - verified_at: TIMESTAMPTZ, nullable=True
    最近一次被权威数据确认的时间戳

业务背景：
- 修复 build_full_year_calendar 对"今天"开盘前误判为非交易日的 bug
  （advice.md v9 Task 1：盘前 today K 线未生成时，is_trading_day 应保持 true + status=unknown）
- is_trading_day_async 自愈机制：DB status=unknown 时降级查询 pytdx 在线，
  避免把 DB 中未确认的 false 当权威结果
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "046_calendar_source_status"
down_revision: str | None = "045_beta_applications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # [Calendar] - 描述: 增加 source/status/verified_at 三字段
    # source/status 默认 'pytdx'/'unknown'，保证历史数据迁移后语义可识别
    op.add_column(
        "trading_calendar",
        sa.Column(
            "source",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'pytdx'"),
            comment="数据来源：pytdx/weekday/holiday",
        ),
    )
    op.add_column(
        "trading_calendar",
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'unknown'"),
            comment="确认状态：confirmed_trading/confirmed_closed/unknown",
        ),
    )
    op.add_column(
        "trading_calendar",
        sa.Column(
            "verified_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="最近一次被权威数据确认的时间戳",
        ),
    )


def downgrade() -> None:
    # 回滚：删除三字段
    op.drop_column("trading_calendar", "verified_at")
    op.drop_column("trading_calendar", "status")
    op.drop_column("trading_calendar", "source")


if __name__ == "__main__":
    # 自测入口：验证 revision 链与函数定义（不连接数据库）
    assert revision == "046_calendar_source_status"
    assert down_revision == "045_beta_applications"
    assert callable(upgrade)
    assert callable(downgrade)
    print(f"revision={revision}")
    print(f"down_revision={down_revision}")
    print("OK: 迁移文件验证通过")
