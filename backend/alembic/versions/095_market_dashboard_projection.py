"""095_market_dashboard_projection: Market Dashboard read projection schema

[PANJI-MARKET-DASHBOARD-P1-F0] 建立 Market Dashboard 最小 read projection schema。

背景（E2 真实只读 smoke 证据，非猜测）：
- 真实全量 UI 请求 21.55s，其中 bars SQL + ``.all()`` + DataFrame ≈ 15.75s（≈73%），
  峰值 RSS ≈ 1.84GB；交互页面不可能每次请求重算 ~193 万行 bars。
- 因此改为「盘后一次计算 → 落库轻量投影 → UI 只读聚合结果」。

本迁移只建立 schema：
- 不含 projection builder / scheduler / after-close / API / frontend；
- 不写入、不读取任何 projection 数据；
- 不新增 run table / status / publication / lineage / retry / cache_key。

两张表：
1. ``market_dashboard_market_daily``  全市场每日一行（PK trade_date）
2. ``market_dashboard_scope_daily``   最新 membership replay 的板块/概念投影
   PK (board_id, trade_date)，FK board_id -> market_boards.id ON DELETE CASCADE

语义约束（不得违反）：
- 只存 counts（valid/above）；**不存 ratio**。读取时 ratio = above_count / valid_count，
  避免「count 与 ratio 不一致」的双重事实。
- 不存 normalized_index / MA delta / scope name / scope type / hierarchy level
  （派生值或已有 SSOT）。
- scope row 保存 ``membership_version``：历史 scope row 属于 latest_snapshot_replay
  （而非真实历史 membership），read path 用它检测 stale projection。本迁移只存字段，
  不实现 stale handling。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "095_market_dashboard_projection"
down_revision: str | None = "094_invite_code_ciphertext"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# MA 窗口档位（与 build_dashboard_history 的 WINDOWS 语义一致）
_WINDOWS: tuple[int, ...] = (5, 10, 20, 50, 120)


def _count_columns() -> list[sa.Column]:
    """两张投影表共用的计数列（member / return / 每档 MA valid+above）。

    所有计数列 NOT NULL；``equal_weight_return`` 可空（fail-closed 无有效成员时）。
    """
    cols: list[sa.Column] = [
        sa.Column("member_count", sa.Integer(), nullable=False),
        sa.Column("valid_return_count", sa.Integer(), nullable=False),
        sa.Column("equal_weight_return", sa.Float(), nullable=True),
    ]
    for k in _WINDOWS:
        cols.append(sa.Column(f"ma{k}_valid_count", sa.Integer(), nullable=False))
        cols.append(sa.Column(f"ma{k}_above_count", sa.Integer(), nullable=False))
    return cols


def _count_checks(prefix: str) -> list[sa.CheckConstraint]:
    """数据不变量：counts 非负，且 above <= valid <= member、valid_return <= member。

    DB 层 CHECK 是投影正确性的最后防线；不依赖写入方自觉。
    """
    checks: list[sa.CheckConstraint] = [
        sa.CheckConstraint("member_count >= 0", name=f"{prefix}_member_count_check"),
        sa.CheckConstraint("valid_return_count >= 0", name=f"{prefix}_valid_return_count_check"),
        sa.CheckConstraint(
            "valid_return_count <= member_count",
            name=f"{prefix}_valid_return_le_member_check",
        ),
    ]
    for k in _WINDOWS:
        checks.append(
            sa.CheckConstraint(
                f"ma{k}_valid_count >= 0 AND ma{k}_above_count >= 0 "
                f"AND ma{k}_above_count <= ma{k}_valid_count "
                f"AND ma{k}_valid_count <= member_count",
                name=f"{prefix}_ma{k}_counts_check",
            )
        )
    return checks


def upgrade() -> None:
    # 1. 全市场每日投影
    op.create_table(
        "market_dashboard_market_daily",
        sa.Column("trade_date", sa.Date(), primary_key=True, nullable=False),
        *_count_columns(),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        *_count_checks("market_dashboard_market_daily"),
        comment="Market Dashboard 全市场每日投影（UI 只读；由盘后 builder 写）",
    )

    # 2. 板块/概念每日投影（latest_snapshot_replay membership）
    op.create_table(
        "market_dashboard_scope_daily",
        sa.Column("board_id", UUID(as_uuid=True), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("membership_version", sa.String(128), nullable=False),
        *_count_columns(),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("board_id", "trade_date"),
        sa.ForeignKeyConstraint(["board_id"], ["market_boards.id"], ondelete="CASCADE"),
        *_count_checks("market_dashboard_scope_daily"),
        comment="Market Dashboard 板块/概念每日投影（membership=latest_snapshot_replay）",
    )
    # 行业/概念列表页每天读取所有板块的 T 与 T-5，需要按 trade_date 过滤
    op.create_index(
        "ix_market_dashboard_scope_daily_trade_date",
        "market_dashboard_scope_daily",
        ["trade_date"],
    )


def downgrade() -> None:
    # 先 drop scope table（含 FK/index），再 drop market table，保证完整回滚
    op.drop_index(
        "ix_market_dashboard_scope_daily_trade_date",
        table_name="market_dashboard_scope_daily",
    )
    op.drop_table("market_dashboard_scope_daily")
    op.drop_table("market_dashboard_market_daily")


if __name__ == "__main__":
    assert revision == "095_market_dashboard_projection"
    assert down_revision == "094_invite_code_ciphertext"
    assert callable(upgrade)
    assert callable(downgrade)
    print(f"revision={revision}")
    print(f"down_revision={down_revision}")
    print("OK: 迁移文件验证通过")
