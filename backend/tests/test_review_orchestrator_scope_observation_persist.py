"""A 步契约测试：review_orchestrator._persist_canonical_scope_observation。

验证规范 Scope Observation 七段事实层双写（PRD §7.2-§7.17 v2.3）：
- activated scope（industry_l1/l2/l3 + concept）从 batch-prepared map 取
  PreparedScope → compute_scope_observation → check_observation_invariants →
  save_scope_observation_fact
- [REVIEW-EXECUTION-PATH-CONSOLIDATION] 本函数不再调用任何 single-scope
  preparation：PreparedScope 一律由 compute_run / resume_run 通过唯一 owner
  ``prepare_current_scope_observations_batch`` 一次 batch prepare 后传入。
- market / major_index / style：PreparedScope.pit_status_t == unavailable 时
  直接 return，不写表（owner C 兼容生产）
- batch prepare 未包含该 scope（missing key）时直接跳过
- invariant 失败时抛 ValueError（上层 _compute_scope_metrics_phase 的
  try/except 以 warning 记录）

全部为纯单元/mock 测试，不连库（PURE_UNIT_TEST=1）。
"""
from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.domain.review.analysis.leadership_migration import LeadershipMigrationFacts
from app.services import review_orchestrator_service as orch
from app.services.review_observation_prep_service import PreparedScope

pytestmark = pytest.mark.pure_unit


def _make_run() -> object:
    run = type("Run", (), {})()
    run.id = uuid.uuid4()
    run.trade_date = date(2026, 8, 12)
    run.algorithm_version = "review-v2.3"
    # [REVIEW-CANONICAL-RUNTIME-REPLACEMENT] composition 将 per-scope readiness
    # 写入 run.metadata_json，因此 mock run 必须提供可变 metadata_json。
    run.metadata_json = {}
    return run


def _scope(scope_type: str, scope_key: str) -> object:
    definition = type("Scope", (), {})()
    definition.scope_type = scope_type
    definition.scope_key = scope_key
    # A 步过滤用 scope_name；默认给一个非排除的真实主题名，避免误触发过滤。
    definition.scope_name = "锂电池概念"
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
        event_coverage_member_ids=("600000.SH",) if not unavailable else None,
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


def _fake_leadership(scope_key: str) -> LeadershipMigrationFacts:
    """真实运行时由 compute_scope_leadership_batch 产出并注入 leadership_map。

    [REVIEW-BACKEND-FINAL-CLOSURE Phase 5.5] leadership 是 domain dataclass
    （``LeadershipMigrationFacts``），orchestrator 在 _persist_canonical_scope_observation
    内通过唯一 serializer ``serialize_leadership_migration`` 转 dict 后才给
    ``compose_canonical_review_scope``（后者 _layer_status 要求 Mapping）。本 helper
    返回真实 dataclass，确保测试覆盖真实 runtime boundary，而非 fake dict 掩盖类型不匹配。
    """
    return LeadershipMigrationFacts(
        trade_date="2024-01-03",
        status="ready",
        reason=None,
        coverage=1.0,
        previous_direction=None,
        current_direction=1,
        previous_rankable_count=2,
        current_rankable_count=2,
        previous_leader_count=2,
        current_leader_count=2,
        retained_count=2,
        entrant_count=0,
        exit_count=0,
        previous_retention=1.0,
        jaccard_stability=1.0,
        migration=None,
        previous_leader_ids=["A", "B"],
        current_leader_ids=["A", "B"],
        entrant_ids=(),
        exit_ids=(),
    )  # 注意：LeadershipMigrationFacts 无 direction 字段


@pytest.mark.asyncio
async def test_activated_scope_persists_fact():
    """industry_l2 activated scope：从 batch map 取 prep → compute → invariant → save。

    [REVIEW-CANONICAL-RUNTIME-REPLACEMENT] 落库后还会计算 canonical 六键
    composition（internal_structure / member_attribution 由它们各自的 canonical
    owner 产出；这里 mock 其输出，placeholder member 不满足真实 member attribution
    的字段契约）。返回 composition dict，且 composition_readiness 写入 run metadata。
    """
    run = _make_run()
    scope = _scope("industry_l2", "sw_electronics")
    fake_prep = _prep("industry_l2", "sw_electronics")

    with patch.object(
        orch, "compute_scope_observation", return_value=OBSERVATION,
    ) as mock_compute, patch.object(
        orch, "check_observation_invariants", return_value=[{"ok": True, "name": "x"}],
    ) as mock_check, patch.object(
        orch, "save_scope_observation_fact", AsyncMock(return_value=object()),
    ) as mock_save, patch.object(
        orch, "save_scope_composition_snapshot", AsyncMock(return_value=object()),
    ), patch.object(
        orch, "compute_internal_structure", return_value={"status": "ready", "x": 1},
    ), patch.object(
        orch, "compute_member_attribution", return_value={"status": "ready", "y": 1},
    ):
        result = await orch._persist_canonical_scope_observation(
            _mock_session(), run, scope,  # type: ignore[arg-type]
            prepared_observations={"sw_electronics": fake_prep},
            dynamics_map={"sw_electronics": {"status": "ready", "axis": [1]}},
            leadership_map={"sw_electronics": _fake_leadership("sw_electronics")},
        )

    mock_compute.assert_called_once()
    mock_check.assert_called_once_with(OBSERVATION)
    mock_save.assert_awaited_once()
    # save 拿到的 prep / observation 与链路一致（位置参数）
    args, kwargs = mock_save.await_args
    assert args[1] is fake_prep
    assert args[2] is OBSERVATION
    assert kwargs["algorithm_version"] == "review-v2.3"
    # [REVIEW-BACKEND-FINAL-CLOSURE P0] save 必须绑定生成该 fact 的 ReviewRun
    # （run lineage），否则同日双 run 会覆盖 Observation 污染 Composition。
    assert kwargs["review_run_id"] == run.id
    # canonical composition 已产出：六键固定契约 + readiness=ready + 已写入 run metadata
    assert result is not None
    assert result["composition_readiness"] == "ready"
    assert {k for k in ("scope_observation", "historical_dynamics",
                        "internal_structure_facts", "leadership", "member_attribution")
            if k in result} == {"scope_observation", "historical_dynamics",
                                "internal_structure_facts", "leadership", "member_attribution"}
    assert (
        run.metadata_json["canonical_composition_readiness"]["sw_electronics"]
        == "ready"
    )


@pytest.mark.asyncio
async def test_excluded_concept_scope_returns_without_prepare_or_save():
    """A 级机制/资格/事件标签概念：A 步持久化直接排除，连 prep 查找都不做。"""
    run = _make_run()
    scope = _scope("concept", str(uuid.uuid4()))
    scope.scope_name = "融资融券"

    with patch.object(
        orch, "save_scope_observation_fact", AsyncMock(),
    ) as mock_save:
        await orch._persist_canonical_scope_observation(
            _mock_session(), run, scope,  # type: ignore[arg-type]
            prepared_observations={},
        )

    mock_save.assert_not_awaited()


@pytest.mark.asyncio
async def test_small_member_concept_scope_returns_without_save():
    """成员数 <=10 的 concept：A 步持久化排除（按 batch prep 的真实 count 判定），不写表。"""
    run = _make_run()
    scope = _scope("concept", str(uuid.uuid4()))
    scope.scope_name = "赛马概念"
    # _prep 默认 1 个成员，满足 <=10 触发成员数排除。
    fake_prep = _prep("concept", str(uuid.uuid4()))

    with patch.object(
        orch, "compute_scope_observation", return_value=OBSERVATION,
    ), patch.object(
        orch, "check_observation_invariants", return_value=[{"ok": True, "name": "x"}],
    ), patch.object(
        orch, "save_scope_observation_fact", AsyncMock(),
    ) as mock_save:
        await orch._persist_canonical_scope_observation(
            _mock_session(), run, scope,  # type: ignore[arg-type]
            prepared_observations={scope.scope_key: fake_prep},
        )

    mock_save.assert_not_awaited()


@pytest.mark.asyncio
async def test_unavailable_scope_returns_without_persist():
    """market/major_index/style 或空成员：直接 return，不写表。"""
    for scope_type in ("market", "major_index", "style"):
        run = _make_run()
        scope = _scope(scope_type, "ALL_A_SHARE" if scope_type == "market" else "csi300")
        fake_prep = _prep(scope_type, "x", unavailable=True)

        with patch.object(
            orch, "save_scope_observation_fact", AsyncMock(),
        ) as mock_save:
            await orch._persist_canonical_scope_observation(
                _mock_session(), run, scope,  # type: ignore[arg-type]
                prepared_observations={"x": fake_prep},
            )

        mock_save.assert_not_awaited()


@pytest.mark.asyncio
async def test_activated_scope_missing_from_batch_map_fails_closed():
    """[REVIEW-CANONICAL-RUNTIME-REPLACEMENT] ACTIVATED family whose batch prep
    is missing must FAIL CLOSED (raise), and must NOT silently skip or fall back
    to any legacy result.  This replaces the old idle-skip contract. """
    run = _make_run()
    scope = _scope("industry_l1", "sw_electronics")

    with patch.object(
        orch, "compute_scope_observation", return_value=OBSERVATION,
    ), patch.object(
        orch, "save_scope_observation_fact", AsyncMock(),
    ) as mock_save:
        with pytest.raises(ValueError, match="canonical batch prepare missing"):
            await orch._persist_canonical_scope_observation(
                _mock_session(), run, scope,  # type: ignore[arg-type]
                prepared_observations={},
            )

    mock_save.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_activated_family_skips_without_persist():
    """[REVIEW-CANONICAL-RUNTIME-REPLACEMENT] market (non-activated family) is a
    LEGAL SKIP by ScopeCapability regardless of whether its PIT membership
    resolves — it must never reach save_scope_observation_fact, never raise for
    a missing batch map, and never fall back to legacy."""
    run = _make_run()
    scope = _scope("market", "market")

    with patch.object(
        orch, "compute_scope_observation", return_value=OBSERVATION,
    ), patch.object(
        orch, "save_scope_observation_fact", AsyncMock(),
    ) as mock_save:
        await orch._persist_canonical_scope_observation(
            _mock_session(), run, scope,  # type: ignore[arg-type]
            prepared_observations={},  # even if prep absent, market is a legal skip
        )

    mock_save.assert_not_awaited()


@pytest.mark.asyncio
async def test_invariants_failed_raises_value_error():
    """invariant 校验失败必须抛 ValueError（上层 try/except 隔离为 warning）。"""
    run = _make_run()
    scope_key = "x"
    scope = _scope("industry_l3", scope_key)
    fake_prep = _prep("industry_l3", scope_key)

    with patch.object(
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
                prepared_observations={scope_key: fake_prep},
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
    scope_key = "x"
    scope = _scope("industry_l3", scope_key)
    fake_prep = _prep("industry_l3", scope_key)

    session = _mock_session()
    save_error = RuntimeError("psycopg2: deadlock detected")

    with patch.object(
        orch, "compute_scope_observation", return_value=OBSERVATION,
    ), patch.object(
        orch, "check_observation_invariants", return_value=[{"ok": True, "name": "all"}],
    ), patch.object(
        orch, "save_scope_observation_fact", AsyncMock(side_effect=save_error),
    ):
        with pytest.raises(RuntimeError, match="deadlock detected"):
            await orch._persist_canonical_scope_observation(
                session, run, scope,  # type: ignore[arg-type]
                prepared_observations={scope_key: fake_prep},
            )
        # savepoint 已建立并回滚，异常向外传播（由上层 catch 处理，不污染 legacy）。
        assert session.begin_nested.called


@pytest.mark.asyncio
async def test_metadata_persist_uses_whole_dict_reassignment_not_inplace_mutation():
    """P0 correctness：run.metadata_json 必须整体赋新 dict，不能就地 setdefault 改键。

    `MarketReviewRun.metadata_json` 是普通 mapped_column(JSONB)，无 MutableDict；
    就地 dict.setdefault / __setitem__ 不会被 SQLAlchemy 标记 dirty，commit 后
    结果静默丢失（同文件 _bind_or_reuse_canonical_history_source 已踩过同样坑）。

    该测试直接验证行为本质：
    - 传入的「原始」metadata dict 不被原地修改（不含 canonical_composition_readiness）；
    - run.metadata_json 被替换为「新对象」；
    - 新对象含正确的 per-scope readiness / coverage。
    """
    run = _make_run()
    # 预置一个已有其他键的旧 metadata（模拟真实运行中已有字段）
    old_meta: dict = {"existing_key": "existing_value"}
    run.metadata_json = old_meta
    scope = _scope("industry_l2", "sw_electronics")
    fake_prep = _prep("industry_l2", "sw_electronics")

    with patch.object(
        orch, "compute_scope_observation", return_value=OBSERVATION,
    ), patch.object(
        orch, "check_observation_invariants", return_value=[{"ok": True, "name": "x"}],
    ), patch.object(
        orch, "save_scope_observation_fact", AsyncMock(return_value=object()),
    ), patch.object(
        orch, "save_scope_composition_snapshot", AsyncMock(return_value=object()),
    ), patch.object(
        orch, "compute_internal_structure", return_value={"status": "ready", "x": 1},
    ), patch.object(
        orch, "compute_member_attribution", return_value={"status": "ready", "x": 1},
    ):
        await orch._persist_canonical_scope_observation(
            _mock_session(), run, scope,  # type: ignore[arg-type]
            prepared_observations={"sw_electronics": fake_prep},
            dynamics_map={"sw_electronics": {"status": "ready", "axis": [1]}},
            leadership_map={"sw_electronics": _fake_leadership("sw_electronics")},
        )

    # 1) 原始旧 dict 未被就地修改（证明不是 setdefault 原地改键）
    assert "canonical_composition_readiness" not in old_meta
    assert old_meta == {"existing_key": "existing_value"}
    # 2) run.metadata_json 被替换为新对象（不是同一个引用）
    assert run.metadata_json is not old_meta
    assert isinstance(run.metadata_json, dict)
    # 3) 新对象含正确的 per-scope readiness / coverage，且保留旧键
    assert run.metadata_json["existing_key"] == "existing_value"
    assert run.metadata_json["canonical_composition_readiness"]["sw_electronics"] == "ready"
    assert run.metadata_json["canonical_coverage"]["sw_electronics"] == {
        "provided": len(fake_prep.members),
        "eligible": len(fake_prep.pit_member_ids),
    }


@pytest.mark.asyncio
async def test_activated_scope_persists_composition_snapshot():
    """[REVIEW-BACKEND-FINAL-CLOSURE Phase 4] 同一 scope 事务内，orchestrator 必须
    将完整 Composition 落库到 ReviewScopeCompositionSnapshot 薄表（单 JSONB 全存，
    已验证 payload 上限 ~130 KiB），grain = review_run_id + scope_type + scope_key。
    """
    run = _make_run()
    scope = _scope("industry_l2", "sw_electronics")
    fake_prep = _prep("industry_l2", "sw_electronics")

    with patch.object(
        orch, "compute_scope_observation", return_value=OBSERVATION,
    ), patch.object(
        orch, "check_observation_invariants", return_value=[{"ok": True, "name": "x"}],
    ), patch.object(
        orch, "save_scope_observation_fact", AsyncMock(return_value=object()),
    ) as mock_fact, patch.object(
        orch, "save_scope_composition_snapshot", AsyncMock(return_value=object()),
    ) as mock_comp, patch.object(
        orch, "compute_internal_structure", return_value={"status": "ready", "x": 1},
    ), patch.object(
        orch, "compute_member_attribution", return_value={"status": "ready", "y": 1},
    ):
        await orch._persist_canonical_scope_observation(
            _mock_session(), run, scope,  # type: ignore[arg-type]
            prepared_observations={"sw_electronics": fake_prep},
            dynamics_map={"sw_electronics": {"status": "ready", "axis": [1]}},
            leadership_map={"sw_electronics": _fake_leadership("sw_electronics")},
        )

    # 落库被调用一次，且参数携带正确 grain + composition payload
    mock_comp.assert_called_once()
    _, kwargs = mock_comp.call_args
    assert kwargs["review_run_id"] == run.id
    assert kwargs["scope_type"] == "industry_l2"
    assert kwargs["scope_key"] == "sw_electronics"
    assert kwargs["algorithm_version"] == run.algorithm_version
    assert isinstance(kwargs["composition_payload"], dict)
    assert kwargs["composition_payload"]["composition_readiness"] == "ready"
    # fact 与 composition 均落库（两个薄表同事务）
    mock_fact.assert_called_once()


@pytest.mark.asyncio
async def test_activated_scope_persists_with_real_dynamics_producer_shape():
    """REVIEW-DYNAMICS-COMPOSITION-CONTRACT regression (Gate 3).

    The production ``dynamics_map`` value is the raw
    ``compute_current_static_scope_dynamics_batch`` item — NO top-level ``status``
    (keys: scope / membership / observation_series / scope_dynamics / metrics).
    Previously this raised ``ReviewCompositionError`` inside
    ``compose_canonical_review_scope``. The boundary adapter
    ``_adapt_scope_dynamics_to_composition_layer`` derives the layer status from
    the canonical ``scope_dynamics["dynamics_phase"]`` tail, so the full chain
    (real producer shape → boundary → composition) now succeeds.
    """
    run = _make_run()
    scope = _scope("industry_l2", "sw_electronics")
    fake_prep = _prep("industry_l2", "sw_electronics")

    raw_dynamics = {
        "scope": {"scope_type": "industry_l2", "scope_key": "sw_electronics"},
        "membership": {"member_count": 1},
        "observation_series": {"primitives": {}},
        "scope_dynamics": {
            "historical_dynamics": {
                "position": [{"trade_date": "2026-08-12", "value": 0.1, "status": "ready"}],
            },
            "dynamics_phase": [{"trade_date": "2026-08-12", "phase": None, "status": "ready"}],
        },
        "metrics": {"trade_date_count": 1},
    }

    with patch.object(
        orch, "compute_scope_observation", return_value=OBSERVATION,
    ) as mock_compute, patch.object(
        orch, "check_observation_invariants", return_value=[{"ok": True, "name": "x"}],
    ) as mock_check, patch.object(
        orch, "save_scope_observation_fact", AsyncMock(return_value=object()),
    ) as mock_save, patch.object(
        orch, "save_scope_composition_snapshot", AsyncMock(return_value=object()),
    ) as mock_comp, patch.object(
        orch, "compute_internal_structure", return_value={"status": "ready", "x": 1},
    ), patch.object(
        orch, "compute_member_attribution", return_value={"status": "ready", "y": 1},
    ):
        # dynamics_map carries the RAW producer shape (no top-level status)
        result = await orch._persist_canonical_scope_observation(
            _mock_session(), run, scope,  # type: ignore[arg-type]
            prepared_observations={"sw_electronics": fake_prep},
            dynamics_map={"sw_electronics": raw_dynamics},
            leadership_map={"sw_electronics": _fake_leadership("sw_electronics")},
        )

    # Real dynamics entered composition via the adapter, readiness == ready,
    # and the raw scope_dynamics payload is retained (not discarded).
    assert result is not None
    assert result["composition_readiness"] == "ready"
    assert result["historical_dynamics"]["status"] == "ready"
    assert result["historical_dynamics"]["scope_dynamics"] is raw_dynamics["scope_dynamics"]
    assert result["historical_dynamics"]["metrics"] is raw_dynamics["metrics"]
    mock_compute.assert_called_once()
    mock_check.assert_called_once()
    mock_save.assert_awaited_once()
    mock_comp.assert_awaited_once()
