"""058 research feature matrix - 研究特征矩阵轻量宽表

Revision ID: 058_research_feature_matrix
Revises: 057_stock_feature_snapshot_runs
Create Date: 2026-07-08

变更内容：
- 新增 research_feature_matrix_runs 表（run 级元数据，按月分批）
- 新增 research_feature_matrix_rows 表（扁平宽表，33 个 feature 列，不存 JSON payload）
- 字段命名用下划线：causal_atr / confirmed_delay_confirmed_swing_high / hindsight_dsa_finalized_segment / label_future_return_10d
- registry 仍保留 dotted key（causal.atr），写 DB 时映射成下划线列名

设计说明：
- 与生产 stock_feature_snapshots 严格分离：研究矩阵不接入 watchlist_ready，不修改生产 snapshot
- 宽表设计（非 EAV），33 个 feature 列直接平铺，避免 JSONB payload
- 索引精简：unique(instrument_id, trade_date) + index(trade_date) + index(instrument_id) + index(run_id)
- 不给单个 feature 列建索引（后续按查询需求再加）
- metadata_json 只放小摘要（month/scope/notes），不存完整 payload
- 不建 GIN 索引

用法：
    cd backend && alembic upgrade head
    cd backend && alembic downgrade -1
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "058_research_feature_matrix"
down_revision: str | None = "057_stock_feature_snapshot_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# [FeatureColumns] - 33 个 feature 列定义 (column_name, sa_type, comment)
# 与 feature_causality_registry.db_column() 1:1 对应
_FLOAT_COLS: list[tuple[str, str]] = [
    ("causal_atr", "ATR 波动率"),
    ("causal_bb_percent_b", "BB %B"),
    ("causal_bb_bandwidth_pct", "BB 带宽百分比"),
    ("causal_sqzmom_val", "SQZMOM 动量值"),
    ("causal_sqzmom_delta_1", "SQZMOM 一阶差分"),
    ("causal_volume_ratio_20", "20 日成交量比率"),
    ("causal_volume_percentile_120", "120 日成交量百分位"),
    ("causal_active_swing_high", "active swing 高点"),
    ("causal_active_swing_low", "active swing 低点"),
    ("causal_developing_swing_high", "developing swing 高点"),
    ("causal_developing_swing_low", "developing swing 低点"),
    ("confirmed_delay_confirmed_swing_high", "已确认 swing 高点"),
    ("confirmed_delay_confirmed_swing_low", "已确认 swing 低点"),
    ("hindsight_node_cluster_support", "Node Cluster 支撑"),
    ("hindsight_node_cluster_resistance", "Node Cluster 阻力"),
    ("label_future_return_5d", "未来 5 日收益率"),
    ("label_future_return_10d", "未来 10 日收益率"),
    ("label_future_return_20d", "未来 20 日收益率"),
    ("label_future_max_drawdown_10d", "未来 10 日最大回撤"),
    ("label_future_max_drawdown_20d", "未来 20 日最大回撤"),
]
_TEXT_COLS: list[tuple[str, str]] = [
    ("causal_active_swing_dir", "active swing 方向"),
    ("causal_developing_swing_dir", "developing swing 方向"),
    ("causal_dsa_confirmed_direction", "DSA 方向（当时已确认）"),
    ("hindsight_dsa_finalized_direction", "DSA 方向（未来确认后）"),
    ("hindsight_node_cluster_label", "Node Cluster 结构标注"),
]
_INT_COLS: list[tuple[str, str]] = [
    ("causal_dsa_confirmed_segment", "DSA 段编号（当时已确认）"),
    ("causal_dsa_confirmed_age_bars", "DSA 段已持续 bar 数"),
    ("confirmed_delay_bars_since_confirmed_swing_high", "距确认 swing 高点 bar 数"),
    ("confirmed_delay_bars_since_confirmed_swing_low", "距确认 swing 低点 bar 数"),
    ("hindsight_dsa_finalized_segment", "DSA 段编号（未来确认后）"),
    ("hindsight_dsa_finalized_age_bars", "DSA 段最终持续 bar 数"),
    ("label_breakout_success_10d", "未来 10 日突破成功（0/1）"),
    ("label_failure_breakdown_10d", "未来 10 日破位失败（0/1）"),
]


def upgrade() -> None:
    # ===== research_feature_matrix_runs =====
    op.create_table(
        "research_feature_matrix_runs",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            comment="运行 ID",
        ),
        sa.Column("run_key", sa.Text(), nullable=False, comment="运行唯一键（如 2026-01_full）"),
        sa.Column("month", sa.Text(), nullable=False, comment="月份 YYYY-MM"),
        sa.Column("start_date", sa.Date(), nullable=False, comment="起始日期"),
        sa.Column("end_date", sa.Date(), nullable=False, comment="结束日期"),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'running'"),
            comment="运行状态：running/succeeded/failed",
        ),
        sa.Column("instruments_count", sa.Integer(), nullable=True, comment="股票数"),
        sa.Column("trade_dates_count", sa.Integer(), nullable=True, comment="交易日数"),
        sa.Column("rows_count", sa.Integer(), nullable=True, comment="写入行数"),
        sa.Column("failed_count", sa.Integer(), nullable=True, comment="失败行数"),
        sa.Column("duration_seconds", sa.Float(), nullable=True, comment="耗时秒"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True, comment="开始时间"),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True, comment="完成时间"),
        sa.Column(
            "metadata_json",
            JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="小摘要 JSONB（scope/notes/thresholds）",
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
        sa.UniqueConstraint("run_key", name="uq_research_matrix_runs_run_key"),
    )
    op.create_index(
        "ix_research_matrix_runs_month",
        "research_feature_matrix_runs",
        ["month"],
    )
    op.create_index(
        "ix_research_matrix_runs_status",
        "research_feature_matrix_runs",
        ["status"],
    )

    # ===== research_feature_matrix_rows（扁平宽表）=====
    op.create_table(
        "research_feature_matrix_rows",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            comment="行 ID",
        ),
        sa.Column("run_id", UUID(as_uuid=True), nullable=False, comment="所属 run ID"),
        sa.Column("instrument_id", UUID(as_uuid=True), nullable=False, comment="股票 ID"),
        sa.Column("symbol", sa.Text(), nullable=False, comment="股票代码"),
        sa.Column("trade_date", sa.Date(), nullable=False, comment="交易日"),
        # 33 个 feature 列（全部 nullable，warmup 期可为 NULL）
        *[
            sa.Column(name, sa.Float(), nullable=True, comment=comment)
            for name, comment in _FLOAT_COLS
        ],
        *[
            sa.Column(name, sa.Text(), nullable=True, comment=comment)
            for name, comment in _TEXT_COLS
        ],
        *[
            sa.Column(name, sa.Integer(), nullable=True, comment=comment)
            for name, comment in _INT_COLS
        ],
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="创建时间",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "instrument_id",
            "trade_date",
            name="uq_research_matrix_rows_inst_date",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["research_feature_matrix_runs.id"],
            name="fk_research_matrix_rows_run_id",
        ),
    )
    op.create_index(
        "ix_research_matrix_rows_trade_date",
        "research_feature_matrix_rows",
        ["trade_date"],
    )
    op.create_index(
        "ix_research_matrix_rows_instrument_id",
        "research_feature_matrix_rows",
        ["instrument_id"],
    )
    op.create_index(
        "ix_research_matrix_rows_run_id",
        "research_feature_matrix_rows",
        ["run_id"],
    )


def downgrade() -> None:
    op.drop_table("research_feature_matrix_rows")
    op.drop_table("research_feature_matrix_runs")


if __name__ == "__main__":
    # 自测入口：验证 revision 链与列数
    assert revision == "058_research_feature_matrix"
    assert down_revision == "057_stock_feature_snapshot_runs"
    total_cols = len(_FLOAT_COLS) + len(_TEXT_COLS) + len(_INT_COLS)
    assert total_cols == 33, f"expected 33 feature columns, got {total_cols}"
    # 验证列名唯一
    all_names = [c[0] for c in _FLOAT_COLS + _TEXT_COLS + _INT_COLS]
    assert len(all_names) == len(set(all_names)), "feature column names not unique"
    print(f"revision={revision}")
    print(f"down_revision={down_revision}")
    print(f"feature columns: {total_cols} (float={len(_FLOAT_COLS)}, text={len(_TEXT_COLS)}, int={len(_INT_COLS)})")
    print("OK: 迁移文件验证通过")
