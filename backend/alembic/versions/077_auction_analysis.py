"""077 auction_analysis - 竞价分析层（锚点+扫描+聚合+追踪）

Revision ID: 077_auction_analysis
Revises: 076_market_review_workbench
Create Date: 2026-07-30

变更内容（[CHANGE-20260730-018] 竞价分析完整链路）：
- 新增 7 张表实现竞价锚点→扫描→聚合→追踪完整链路
- auction_anchor_snapshots: 每日锚点快照（run 级状态）
- auction_anchor_items: 个股锚点（structure/chip/composite）
- auction_anchor_publications: 锚点发布指针
- auction_scan_runs: 竞价扫描 run（最终竞价/开盘验证）
- auction_instrument_results: 个股竞价结果（位置/事件/参与度）
- auction_scope_results: 板块/市场竞价聚合
- auction_event_trackings: 竞价事件生命周期追踪

设计原则：
- 所有表含 trade_date、algorithm_version、source_core_run_id、source_chip_run_id
- 复权版本 price_adjustment_version 可追溯
- coverage、status、reason_codes 标准化
- 唯一键、索引保证幂等
- 非破坏性：纯新增表
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "077_auction_analysis"
down_revision: str | None = "076_market_review_workbench"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. auction_anchor_snapshots — 每日锚点快照（run 级状态）
    op.create_table(
        "auction_anchor_snapshots",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False, comment="业务交易日"),
        sa.Column("source_core_run_id", UUID(as_uuid=True), nullable=False,
                  comment="输入 stock_core snapshot_run_id"),
        sa.Column("source_chip_run_id", UUID(as_uuid=True), nullable=True,
                  comment="输入 chip_consensus run_id（null=未完成）"),
        sa.Column("algorithm_version", sa.Text(), nullable=False,
                  comment="锚点算法版本"),
        sa.Column("price_adjustment_version", sa.Text(), nullable=False,
                  comment="复权因子版本"),
        sa.Column("status", sa.Text(), nullable=False,
                  comment="running/succeeded/failed/partial/structure_only"),
        sa.Column("eligible_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ready_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("coverage_ratio", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("missing_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("missing_reasons", JSONB, nullable=False, server_default="{}"),
        sa.Column("structure_anchor_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("chip_anchor_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("composite_anchor_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("trade_date", "algorithm_version", name="uq_auction_anchor_snap_date_ver"),
    )
    op.create_index("ix_auction_anchor_snapshots_trade_date", "auction_anchor_snapshots", ["trade_date"])
    op.create_index("ix_auction_anchor_snapshots_status", "auction_anchor_snapshots", ["status"])

    # 2. auction_anchor_items — 个股锚点
    # [P0-6 修复 2026-07-31] 旧唯一键 (trade_date, instrument_id, anchor_type, direction) 会
    # 吞掉同方向同类型的多个 OB/BOS。改为 (snapshot_id, instrument_id, anchor_key)，
    # anchor_key 由来源事件 ID 或子类型+序号唯一标识，保存全部有效锚点，
    # 扫描仅选择 is_active/priority_rank。
    # [P0-7 修复 2026-07-31] source 拆为 source_kind (core/chip) 和 source_run_id，
    # 不再用 UUID 字段同时表达锚点语义和运行版本。
    op.create_table(
        "auction_anchor_items",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("snapshot_id", UUID(as_uuid=True),
                  sa.ForeignKey("auction_anchor_snapshots.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("instrument_id", UUID(as_uuid=True), nullable=False,
                  comment="个股 instrument_id"),
        sa.Column("anchor_type", sa.Text(), nullable=False,
                  comment="structure/chip/composite"),
        sa.Column("anchor_key", sa.Text(), nullable=False,
                  comment="同股同 snapshot 内唯一键：bos_<event_id>/ob_<event_id>/poc/..."),
        sa.Column("anchor_subtype", sa.Text(), nullable=True,
                  comment="bos/choch/ob_created/trailing_top/trailing_bottom/poc/vah/val/cross/composite"),
        sa.Column("source_kind", sa.Text(), nullable=False,
                  comment="core/chip（composite 取 core）"),
        sa.Column("source_run_id", UUID(as_uuid=True), nullable=False,
                  comment="source_core_run_id 或 source_chip_run_id（不再混用 source 字段）"),
        sa.Column("source_event_id", UUID(as_uuid=True), nullable=True,
                  comment="关联结构/筹码事件 ID（如有）"),
        sa.Column("source_time", sa.DateTime(timezone=True), nullable=True,
                  comment="锚点来源事件时间（occurredAt 等）"),
        sa.Column("direction", sa.Text(), nullable=False, comment="up/down"),
        sa.Column("lower_price", sa.Numeric(12, 4), nullable=False),
        sa.Column("upper_price", sa.Numeric(12, 4), nullable=False),
        sa.Column("center_price", sa.Numeric(12, 4), nullable=False),
        sa.Column("strength", sa.Float(), nullable=False, comment="0.0-1.0"),
        sa.Column("priority_rank", sa.Integer(), nullable=True,
                  comment="活跃锚点优先级（lower=higher priority，扫描时按此排序）"),
        sa.Column("freshness", sa.Text(), nullable=False,
                  comment="fresh/stale/expired"),
        sa.Column("validity", sa.Text(), nullable=False,
                  comment="valid/invalid/invalidated"),
        sa.Column("price_adjustment_version", sa.Text(), nullable=False),
        sa.Column("structure_payload", JSONB, nullable=True,
                  comment="结构锚点扩展：high/low/bos/choch/ob/invalidation"),
        sa.Column("chip_payload", JSONB, nullable=True,
                  comment="筹码锚点扩展：upper_zone/lower_zone/main_peak/cross"),
        sa.Column("distance_at_close", sa.Numeric(12, 4), nullable=True,
                  comment="昨收价相对锚点中心的距离"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("reason_codes", JSONB, nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("snapshot_id", "instrument_id", "anchor_key",
                            name="uq_auction_anchor_items_snap_inst_key"),
    )
    op.create_index("ix_auction_anchor_items_trade_date", "auction_anchor_items", ["trade_date"])
    op.create_index("ix_auction_anchor_items_instrument", "auction_anchor_items", ["instrument_id"])
    op.create_index("ix_auction_anchor_items_snapshot", "auction_anchor_items", ["snapshot_id"])
    op.create_index("ix_auction_anchor_items_active", "auction_anchor_items", ["is_active"])
    op.create_index(
        "ix_auction_anchor_items_priority",
        "auction_anchor_items",
        ["snapshot_id", "instrument_id", "is_active", "priority_rank"],
    )

    # 3. auction_anchor_publications — 锚点发布指针
    op.create_table(
        "auction_anchor_publications",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("snapshot_id", UUID(as_uuid=True),
                  sa.ForeignKey("auction_anchor_snapshots.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("algorithm_version", sa.Text(), nullable=False),
        sa.Column("source_core_run_id", UUID(as_uuid=True), nullable=False),
        sa.Column("source_chip_run_id", UUID(as_uuid=True), nullable=True),
        sa.Column("coverage_ratio", sa.Float(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("superseded_by", UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("trade_date", "algorithm_version",
                            name="uq_auction_anchor_pub_date_ver"),
    )
    op.create_index("ix_auction_anchor_pub_trade_date", "auction_anchor_publications", ["trade_date"])

    # 4. auction_scan_runs — 竞价扫描 run
    # [P0-4 修复 2026-07-31] 新增 attempt_count 支持失败/部分成功重试（递增 attempt）；
    # fencing_epoch 用于租约过期 fencing 恢复（旧 Worker 写入被拒绝）。
    op.create_table(
        "auction_scan_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("auction_type", sa.Text(), nullable=False,
                  comment="final(最终竞价)/opening(开盘验证)"),
        sa.Column("source_anchor_snapshot_id", UUID(as_uuid=True),
                  sa.ForeignKey("auction_anchor_snapshots.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("source_anchor_publication_id", UUID(as_uuid=True),
                  sa.ForeignKey("auction_anchor_publications.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("algorithm_version", sa.Text(), nullable=False),
        sa.Column("price_adjustment_version", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False,
                  comment="queued/running/succeeded/failed/partial"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="1",
                  comment="尝试次数（succeeded/running 租约有效时不递增；failed/partial 重试递增）"),
        sa.Column("eligible_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ready_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("coverage_ratio", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("missing_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("missing_reasons", JSONB, nullable=False, server_default="{}"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("worker_id", sa.Text(), nullable=True),
        sa.Column("lease_epoch", sa.Integer(), nullable=True,
                  comment="lease fencing epoch（旧 Worker 写入被拒绝）"),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("trade_date", "auction_type", "algorithm_version",
                            name="uq_auction_scan_run_date_type_ver"),
    )
    op.create_index("ix_auction_scan_runs_trade_date", "auction_scan_runs", ["trade_date"])
    op.create_index("ix_auction_scan_runs_status", "auction_scan_runs", ["status"])
    op.create_index("ix_auction_scan_runs_type", "auction_scan_runs", ["auction_type"])

    # 5. auction_instrument_results — 个股竞价结果
    op.create_table(
        "auction_instrument_results",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("scan_run_id", UUID(as_uuid=True),
                  sa.ForeignKey("auction_scan_runs.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("instrument_id", UUID(as_uuid=True), nullable=False),
        sa.Column("final_auction_price", sa.Numeric(12, 4), nullable=True),
        sa.Column("prev_close", sa.Numeric(12, 4), nullable=True),
        sa.Column("change_pct", sa.Float(), nullable=True),
        sa.Column("auction_volume", sa.BigInteger(), nullable=True),
        sa.Column("auction_amount", sa.Numeric(18, 2), nullable=True),
        sa.Column("relative_volume_median_20d", sa.Float(), nullable=True),
        sa.Column("volume_percentile", sa.Float(), nullable=True),
        sa.Column("atr_distance_pct", sa.Float(), nullable=True,
                  comment="ATR标准化距离"),
        sa.Column("is_suspended", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_limit_up", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_limit_down", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_ex_right", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("structure_position", sa.Text(), nullable=True,
                  comment="below_low/below_trigger/demand_ob/normal/supply_ob/above_trigger/above_high"),
        sa.Column("chip_position", sa.Text(), nullable=True,
                  comment="below_lower/lower_zone/between/upper_zone/above_upper"),
        sa.Column("event_type", sa.Text(), nullable=True,
                  comment="dual_breakout/structure_breakout/chip_repricing/support_confirm/resistance_blocked/test_upper/test_lower/inside_open/insufficient_participation/structure_chip_conflict/anchor_insufficient/anchor_expired"),
        sa.Column("event_lifecycle", sa.Text(), nullable=True,
                  comment="formed/confirmed/weakened/failed/expired"),
        sa.Column("participation_level", sa.Text(), nullable=True,
                  comment="abnormal_low/low/normal/high/abnormal_high"),
        sa.Column("trend_background", sa.Text(), nullable=True,
                  comment="up/down/neutral"),
        sa.Column("anchor_ids", JSONB, nullable=True,
                  comment="关联锚点 ID 列表"),
        sa.Column("detail_payload", JSONB, nullable=True,
                  comment="详细分析数据"),
        sa.Column("reason_codes", JSONB, nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("scan_run_id", "instrument_id",
                            name="uq_auction_inst_res_run_inst"),
    )
    op.create_index("ix_auction_inst_res_trade_date", "auction_instrument_results", ["trade_date"])
    op.create_index("ix_auction_inst_res_instrument", "auction_instrument_results", ["instrument_id"])
    op.create_index("ix_auction_inst_res_event", "auction_instrument_results", ["event_type"])

    # 6. auction_scope_results — 板块/市场竞价聚合
    op.create_table(
        "auction_scope_results",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("scan_run_id", UUID(as_uuid=True),
                  sa.ForeignKey("auction_scan_runs.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("scope_type", sa.Text(), nullable=False,
                  comment="market/industry/concept"),
        sa.Column("scope_id", UUID(as_uuid=True), nullable=True,
                  comment="board_id（market 时为 null）"),
        sa.Column("scope_name", sa.Text(), nullable=True),
        sa.Column("total_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("valid_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("coverage_ratio", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("open_high_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("open_flat_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("open_low_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("median_change_pct", sa.Float(), nullable=True),
        sa.Column("p25_change_pct", sa.Float(), nullable=True),
        sa.Column("p75_change_pct", sa.Float(), nullable=True),
        sa.Column("equal_weight_change_pct", sa.Float(), nullable=True),
        sa.Column("amount_weight_change_pct", sa.Float(), nullable=True),
        sa.Column("structure_breakout_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("structure_breakdown_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("chip_cross_up_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("chip_cross_down_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("dual_breakout_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("dual_breakdown_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("resistance_zone_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("support_zone_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("participation_median", sa.Float(), nullable=True),
        sa.Column("abnormal_volume_pct", sa.Float(), nullable=True),
        sa.Column("top3_contribution", sa.Float(), nullable=True),
        sa.Column("top5_contribution", sa.Float(), nullable=True),
        sa.Column("hhi", sa.Float(), nullable=True),
        sa.Column("leader_median_gap", sa.Float(), nullable=True),
        sa.Column("positive_coverage", sa.Float(), nullable=True),
        sa.Column("negative_coverage", sa.Float(), nullable=True),
        sa.Column("dispersion", sa.Float(), nullable=True),
        sa.Column("status_label", sa.Text(), nullable=True,
                  comment="full_repricing/leader_driven/initial_diffusion/resistance_high_open/support_repair/full_breakdown/high_divergence/inconclusive"),
        sa.Column("confidence_level", sa.Text(), nullable=True,
                  comment="high/medium/low"),
        sa.Column("payload", JSONB, nullable=False, server_default="{}",
                  comment="完整统计含分子分母"),
        sa.Column("reason_codes", JSONB, nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("scan_run_id", "scope_type", "scope_id",
                            name="uq_auction_scope_res_run_type_scope"),
    )
    op.create_index("ix_auction_scope_res_trade_date", "auction_scope_results", ["trade_date"])
    op.create_index("ix_auction_scope_res_type", "auction_scope_results", ["scope_type"])
    op.create_index("ix_auction_scope_res_status", "auction_scope_results", ["status_label"])

    # 7. auction_event_trackings — 竞价事件生命周期追踪
    # [P0-5 修复 2026-07-31] 完整生命周期 formed/confirmed/continued/weakened/failed/transformed/expired。
    # update_event_lifecycle 在 confirmed 后再次判断 continued（开盘后窗口维持触发）
    # 或 weakened（回落至触发线之下）；并通过 AuctionScopeResult 判断板块扩散失败、
    # 龙头孤立、指数与中位数背离 → 触发 transformed。
    op.create_table(
        "auction_event_trackings",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("scan_run_id", UUID(as_uuid=True),
                  sa.ForeignKey("auction_scan_runs.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("instrument_id", UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False,
                  comment="dual_breakout/structure_breakout/chip_repricing/support_confirm/resistance_blocked/dual_breakdown/structure_breakdown/chip_loss"),
        sa.Column("lifecycle", sa.Text(), nullable=False,
                  comment="formed/confirmed/continued/weakened/failed/transformed/expired"),
        sa.Column("anchor_id", UUID(as_uuid=True), nullable=True,
                  comment="关联 auction_anchor_items.id"),
        sa.Column("trigger_price", sa.Numeric(12, 4), nullable=True),
        sa.Column("trigger_condition", sa.Text(), nullable=True),
        sa.Column("formed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("continued_at", sa.DateTime(timezone=True), nullable=True,
                  comment="confirmed 后开盘窗口维持触发时记录"),
        sa.Column("weakened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("transformed_at", sa.DateTime(timezone=True), nullable=True,
                  comment="板块扩散失败/龙头孤立/指数背离等结构性变化时记录"),
        sa.Column("expired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmation_data", JSONB, nullable=True),
        sa.Column("reason_codes", JSONB, nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("scan_run_id", "instrument_id", "event_type",
                            name="uq_auction_event_track_run_inst_type"),
    )
    op.create_index("ix_auction_event_track_trade_date", "auction_event_trackings", ["trade_date"])
    op.create_index("ix_auction_event_track_instrument", "auction_event_trackings", ["instrument_id"])
    op.create_index("ix_auction_event_track_lifecycle", "auction_event_trackings", ["lifecycle"])


def downgrade() -> None:
    op.drop_table("auction_event_trackings")
    op.drop_table("auction_scope_results")
    op.drop_table("auction_instrument_results")
    op.drop_table("auction_scan_runs")
    op.drop_table("auction_anchor_publications")
    op.drop_table("auction_anchor_items")
    op.drop_table("auction_anchor_snapshots")
