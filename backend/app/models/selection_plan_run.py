"""选股组合运行/结果/证据 ORM 模型（C4）。

对应迁移 007_selection_plans 中的运行侧三张表：
- selection_plan_runs: 方案运行记录（idempotency_key 唯一，防重复运行）
- selection_plan_results: 方案运行结果（唯一约束 plan_run + instrument，
  matched_member_ids 为命中成员 ID 数组，rank_value 排名分值，summary 指标摘要）
- selection_result_evidence: 结果证据链（复合主键 selection_result_id + member_id，
  记录每个成员对每个标的的命中证据，reason_code 标注缺失原因）

字段映射说明（任务描述 → 迁移 DDL，以迁移为准）：
- SelectionPlanRun.trigger_kind/error → 迁移无此列（trigger_kind 作为运行时参数参与幂等键计算，不持久化）
- SelectionPlanResult.rank/score/contributing_members → rank_value(float)/matched_member_ids(ARRAY)/summary(JSONB)
- SelectionPlanEvidence.id/run_id/instrument_id/metrics_summary/missing_reason →
  迁移无单独 id（复合主键 selection_result_id+member_id），无 run_id/instrument_id（经 selection_result_id 关联），
  metrics_summary → summary，missing_reason → reason_code

幂等键设计：
- idempotency_key = sha256(revision_id + trade_date + trigger_kind + input_run_set_hash)[:16]
- trigger_kind 为运行时参数（manual/scheduled/replay），不持久化为列，但参与幂等键计算
- 相同 idempotency_key 的运行不重复执行（unique 约束 + 业务层查询双重保障）

reason_code 枚举（业务约定，迁移未加 CHECK 约束）：
- NO_RESULT: 策略无结果（该成员策略在 trade_date 无任何 StrategyResult）
- FILTERED_OUT: 条件筛选未通过（策略有结果但未通过成员 conditions 的 AND 筛选）
- DATA_MISSING: 行情缺失（策略运行时缺少行情数据，无法计算）
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class SelectionPlanRun(Base):
    """选股方案运行记录 - 一次方案执行的生命周期。

    对应迁移 007 selection_plan_runs 表。
    idempotency_key 唯一约束防止重复运行（幂等）。
    status: pending/running/succeeded/failed（迁移未加 CHECK，由业务层控制）。

    字段映射（任务描述 → 迁移 DDL）：
    - trigger_kind → 迁移无此列（运行时参数，参与幂等键计算）
    - error → 迁移无此列（失败信息记录在 status=failed，详情由日志/证据链承载）
    """

    __tablename__ = "selection_plan_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
        comment="运行 ID",
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
        comment="触发用户 ID",
    )
    selection_plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("selection_plans.id"),
        nullable=False,
        comment="方案 ID",
    )
    revision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("selection_plan_revisions.id"),
        nullable=False,
        comment="方案版本 ID（运行绑定不可变 revision）",
    )
    trade_date: Mapped[date] = mapped_column(
        Date(), nullable=False, comment="交易日"
    )
    status: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="运行状态：pending/running/succeeded/failed",
    )
    input_run_set_hash: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="输入运行集哈希（成员策略结果集指纹）"
    )
    idempotency_key: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        unique=True,
        comment="幂等键（revision_id+trade_date+trigger_kind+input_run_set_hash 的哈希）",
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="开始时间"
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="完成时间"
    )

    def __repr__(self) -> str:
        return (
            f"<SelectionPlanRun(trade_date={self.trade_date}, "
            f"status={self.status!r})>"
        )


class SelectionPlanResult(Base):
    """方案运行结果 - 单只标的在一次运行中的组合命中结果。

    对应迁移 007 selection_plan_results 表。
    唯一约束 (plan_run_id, instrument_id) 确保同一运行同一标的结果唯一。
    matched: 是否最终命中（ALL 需所有成员命中，ANY 需任一成员命中）。
    matched_member_ids: 命中该标的的成员 ID 数组（证据链索引）。
    rank_value: 排名分值（按 sort_spec 计算，nullable 表示未排名）。
    summary: 指标摘要 JSONB（含各成员关键指标快照）。

    字段映射（任务描述 → 迁移 DDL）：
    - rank(int)/score(numeric)/contributing_members(jsonb) →
      rank_value(float)/matched_member_ids(ARRAY[UUID])/summary(JSONB)
    """

    __tablename__ = "selection_plan_results"
    __table_args__ = (
        UniqueConstraint("plan_run_id", "instrument_id", name="selection_plan_results_uniq"),
        Index(
            "ix_selection_result_match",
            "plan_run_id",
            "matched",
            "rank_value",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
        comment="结果 ID",
    )
    plan_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("selection_plan_runs.id", ondelete="CASCADE"),
        nullable=False,
        comment="所属运行 ID",
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("instruments.id"),
        nullable=False,
        comment="标的 ID",
    )
    matched: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, comment="是否最终命中"
    )
    matched_member_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)),
        nullable=False,
        server_default=func.text("'{}'"),
        comment="命中该标的的成员 ID 数组",
    )
    rank_value: Mapped[float | None] = mapped_column(
        Float(), nullable=True, comment="排名分值（按 sort_spec 计算）"
    )
    summary: Mapped[dict[str, Any]] = mapped_column(
        JSONB(astext_type=Text()),
        nullable=False,
        server_default=func.text("'{}'"),
        comment="指标摘要 JSONB",
    )

    def __repr__(self) -> str:
        return (
            f"<SelectionPlanResult(instrument_id={self.instrument_id!r}, "
            f"matched={self.matched}, rank_value={self.rank_value})>"
        )


class SelectionResultEvidence(Base):
    """结果证据链 - 每个成员对每个标的的命中证据。

    对应迁移 007 selection_result_evidence 表（注意：DDL 表名无 plan 前缀）。
    复合主键 (selection_result_id, member_id) 确保同一结果同一成员证据唯一。
    strategy_result_id 可空（策略无结果时为 None）。
    reason_code 标注缺失原因：NO_RESULT/FILTERED_OUT/DATA_MISSING。
    summary 存储该成员对该标的的指标摘要。

    字段映射（任务描述 → 迁移 DDL）：
    - id/run_id/instrument_id → 迁移无单独 id（复合主键），无 run_id/instrument_id（经 selection_result_id 关联）
    - metrics_summary → summary
    - missing_reason → reason_code
    """

    __tablename__ = "selection_result_evidence"
    __table_args__ = (
        PrimaryKeyConstraint("selection_result_id", "member_id"),
    )

    selection_result_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("selection_plan_results.id", ondelete="CASCADE"),
        nullable=False,
        comment="所属结果 ID（复合主键之一）",
    )
    member_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("selection_plan_members.id"),
        nullable=False,
        comment="成员 ID（复合主键之一）",
    )
    strategy_result_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("strategy_results.id"),
        nullable=True,
        comment="原始策略结果 ID（策略无结果时为 None）",
    )
    matched: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, comment="该成员是否命中该标的"
    )
    reason_code: Mapped[str | None] = mapped_column(
        Text(),
        nullable=True,
        comment="缺失原因：NO_RESULT/FILTERED_OUT/DATA_MISSING（命中时为 None）",
    )
    summary: Mapped[dict[str, Any]] = mapped_column(
        JSONB(astext_type=Text()),
        nullable=False,
        server_default=func.text("'{}'"),
        comment="该成员对该标的的指标摘要 JSONB",
    )

    def __repr__(self) -> str:
        return (
            f"<SelectionResultEvidence(member_id={self.member_id!r}, "
            f"matched={self.matched}, reason_code={self.reason_code!r})>"
        )


if __name__ == "__main__":
    # 自测入口：验证 ORM 模型映射（无副作用，不连接数据库）
    print(f"SelectionPlanRun.__tablename__={SelectionPlanRun.__tablename__}")
    run_cols = [c.name for c in SelectionPlanRun.__table__.columns]
    print(f"SelectionPlanRun columns={run_cols}")
    for required in ["id", "user_id", "selection_plan_id", "revision_id", "trade_date", "status", "input_run_set_hash", "idempotency_key", "started_at", "finished_at"]:
        assert required in run_cols, f"SelectionPlanRun 缺少列: {required}"
    # 验证幂等键唯一约束
    run_constraints = [c.name for c in SelectionPlanRun.__table__.constraints if hasattr(c, "name") and c.name]
    print(f"Run constraints={run_constraints}")

    print(f"SelectionPlanResult.__tablename__={SelectionPlanResult.__tablename__}")
    res_cols = [c.name for c in SelectionPlanResult.__table__.columns]
    print(f"SelectionPlanResult columns={res_cols}")
    for required in ["id", "plan_run_id", "instrument_id", "matched", "matched_member_ids", "rank_value", "summary"]:
        assert required in res_cols, f"SelectionPlanResult 缺少列: {required}"
    # 验证索引
    res_indexes = [idx.name for idx in SelectionPlanResult.__table__.indexes]
    print(f"Result indexes={res_indexes}")
    assert "ix_selection_result_match" in res_indexes

    print(f"SelectionResultEvidence.__tablename__={SelectionResultEvidence.__tablename__}")
    ev_cols = [c.name for c in SelectionResultEvidence.__table__.columns]
    print(f"SelectionResultEvidence columns={ev_cols}")
    for required in ["selection_result_id", "member_id", "strategy_result_id", "matched", "reason_code", "summary"]:
        assert required in ev_cols, f"SelectionResultEvidence 缺少列: {required}"
    # 验证复合主键
    ev_pk = [c.name for c in SelectionResultEvidence.__table__.primary_key.columns]
    print(f"Evidence PK={ev_pk}")
    assert ev_pk == ["selection_result_id", "member_id"], f"复合主键不匹配: {ev_pk}"

    print("OK")
