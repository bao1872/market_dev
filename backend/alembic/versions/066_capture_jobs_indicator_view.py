"""066 capture_jobs add indicator_view column

Revision ID: 066_capture_jobs_indicator_view
Revises: 065_instruments_factor_reconciliation
Create Date: 2026-07-20

变更内容（CHANGE-20260720-003 §三 三类监控独立飞书图片）：
- capture_jobs 新增 indicator_view TEXT 列，记录本次截图对应的指标视图
  - 'node_cluster'：筹码共识价（VolumeNodeMonitor）
  - 'bollinger'：布林带（BollingerMonitor）
  - 'smc'：SMC 结构（SmcMonitor）
- nullable=True 兼容历史数据（NULL 表示历史截图，未区分视图）
- 新写入的 CaptureJob 必须填充 indicator_view（由 monitor_batch_service /
  stock_detail_feishu_service 在创建 CaptureJob 时透传）

设计说明：
- 一张截图只渲染一个指标视图，禁止三类指标叠在同一张图
- 缓存键与输出文件名也包含 indicator_view，防止不同指标复用旧图
- 不加索引（按 message_group_id / status 查询已覆盖，不按 indicator_view 查询）

配合：
- app.constants.indicator_view.INDICATOR_VIEW_VALUES
- app.constants.capture.FEISHU_CAPTURE_PRESETS
- app.services.stock_capture_service._build_cache_key（缓存键含 indicator_view）
- app.services.monitor_batch_service._send_chart_images_via_outbox（自动映射）
- app.services.stock_detail_feishu_service.send_stock_detail_to_feishu（用户选择）

用法：
    cd backend && alembic upgrade head
    cd backend && alembic downgrade -1
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "066_capture_jobs_indicator_view"
down_revision: str | None = "065_instruments_factor_reconciliation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """添加 capture_jobs.indicator_view 列。"""
    op.add_column(
        "capture_jobs",
        sa.Column(
            "indicator_view",
            sa.Text(),
            nullable=True,
            comment="指标视图 node_cluster|bollinger|smc（历史数据为 NULL）",
        ),
    )


def downgrade() -> None:
    """移除 capture_jobs.indicator_view 列。"""
    op.drop_column("capture_jobs", "indicator_view")
