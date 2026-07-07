"""057 stock feature snapshot runs - 特征快照运行记录与发布门禁

Revision ID: 057_stock_feature_snapshot_runs
Revises: 056_stock_feature_snapshots
Create Date: 2026-07-08

变更内容：
- 新增 stock_feature_snapshot_runs 表
- 记录每次 after_close/backfill/manual 快照计算的运行生命周期
- 状态机：running → succeeded/failed
- 仅 succeeded run 对应的 snapshot 行可被 watchlist 读取（publish gate）

设计说明：
- 唯一约束采用 PARTIAL UNIQUE INDEX（仅 status='running' 时唯一），
  允许失败后创建新 run 重试，历史 failed 记录保留审计。
- metadata_ 列名加下划线后缀避免与 SQLAlchemy 保留属性 metadata 冲突。
- 不给 JSONB 加 GIN 索引（run 表很小，每天 after_close 一条）。
- 仅在 (trade_date, status) 与 (trade_date, schema_version) 上建 btree 索引。

用法：
    cd backend && alembic upgrade head
    cd backend && alembic downgrade -1
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "057_stock_feature_snapshot_runs"
down_revision: str | None = "056_stock_feature_snapshots"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "stock_feature_snapshot_runs",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            comment="运行 ID",
        ),
        sa.Column(
            "trade_date",
            sa.Date(),
            nullable=False,
            comment="业务交易日",
        ),
        sa.Column(
            "schema_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
            comment="快照 schema 版本",
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
            "run_type",
            sa.Text(),
            nullable=False,
            comment="触发方式：after_close/backfill/manual",
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'running'"),
            comment="运行状态：running/succeeded/failed",
        ),
        sa.Column(
            "expected_count",
            sa.Integer(),
            nullable=True,
            comment="预期快照数（active A 股总数）",
        ),
        sa.Column(
            "snapshot_count",
            sa.Integer(),
            nullable=True,
            comment="实际写入快照数（含降级）",
        ),
        sa.Column(
            "failed_count",
            sa.Integer(),
            nullable=True,
            comment="失败股票数",
        ),
        sa.Column(
            "skipped_count",
            sa.Integer(),
            nullable=True,
            comment="跳过股票数（停牌/无数据）",
        ),
        sa.Column(
            "failure_rate",
            sa.Float(),
            nullable=True,
            comment="失败率 0.0-1.0",
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="开始时间",
        ),
        sa.Column(
            "finished_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="完成时间",
        ),
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="发布时间（succeeded 时写入，watchlist 据此判断是否可读）",
        ),
        sa.Column(
            "metadata_",
            JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="额外元数据 JSONB（如 failure_threshold、rollback_reason）",
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
    )
    # [Idempotency] - 部分唯一索引：仅约束 status='running' 的活跃记录，允许 failed 后重试
    op.create_index(
        "uq_snapshot_runs_active_key",
        "stock_feature_snapshot_runs",
        [
            "trade_date",
            "schema_version",
            "primary_timeframe",
            "secondary_timeframe",
            "adj",
            "run_type",
        ],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
    )
    op.create_index(
        "ix_snapshot_runs_trade_date_status",
        "stock_feature_snapshot_runs",
        ["trade_date", "status"],
    )
    op.create_index(
        "ix_snapshot_runs_trade_date_schema",
        "stock_feature_snapshot_runs",
        ["trade_date", "schema_version"],
    )


def downgrade() -> None:
    op.drop_table("stock_feature_snapshot_runs")


if __name__ == "__main__":
    # 自测入口：验证 revision 链与函数定义（不连接数据库）
    assert revision == "057_stock_feature_snapshot_runs"
    assert down_revision == "056_stock_feature_snapshots"
    assert callable(upgrade)
    assert callable(downgrade)
    print(f"revision={revision}")
    print(f"down_revision={down_revision}")
    print("OK: 迁移文件验证通过")
