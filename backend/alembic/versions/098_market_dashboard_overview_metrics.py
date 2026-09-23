"""098_market_dashboard_overview_metrics: 扩展全市场每日投影

[PANJI-MARKET-OVERVIEW] 在大盘 Overview 升级中，向 ``market_dashboard_market_daily``
追加「今日市场快照 + 250 日轨迹」所需的 additive 列。

冻结合同：
- 全部为可空列；旧投影行保持 NULL（前端以「—」呈现，绝不伪造 0）。
- **不**追加 CHECK 约束：新列可空，且值由盘后 builder 一次性计算，DB 层不强制
  非负（NULL 与真实 0 必须可区分；旧行即 NULL）。
- 不改动 095 已有的 count / MA 列与约束；不另造第二张市场投影表。
- 新列语义（写入方 = 盘后 ``rebuilding_market_dashboard``）：
  - advance_count / decline_count / flat_count / change_valid_count
        涨跌家数（close vs 前收，exact-T，全市场 universe）
  - turnover_amount / turnover_valid_count
        全市场成交额 = SUM(bars_daily.amount)（元；NULL=部分不可用，不伪装总额）
  - limit_up_count / limit_down_count
        通达信口径涨跌停家数（880006 decoded：close→涨停, open→跌停, scale=1）
  - sse_close / szse_close / chinext_close
        上证 / 深证 / 创业板 指数收盘（pytdx 000001/399001/399006，事务外拉取）
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "098_market_dashboard_overview_metrics"
down_revision: str | None = "097_split_market_review_capability"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "market_dashboard_market_daily"

# (列名, 类型) —— 全部可空，旧行兼容为 NULL。
_NEW_COLUMNS: tuple[tuple[str, sa.TypeEngine], ...] = (
    ("advance_count", sa.Integer()),
    ("decline_count", sa.Integer()),
    ("flat_count", sa.Integer()),
    ("change_valid_count", sa.Integer()),
    ("turnover_amount", sa.Float()),
    ("turnover_valid_count", sa.Integer()),
    ("limit_up_count", sa.Integer()),
    ("limit_down_count", sa.Integer()),
    ("sse_close", sa.Float()),
    ("szse_close", sa.Float()),
    ("chinext_close", sa.Float()),
)


def upgrade() -> None:
    for name, col_type in _NEW_COLUMNS:
        # 若已存在（幂等重跑）则跳过，避免 alembic 在重复迁移时失败。
        op.add_column(_TABLE, sa.Column(name, col_type, nullable=True))


def downgrade() -> None:
    for name, _col_type in reversed(_NEW_COLUMNS):
        op.drop_column(_TABLE, name)


if __name__ == "__main__":
    assert revision == "098_market_dashboard_overview_metrics"
    assert down_revision == "097_split_market_review_capability"
    assert callable(upgrade)
    assert callable(downgrade)
    print(f"revision={revision}")
    print(f"down_revision={down_revision}")
    print("OK: 迁移文件验证通过")
