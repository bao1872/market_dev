"""A 步契约测试：review_orchestrator._persist_canonical_scope_observation。

验证规范 Scope Observation 七段事实层双写（PRD §7.2-§7.17 v2.3）：
- activated scope（industry_l1/l2/l3 + concept）调用 prepare_scope →
  compute_scope_observation → check_observation_invariants →
  save_scope_observation_fact
- market / major_index / style：prepare_scope 返回 unavailable 时直接 return，
  不写表（双轨并存，本轮不破坏 legacy Discovery）
- invariant 失败时抛 ValueError（上层 _compute_scope_metrics_phase 的
  try/except 仅 warning，不破坏 legacy signal）

全部为纯单元/mock 测试，不连库（PURE_UNIT_TEST=1）。
"""
from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.pure_unit

from app.services import review_orchestrator_service as orch
from app.services.review_observation_prep_service import PreparedScope


def _make_run() -> object:
    run = type("Run", (), {})()
    run.trade_date = date(2026, 8, 12)
    run.algorithm_version = "review-v2.3"
    return run


def _scope(scope_type: str, scope_key: str) -> object:
    definition = type("Scope", (), {})()
    definition.scope_type = scope_type
    definition.scope_key = scope_key
    return definition


def _mock_session() -> AsyncMock:
    """AsyncSession mock whose begin_nested() is a real async context manager.

    Mirrors production async SQLAlchemy ``AsyncSession.begin_nested`` so the
    savepoint protocol (CORRECTION: nested transaction isolation) is exercised.
    """
    session = AsyncMock()
    # 用 Mock 记录调用，同时返回真正的 async context manager（符合生产 async SQLAlchemy API）。
    begin_nested_cm = AsyncMock()
    begin_nested_cm.__aenter__ = AsyncMock(return_value=AsyncMock())
    begin_nested_cm.__aexit__ = AsyncMock(return_value=False)

    begin_nested = MagicMock(return_value=begin_nested_cm)

    session.begin_nested = begin_nested
    return session


def _prep(scope_type: str, scope_key: str, *, unavailable: bool = False) -> PreparedScope:
    # 测试仅验证 orchestrator 调用链路（下游全部 mock）；members/events 用占位
    # 对象满足 PreparedScope 非空判断即可，不依赖具体 domain 构造。
    member = type("Member", (), {"instrument_id": "600000.SH"})
    return PreparedScope(
        scope_type=scope_type,
        scope_key=scope_key,
        scope_name="测试板块",
        trade_date=date(2026, 8, 12),
        canonical_t1=date(2026, 8, 11),
        pit_member_ids=("600000.SH",) if not unavailable else (),
        pit_member_ids_t1=("600000.SH",),
        members=(member,) if not unavailable else (),
        t1_membership_available=True,
        pit_status_t="available" if not unavailable else "unavailable",
        pit_status_t1="available",
        diagnostics=(),
        events=(type("Event", (), {"instrument_id": "600000.SH"}),)
        if not unavailable
        else (),
    )


OBSERVATION = {
    "scope": {"scope_type": "industry_l2", "scope_key": "x"},
    "price": {"return_level": {}},
    "trend": {"state": {}},
    "structure": {"events": []},
    "momentum": {"state": {}},
    "participation": {"volume": {}},
    "chip": {"unresolved": True},
}


@pytest.mark.asyncio
async def test_activated_scope_persists_fact():
    """industry_l2 activated scope：完整走 prepare→compute→invariant→save。"""
    run = _make_run()
    scope = _scope("industry_l2", "sw_electronics")
    fake_prep = _prep("industry_l2", "sw_electronics")

    with patch.object(
        orch, "prepare_scope", AsyncMock(return_value=fake_prep),
    ) as mock_prepare, patch.object(
        orch, "compute_scope_observation", return_value=OBSERVATION,
    ) as mock_compute, patch.object(
        orch, "check_observation_invariants", return_value=[{"ok": True, "name": "x"}],
    ) as mock_check, patch.object(
        orch, "save_scope_observation_fact", AsyncMock(return_value=object()),
    ) as mock_save:
        await orch._persist_canonical_scope_observation(
            _mock_session(), run, scope,  # type: ignore[arg-type]
        )

    mock_prepare.assert_awaited_once()
    mock_compute.assert_called_once()
    mock_check.assert_called_once_with(OBSERVATION)
    mock_save.assert_awaited_once()
    # save 拿到的 prep / observation 与链路一致（位置参数）
    args, kwargs = mock_save.await_args
    assert args[1] is fake_prep
    assert args[2] is OBSERVATION
    assert kwargs["algorithm_version"] == "review-v2.3"


@pytest.mark.asyncio
async def test_unavailable_scope_returns_without_persist():
    """market/major_index/style 或空成员：直接 return，不写表。"""
    for scope_type in ("market", "major_index", "style"):
        run = _make_run()
        scope = _scope(scope_type, "ALL_A_SHARE" if scope_type == "market" else "csi300")
        fake_prep = _prep(scope_type, "x", unavailable=True)

        with patch.object(
            orch, "prepare_scope", AsyncMock(return_value=fake_prep),
        ) as mock_prepare, patch.object(
            orch, "save_scope_observation_fact", AsyncMock(),
        ) as mock_save:
            await orch._persist_canonical_scope_observation(
                _mock_session(), run, scope,  # type: ignore[arg-type]
            )

        mock_prepare.assert_awaited_once()
        mock_save.assert_not_awaited()


@pytest.mark.asyncio
async def test_invariants_failed_raises_value_error():
    """invariant 校验失败必须抛 ValueError（上层 try/except 隔离为 warning）。"""
    run = _make_run()
    scope = _scope("industry_l3", str(uuid.uuid4()))
    fake_prep = _prep("industry_l3", "x")

    with patch.object(
        orch, "prepare_scope", AsyncMock(return_value=fake_prep),
    ), patch.object(
        orch, "compute_scope_observation", return_value=OBSERVATION,
    ), patch.object(
        orch, "check_observation_invariants",
        return_value=[{"ok": False, "name": "scope", "detail": "missing section"}],
    ), patch.object(
        orch, "save_scope_observation_fact", AsyncMock(),
    ) as mock_save:
        with pytest.raises(ValueError, match="scope observation invariant failed"):
            await orch._persist_canonical_scope_observation(
                _mock_session(), run, scope,  # type: ignore[arg-type]
            )
        mock_save.assert_not_awaited()


@pytest.mark.asyncio
async def test_canonical_db_failure_propagates_within_savepoint():
    """CORRECTION: canonical 双写必须在 nested transaction/savepoint 内执行。

    save_scope_observation_fact 抛 DB 错误时，异常必须向上传播（由上层
    _compute_scope_metrics_phase 的 try/except 降级为 warning），且 begin_nested
    已被进入（savepoint 隔离，外层 legacy transaction 可继续提交）。
    """
    run = _make_run()
    scope = _scope("industry_l3", str(uuid.uuid4()))
    fake_prep = _prep("industry_l3", "x")

    session = _mock_session()
    save_error = RuntimeError("psycopg2: deadlock detected")

    with patch.object(
        orch, "prepare_scope", AsyncMock(return_value=fake_prep),
    ), patch.object(
        orch, "compute_scope_observation", return_value=OBSERVATION,
    ), patch.object(
        orch, "check_observation_invariants", return_value=[{"ok": True, "name": "all"}],
    ), patch.object(
        orch, "save_scope_observation_fact", AsyncMock(side_effect=save_error),
    ):
        with pytest.raises(RuntimeError, match="deadlock detected"):
            await orch._persist_canonical_scope_observation(session, run, scope)
        # savepoint 已建立并回滚，异常向外传播（由上层 catch 处理，不污染 legacy）。
        assert session.begin_nested.called
