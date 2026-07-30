"""076 market_review_workbench - 复盘模块工作台

Revision ID: 076_market_review_workbench
Revises: 075_market_data_quality
Create Date: 2026-07-30

变更内容（复盘模块 Phase 1 数据骨架，PRD §5.1-5.8）：
- 新增 market_review_runs：某交易日完整复盘版本（PRD §5.1）
- 新增 market_review_run_items：范围×阶段检查点（PRD §5.2）
- 新增 market_review_scope_snapshots：每个范围 P/Q/U/C/V 与证据（PRD §5.3）
- 新增 market_review_signals：三类偏差筛选器命中（PRD §5.4）
- 新增 market_review_signal_attributions：第二级范围下钻（PRD §5.5）
- 新增 market_review_signal_instruments：代表股票与贡献（PRD §5.6）
- 新增 market_review_trackings：用户追踪（PRD §5.7）
- 新增 market_review_tracking_evaluations：逐日追踪结果（PRD §5.8）

设计要点：
- 全部使用 UUID 主键（server_default=gen_random_uuid()）
- 状态枚举使用 CheckConstraint 约束（runs/items/signals/instruments/trackings）
- JSONB 字段用于 P/Q/U/C/V payload、证据、排序键等存储
- run→items/scope_snapshots/signals/evaluations 通过 ON DELETE CASCADE 级联
- trackings.source_signal_id / instrument_id 等可选外键使用 ON DELETE SET NULL
- market_review_signals.previous_signal_id / transformed_to_signal_id 自引用 SET NULL

非破坏性：
- 纯新增表，不修改 074/075 等已应用迁移
- 部署后表为空，由后续 Phase 1 代码（review_orchestrator/scope_service）异步填充
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "076_market_review_workbench"
down_revision: str | None = "075_market_data_quality"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. market_review_runs - 复盘 run 版本（PRD §5.1）
    op.create_table(
        "market_review_runs",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("trade_date", sa.Date(), nullable=False, comment="业务交易日"),
        sa.Column(
            "source_core_run_id",
            UUID(as_uuid=True),
            nullable=False,
            comment="输入 stock_core snapshot_run_id（factor_publications.data_run_id）",
        ),
        sa.Column(
            "source_board_run_id",
            UUID(as_uuid=True),
            nullable=False,
            comment="输入 board_analysis_snapshot 的 source_core_run_id",
        ),
        sa.Column(
            "algorithm_version",
            sa.Text(),
            nullable=False,
            comment="复盘算法版本（如 review-1.0.0）",
        ),
        sa.Column(
            "filter_version",
            sa.Text(),
            nullable=False,
            comment="筛选器配置版本（如 filters-1.0.0）",
        ),
        sa.Column(
            "baseline_window",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("120"),
            comment="历史基线窗口（默认120，最低60）",
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            comment=(
                "状态：created/computing/partial/signals_ready/published/"
                "completed_with_errors/failed/cancelled"
            ),
        ),
        sa.Column(
            "expected_scope_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="期望扫描范围总数",
        ),
        sa.Column(
            "succeeded_scope_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="扫描成功范围数",
        ),
        sa.Column(
            "failed_scope_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="扫描失败范围数",
        ),
        sa.Column(
            "signal_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="命中信号总数",
        ),
        sa.Column(
            "coverage_ratio",
            sa.Numeric(),
            nullable=False,
            comment="整体覆盖率 = succeeded_scope_count / expected_scope_count",
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="计算开始时间",
        ),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="计算完成时间",
        ),
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="发布时间（写入 factor_publications 的时间）",
        ),
        sa.Column(
            "metadata_json",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="run 级元数据 JSON",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "trade_date",
            "source_core_run_id",
            "source_board_run_id",
            "algorithm_version",
            "filter_version",
            name="uq_review_runs_date_core_board_algo_filter",
        ),
        sa.Index("ix_review_runs_status", "status"),
        sa.Index("ix_review_runs_trade_date", "trade_date"),
        sa.CheckConstraint(
            "status IN ("
            "'created','computing','partial','signals_ready','published',"
            "'completed_with_errors','failed','cancelled'"
            ")",
            name="review_runs_status_check",
        ),
        comment="复盘 run 版本表（某交易日完整复盘版本）",
    )

    # 2. market_review_run_items - 范围×阶段检查点（PRD §5.2）
    op.create_table(
        "market_review_run_items",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "review_run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("market_review_runs.id", ondelete="CASCADE"),
            nullable=False,
            comment="关联 market_review_runs.id",
        ),
        sa.Column(
            "scope_type",
            sa.Text(),
            nullable=False,
            comment=(
                "范围类型：market/major_index/style/industry_l1/"
                "industry_l2/industry_l3/concept/instrument"
            ),
        ),
        sa.Column(
            "scope_key",
            sa.Text(),
            nullable=False,
            comment="范围标识（如 industry_l1 的行业代码）",
        ),
        sa.Column(
            "phase",
            sa.Text(),
            nullable=False,
            comment="阶段：metrics/signals/attribution/tracking",
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
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
            comment="输入 hash（用于幂等校验，相同 hash+版本不重算）",
        ),
        sa.Column(
            "lease_epoch",
            sa.Integer(),
            nullable=True,
            comment="租约 epoch（并发 claim 用）",
        ),
        sa.Column(
            "lease_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="租约过期时间",
        ),
        sa.Column(
            "last_error",
            sa.Text(),
            nullable=True,
            comment="最近错误信息（status=failed 时填充）",
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="阶段开始时间",
        ),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="阶段完成时间",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "review_run_id",
            "scope_type",
            "scope_key",
            "phase",
            name="uq_review_items_run_scope_phase",
        ),
        sa.Index("ix_review_items_run_status", "review_run_id", "status"),
        sa.Index("ix_review_items_scope", "scope_type", "scope_key"),
        sa.CheckConstraint(
            "phase IN ('metrics','signals','attribution','tracking')",
            name="review_items_phase_check",
        ),
        sa.CheckConstraint(
            "status IN ('pending','running','succeeded','failed','skipped')",
            name="review_items_status_check",
        ),
        comment="复盘范围×阶段检查点表",
    )

    # 3. market_review_scope_snapshots - P/Q/U/C/V 快照（PRD §5.3）
    op.create_table(
        "market_review_scope_snapshots",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "review_run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("market_review_runs.id", ondelete="CASCADE"),
            nullable=False,
            comment="关联 market_review_runs.id",
        ),
        sa.Column("trade_date", sa.Date(), nullable=False, comment="业务交易日"),
        sa.Column(
            "scope_type",
            sa.Text(),
            nullable=False,
            comment="范围类型",
        ),
        sa.Column(
            "scope_key",
            sa.Text(),
            nullable=False,
            comment="范围标识",
        ),
        sa.Column(
            "scope_name",
            sa.Text(),
            nullable=False,
            comment="范围名称（冗余存储，便于查询展示）",
        ),
        sa.Column(
            "parent_scope_type",
            sa.Text(),
            nullable=True,
            comment="父范围类型（下钻时填充）",
        ),
        sa.Column(
            "parent_scope_key",
            sa.Text(),
            nullable=True,
            comment="父范围标识",
        ),
        sa.Column(
            "source_board_snapshot_id",
            UUID(as_uuid=True),
            sa.ForeignKey("board_analysis_snapshots.id", ondelete="SET NULL"),
            nullable=True,
            comment="关联 board_analysis_snapshots.id（行业/概念范围填充）",
        ),
        sa.Column(
            "eligible_count",
            sa.Integer(),
            nullable=False,
            comment="范围成员总数",
        ),
        sa.Column(
            "ready_count",
            sa.Integer(),
            nullable=False,
            comment="有效成员数",
        ),
        sa.Column(
            "coverage_ratio",
            sa.Numeric(),
            nullable=False,
            comment="覆盖率 = ready_count / eligible_count",
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            comment="快照状态：ready/insufficient_history/partial/unavailable",
        ),
        sa.Column(
            "p_payload",
            JSONB,
            nullable=True,
            comment="P 价格表现强度 payload（PRD §7.2）",
        ),
        sa.Column(
            "q_payload",
            JSONB,
            nullable=True,
            comment="Q 内部结构质量 payload（PRD §7.3）",
        ),
        sa.Column(
            "u_payload",
            JSONB,
            nullable=True,
            comment="U 参与范围 payload（PRD §7.4）",
        ),
        sa.Column(
            "c_payload",
            JSONB,
            nullable=True,
            comment="C 集中程度 payload（PRD §7.5）",
        ),
        sa.Column(
            "v_payload",
            JSONB,
            nullable=True,
            comment="V 成交活跃与效率 payload（PRD §7.6）",
        ),
        sa.Column(
            "data_quality_json",
            JSONB,
            nullable=True,
            comment="数据质量明细 JSON",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "review_run_id",
            "scope_type",
            "scope_key",
            name="uq_review_scope_snapshots_run_scope",
        ),
        sa.Index("ix_review_scope_snapshots_run_type", "review_run_id", "scope_type"),
        sa.Index("ix_review_scope_snapshots_date_type", "trade_date", "scope_type"),
        comment="复盘范围 P/Q/U/C/V 快照表",
    )

    # 4. market_review_signals - 三类筛选器命中（PRD §5.4）
    op.create_table(
        "market_review_signals",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "review_run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("market_review_runs.id", ondelete="CASCADE"),
            nullable=False,
            comment="关联 market_review_runs.id",
        ),
        sa.Column("trade_date", sa.Date(), nullable=False, comment="业务交易日"),
        sa.Column(
            "filter_family",
            sa.Text(),
            nullable=False,
            comment="筛选器族：A/B/C",
        ),
        sa.Column(
            "signal_type",
            sa.Text(),
            nullable=False,
            comment="信号类型（如 surface_strong_internal_weak）",
        ),
        sa.Column(
            "scope_type",
            sa.Text(),
            nullable=False,
            comment="范围类型",
        ),
        sa.Column(
            "scope_key",
            sa.Text(),
            nullable=False,
            comment="范围标识",
        ),
        sa.Column(
            "scope_name",
            sa.Text(),
            nullable=False,
            comment="范围名称（冗余存储）",
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            comment=(
                "信号生命周期状态："
                "new/continuing/confirmed/weakened/invalidated/transformed"
            ),
        ),
        sa.Column(
            "first_seen_date",
            sa.Date(),
            nullable=False,
            comment="信号首次出现日期",
        ),
        sa.Column(
            "previous_signal_id",
            UUID(as_uuid=True),
            sa.ForeignKey("market_review_signals.id", ondelete="SET NULL"),
            nullable=True,
            comment="前一交易日同 scope 同 signal_type 的信号 ID",
        ),
        sa.Column(
            "transformed_to_signal_id",
            UUID(as_uuid=True),
            sa.ForeignKey("market_review_signals.id", ondelete="SET NULL"),
            nullable=True,
            comment="转化后的新信号 ID（status=transformed 时填充）",
        ),
        sa.Column(
            "trigger_payload",
            JSONB,
            nullable=True,
            comment="触发条件 payload",
        ),
        sa.Column(
            "baseline_payload",
            JSONB,
            nullable=True,
            comment="基线 payload",
        ),
        sa.Column(
            "evidence_payload",
            JSONB,
            nullable=True,
            comment="证据 payload",
        ),
        sa.Column(
            "confirmation_rule",
            JSONB,
            nullable=True,
            comment="确认规则 JSON",
        ),
        sa.Column(
            "invalidation_rule",
            JSONB,
            nullable=True,
            comment="失效规则 JSON",
        ),
        sa.Column(
            "coverage_ratio",
            sa.Numeric(),
            nullable=True,
            comment="覆盖率",
        ),
        sa.Column(
            "rank_key",
            JSONB,
            nullable=True,
            comment=(
                "排序键 JSON：偏差历史分位/当日变化分位/持续日数/coverage/"
                "scope_type 优先级/scope_name"
            ),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "review_run_id",
            "filter_family",
            "signal_type",
            "scope_type",
            "scope_key",
            name="uq_review_signals_run_family_type_scope",
        ),
        sa.Index("ix_review_signals_run_scope", "review_run_id", "scope_type"),
        sa.Index("ix_review_signals_run_family", "review_run_id", "filter_family"),
        sa.Index("ix_review_signals_date_status", "trade_date", "status"),
        sa.Index("ix_review_signals_scope", "scope_type", "scope_key"),
        sa.CheckConstraint(
            "filter_family IN ('A','B','C')",
            name="review_signals_filter_family_check",
        ),
        sa.CheckConstraint(
            "status IN ("
            "'new','continuing','confirmed','weakened','invalidated','transformed'"
            ")",
            name="review_signals_status_check",
        ),
        comment="复盘三类偏差筛选器命中信号表",
    )

    # 5. market_review_signal_attributions - 子范围下钻（PRD §5.5）
    op.create_table(
        "market_review_signal_attributions",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "signal_id",
            UUID(as_uuid=True),
            sa.ForeignKey("market_review_signals.id", ondelete="CASCADE"),
            nullable=False,
            comment="关联 market_review_signals.id",
        ),
        sa.Column(
            "child_scope_type",
            sa.Text(),
            nullable=False,
            comment="子范围类型",
        ),
        sa.Column(
            "child_scope_key",
            sa.Text(),
            nullable=False,
            comment="子范围标识",
        ),
        sa.Column(
            "child_scope_name",
            sa.Text(),
            nullable=False,
            comment="子范围名称（冗余存储）",
        ),
        sa.Column(
            "relation_type",
            sa.Text(),
            nullable=True,
            comment="与父范围关系类型",
        ),
        sa.Column(
            "contribution_value",
            sa.Numeric(),
            nullable=True,
            comment="贡献值（可正可负）",
        ),
        sa.Column(
            "contribution_rank",
            sa.Integer(),
            nullable=True,
            comment="贡献排名（按绝对贡献排序）",
        ),
        sa.Column(
            "metrics_payload",
            JSONB,
            nullable=True,
            comment="子范围指标 payload",
        ),
        sa.Column(
            "evidence_payload",
            JSONB,
            nullable=True,
            comment="证据 payload",
        ),
        sa.Column(
            "coverage_ratio",
            sa.Numeric(),
            nullable=True,
            comment="覆盖率",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Index("ix_review_attributions_signal", "signal_id"),
        sa.Index(
            "ix_review_attributions_signal_rank",
            "signal_id",
            "contribution_rank",
        ),
        comment="复盘信号子范围归因表",
    )

    # 6. market_review_signal_instruments - 代表股票贡献（PRD §5.6）
    op.create_table(
        "market_review_signal_instruments",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "signal_id",
            UUID(as_uuid=True),
            sa.ForeignKey("market_review_signals.id", ondelete="CASCADE"),
            nullable=False,
            comment="关联 market_review_signals.id",
        ),
        sa.Column(
            "instrument_id",
            UUID(as_uuid=True),
            sa.ForeignKey("instruments.id"),
            nullable=False,
            comment="关联 instruments.id",
        ),
        sa.Column(
            "symbol",
            sa.Text(),
            nullable=False,
            comment="股票代码（冗余存储）",
        ),
        sa.Column(
            "name",
            sa.Text(),
            nullable=False,
            comment="股票名称（冗余存储）",
        ),
        sa.Column(
            "board_role",
            sa.Text(),
            nullable=True,
            comment=(
                "板块角色：core/second_line/elasticity/follower/laggard/unclassified"
            ),
        ),
        sa.Column(
            "relation_to_scope",
            sa.Text(),
            nullable=True,
            comment=(
                "与板块关系：synchronized_strengthening/synchronized_weakening/"
                "instrument_leads_scope/scope_strong_instrument_lags/"
                "instrument_strong_scope_unsupported/unconfirmed"
            ),
        ),
        sa.Column(
            "contribution_value",
            sa.Numeric(),
            nullable=True,
            comment="贡献值",
        ),
        sa.Column(
            "contribution_rank",
            sa.Integer(),
            nullable=True,
            comment="贡献排名",
        ),
        sa.Column(
            "first_pyramid_payload",
            JSONB,
            nullable=True,
            comment="第一金字塔 payload（趋势/结构/动量/筹码）",
        ),
        sa.Column(
            "fresh_events_payload",
            JSONB,
            nullable=True,
            comment="新鲜事件 payload",
        ),
        sa.Column(
            "source_snapshot_id",
            UUID(as_uuid=True),
            nullable=True,
            comment="来源快照 ID（stock_feature_snapshot_runs.id）",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Index("ix_review_instruments_signal", "signal_id"),
        sa.Index(
            "ix_review_instruments_signal_rank",
            "signal_id",
            "contribution_rank",
        ),
        sa.Index("ix_review_instruments_instrument", "instrument_id"),
        sa.CheckConstraint(
            "board_role IN ("
            "'core','second_line','elasticity','follower','laggard','unclassified'"
            ")",
            name="review_instruments_board_role_check",
        ),
        sa.CheckConstraint(
            "relation_to_scope IN ("
            "'synchronized_strengthening','synchronized_weakening',"
            "'instrument_leads_scope','scope_strong_instrument_lags',"
            "'instrument_strong_scope_unsupported','unconfirmed'"
            ")",
            name="review_instruments_relation_to_scope_check",
        ),
        comment="复盘信号代表股票与贡献表",
    )

    # 7. market_review_trackings - 用户追踪（PRD §5.7）
    op.create_table(
        "market_review_trackings",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            comment="关联 users.id",
        ),
        sa.Column(
            "source_signal_id",
            UUID(as_uuid=True),
            sa.ForeignKey("market_review_signals.id", ondelete="SET NULL"),
            nullable=True,
            comment="关联 market_review_signals.id（追踪 signal 时填充）",
        ),
        sa.Column(
            "tracking_type",
            sa.Text(),
            nullable=False,
            comment="追踪类型：signal/scope/instrument",
        ),
        sa.Column(
            "scope_type",
            sa.Text(),
            nullable=True,
            comment="范围类型（追踪 scope 时填充）",
        ),
        sa.Column(
            "scope_key",
            sa.Text(),
            nullable=True,
            comment="范围标识（追踪 scope 时填充）",
        ),
        sa.Column(
            "instrument_id",
            UUID(as_uuid=True),
            sa.ForeignKey("instruments.id", ondelete="SET NULL"),
            nullable=True,
            comment="关联 instruments.id（追踪 instrument 时填充）",
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            comment="状态：active/confirmed/invalidated/closed",
        ),
        sa.Column(
            "confirmation_conditions",
            JSONB,
            nullable=True,
            comment="确认条件 JSON",
        ),
        sa.Column(
            "invalidation_conditions",
            JSONB,
            nullable=True,
            comment="失效条件 JSON",
        ),
        sa.Column(
            "note",
            sa.Text(),
            nullable=True,
            comment="用户备注",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "closed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="关闭时间（status=closed 时填充）",
        ),
        sa.Index("ix_review_trackings_user_status", "user_id", "status"),
        sa.Index("ix_review_trackings_signal", "source_signal_id"),
        sa.Index("ix_review_trackings_instrument", "instrument_id"),
        sa.CheckConstraint(
            "tracking_type IN ('signal','scope','instrument')",
            name="review_trackings_tracking_type_check",
        ),
        sa.CheckConstraint(
            "status IN ('active','confirmed','invalidated','closed')",
            name="review_trackings_status_check",
        ),
        comment="复盘用户追踪表",
    )

    # 8. market_review_tracking_evaluations - 逐日追踪结果（PRD §5.8）
    op.create_table(
        "market_review_tracking_evaluations",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tracking_id",
            UUID(as_uuid=True),
            sa.ForeignKey("market_review_trackings.id", ondelete="CASCADE"),
            nullable=False,
            comment="关联 market_review_trackings.id",
        ),
        sa.Column(
            "review_run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("market_review_runs.id", ondelete="CASCADE"),
            nullable=False,
            comment="关联 market_review_runs.id",
        ),
        sa.Column("trade_date", sa.Date(), nullable=False, comment="业务交易日"),
        sa.Column(
            "previous_state",
            sa.Text(),
            nullable=True,
            comment="前一交易日状态",
        ),
        sa.Column(
            "current_state",
            sa.Text(),
            nullable=False,
            comment="当日状态",
        ),
        sa.Column(
            "evaluation_payload",
            JSONB,
            nullable=True,
            comment="评估 payload（证据与触发条件检查结果）",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "tracking_id",
            "trade_date",
            name="uq_review_evaluations_tracking_date",
        ),
        sa.Index("ix_review_evaluations_run", "review_run_id"),
        comment="复盘逐日追踪评估表",
    )


def downgrade() -> None:
    op.drop_table("market_review_tracking_evaluations")
    op.drop_table("market_review_trackings")
    op.drop_table("market_review_signal_instruments")
    op.drop_table("market_review_signal_attributions")
    op.drop_table("market_review_signals")
    op.drop_table("market_review_scope_snapshots")
    op.drop_table("market_review_run_items")
    op.drop_table("market_review_runs")
