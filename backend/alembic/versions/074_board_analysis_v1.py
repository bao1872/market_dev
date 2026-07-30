"""074 board_analysis_v1 - 板块分析 V1

Revision ID: 074_board_analysis_v1
Revises: 073_incremental_factor_publication
Create Date: 2026-07-30

变更内容（[CHANGE-20260730-011] 板块分析 V1）：
- 新增 board_analysis_snapshots 表：板块级聚合分析结果
- 复用 factor_publications 表发布指针（publication_kind=market_aggregation,
  scope_type=board, scope_key=board_id::text, data_run_id=board_analysis_snapshot.id）
- 单表设计：每条记录既是 run 又是 snapshot（含 status/started_at/finished_at）
- 唯一键：(trade_date, board_id, algorithm_version) 保证幂等
- coverage_ratio >= 0.95 才可正式发布（写入 factor_publications）

输入门禁（PRD §板块分析 V1）：
- 只纳入 published stock_core pointer 同 run、core_factor_ready=true、
  valid_for_market_aggregation=true 的股票
- 数据不足股票进入 coverage 分母说明但不参与有效统计
- 行业与概念分开计算，成员和股票因子必须同一 trade_date
- 禁止使用未来数据

指标 payload 至少包括：
- 趋势上/下/中性比例、平均VWAP偏离、强度分布
- 主要/短线结构方向和BOS/CHoCH/OB/EQH/EQL事件率
- 挤压/释放、正负动量、增强/减弱比例
- 放量/缩量比例、20/200日分位分布
- 成员总数、有效数、缺失数、coverage和缺失原因

非破坏性：
- 纯新增表，不修改现有表结构
- 部署后表为空，由新代码异步填充
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "074_board_analysis_v1"
down_revision: str | None = "073_incremental_factor_publication"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "board_analysis_snapshots",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("trade_date", sa.Date(), nullable=False, comment="业务交易日"),
        sa.Column(
            "board_id",
            UUID(as_uuid=True),
            sa.ForeignKey("market_boards.id", ondelete="CASCADE"),
            nullable=False,
            comment="板块 ID（关联 market_boards.id）",
        ),
        sa.Column(
            "board_type",
            sa.Text(),
            nullable=False,
            comment="板块类型：industry | concept",
        ),
        sa.Column(
            "board_name",
            sa.Text(),
            nullable=False,
            comment="板块名称（冗余存储，便于查询展示，避免 JOIN）",
        ),
        sa.Column(
            "source_core_run_id",
            UUID(as_uuid=True),
            nullable=False,
            comment="输入 stock_core snapshot_run_id（factor_publications.data_run_id）",
        ),
        sa.Column(
            "algorithm_version",
            sa.Text(),
            nullable=False,
            comment="板块分析算法版本（用于幂等和参数 hash 关联）",
        ),
        sa.Column(
            "parameter_hash",
            sa.Text(),
            nullable=False,
            comment="参数 hash（含算法版本与固定参数，便于跨入口一致性校验）",
        ),
        sa.Column(
            "eligible_count",
            sa.Integer(),
            nullable=False,
            comment="板块成员总数（含数据不足股票）",
        ),
        sa.Column(
            "ready_count",
            sa.Integer(),
            nullable=False,
            comment="有效股票数（core_factor_ready=true 且同 source_core_run_id）",
        ),
        sa.Column(
            "coverage_ratio",
            sa.Float(),
            nullable=False,
            comment="覆盖率 = ready_count / eligible_count；>=0.95 才可正式发布",
        ),
        sa.Column(
            "missing_count",
            sa.Integer(),
            nullable=False,
            comment="缺失股票数（eligible - ready）",
        ),
        sa.Column(
            "missing_reasons",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="缺失原因分布 JSON：{reason_code: count}",
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            comment="状态：pending/running/succeeded/failed/partial",
        ),
        sa.Column(
            "payload",
            JSONB,
            nullable=False,
            comment="板块分析指标 payload JSON（趋势/结构/动量/量能/事件率分布）",
        ),
        sa.Column(
            "error_message",
            sa.Text(),
            nullable=True,
            comment="计算失败原因（status=failed 时填充）",
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="计算开始时间",
        ),
        sa.Column(
            "finished_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="计算完成时间",
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
            "board_id",
            "algorithm_version",
            name="uq_board_analysis_snapshots_date_board_ver",
        ),
        sa.Index(
            "ix_board_analysis_snapshots_date_type",
            "trade_date",
            "board_type",
        ),
        sa.Index(
            "ix_board_analysis_snapshots_board_date",
            "board_id",
            "trade_date",
        ),
        comment="板块分析 V1 快照表（单表设计，含 run 级状态字段）",
    )


def downgrade() -> None:
    op.drop_table("board_analysis_snapshots")
