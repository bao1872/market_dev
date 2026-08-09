"""Review historical lineage (M2)

Revision ID: 088_review_historical_lineage
Revises: 087_stock_core_atomic_publication
Create Date: 2026-08-08

[M2] Review historical lineage migration：
为 Review historical replay 增加 dual-lineage（LIVE / HISTORY_REPLAY）持久化。

1. first_pyramid_history_daily_state
   - 加 source_history_run_id（UUID NULL FK -> first_pyramid_history_runs.id, ON DELETE SET NULL）
   - 加 history_contract_version（TEXT NULL）
   - 旧行允许 NULL；新 review-history-v2 replay 必须写两者。

2. market_review_metric_observations
   - 加 source_kind（TEXT NOT NULL default 'live'）
   - review_run_id 改可空（DROP NOT NULL）
   - 加 source_history_run_id（UUID NULL FK -> first_pyramid_history_runs.id）
   - 加 history_contract_version（TEXT NULL）
   - 加 taxonomy_compatibility_key（TEXT NULL）
   - 加 CHECK ck_review_observation_dual_lineage：
       (source_kind='live' AND review_run_id NOT NULL AND source_history_run_id NULL) OR
       (source_kind='history_replay' AND review_run_id NULL AND source_history_run_id NOT NULL)
   - drop 现有普通 UNIQUE uq_review_metric_observation_run_scope_component
   - 建 LIVE partial unique index + HISTORY_REPLAY partial unique index

3. first_pyramid_history_events
   - 加 history_contract_version（TEXT NULL，versioned event 写 review-history-v2）
   - 事件唯一性 contract-aware：drop 旧三字段 UNIQUE
     uq_first_pyramid_history_events_instr_ver_evid
   - 建 legacy partial unique index（WHERE history_contract_version IS NULL）+
     versioned partial unique index（WHERE history_contract_version IS NOT NULL）
   - downgrade 前置检查：存在 versioned event 必须 fail fast（不自动删 v2 数据）

风险：M2。obs 表当前 0 行（无数据迁移风险）；改现有列 NOT NULL→可空需表锁（表小）。
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "088_review_historical_lineage"
down_revision = "087_stock_core_atomic_publication"
branch_labels = None
depends_on = None

_OBS_TABLE = "market_review_metric_observations"
_STATE_TABLE = "first_pyramid_history_daily_state"
_HIST_RUN_TABLE = "first_pyramid_history_runs"
_OLD_UQ = "uq_review_metric_observation_run_scope_component"
_NEW_CHECK = "ck_review_observation_dual_lineage"
_LIVE_IDX = "uq_review_obs_live_run_scope_component"
_REPLAY_IDX = "uq_review_obs_replay_run_date_scope_component"
_EVENTS_TABLE = "first_pyramid_history_events"
_OLD_EVENT_UQ = "uq_first_pyramid_history_events_instr_ver_evid"
_LEGACY_EVENT_IDX = "uq_first_pyramid_history_events_instr_ver_evid"
_VERSIONED_EVENT_IDX = "uq_first_pyramid_history_events_instr_ver_cv_evid"


def upgrade() -> None:
    # ---- 1. daily_state lineage ----
    op.add_column(
        _STATE_TABLE,
        sa.Column(
            "source_history_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(_HIST_RUN_TABLE + ".id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        _STATE_TABLE,
        sa.Column("history_contract_version", sa.Text(), nullable=True),
    )
    # [CHANGE-20260808] event contract isolation（§7）：旧 NULL/v1 事件与 v2 事件
    # 双计防护。新 review-history-v2 events 必须写 review-history-v2。
    op.add_column(
        _EVENTS_TABLE,
        sa.Column("history_contract_version", sa.Text(), nullable=True),
    )
    # [CHANGE-20260808] 事件唯一性 contract-aware：普通 UNIQUE 无法表达
    # "旧 NULL event 与 v2 event 若 event_id 相同可共存"。drop 旧三字段 UNIQUE，
    # 改两个 partial unique index：
    #   - legacy：UNIQUE(instrument_id, algorithm_version, event_id)
    #             WHERE history_contract_version IS NULL
    #   - versioned：UNIQUE(instrument_id, algorithm_version, history_contract_version, event_id)
    #             WHERE history_contract_version IS NOT NULL
    op.drop_constraint(_OLD_EVENT_UQ, _EVENTS_TABLE, type_="unique")
    op.create_index(
        _LEGACY_EVENT_IDX,
        _EVENTS_TABLE,
        ["instrument_id", "algorithm_version", "event_id"],
        unique=True,
        postgresql_where=sa.text("history_contract_version IS NULL"),
    )
    op.create_index(
        _VERSIONED_EVENT_IDX,
        _EVENTS_TABLE,
        ["instrument_id", "algorithm_version", "history_contract_version", "event_id"],
        unique=True,
        postgresql_where=sa.text("history_contract_version IS NOT NULL"),
    )

    # ---- 2. observation dual lineage ----
    op.add_column(
        _OBS_TABLE,
        sa.Column(
            "source_kind",
            sa.Text(),
            nullable=False,
            server_default="live",
        ),
    )
    # review_run_id NOT NULL → 可空
    op.alter_column(_OBS_TABLE, "review_run_id", nullable=True)
    op.add_column(
        _OBS_TABLE,
        sa.Column(
            "source_history_run_id",
            postgresql.UUID(as_uuid=True),
            # FK delete=RESTRICT：lineage source 不得静默消失（否则破坏 replay CHECK 溯源）。
            sa.ForeignKey(_HIST_RUN_TABLE + ".id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    op.add_column(
        _OBS_TABLE,
        sa.Column("history_contract_version", sa.Text(), nullable=True),
    )
    op.add_column(
        _OBS_TABLE,
        sa.Column("taxonomy_compatibility_key", sa.Text(), nullable=True),
    )
    # dual-lineage CHECK
    op.create_check_constraint(
        _NEW_CHECK,
        _OBS_TABLE,
        "((source_kind = 'live' AND review_run_id IS NOT NULL "
        "AND source_history_run_id IS NULL) OR "
        "(source_kind = 'history_replay' AND review_run_id IS NULL "
        "AND source_history_run_id IS NOT NULL))",
    )
    # drop 普通 UNIQUE，替换为两类 partial unique index
    op.drop_constraint(_OLD_UQ, _OBS_TABLE, type_="unique")
    op.create_index(
        _LIVE_IDX,
        _OBS_TABLE,
        ["review_run_id", "scope_type", "scope_key", "metric_code", "component_name"],
        unique=True,
        postgresql_where=sa.text("source_kind = 'live'"),
    )
    op.create_index(
        _REPLAY_IDX,
        _OBS_TABLE,
        [
            "source_history_run_id", "trade_date", "scope_type", "scope_key",
            "metric_code", "component_name",
        ],
        unique=True,
        postgresql_where=sa.text("source_kind = 'history_replay'"),
    )


def downgrade() -> None:
    # [CHANGE-20260808] downgrade precondition（§9）：
    # 若已存在 source_kind='history_replay' observation（review_run_id=NULL），
    # downgrade 到 review_run_id NOT NULL 不可逆。fail fast，不自动删除 replay 数据。
    bind = op.get_bind()
    replay_count = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM " + _OBS_TABLE + " WHERE source_kind = 'history_replay'"
        )
    ).scalar()
    if replay_count and replay_count > 0:
        raise RuntimeError(
            f"downgrade blocked: found {replay_count} history_replay observation "
            "row(s) (review_run_id NULL). Downgrade would break replay lineage; "
            "delete replay data explicitly before downgrade."
        )
    # [CHANGE-20260808] event downgrade precondition（§5）：若已存在
    # history_contract_version 非空的 versioned event，降级恢复旧三字段 UNIQUE 时
    # versioned + legacy event 可能冲突。不得自动删除 v2 event → fail fast。
    v2_event_count = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM " + _EVENTS_TABLE
            + " WHERE history_contract_version IS NOT NULL"
        )
    ).scalar()
    if v2_event_count and v2_event_count > 0:
        raise RuntimeError(
            f"downgrade blocked: found {v2_event_count} versioned "
            "first_pyramid_history_events row(s) (history_contract_version NOT NULL). "
            "Downgrade would restore the legacy 3-column UNIQUE which may conflict with "
            "versioned events; delete v2 events explicitly before downgrade."
        )

    # ---- 2. observation dual lineage 逆序 ----
    op.drop_index(_REPLAY_IDX, table_name=_OBS_TABLE)
    op.drop_index(_LIVE_IDX, table_name=_OBS_TABLE)
    # 恢复普通 UNIQUE（对现有 live 行重建）
    op.create_unique_constraint(
        _OLD_UQ,
        _OBS_TABLE,
        ["review_run_id", "scope_type", "scope_key", "metric_code", "component_name"],
    )
    op.drop_constraint(_NEW_CHECK, _OBS_TABLE, type_="check")
    op.drop_column(_OBS_TABLE, "taxonomy_compatibility_key")
    op.drop_column(_OBS_TABLE, "history_contract_version")
    op.drop_column(_OBS_TABLE, "source_history_run_id")
    # review_run_id 可空 → NOT NULL（仅当无 NULL 行；downgrade 前需确认）
    op.alter_column(_OBS_TABLE, "review_run_id", nullable=False)
    op.drop_column(_OBS_TABLE, "source_kind")

    # ---- 1. daily_state lineage ----
    op.drop_column(_STATE_TABLE, "history_contract_version")
    op.drop_column(_STATE_TABLE, "source_history_run_id")

    # [CHANGE-20260808] event contract isolation（§7）逆序：先 drop 两个 partial unique
    # index，恢复旧三字段 UNIQUE 约束，再 drop event contract column，
    # 保证 087 → upgrade 088 → downgrade 真实恢复 087 schema（对称）。
    op.drop_index(_VERSIONED_EVENT_IDX, table_name=_EVENTS_TABLE)
    op.drop_index(_LEGACY_EVENT_IDX, table_name=_EVENTS_TABLE)
    op.create_unique_constraint(
        _OLD_EVENT_UQ,
        _EVENTS_TABLE,
        ["instrument_id", "algorithm_version", "event_id"],
    )
    op.drop_column(_EVENTS_TABLE, "history_contract_version")
