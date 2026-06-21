"""新增筛选索引

为 strategy_results 和 strategy_result_metrics 增加筛选索引，
优化 SQL 端过滤查询性能。

索引说明：
- ix_results_run_date: strategy_results(run_id, trade_date)
  覆盖按 published run + trade_date 查询的场景
- ix_metrics_key_value_filter: strategy_result_metrics(metric_key, numeric_value)
  覆盖 EXISTS 子查询 WHERE metric_key = ? AND numeric_value >= ? 的场景
- ix_runs_status_strategy: strategy_runs(status, strategy_version_id)
  覆盖 Worker 轮询 queued run 的场景

Revision ID: 017_filter_indexes
Revises: 016_run_published_at
Create Date: 2026-06-20
"""
from alembic import op

revision = "017_filter_indexes"
down_revision = "016_run_published_at"


def upgrade():
    # 1. strategy_results: 按 run_id + trade_date 查询的筛选索引
    op.create_index(
        "ix_results_run_date",
        "strategy_results",
        ["run_id", "trade_date"],
    )

    # 2. strategy_result_metrics: 按 metric_key + numeric_value 筛选的复合索引
    #    覆盖 EXISTS 子查询：WHERE metric_key = ? AND numeric_value >= ?
    op.create_index(
        "ix_metrics_key_value_filter",
        "strategy_result_metrics",
        ["metric_key", "numeric_value"],
    )

    # 3. strategy_runs: 按 status + strategy_version_id 查询的索引（Worker 轮询用）
    op.create_index(
        "ix_runs_status_strategy",
        "strategy_runs",
        ["status", "strategy_version_id"],
    )


def downgrade():
    op.drop_index("ix_runs_status_strategy", "strategy_runs")
    op.drop_index("ix_metrics_key_value_filter", "strategy_result_metrics")
    op.drop_index("ix_results_run_date", "strategy_results")
