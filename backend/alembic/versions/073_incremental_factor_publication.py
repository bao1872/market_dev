"""073 incremental_factor_publication - 增量检查点/分层发布重构

Revision ID: 073_incremental_factor_publication
Revises: 072_first_pyramid_history
Create Date: 2026-07-29

变更内容（[CHANGE-20260729-006] 增量检查点/分层发布重构）：
- 新增 stock_feature_snapshot_run_items 表：单股×阶段粒度检查点，
  支持per-stock commit、失败隔离和断点恢复
- 新增 first_pyramid_history_runs 表：历史回补 run 级追踪
- 新增 first_pyramid_history_run_items 表：历史回补单股 item 级追踪
- 新增 factor_publications 表：分层发布指针，只做小事务原子切换，
  不复制结果数据

设计原则（ref/instruction.md）：
- batch 只控制吞吐/内存，不是完成或发布边界
- 单股结果 commit 成功后才标记 item succeeded
- 单股失败不回滚其他已成功股票
- publication 只指向覆盖率门禁通过的不可变 run
- 不同 run 的数据禁止混合

ID 合同统一：
- orchestrator_job_run_id = SchedulerJobRun.id（任务追踪）
- snapshot_run_id = StockFeatureSnapshotRun.id（数据版本）
- history_run_id = FirstPyramidHistoryRun.id（历史回补版本）
- chip.core_run_id = snapshot_run_id（不再指向 SchedulerJobRun.id）
- factor_publications.data_run_id = snapshot_run_id 或 history_run_id

非破坏性：
- 纯新增表，不修改 071/072 已有表结构
- 不删除旧快照、不重写历史、不改变已发布结果
- 部署后表为空，由新代码异步填充
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "073_incremental_factor_publication"
down_revision: str | None = "072_first_pyramid_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建 4 张新表。"""

    # 1. stock_feature_snapshot_run_items - 单股×阶段检查点
    op.create_table(
        "stock_feature_snapshot_run_items",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            comment="item ID",
        ),
        sa.Column(
            "snapshot_run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("stock_feature_snapshot_runs.id", ondelete="CASCADE"),
            nullable=False,
            comment="关联的 StockFeatureSnapshotRun.id（数据版本）",
        ),
        sa.Column(
            "instrument_id",
            UUID(as_uuid=True),
            sa.ForeignKey("instruments.id"),
            nullable=False,
            comment="股票 ID",
        ),
        sa.Column(
            "phase",
            sa.Text(),
            nullable=False,
            comment="阶段：core / event_outbox",
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default="pending",
            comment="状态：pending/running/succeeded/failed/skipped",
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="尝试次数，首次 0，自动 resume 递增",
        ),
        sa.Column(
            "input_hash",
            sa.Text(),
            nullable=True,
            comment="输入 bars hash（用于校验重跑一致性）",
        ),
        sa.Column(
            "worker_instance_id",
            sa.String(64),
            nullable=True,
            comment="Worker 实例标识 hostname:pid",
        ),
        sa.Column(
            "lease_epoch",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="租约代际，Worker 领取时递增，写操作校验防 fencing",
        ),
        sa.Column(
            "lease_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="租约过期时间",
        ),
        sa.Column(
            "result_count",
            sa.Integer(),
            nullable=True,
            comment="结果数（如 snapshot 写入数）",
        ),
        sa.Column(
            "last_error",
            sa.Text(),
            nullable=True,
            comment="失败原因",
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="实际开始时间",
        ),
        sa.Column(
            "heartbeat_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="心跳时间",
        ),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="完成时间",
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
            "snapshot_run_id",
            "instrument_id",
            "phase",
            name="uq_snapshot_run_items_run_instr_phase",
        ),
        sa.Index(
            "ix_snapshot_run_items_run_status",
            "snapshot_run_id",
            "status",
        ),
        sa.Index(
            "ix_snapshot_run_items_run_phase_status",
            "snapshot_run_id",
            "phase",
            "status",
        ),
        sa.Index(
            "ix_snapshot_run_items_lease_expires",
            "lease_expires_at",
            postgresql_where=sa.text("status = 'running'"),
        ),
    )

    # 2. first_pyramid_history_runs - 历史回补 run 级追踪
    op.create_table(
        "first_pyramid_history_runs",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            comment="history run ID",
        ),
        sa.Column(
            "scheduler_job_run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("scheduler_job_runs.id", ondelete="SET NULL"),
            nullable=True,
            comment="关联的 SchedulerJobRun.id（任务追踪，纯 metadata）",
        ),
        sa.Column(
            "algorithm_version",
            sa.Text(),
            nullable=False,
            comment="core 算法版本（FIRST_PYRAMID_CORE_ALGORITHM_VERSION）",
        ),
        sa.Column(
            "parameter_hash",
            sa.Text(),
            nullable=False,
            comment="参数 hash（output_bars + include_chip 等）",
        ),
        sa.Column(
            "output_bars",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("250"),
            comment="输出最近 N 个有效日",
        ),
        sa.Column(
            "scope",
            sa.Text(),
            nullable=False,
            server_default="all_a_share",
            comment="范围：all_a_share / canary / symbols",
        ),
        sa.Column(
            "expected_count",
            sa.Integer(),
            nullable=True,
            comment="预期 instrument 数量",
        ),
        sa.Column(
            "succeeded_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="成功 instrument 数量",
        ),
        sa.Column(
            "failed_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="失败 instrument 数量",
        ),
        sa.Column(
            "skipped_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="跳过 instrument 数量",
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default="running",
            comment="状态：running/partial/succeeded/failed",
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="开始时间",
        ),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="完成时间",
        ),
        sa.Column(
            "metadata_json",
            sa.Text(),
            nullable=True,
            comment="额外元数据 JSON",
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
        sa.Index(
            "ix_first_pyramid_history_runs_algo_ver",
            "algorithm_version",
        ),
        sa.Index(
            "ix_first_pyramid_history_runs_status",
            "status",
        ),
    )

    # 3. first_pyramid_history_run_items - 历史回补单股 item
    op.create_table(
        "first_pyramid_history_run_items",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            comment="item ID",
        ),
        sa.Column(
            "history_run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("first_pyramid_history_runs.id", ondelete="CASCADE"),
            nullable=False,
            comment="关联的 FirstPyramidHistoryRun.id",
        ),
        sa.Column(
            "instrument_id",
            UUID(as_uuid=True),
            sa.ForeignKey("instruments.id"),
            nullable=False,
            comment="股票 ID",
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default="pending",
            comment="状态：pending/running/succeeded/failed/skipped",
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="尝试次数",
        ),
        sa.Column(
            "input_hash",
            sa.Text(),
            nullable=True,
            comment="输入 bars hash",
        ),
        sa.Column(
            "worker_instance_id",
            sa.String(64),
            nullable=True,
            comment="Worker 实例标识 hostname:pid",
        ),
        sa.Column(
            "lease_epoch",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="租约代际，Worker 领取时递增，写操作校验防 fencing",
        ),
        sa.Column(
            "lease_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="租约过期时间",
        ),
        sa.Column(
            "daily_state_count",
            sa.Integer(),
            nullable=True,
            comment="写入的 daily_state 行数",
        ),
        sa.Column(
            "event_count",
            sa.Integer(),
            nullable=True,
            comment="写入的 event 行数",
        ),
        sa.Column(
            "last_error",
            sa.Text(),
            nullable=True,
            comment="失败原因",
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="实际开始时间",
        ),
        sa.Column(
            "heartbeat_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="心跳时间",
        ),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="完成时间",
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
            "history_run_id",
            "instrument_id",
            name="uq_history_run_items_run_instr",
        ),
        sa.Index(
            "ix_history_run_items_run_status",
            "history_run_id",
            "status",
        ),
        sa.Index(
            "ix_history_run_items_lease_expires",
            "lease_expires_at",
            postgresql_where=sa.text("status = 'running'"),
        ),
    )

    # 4. factor_publications - 分层发布指针
    op.create_table(
        "factor_publications",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            comment="publication ID",
        ),
        sa.Column(
            "scope_type",
            sa.Text(),
            nullable=False,
            comment="范围类型：market / instrument",
        ),
        sa.Column(
            "scope_key",
            sa.Text(),
            nullable=False,
            comment="范围键：market 全市场 / instrument_id 单股",
        ),
        sa.Column(
            "trade_date",
            sa.Date(),
            nullable=False,
            comment="业务交易日（所有 publication 都按交易日，禁止 NULL 避免普通唯一约束允许多 NULL）",
        ),
        sa.Column(
            "publication_kind",
            sa.Text(),
            nullable=False,
            comment="发布类型：stock_core / market_aggregation / history_cross_section",
        ),
        sa.Column(
            "algorithm_version",
            sa.Text(),
            nullable=False,
            comment="算法版本",
        ),
        sa.Column(
            "data_run_id",
            UUID(as_uuid=True),
            nullable=False,
            comment="指向的数据 run ID（snapshot_run_id 或 history_run_id）",
        ),
        sa.Column(
            "coverage_ratio",
            sa.Float(),
            nullable=True,
            comment="覆盖率（succeeded / expected）",
        ),
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="发布时间",
        ),
        sa.Column(
            "metadata_json",
            sa.Text(),
            nullable=True,
            comment="额外元数据 JSON",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="创建时间",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "scope_type",
            "scope_key",
            "trade_date",
            "publication_kind",
            name="uq_factor_publications_scope_date_kind",
        ),
        sa.Index(
            "ix_factor_publications_kind_date",
            "publication_kind",
            "trade_date",
        ),
        sa.Index(
            "ix_factor_publications_scope_kind",
            "scope_type",
            "scope_key",
            "publication_kind",
        ),
    )


def downgrade() -> None:
    op.drop_table("factor_publications")
    op.drop_table("first_pyramid_history_run_items")
    op.drop_table("first_pyramid_history_runs")
    op.drop_table("stock_feature_snapshot_run_items")
