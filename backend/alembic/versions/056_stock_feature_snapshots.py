"""056 stock feature snapshots - 盘后特征快照持久化

Revision ID: 056_stock_feature_snapshots
Revises: 055_feishu_platform_app_only
Create Date: 2026-07-07

变更内容：
- 新增 stock_feature_snapshots 表
- 保存每只标的每个交易日的 point-in-time 结构/时序特征快照
- 支持 upsert 幂等写入与历史回补

设计说明：
- structural_payload / temporal_payload 保存完整因子输出
- summary_payload 保存前端列表用摘要
- degraded_reasons 记录数据不足等降级原因
- 不给 full payload 加 GIN 索引，优先节省磁盘

用法：
    cd backend && alembic upgrade head
    cd backend && alembic downgrade -1
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "056_stock_feature_snapshots"
down_revision: str | None = "055_feishu_platform_app_only"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "stock_feature_snapshots",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            comment="快照 ID",
        ),
        sa.Column(
            "instrument_id",
            UUID(as_uuid=True),
            sa.ForeignKey("instruments.id"),
            nullable=False,
            comment="股票 ID",
        ),
        sa.Column(
            "trade_date",
            sa.Date(),
            nullable=False,
            comment="业务交易日",
        ),
        sa.Column(
            "primary_timeframe",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'1d'"),
            comment="主时间周期",
        ),
        sa.Column(
            "secondary_timeframe",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'15m'"),
            comment="次时间周期",
        ),
        sa.Column(
            "adj",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'qfq'"),
            comment="复权方式",
        ),
        sa.Column(
            "schema_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
            comment="快照 schema 版本",
        ),
        sa.Column(
            "source_primary_bar_time",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="主周期数据源截止时间（日线为 trade_date 15:00+08:00）",
        ),
        sa.Column(
            "source_secondary_bar_time",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="次周期数据源截止时间（15m 为最后一根 15m bar 的 trade_time）",
        ),
        sa.Column(
            "structural_payload",
            JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="结构因子完整输出 JSONB",
        ),
        sa.Column(
            "temporal_payload",
            JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="时序特征完整输出 JSONB",
        ),
        sa.Column(
            "summary_payload",
            JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="前端列表用摘要 JSONB",
        ),
        sa.Column(
            "degraded_reasons",
            JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'"),
            comment="降级原因列表（如数据不足）",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="创建时间",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="更新时间",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "instrument_id",
            "trade_date",
            "primary_timeframe",
            "secondary_timeframe",
            "adj",
            "schema_version",
            name="uq_feature_snapshot_instrument_date_tf_adj_schema",
        ),
        sa.Index(
            "ix_feature_snapshot_trade_date_schema",
            "trade_date",
            "schema_version",
        ),
        sa.Index(
            "ix_feature_snapshot_instrument_date",
            "instrument_id",
            "trade_date",
            postgresql_using="btree",
            postgresql_ops={"trade_date": "desc"},
        ),
        sa.Index(
            "ix_feature_snapshot_date_instrument",
            "trade_date",
            "instrument_id",
        ),
    )


def downgrade() -> None:
    op.drop_table("stock_feature_snapshots")


if __name__ == "__main__":
    # 自测入口：验证 revision 链与函数定义（不连接数据库）
    assert revision == "056_stock_feature_snapshots"
    assert down_revision == "055_feishu_platform_app_only"
    assert callable(upgrade)
    assert callable(downgrade)
    print(f"revision={revision}")
    print(f"down_revision={down_revision}")
    print("OK: 迁移文件验证通过")
