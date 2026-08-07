"""[AUD-05 2026-08-07] Review run 不可变性合同测试。

纯单元测试（mock AsyncSession），无需数据库：
    PURE_UNIT_TEST=1 python -m pytest tests/test_review_immutability_contract.py -v

锁定的不变量：**已存在的 Review run 不得被后续 create_run 改写。**

背景（审计 AUD-05）：改动前 create_run 使用
`ON CONFLICT ... DO UPDATE SET metadata_json, source_chip_run_id, degraded_reasons`。
其后果是——盘后 chip 作为异步增强产品晚于 Review 完成，任何一次对同一
(trade_date, core, board, algo, filter) 的重复 create_run，都会用"当时的 chip 状态"
改写一个**可能已经发布**的 Review run 的血统与降级原因。这让 Review 的输入身份
随时间漂移，同一个 run_id 在不同时刻读出的 lineage 不同。

现在语义为 `ON CONFLICT DO NOTHING` + 读回原行：
- 首次创建：正常 INSERT；
- 重复调用：DB 层零写入，返回既有行（幂等语义不变）。
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from app.models.board_analysis_snapshot import BoardAnalysisRun
from app.models.market_review import MarketReviewRun
from app.services.review_orchestrator_service import create_run

pytestmark = pytest.mark.asyncio

TRADE_DATE = date(2026, 8, 5)


class _FakeResult:
    def __init__(self, *, scalar: object = None) -> None:
        self._scalar = scalar

    def scalar_one_or_none(self) -> object:
        return self._scalar

    def scalar(self) -> object:
        return self._scalar


class _Pointer:
    def __init__(self, data_run_id: uuid.UUID) -> None:
        self.data_run_id = data_run_id


class _ImmutabilitySession:
    """模拟"run 已存在"场景，记录 INSERT 语句以断言其不携带 UPDATE 语义。"""

    def __init__(self, *, core_id: uuid.UUID, board_id: uuid.UUID,
                 existing: MarketReviewRun) -> None:
        self._core_id = core_id
        self._board_id = board_id
        self._existing = existing
        self._board_run = BoardAnalysisRun(
            id=board_id,
            trade_date=TRADE_DATE,
            source_core_run_id=core_id,
            status="succeeded",
        )
        self.insert_sql: list[str] = []

    async def execute(self, stmt):
        try:
            compiled = stmt.compile(
                compile_kwargs={"literal_binds": True},
            ).string
        except Exception:
            # JSONB 等类型无 literal renderer，退化为带占位符的编译文本；
            # 本测试只断言 SQL 结构（ON CONFLICT 子句），不依赖字面量值。
            compiled = str(stmt.compile())
        low = compiled.lower()
        if "insert into" in low:
            self.insert_sql.append(low)
            return _FakeResult(scalar=None)
        if "publication_kind" in low and "stock_core" in low:
            return _FakeResult(scalar=_Pointer(self._core_id))
        if "publication_kind" in low and "market_aggregation" in low:
            return _FakeResult(scalar=_Pointer(self._board_id))
        return _FakeResult(scalar=self._existing)

    async def get(self, model, ident):
        return self._board_run

    async def flush(self):
        return None


def _make_existing(core_id: uuid.UUID, board_id: uuid.UUID) -> MarketReviewRun:
    """构造一个"已发布"的 Review run（带干净 lineage）。"""
    run = MarketReviewRun(
        id=uuid.uuid4(),
        trade_date=TRADE_DATE,
        source_core_run_id=core_id,
        source_board_run_id=board_id,
        algorithm_version="review-2.0.0",
        filter_version="filters-1.1.0",
        baseline_window=120,
        status="published",
    )
    run.source_chip_run_id = None
    run.degraded_reasons = []
    run.metadata_json = {"idempotency_key": "first-call"}
    return run


async def test_late_chip_does_not_rewrite_existing_run() -> None:
    """晚到 chip 后重复 create_run：返回原行，lineage 与降级原因不变。"""
    core_id = uuid.uuid4()
    board_id = uuid.uuid4()
    existing = _make_existing(core_id, board_id)
    before = {
        "id": existing.id,
        "source_core_run_id": existing.source_core_run_id,
        "source_board_run_id": existing.source_board_run_id,
        "source_chip_run_id": existing.source_chip_run_id,
        "degraded_reasons": list(existing.degraded_reasons),
        "status": existing.status,
    }

    session = _ImmutabilitySession(
        core_id=core_id, board_id=board_id, existing=existing,
    )

    # 第二次调用（模拟 chip 已完成后的重复触发）
    run = await create_run(
        session,  # type: ignore[arg-type]
        trade_date=TRADE_DATE,
        idempotency_key="second-call-after-chip",
    )

    assert run is existing, "重复 create_run 必须返回既有 run，不得新建"
    assert run.id == before["id"]
    assert run.source_core_run_id == before["source_core_run_id"]
    assert run.source_board_run_id == before["source_board_run_id"]
    assert run.source_chip_run_id == before["source_chip_run_id"], (
        "晚到 chip 不得写入已存在 Review run 的 lineage"
    )
    assert list(run.degraded_reasons) == before["degraded_reasons"], (
        "晚到 chip 不得改写已存在 Review run 的降级原因"
    )
    assert run.status == before["status"], "重复创建不得回退已发布 run 的状态"


async def test_repeat_create_run_emits_do_nothing_not_do_update() -> None:
    """SQL 层保证：ON CONFLICT DO NOTHING，不得是 DO UPDATE。

    这是"已有 run 不被改写"唯一可靠的结构性保证 —— 仅靠应用层不传 chip 字段
    并不足够，DO UPDATE 仍会用新 INSERT 的其余字段覆盖既有行。
    """
    core_id = uuid.uuid4()
    board_id = uuid.uuid4()
    session = _ImmutabilitySession(
        core_id=core_id, board_id=board_id,
        existing=_make_existing(core_id, board_id),
    )

    await create_run(session, trade_date=TRADE_DATE)  # type: ignore[arg-type]

    assert session.insert_sql, "必须执行 INSERT"
    sql = "\n".join(session.insert_sql)
    assert "on conflict" in sql
    assert "do nothing" in sql, "必须 DO NOTHING 才能保证已有 run 不被改写"
    assert "do update" not in sql, (
        "DO UPDATE 会让晚到的增强产品改写已发布 Review run"
    )
    # metadata 也不得被刷新（idempotency_key 属调用方追踪信息，
    # 但改写已发布 run 的 metadata 同样破坏不可变性）
    assert "set " not in sql.split("on conflict")[-1], (
        "ON CONFLICT 子句不得包含任何 SET"
    )


async def test_metadata_of_existing_run_is_not_refreshed() -> None:
    """第二次调用传入不同 idempotency_key，既有 run 的 metadata 不变。"""
    core_id = uuid.uuid4()
    board_id = uuid.uuid4()
    existing = _make_existing(core_id, board_id)
    session = _ImmutabilitySession(
        core_id=core_id, board_id=board_id, existing=existing,
    )

    run = await create_run(
        session,  # type: ignore[arg-type]
        trade_date=TRADE_DATE,
        idempotency_key="a-different-key",
    )

    assert run.metadata_json.get("idempotency_key") == "first-call", (
        "已存在 run 的 metadata 不得被后续调用刷新"
    )
