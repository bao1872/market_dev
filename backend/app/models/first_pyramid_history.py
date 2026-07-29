"""FirstPyramidHistory ORM 模型 - 第一金字塔历史回补持久化。

对应迁移 072_first_pyramid_history 中的两张表：
1. first_pyramid_history_daily_state: 每只标的每个交易日的 point-in-time daily state
2. first_pyramid_history_events: 不可变事件流（BOS/CHoCH/OB_*/EQH/EQL/SQZ_RELEASE/...）

设计说明（[CHANGE-20260729-003] 核心与筹码解耦 - P0-11 非筹码历史回补）：
- 按"个股为外层，一次调用 history SSOT"模式回补：
  compute_first_pyramid_history 一次计算多日 daily_state + events
- 禁止逐日调用 snapshot，禁止回补 chip
- 唯一键支持幂等重跑，相同 (instrument_id, trade_date, algorithm_version) 重复
  upsert 只更新内容
- events 表事件一旦写入不可修改（on_conflict_do_nothing 语义，重跑不覆盖）

模块自测：
    python -m app.models.first_pyramid_history
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Date, DateTime, ForeignKey, Index, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class FirstPyramidHistoryDailyState(Base):
    """第一金字塔历史 daily_state - 每只标的每个交易日的 point-in-time 状态固化。

    [CHANGE-20260729-003] P0-11 非筹码历史回补：一次调用 history SSOT 产出多日状态。
    """

    __tablename__ = "first_pyramid_history_daily_state"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
        comment="daily state 行 ID",
    )
    instrument_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("instruments.id"),
        nullable=False,
        comment="股票 ID",
    )
    trade_date: Mapped[date] = mapped_column(
        Date(),
        nullable=False,
        comment="业务交易日（对应 daily_state.time）",
    )
    algorithm_version: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="core 算法版本（FIRST_PYRAMID_CORE_ALGORITHM_VERSION）",
    )
    input_hash: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="输入 bars hash（用于校验重跑一致性）",
    )
    state_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB(astext_type=Text()),
        nullable=False,
        comment="daily_state 单行完整字段 JSONB（trend/structure/momentum/volume 等）",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="创建时间",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
        comment="更新时间",
    )

    __table_args__ = (
        UniqueConstraint(
            "instrument_id",
            "trade_date",
            "algorithm_version",
            name="uq_first_pyramid_history_daily_state_instr_date_ver",
        ),
        Index(
            "ix_first_pyramid_history_daily_state_trade_date",
            "trade_date",
        ),
        Index(
            "ix_first_pyramid_history_daily_state_instr_date",
            "instrument_id",
            "trade_date",
            postgresql_using="btree",
            postgresql_ops={"trade_date": "desc"},
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<FirstPyramidHistoryDailyState("
            f"instrument_id={self.instrument_id!r}, "
            f"trade_date={self.trade_date!r}, "
            f"algorithm_version={self.algorithm_version!r})>"
        )


class FirstPyramidHistoryEvent(Base):
    """第一金字塔历史不可变事件 - BOS/CHoCH/OB_*/EQH/EQL/SQZ_RELEASE/ZERO_CROSS_*。

    事件一旦写入不可修改（重跑使用 on_conflict_do_nothing）。
    """

    __tablename__ = "first_pyramid_history_events"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
        comment="事件行 ID",
    )
    instrument_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("instruments.id"),
        nullable=False,
        comment="股票 ID",
    )
    algorithm_version: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="core 算法版本",
    )
    event_type: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="事件类型：BOS/CHoCH/OB_CREATED/OB_ENTERED/OB_MITIGATED/EQH/EQL/SQZ_RELEASE/ZERO_CROSS_*",
    )
    event_id: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="事件稳定标识（bar_index+type 或 anchor_time+type），用于幂等去重",
    )
    event_time: Mapped[str | None] = mapped_column(
        Text(),
        nullable=True,
        comment="事件发生时间（ISO 字符串，对应 bar time）",
    )
    event_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB(astext_type=Text()),
        nullable=False,
        comment="事件完整字段 JSONB（不可变）",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="创建时间",
    )

    __table_args__ = (
        UniqueConstraint(
            "instrument_id",
            "algorithm_version",
            "event_id",
            name="uq_first_pyramid_history_events_instr_ver_evid",
        ),
        Index(
            "ix_first_pyramid_history_events_instr_type",
            "instrument_id",
            "event_type",
        ),
        Index(
            "ix_first_pyramid_history_events_type",
            "event_type",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<FirstPyramidHistoryEvent("
            f"instrument_id={self.instrument_id!r}, "
            f"event_type={self.event_type!r}, "
            f"event_id={self.event_id!r})>"
        )


if __name__ == "__main__":
    ds_cols = FirstPyramidHistoryDailyState.__table__.columns
    ds_expected = {
        "id", "instrument_id", "trade_date", "algorithm_version",
        "input_hash", "state_payload", "created_at", "updated_at",
    }
    ds_actual = {c.name for c in ds_cols}
    assert ds_expected == ds_actual, f"daily_state 字段不匹配: {ds_expected ^ ds_actual}"
    print(f"OK: {FirstPyramidHistoryDailyState.__tablename__} columns verified")
    print(f"columns: {sorted(ds_actual)}")

    ev_cols = FirstPyramidHistoryEvent.__table__.columns
    ev_expected = {
        "id", "instrument_id", "algorithm_version", "event_type",
        "event_id", "event_time", "event_payload", "created_at",
    }
    ev_actual = {c.name for c in ev_cols}
    assert ev_expected == ev_actual, f"events 字段不匹配: {ev_expected ^ ev_actual}"
    print(f"OK: {FirstPyramidHistoryEvent.__tablename__} columns verified")
    print(f"columns: {sorted(ev_actual)}")
