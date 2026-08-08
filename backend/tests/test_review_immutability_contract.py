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
from app.services.review_orchestrator_service import (
    ReviewOrchestratorError,
    ReviewRunCreation,
    compute_run,
    create_run_with_result,
)

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
    creation = await create_run_with_result(
        session,  # type: ignore[arg-type]
        trade_date=TRADE_DATE,
        idempotency_key="second-call-after-chip",
    )
    run = creation.run
    created = creation.created

    # [Phase4.1 corrective] created 语义：复用既有 run 时 created 必须为 False
    assert created is False, "复用既有 run 必须显式返回 created=False"
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

    creation = await create_run_with_result(session, trade_date=TRADE_DATE)  # type: ignore[arg-type]
    created = creation.created
    assert created is False, "既有 run 被复用时 created 必须为 False"

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

    creation = await create_run_with_result(
        session,  # type: ignore[arg-type]
        trade_date=TRADE_DATE,
        idempotency_key="a-different-key",
    )
    run = creation.run
    assert creation.created is False, "复用既有 run 必须显式返回 created=False"

    assert run.metadata_json.get("idempotency_key") == "first-call", (
        "已存在 run 的 metadata 不得被后续调用刷新"
    )


async def test_published_run_forbids_inplace_recompute() -> None:
    """[Phase4.1 corrective] 服务层最后防线：已发布 run 禁止 compute_run 原地重算。

    无论调用方是 Admin POST 复用既有 run、还是 canary/debug 误用正式 run 身份，
    compute_run 在 status==published 时必须抛 ReviewOrchestratorError，且不得修改 run。
    """
    core_id = uuid.uuid4()
    board_id = uuid.uuid4()
    existing = _make_existing(core_id, board_id)
    existing.status = "published"

    session = _ImmutabilitySession(
        core_id=core_id, board_id=board_id, existing=existing,
    )

    with pytest.raises(ReviewOrchestratorError):
        await compute_run(session, existing)  # type: ignore[arg-type]

    # 守卫不得副作用地改动已发布 run 的 status / lineage
    assert existing.status == "published"
    assert existing.source_chip_run_id is None


# ---------------------------------------------------------------------------
# Scope 兼容性（canary / formal）—— 纯函数直接测试
# [Phase4.1 corrective] scope 判据抽成 check_run_scope_compatibility，
# 不依赖复杂 AsyncSession mock 验证 scope contract。
# ---------------------------------------------------------------------------


from app.services.review_orchestrator_service import (  # noqa: E402
    check_run_scope_compatibility,
)


async def test_scope_formal_to_formal_is_compatible() -> None:
    """formal 全市场 → formal 全市场：scope 一致，可安全复用。"""
    assert check_run_scope_compatibility(
        existing_canary=False,
        existing_symbols=[],
        requested_canary=False,
        requested_symbols=[],
    ) is True


async def test_scope_same_canary_is_compatible() -> None:
    """canary + 相同 symbols → canary + 相同 symbols：scope 一致，可安全复用。"""
    assert check_run_scope_compatibility(
        existing_canary=True,
        existing_symbols=["SYM_A", "SYM_B"],
        requested_canary=True,
        requested_symbols=["SYM_B", "SYM_A"],  # 顺序无关（frozenset）
    ) is True


async def test_scope_formal_to_canary_is_conflict() -> None:
    """既有 formal 全市场，新请求 canary：scope 冲突，必须 fail-safe reject。"""
    assert check_run_scope_compatibility(
        existing_canary=False,
        existing_symbols=[],
        requested_canary=True,
        requested_symbols=["SYM_A"],
    ) is False


async def test_scope_canary_to_formal_is_conflict() -> None:
    """既有 canary，新请求 formal 全市场：scope 冲突，必须 fail-safe reject。"""
    assert check_run_scope_compatibility(
        existing_canary=True,
        existing_symbols=["SYM_A"],
        requested_canary=False,
        requested_symbols=[],
    ) is False


async def test_scope_different_symbols_is_conflict() -> None:
    """都是 canary 但 symbols 不同：scope 冲突，必须 fail-safe reject。"""
    assert check_run_scope_compatibility(
        existing_canary=True,
        existing_symbols=["SYM_A"],
        requested_canary=True,
        requested_symbols=["SYM_A", "SYM_B"],
    ) is False


async def test_created_true_on_fresh_insert_via_immutability_session() -> None:
    """首次创建（无既有 run）create_run 返回 created=True。

    用与既有 immutability 测试一致的 _ImmutabilitySession 行为，不依赖额外脆弱 mock。
    这里通过 get_run_by_keys SELECT 返回 None（无既有行）来模拟"首次"路径。
    """
    core_id = uuid.uuid4()
    board_id = uuid.uuid4()

    class _FreshSession:
        """首次插入场景：get_run_by_keys/SELECT 返回 None，INSERT RETURNING 命中。"""

        def __init__(self) -> None:
            self._core_id = core_id
            self._board_id = board_id
            self._inserted = False
            self._board = BoardAnalysisRun(
                id=board_id, trade_date=TRADE_DATE,
                source_core_run_id=core_id, status="succeeded",
            )

        async def execute(self, stmt):
            compiled = str(stmt.compile()).lower()
            if "insert into" in compiled and "returning" in compiled:
                self._inserted = True
                # RETURNING id 路径：scalar_one_or_none 返回非 None → created=True
                return _FakeResult(scalar="inserted-id")
            # _get_publication 查询 factor_publications（stock_core pointer），
            # 无论 kind 参数是否内联，表名一定出现在 SQL 中。
            if "factor_publications" in compiled:
                return _FakeResult(scalar=_Pointer(self._core_id))
            # 首次 SELECT（含 get_run_by_keys）返回 None → 走 INSERT；
            # INSERT 之后再次 SELECT（读取 upsert 后的 run）返回刚插入的 run。
            if self._inserted:
                return _FakeResult(
                    scalar=MarketReviewRun(
                        id=uuid.uuid4(),
                        trade_date=TRADE_DATE,
                        source_core_run_id=self._core_id,
                        source_board_run_id=self._board_id,
                        algorithm_version="review-2.0.0",
                        filter_version="filters-1.1.0",
                        status="created",
                        metadata_json={"canary": False, "symbols": None},
                    ),
                )
            return _FakeResult(scalar=None)

        async def get(self, model, ident):
            return self._board

        async def flush(self):
            return None

    creation = await create_run_with_result(
        _FreshSession(),  # type: ignore[arg-type]
        trade_date=TRADE_DATE,
    )
    assert creation.created is True, "首次插入必须返回 created=True"
    assert isinstance(creation.run, MarketReviewRun)


async def test_returns_review_run_creation_dataclass() -> None:
    """create_run 必须返回 ReviewRunCreation（而非裸 tuple / 裸 run）。"""
    core_id = uuid.uuid4()
    board_id = uuid.uuid4()
    existing = _make_existing(core_id, board_id)
    session = _ImmutabilitySession(
        core_id=core_id, board_id=board_id, existing=existing,
    )
    creation = await create_run_with_result(session, trade_date=TRADE_DATE)  # type: ignore[arg-type]
    assert isinstance(creation, ReviewRunCreation)
    assert isinstance(creation.run, MarketReviewRun)
    assert creation.created in (True, False)
