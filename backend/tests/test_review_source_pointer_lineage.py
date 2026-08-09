"""Review source lineage (PC-40 / PC-41) 纯单元测试（Phase 4D.3, 2026-08-09）。

覆盖 `_resolve_source_run_ids` 的合同：
- PC-40：Review 只消费正式 market_aggregation pointer
- PC-41：`review.source_board_run_id == market_aggregation.pointer.data_run_id`
- PC-42 regression：pointer 指向 degraded partial board run 时 Review 可接受，
  但 source_board_run_id 仍严格等于 pointer.data_run_id

本测试为纯单元测试（PURE_UNIT_TEST），不连接数据库。
"""

from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import AsyncMock, Mock

import pytest

from app.models.factor_publication import (
    PUBLICATION_KIND_MARKET_AGGREGATION,
    PUBLICATION_KIND_STOCK_CORE,
)
from app.services import review_orchestrator_service as ros
from app.services.review_orchestrator_service import (
    ReviewOrchestratorError,
    _resolve_source_run_ids,
)

pytestmark = pytest.mark.pure_unit

_TRADE_DATE = date(2026, 8, 4)


def _pub(data_run_id: uuid.UUID) -> Mock:
    pub = Mock()
    pub.data_run_id = data_run_id
    return pub


def _board_run(
    *,
    run_id: uuid.UUID,
    status: str,
    core_id: uuid.UUID,
    trade_date: date = _TRADE_DATE,
) -> Mock:
    run = Mock()
    run.id = run_id
    run.status = status
    run.trade_date = trade_date
    run.source_core_run_id = core_id
    return run


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    *,
    core_pub: Mock | None,
    board_pub: Mock | None,
    board_run: Mock | None,
) -> Mock:
    async def _fake_get_publication(_session, _trade_date, kind):
        if kind == PUBLICATION_KIND_STOCK_CORE:
            return core_pub
        if kind == PUBLICATION_KIND_MARKET_AGGREGATION:
            return board_pub
        return None

    monkeypatch.setattr(ros, "_get_publication", _fake_get_publication)
    session = Mock()
    session.get = AsyncMock(return_value=board_run)
    return session


async def _resolve(session, *, board_id=None, core_id=None):
    return await _resolve_source_run_ids(
        session,
        _TRADE_DATE,
        source_core_run_id=core_id,
        source_board_run_id=board_id,
    )


# ---------------------------------------------------------------------------
# §23 A/B：接受
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_pointer_to_succeeded_board_run_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A. pointer → succeeded board run → accept。"""
    core_id, board_id = uuid.uuid4(), uuid.uuid4()
    session = _wire(
        monkeypatch,
        core_pub=_pub(core_id),
        board_pub=_pub(board_id),
        board_run=_board_run(run_id=board_id, status="succeeded", core_id=core_id),
    )
    assert await _resolve(session) == (core_id, board_id)


@pytest.mark.asyncio
async def test_b_pointer_to_degraded_partial_run_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B. [PC-42] pointer → formally published degraded partial run → accept。

    关键：Review 接受的是「正式 pointer 指向的 degraded run」，
    并且 source_board_run_id 仍严格等于 pointer.data_run_id。
    """
    core_id, board_id = uuid.uuid4(), uuid.uuid4()
    session = _wire(
        monkeypatch,
        core_pub=_pub(core_id),
        board_pub=_pub(board_id),
        board_run=_board_run(run_id=board_id, status="partial", core_id=core_id),
    )
    resolved_core, resolved_board = await _resolve(session)
    assert resolved_core == core_id
    # PC-41 恒等式
    assert resolved_board == board_id


# ---------------------------------------------------------------------------
# §23 C/D/E：拒绝
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_c_missing_board_pointer_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C. pointer missing → reject（PC-40，禁止 fallback 到任意 run）。"""
    core_id = uuid.uuid4()
    session = _wire(
        monkeypatch, core_pub=_pub(core_id), board_pub=None, board_run=None,
    )
    with pytest.raises(ReviewOrchestratorError, match="无已发布 board_analysis pointer"):
        await _resolve(session)


@pytest.mark.asyncio
async def test_c_missing_board_pointer_rejected_even_with_explicit_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C'. 即使调用方显式给出 board run id，无正式 pointer 仍必须拒绝。"""
    core_id, board_id = uuid.uuid4(), uuid.uuid4()
    session = _wire(
        monkeypatch,
        core_pub=_pub(core_id),
        board_pub=None,
        board_run=_board_run(run_id=board_id, status="partial", core_id=core_id),
    )
    with pytest.raises(ReviewOrchestratorError, match="无已发布 board_analysis pointer"):
        await _resolve(session, board_id=board_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "pending", "running"])
async def test_d_pointer_to_failed_or_non_terminal_run_is_rejected(
    monkeypatch: pytest.MonkeyPatch, status: str,
) -> None:
    """D. pointer → failed / 非终态 run → reject。"""
    core_id, board_id = uuid.uuid4(), uuid.uuid4()
    session = _wire(
        monkeypatch,
        core_pub=_pub(core_id),
        board_pub=_pub(board_id),
        board_run=_board_run(run_id=board_id, status=status, core_id=core_id),
    )
    with pytest.raises(ReviewOrchestratorError, match="Board batch 非 ready"):
        await _resolve(session)


@pytest.mark.asyncio
async def test_e_explicit_board_run_id_diverging_from_pointer_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E. [PC-41] explicit source_board_run_id != pointer.data_run_id → reject。"""
    core_id = uuid.uuid4()
    pointer_board_id, other_board_id = uuid.uuid4(), uuid.uuid4()
    session = _wire(
        monkeypatch,
        core_pub=_pub(core_id),
        board_pub=_pub(pointer_board_id),
        board_run=_board_run(
            run_id=other_board_id, status="succeeded", core_id=core_id,
        ),
    )
    with pytest.raises(ReviewOrchestratorError, match=r"\[PC-41\]"):
        await _resolve(session, board_id=other_board_id)


@pytest.mark.asyncio
async def test_board_run_from_different_core_lineage_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """degraded 允许，但 lineage 失配仍必须拒绝。"""
    core_id, board_id = uuid.uuid4(), uuid.uuid4()
    session = _wire(
        monkeypatch,
        core_pub=_pub(core_id),
        board_pub=_pub(board_id),
        board_run=_board_run(
            run_id=board_id, status="partial", core_id=uuid.uuid4(),
        ),
    )
    with pytest.raises(ReviewOrchestratorError, match="不同源"):
        await _resolve(session)


@pytest.mark.asyncio
async def test_missing_stock_core_pointer_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PC-40：stock_core pointer 缺失同样必须拒绝。"""
    session = _wire(
        monkeypatch, core_pub=None, board_pub=_pub(uuid.uuid4()), board_run=None,
    )
    with pytest.raises(ReviewOrchestratorError, match="无已发布 stock_core pointer"):
        await _resolve(session)
