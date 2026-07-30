"""075 market_data_quality - 全市场行情扫描与修复

Revision ID: 075_market_data_quality
Revises: 074_board_analysis_v1
Create Date: 2026-07-30

变更内容（Stage 5 P0 全市场行情扫描与修复）：
- 新增 market_data_quality_runs 表：run 级元数据，含 run_key 幂等键
- 新增 market_data_quality_items 表：per-instrument 检查明细
- run_key 格式："mdq:{timeframe}:{start}:{end}:{algorithm_version}"
- algorithm_version 常量 "mdq-v1.0.0"，parameter_hash 关联固定参数
- 支持 1d / 15m 两个 timeframe，分别扫描 bars_daily / bars_15min
- 修复模式（repair_mode=True）仅对 classification=DB_MISSING 的 item 执行 upsert

数据安全：
- 只写 raw OHLCV，不写 qfq 价格
- 修复使用 ON CONFLICT DO UPDATE，不覆盖整张表
- factor 重算委托 adjustment_factor_calculator.calculate_adjustment_factor_series

非破坏性：
- 纯新增表，不修改 074 等已有迁移
- 部署后表为空，由 CLI（scripts/market_data_quality_cli.py）异步填充
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "075_market_data_quality"
down_revision: str | None = "074_board_analysis_v1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---- run 级表 ----
    op.create_table(
        "market_data_quality_runs",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "run_key",
            sa.Text(),
            nullable=False,
            comment="幂等键，格式 mdq:{timeframe}:{start}:{end}:{algorithm_version}",
        ),
        sa.Column(
            "timeframe",
            sa.Text(),
            nullable=False,
            comment="周期：1d | 15m",
        ),
        sa.Column(
            "start_date",
            sa.Date(),
            nullable=False,
            comment="扫描起始日期（含）",
        ),
        sa.Column(
            "end_date",
            sa.Date(),
            nullable=False,
            comment="扫描结束日期（含）",
        ),
        sa.Column(
            "algorithm_version",
            sa.Text(),
            nullable=False,
            comment="扫描算法版本（常量 mdq-v1.0.0）",
        ),
        sa.Column(
            "parameter_hash",
            sa.Text(),
            nullable=False,
            comment="参数 hash（含算法版本与固定参数，便于跨入口一致性校验）",
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            comment="状态：created/running/succeeded/partial/failed/cancelled",
        ),
        sa.Column(
            "total_instruments",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="待扫描标的总数（活跃 A 股）",
        ),
        sa.Column(
            "succeeded_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="扫描成功数",
        ),
        sa.Column(
            "failed_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="扫描失败数",
        ),
        sa.Column(
            "skipped_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="扫描跳过数",
        ),
        sa.Column(
            "coverage_ratio",
            sa.Float(),
            nullable=False,
            server_default=sa.text("0.0"),
            comment="覆盖率 = succeeded / total",
        ),
        sa.Column(
            "issue_summary",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="issue 分布 JSON：{issue_type: count}",
        ),
        sa.Column(
            "repair_mode",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="是否启用修复模式（仅 DB_MISSING 触发修复）",
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="扫描开始时间",
        ),
        sa.Column(
            "finished_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="扫描完成时间",
        ),
        sa.Column(
            "error_message",
            sa.Text(),
            nullable=True,
            comment="run 级失败原因（status=failed 时填充）",
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
            "run_key",
            name="uq_mdq_runs_run_key",
        ),
        sa.Index(
            "ix_mdq_runs_status",
            "status",
        ),
        sa.Index(
            "ix_mdq_runs_timeframe_date",
            "timeframe",
            "start_date",
            "end_date",
        ),
        comment="全市场行情质量扫描 run 级表（幂等键 run_key）",
    )

    # ---- per-instrument 检查明细表 ----
    op.create_table(
        "market_data_quality_items",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("market_data_quality_runs.id", ondelete="CASCADE"),
            nullable=False,
            comment="关联 market_data_quality_runs.id",
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
            comment="股票代码（冗余存储，便于查询展示）",
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            comment="状态：pending/running/succeeded/failed/skipped",
        ),
        sa.Column(
            "issue_type",
            sa.Text(),
            nullable=True,
            comment=(
                "问题类型：NO_ISSUE/INTERNAL_GAP/TAIL_GAP/DUPLICATE/TIME_REVERSED/"
                "OHLC_INVALID/VOLUME_ANOMALY/AMOUNT_ANOMALY/FACTOR_MISSING/"
                "FACTOR_ANOMALY/BAR_COUNT_INSUFFICIENT"
            ),
        ),
        sa.Column(
            "issue_reason",
            sa.Text(),
            nullable=True,
            comment="问题说明（自由文本，如 missing dates: 2026-02-15, 2026-02-16）",
        ),
        sa.Column(
            "severity",
            sa.Text(),
            nullable=True,
            comment="严重程度：info/warning/error",
        ),
        sa.Column(
            "missing_dates",
            JSONB,
            nullable=True,
            comment="缺失日期列表（ISO 字符串数组）",
        ),
        sa.Column(
            "duplicate_dates",
            JSONB,
            nullable=True,
            comment="重复日期列表（ISO 字符串数组）",
        ),
        sa.Column(
            "first_bar_date",
            sa.Date(),
            nullable=True,
            comment="首条 bar 日期",
        ),
        sa.Column(
            "last_bar_date",
            sa.Date(),
            nullable=True,
            comment="末条 bar 日期",
        ),
        sa.Column(
            "bar_count",
            sa.Integer(),
            nullable=True,
            comment="实际 bar 数",
        ),
        sa.Column(
            "expected_bar_count",
            sa.Integer(),
            nullable=True,
            comment="期望 bar 数（交易日历覆盖数）",
        ),
        sa.Column(
            "factor_min",
            sa.Numeric(precision=20, scale=8),
            nullable=True,
            comment="adj_factor 最小值",
        ),
        sa.Column(
            "factor_max",
            sa.Numeric(precision=20, scale=8),
            nullable=True,
            comment="adj_factor 最大值",
        ),
        sa.Column(
            "factor_anomaly_count",
            sa.Integer(),
            nullable=True,
            comment="adj_factor 异常跳变次数",
        ),
        sa.Column(
            "classification",
            sa.Text(),
            nullable=True,
            comment=(
                "分类：NOT_LISTED/SUSPENDED/DELISTED/SOURCE_MISSING/"
                "DB_MISSING/FACTOR_MISSING/OK"
            ),
        ),
        sa.Column(
            "repair_attempted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="是否尝试过修复",
        ),
        sa.Column(
            "repair_status",
            sa.Text(),
            nullable=True,
            comment="修复状态：pending/succeeded/failed/skipped",
        ),
        sa.Column(
            "repair_message",
            sa.Text(),
            nullable=True,
            comment="修复说明（自由文本）",
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="单标的扫描开始时间",
        ),
        sa.Column(
            "finished_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="单标的扫描完成时间",
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
            "run_id",
            "instrument_id",
            name="uq_mdq_items_run_instrument",
        ),
        sa.Index(
            "ix_mdq_items_run_status",
            "run_id",
            "status",
        ),
        sa.Index(
            "ix_mdq_items_issue_type",
            "issue_type",
        ),
        sa.Index(
            "ix_mdq_items_classification",
            "classification",
        ),
        comment="全市场行情质量扫描 per-instrument 检查明细表",
    )


def downgrade() -> None:
    op.drop_table("market_data_quality_items")
    op.drop_table("market_data_quality_runs")
