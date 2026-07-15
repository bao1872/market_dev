"""063 instruments add total_share / float_share / share_as_of columns

Revision ID: 063_instruments_share_capital
Revises: 062_market_boards
Create Date: 2026-07-13

变更内容：
- instruments 表新增 total_share NUMERIC(20,0) 列（总股本，可空）
- instruments 表新增 float_share NUMERIC(20,0) 列（流通股本，可空）
- instruments 表新增 share_as_of DATE 列（股本数据日期，可空）

设计说明（CHANGE-20260713-010）：
- 数据源：pytdx get_finance_info（已声明依赖，已用于 bars/quotes/xdxr）
- 同步链：每日 bars_refresh 后由 instrument_share_sync_service 同步
- 用户请求时禁止第三方联网：quote 端点只从 DB 读取股本，不调用 pytdx
- 市值计算：total_market_cap = total_share × close_price（同一 as_of）
- 不做历史回填：仅同步当前股本数据，forward-only
- 列设为可空：兼容同步前历史数据，缺失时 quote 返回 null + degraded_reason

用法：
    cd backend && alembic upgrade head
    cd backend && alembic downgrade -1
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "063_instruments_share_capital"
down_revision: str | None = "062_market_boards"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # [Instrument] - 总股本（pytdx zongguben，单位：股）
    op.add_column(
        "instruments",
        sa.Column("total_share", sa.Numeric(precision=20, scale=0), nullable=True),
    )
    # [Instrument] - 流通股本（pytdx liutongguben，单位：股）
    op.add_column(
        "instruments",
        sa.Column("float_share", sa.Numeric(precision=20, scale=0), nullable=True),
    )
    # [Instrument] - 股本数据日期（pytdx updated_date，YYYYMMDD → date）
    op.add_column(
        "instruments",
        sa.Column("share_as_of", sa.Date(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("instruments", "share_as_of")
    op.drop_column("instruments", "float_share")
    op.drop_column("instruments", "total_share")


if __name__ == "__main__":
    # 自测入口：验证 revision 链与函数定义（不连接数据库）
    assert revision == "063_instruments_share_capital"
    assert down_revision == "062_market_boards"
    assert callable(upgrade)
    assert callable(downgrade)
    print(f"revision={revision}")
    print(f"down_revision={down_revision}")
    print("OK: 迁移文件验证通过")
