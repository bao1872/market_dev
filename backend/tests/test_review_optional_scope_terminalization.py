"""[REVIEW-CANONICAL-RUNTIME-REPLACEMENT] Canonical composition 终态化契约测试。

旧 ``_compute_scope_metrics_phase``（含 P/Q/U/C/V snapshot 写入）已物理删除；
唯一 per-scope runtime owner 是 ``_compute_canonical_composition_phase``。
本文件锁定新的终态化契约：

- 非激活家族（market / major_index / style）：ScopeCapability 合法跳过 →
  RUNNING → SKIPPED（结构化 reason），不抛错、不写 legacy snapshot；
- activated 家族（industry_l1/l2/l3/concept）当日无观察（PIT(T) unavailable /
  空成员）→ RUNNING → SKIPPED；
- activated 家族 batch prepare 缺失 → FAIL-CLOSED raise（绝不 silent skip /
  legacy fallback），由外层 scope loop 标记 FAILED；
- invariant / canonical DB 失败 → 异常向上传播（绝不 SKIP）；
- resume 与 compute 共享同一 owner（``_compute_canonical_composition_phase``），
  RUNNING 残留永不阻塞发布门禁；
- 已 succeeded / skipped 的 item 不被 resume 重算；
- ``resolve_scope_members`` 的 typed 转换契约（optional scope）保持不变。

纯单元测试（mock DB 与下游服务），不连接真实数据库。

    PURE_UNIT_TEST=1 backend/.venv/bin/python -m pytest \\
        tests/test_review_optional_scope_terminalization.py
"""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, patch

import pytest

from app.services import review_orchestrator_service as orch
from app.services.board_membership_service import PITMembershipUnavailableError
from app.services.review_orchestrator_service import (
    ITEM_RUNNING,
    ITEM_SKIPPED,
    ITEM_SUCCEEDED,
    PHASE_METRICS,
    ScopeDefinition,
    _compute_canonical_composition_phase,
    resume_run,
)
from app.services.review_scope_service import (
    OPTIONAL_UNAVAILABLE_SCOPE_TYPES,
    OptionalScopeUnavailableError,
    ScopeSnapshotError,
    resolve_scope_members,
)

TRADE_DATE = date(2026, 8, 10)


# =============================================================================
# Helpers
# =============================================================================


def _make_run() -> object:
    return type(
        "R",
        (),
        {
            "id": uuid.uuid4(),
            "trade_date": TRADE_DATE,
            "algorithm_version": "review-v2.3",
            "baseline_window": 120,
            "source_core_run_id": uuid.uuid4(),
        },
    )()


def _scope(scope_type: str, scope_key: str) -> ScopeDefinition:
    return ScopeDefinition(
        scope_type=scope_type,
        scope_key=scope_key,
        scope_name=scope_key,
    )


class _UpsertRecorder:
    """记录 _upsert_run_item 的每次调用，用于断言状态迁移序列。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, _session, **kwargs) -> None:
        self.calls.append(kwargs)

    def statuses(self, phase: str = PHASE_METRICS) -> list[str]:
        return [c["status"] for c in self.calls if c.get("phase") == phase]

    def last(self, phase: str = PHASE_METRICS) -> dict:
        matching = [c for c in self.calls if c.get("phase") == phase]
        assert matching, f"没有 phase={phase} 的 upsert 调用"
        return matching[-1]


async def _run_composition_phase(
    scope: ScopeDefinition,
    *,
    persist_result: dict | None = None,
    persist_error: Exception | None = None,
) -> tuple[dict | None, _UpsertRecorder]:
    """在完全 mock 的下游环境中执行 _compute_canonical_composition_phase。

    ``_persist_canonical_scope_observation`` 是唯一事实来源：返回 None 表示
    合法跳过（非激活家族 / 当日无观察），返回 dict 表示已落库 canonical fact，
    抛错表示 activated 家族 fail-closed 或真实执行异常。
    """
    recorder = _UpsertRecorder()
    run = _make_run()
    persist = AsyncMock(return_value=persist_result)
    if persist_error is not None:
        persist.side_effect = persist_error
    with patch.object(
        orch, "_upsert_run_item", recorder,
    ), patch.object(
        orch, "_persist_canonical_scope_observation", persist,
    ):
        result = await _compute_canonical_composition_phase(
            AsyncMock(),
            run,
            scope,
            prepared_observations={} if persist_result is None else {"k": object()},
        )
    return result, recorder


# =============================================================================
# A. 合法跳过：非激活家族 / 当日无观察 → RUNNING → SKIPPED 终态
# =============================================================================


class TestLegalSkipTerminalizesToSkipped:
    @pytest.mark.parametrize(
        "scope_type",
        ["market", "major_index", "style"],
    )
    async def test_non_activated_family_is_legal_skip(
        self, scope_type: str,
    ):
        """A. 非激活家族按 ScopeCapability 合法跳过 → RUNNING → SKIPPED。"""
        result, recorder = await _run_composition_phase(
            _scope(scope_type, "market" if scope_type == "market" else "k"),
            persist_result=None,
        )

        assert result is None
        # RUNNING → SKIPPED 的确定性终态迁移（无 RUNNING 残留）
        assert recorder.statuses() == [ITEM_RUNNING, ITEM_SKIPPED]
        last = recorder.last()
        assert last["status"] == ITEM_SKIPPED
        # completed_at 必须回填，否则仍是非终态残留
        assert last["completed_at"] is not None
        assert isinstance(last["completed_at"], datetime)
        # last_error 记录稳定的结构化 reason，且绝不回退 legacy P/Q/U/C/V
        assert "legal skip" in last["last_error"]
        assert "fallback" not in last["last_error"]

    async def test_no_observation_today_is_legal_skip(self):
        """A'. activated 家族当日无观察（PIT(T) unavailable / 空成员）→ SKIPPED。"""
        result, recorder = await _run_composition_phase(
            _scope("industry_l1", str(uuid.uuid4())),
            persist_result=None,
        )
        assert result is None
        assert recorder.statuses() == [ITEM_RUNNING, ITEM_SKIPPED]
        assert recorder.last()["completed_at"] is not None


# =============================================================================
# B. 已落库 canonical fact → RUNNING → SUCCEEDED
# =============================================================================


class TestPersistedCompositionSucceeds:
    async def test_persisted_fact_sets_succeeded(self):
        """B. activated 家族持久化 canonical fact → item SUCCEEDED。"""
        composition = {"composition_readiness": "ready", "scope_key": "k"}
        result, recorder = await _run_composition_phase(
            _scope("concept", "k"),
            persist_result=composition,
        )
        assert result is composition
        assert recorder.statuses() == [ITEM_RUNNING, ITEM_SUCCEEDED]
        assert recorder.last()["completed_at"] is not None


# =============================================================================
# C. 真实失败不得被静默 SKIP
# =============================================================================


class TestGenuineFailuresAreNotSkipped:
    async def test_activated_missing_prep_fails_closed(self):
        """C. activated 家族 batch prepare 缺失 → FAIL-CLOSED raise，绝不 SKIP。"""
        recorder = _UpsertRecorder()
        run = _make_run()
        scope = _scope("industry_l1", str(uuid.uuid4()))
        with patch.object(
            orch,
            "_persist_canonical_scope_observation",
            AsyncMock(side_effect=ValueError("canonical batch prepare missing")),
        ), patch.object(orch, "_upsert_run_item", recorder):
            with pytest.raises(ValueError, match="canonical batch prepare missing"):
                await _compute_canonical_composition_phase(
                    AsyncMock(), run, scope, prepared_observations={},
                )
        assert ITEM_SKIPPED not in recorder.statuses()

    async def test_invariant_failure_is_not_skipped(self):
        """C'. invariant 失败必须传播（绝不回退 legacy，绝不 SKIP）。"""
        recorder = _UpsertRecorder()
        run = _make_run()
        scope = _scope("industry_l3", str(uuid.uuid4()))
        with patch.object(
            orch,
            "_persist_canonical_scope_observation",
            AsyncMock(side_effect=ValueError("scope observation invariant failed")),
        ), patch.object(orch, "_upsert_run_item", recorder):
            with pytest.raises(ValueError, match="invariant"):
                await _compute_canonical_composition_phase(
                    AsyncMock(), run, scope, prepared_observations={"k": object()},
                )
        assert ITEM_SKIPPED not in recorder.statuses()

    async def test_unexpected_exception_is_not_skipped(self):
        """C''. 未预期异常（DB/其他）必须传播，绝不 SKIP。"""
        recorder = _UpsertRecorder()
        run = _make_run()
        scope = _scope("style", "large_cap_style")
        with patch.object(
            orch,
            "_persist_canonical_scope_observation",
            AsyncMock(side_effect=RuntimeError("unexpected DB error")),
        ), patch.object(orch, "_upsert_run_item", recorder):
            with pytest.raises(RuntimeError):
                await _compute_canonical_composition_phase(
                    AsyncMock(), run, scope, prepared_observations={},
                )
        assert ITEM_SKIPPED not in recorder.statuses()

    async def test_market_not_in_optional_set(self):
        """C'''. market 不在 legacy optional 集合内 —— 契约级断言。"""
        assert "market" not in OPTIONAL_UNAVAILABLE_SCOPE_TYPES
        assert OPTIONAL_UNAVAILABLE_SCOPE_TYPES == frozenset(
            {"major_index", "style", "industry_l1", "industry_l2", "industry_l3", "concept"},
        )


# =============================================================================
# resolve_scope_members 的 typed 转换契约
# =============================================================================


class TestResolveScopeMembersTypedContract:
    async def test_optional_universe_population_not_ready_is_typed(self):
        membership = type(
            "M", (), {"population_status": "blocked_external_population",
                      "instrument_ids": ()},
        )()
        definition = type("D", (), {"universe_type": "major_index", "name": "沪深300"})()
        with patch(
            "app.services.review_scope_service.resolve_universe_membership_at",
            AsyncMock(return_value=(definition, membership)),
        ):
            with pytest.raises(OptionalScopeUnavailableError) as ei:
                await resolve_scope_members(
                    AsyncMock(), "major_index", "csi300", trade_date=TRADE_DATE,
                )
        exc = ei.value
        assert exc.reason == "population_not_ready"
        assert exc.scope_type == "major_index"
        assert exc.scope_key == "csi300"
        assert exc.population_status == "blocked_external_population"
        assert exc.trade_date == TRADE_DATE
        # 仍是 ScopeSnapshotError 子类，既有 except 分支不被破坏
        assert isinstance(exc, ScopeSnapshotError)

    async def test_optional_universe_pit_unavailable_is_typed(self):
        with patch(
            "app.services.review_scope_service.resolve_universe_membership_at",
            AsyncMock(side_effect=PITMembershipUnavailableError("no PIT version")),
        ):
            with pytest.raises(OptionalScopeUnavailableError) as ei:
                await resolve_scope_members(
                    AsyncMock(), "style", "large_cap_style", trade_date=TRADE_DATE,
                )
        assert ei.value.reason == "pit_membership_unavailable"
        assert ei.value.scope_type == "style"

    async def test_scope_type_mismatch_stays_plain_failure(self):
        membership = type(
            "M", (), {"population_status": "ready", "instrument_ids": ()},
        )()
        definition = type("D", (), {"universe_type": "style", "name": "x"})()
        with patch(
            "app.services.review_scope_service.resolve_universe_membership_at",
            AsyncMock(return_value=(definition, membership)),
        ):
            with pytest.raises(ScopeSnapshotError) as ei:
                await resolve_scope_members(
                    AsyncMock(), "major_index", "csi300", trade_date=TRADE_DATE,
                )
        assert not isinstance(ei.value, OptionalScopeUnavailableError)
        assert "scope_type mismatch" in str(ei.value)

    async def test_invalid_uuid_stays_plain_failure(self):
        with pytest.raises(ScopeSnapshotError) as ei:
            await resolve_scope_members(
                AsyncMock(), "instrument", "not-a-uuid", trade_date=TRADE_DATE,
            )
        assert not isinstance(ei.value, OptionalScopeUnavailableError)

    async def test_optional_board_scope_pit_unavailable_is_typed(self):
        """C'''. concept 属于 optional 集合 —— PIT 不可用是 typed 合法跳过，
        而非普通 failure。"""
        board_id = str(uuid.uuid4())
        board = type("B", (), {"type": "concept", "hierarchyLevel": None, "name": "n"})()
        session = AsyncMock()
        session.execute.return_value = type(
            "R", (), {"scalar_one_or_none": lambda self=None: board},
        )()
        with patch(
            "app.services.review_scope_service.resolve_board_membership_at",
            AsyncMock(side_effect=PITMembershipUnavailableError("no PIT version")),
        ):
            with pytest.raises(OptionalScopeUnavailableError) as ei:
                await resolve_scope_members(
                    session,
                    "concept",
                    board_id,
                    trade_date=TRADE_DATE,
                )
        assert ei.value.reason == "pit_membership_unavailable"
        assert ei.value.scope_type == "concept"
        assert isinstance(ei.value, ScopeSnapshotError)


# =============================================================================
# D. resume 生命周期
# =============================================================================


class _FakeItem:
    def __init__(
        self,
        scope_type: str,
        scope_key: str,
        status: str,
        *,
        phase: str = PHASE_METRICS,
        attempt_count: int = 1,
        lease_expires_at: datetime | None = None,
    ) -> None:
        self.scope_type = scope_type
        self.scope_key = scope_key
        self.status = status
        self.phase = phase
        self.attempt_count = attempt_count
        self.lease_expires_at = lease_expires_at


class TestResumeLifecycle:
    async def _resume_with_items(self, items: list[_FakeItem]):
        """在 mock 环境下运行 resume_run，返回 (result, 被重算的 scope 列表)。"""
        redone: list[tuple[str, str]] = []

        async def fake_composition(_session, _run, scope, **_kwargs):
            redone.append((scope.scope_type, scope.scope_key))
            return None

        async def fake_source(_session, _run):
            return uuid.uuid4(), "h-v2"

        run = type(
            "R",
            (),
            {
                "id": uuid.uuid4(),
                "trade_date": TRADE_DATE,
                "status": "signals_ready",
                "completed_at": None,
                "succeeded_scope_count": 0,
                "failed_scope_count": 0,
                "coverage_ratio": 0,
                "expected_scope_count": len(items),
                "source_core_run_id": uuid.uuid4(),
                "source_board_run_id": uuid.uuid4(),
            },
        )()

        with patch.object(
            orch, "list_run_items", AsyncMock(return_value=items),
        ), patch.object(
            orch, "prepare_current_scope_observations_batch", AsyncMock(return_value={}),
        ), patch.object(
            orch, "_bind_or_reuse_canonical_history_source", fake_source,
        ), patch.object(
            orch, "validate_review_lineage_guard", AsyncMock(),
        ), patch.object(
            orch, "_compute_canonical_composition_phase", fake_composition,
        ), patch.object(
            orch, "_resolve_all_discovery_scopes",
            AsyncMock(return_value=[]),
        ), patch.object(
            orch, "_compute_family_dynamics_maps", AsyncMock(return_value={}),
        ), patch.object(
            # [F1C-A] _count_scope_status 现返回 (succeeded, skipped, failed)
            orch, "_count_scope_status", AsyncMock(return_value=(len(items), 0, 0)),
        ), patch.object(
            orch, "_aggregate_run_data_coverage", AsyncMock(return_value=0),
        ):
            session = AsyncMock()
            result = await resume_run(session, run, only_pending=True)
        return result, redone

    async def test_stale_running_items_are_selected(self):
        """D. resume 精确选中 stale RUNNING（非激活家族），不含已终态 item。"""
        items = [
            _FakeItem("major_index", "csi300", ITEM_RUNNING),
            _FakeItem("major_index", "csi500", ITEM_RUNNING),
            _FakeItem("style", "large_cap_style", ITEM_RUNNING),
            _FakeItem("style", "small_cap_style", ITEM_RUNNING),
            # 已 succeeded / skipped 不得重算
            _FakeItem("market", "ALL_A_SHARE", ITEM_SUCCEEDED),
            _FakeItem("industry_l1", "board-a", ITEM_SUCCEEDED),
            _FakeItem("industry_l1", "board-b", ITEM_SKIPPED),
        ]
        _result, redone = await self._resume_with_items(items)

        assert sorted(redone) == sorted(
            [
                ("major_index", "csi300"),
                ("major_index", "csi500"),
                ("style", "large_cap_style"),
                ("style", "small_cap_style"),
            ],
        )
        # succeeded / skipped 严格不在重算集合内
        assert ("market", "ALL_A_SHARE") not in redone
        assert ("industry_l1", "board-a") not in redone
        assert ("industry_l1", "board-b") not in redone

    async def test_resume_leaves_no_running_residue(self):
        """D'. resume 与 compute 共享同一 owner，合法跳过被终态化为 SKIPPED。"""
        result, recorder = await _run_composition_phase(
            _scope("major_index", "csi300"),
            persist_result=None,
        )
        assert result is None
        # 终态非 RUNNING
        assert recorder.statuses()[-1] == ITEM_SKIPPED
        assert ITEM_RUNNING not in recorder.statuses()[-1:]

    async def test_running_item_with_valid_lease_is_not_resumed(self):
        """未过期租约的 RUNNING 不被抢占（不改动既有 resume 语义）。"""
        future = datetime.now(UTC).replace(year=2099)
        items = [
            _FakeItem(
                "major_index", "csi300", ITEM_RUNNING, lease_expires_at=future,
            ),
        ]
        _result, redone = await self._resume_with_items(items)
        assert redone == []
