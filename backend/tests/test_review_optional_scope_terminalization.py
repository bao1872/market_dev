"""[REVIEW-OPTIONAL-SCOPE-TERMINALIZATION-01 2026-08-10] Optional scope 终态化契约测试。

背景（root cause）：
``_compute_scope_metrics_phase`` 先把 metrics run item 置为 RUNNING，随后调用
``resolve_scope_members``。当 optional scope（major_index / style / industry_l1）的
PIT membership 合法不可用时，旧实现抛出泛型 ``ScopeSnapshotError`` 逃逸到外层
``except Exception``，item 永远停留在 RUNNING —— 而 ``evaluate_publish_gate`` 把
running 视为硬 blocker，导致 Review 永久无法发布。

本文件锁定的契约：
- optional scope PIT 不可用 → typed ``OptionalScopeUnavailableError``
  → metrics item 终态化为 ``skipped`` + ``completed_at`` 非空 + 无异常逃逸；
- market 的真实错误 → 不得 SKIP；
- scope_type mismatch / 非法 UUID 等编码错误 → 不得 SKIP；
- resume 路径与 compute 路径共享同一 ownership point（不分别 patch 两套流程）；
- 已 succeeded / skipped 的 item 不被 resume 重算。

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
    _compute_scope_metrics_phase,
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
            "algorithm_version": "review-v1",
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


async def _run_metrics_phase(scope: ScopeDefinition, resolve_side_effect):
    """在完全 mock 的下游环境中执行 _compute_scope_metrics_phase。"""
    recorder = _UpsertRecorder()
    run = _make_run()
    with patch.object(
        orch, "resolve_scope_members", AsyncMock(side_effect=resolve_side_effect),
    ), patch.object(
        orch, "_upsert_run_item", recorder,
    ), patch.object(
        orch, "_build_scope_history", AsyncMock(return_value=(None, None, None)),
    ), patch.object(
        orch, "_fetch_pyramid_v2_for_scope", AsyncMock(return_value=None),
    ), patch.object(
        orch, "compute_scope_metrics", AsyncMock(return_value=(object(), None)),
    ), patch.object(
        orch, "fetch_member_flat_list", AsyncMock(return_value=[]),
    ):
        # [FIX 5] _compute_scope_metrics_phase 统一返回 (snapshot, history_maps) 二元组，
        # OptionalScopeUnavailableError / 空范围分支返回 (None, None)。
        snapshot, _history = await _compute_scope_metrics_phase(
            AsyncMock(),
            run,
            scope,
            required_history_contract_version="review-history-v2",
            required_source_history_run_id=uuid.uuid4(),
            day_fact_map={},
        )
    return snapshot, recorder


# =============================================================================
# A / B / C. optional scope 不可用 → RUNNING → SKIPPED 终态
# =============================================================================


class TestOptionalScopeTerminalizesToSkipped:
    @pytest.mark.parametrize(
        ("scope_type", "scope_key", "population_status"),
        [
            # A. major_index population blocked_external_population（生产实况）
            ("major_index", "csi300", "blocked_external_population"),
            ("major_index", "csi500", "blocked_external_population"),
            # B. style（生产实况同为 blocked_external_population）
            ("style", "large_cap_style", "blocked_external_population"),
            ("style", "small_cap_style", "blocked_external_population"),
            # C. industry_l1 bootstrap unavailable（publication contract 同为 optional）
            ("industry_l1", str(uuid.uuid4()), "bootstrap_unavailable"),
        ],
    )
    async def test_population_not_ready_becomes_skipped(
        self, scope_type: str, scope_key: str, population_status: str,
    ):
        exc = OptionalScopeUnavailableError(
            reason="population_not_ready",
            scope_type=scope_type,
            scope_key=scope_key,
            population_status=population_status,
            trade_date=TRADE_DATE,
        )
        snapshot, recorder = await _run_metrics_phase(
            _scope(scope_type, scope_key), exc,
        )

        # 无异常逃逸
        assert snapshot is None
        # RUNNING → SKIPPED 的确定性终态迁移
        assert recorder.statuses() == [ITEM_RUNNING, ITEM_SKIPPED]
        last = recorder.last()
        assert last["status"] == ITEM_SKIPPED
        # completed_at 必须回填，否则仍是非终态残留
        assert last["completed_at"] is not None
        assert isinstance(last["completed_at"], datetime)
        # last_error 记录稳定的结构化 reason，而非空
        assert "optional_scope_unavailable" in last["last_error"]
        assert population_status in last["last_error"]

    async def test_pit_membership_unavailable_becomes_skipped(self):
        """B'. PITMembershipUnavailableError 路径同样终态化。"""
        exc = OptionalScopeUnavailableError(
            reason="pit_membership_unavailable",
            scope_type="style",
            scope_key="large_cap_style",
            trade_date=TRADE_DATE,
        )
        snapshot, recorder = await _run_metrics_phase(
            _scope("style", "large_cap_style"), exc,
        )
        assert snapshot is None
        assert recorder.statuses() == [ITEM_RUNNING, ITEM_SKIPPED]
        assert recorder.last()["completed_at"] is not None
        assert "pit_membership_unavailable" in recorder.last()["last_error"]


# =============================================================================
# D / E. 真实失败不得被静默 SKIP
# =============================================================================


class TestGenuineFailuresAreNotSkipped:
    async def test_market_genuine_error_is_not_skipped(self):
        """D. market scope 的真实错误必须传播，不得 SKIP。"""
        scope = _scope("market", "ALL_A_SHARE")
        recorder = _UpsertRecorder()
        run = _make_run()
        with patch.object(
            orch,
            "resolve_scope_members",
            AsyncMock(side_effect=ScopeSnapshotError("market membership 解析失败")),
        ), patch.object(orch, "_upsert_run_item", recorder):
            with pytest.raises(ScopeSnapshotError):
                await _compute_scope_metrics_phase(
                    AsyncMock(), run, scope, day_fact_map={},
                )
        assert ITEM_SKIPPED not in recorder.statuses()

    async def test_market_unavailable_is_not_optional(self):
        """D'. market 不在 optional 集合内 —— 契约级断言。"""
        assert "market" not in OPTIONAL_UNAVAILABLE_SCOPE_TYPES
        assert OPTIONAL_UNAVAILABLE_SCOPE_TYPES == frozenset(
            {"major_index", "style", "industry_l1"},
        )

    async def test_coding_error_is_not_skipped(self):
        """E. scope_type mismatch 等编码错误必须 FAILED，不得 SKIP。"""
        recorder = _UpsertRecorder()
        run = _make_run()
        scope = _scope("major_index", "csi300")
        with patch.object(
            orch,
            "resolve_scope_members",
            AsyncMock(
                side_effect=ScopeSnapshotError(
                    "scope_type mismatch: major_index key=csi300 universe_type=style",
                ),
            ),
        ), patch.object(orch, "_upsert_run_item", recorder):
            with pytest.raises(ScopeSnapshotError):
                await _compute_scope_metrics_phase(
                    AsyncMock(), run, scope, day_fact_map={},
                )
        assert ITEM_SKIPPED not in recorder.statuses()

    async def test_unexpected_exception_is_not_skipped(self):
        """E'. 未预期异常（非 ScopeSnapshotError）必须传播。"""
        recorder = _UpsertRecorder()
        run = _make_run()
        scope = _scope("style", "large_cap_style")
        with patch.object(
            orch,
            "resolve_scope_members",
            AsyncMock(side_effect=RuntimeError("unexpected DB error")),
        ), patch.object(orch, "_upsert_run_item", recorder):
            with pytest.raises(RuntimeError):
                await _compute_scope_metrics_phase(
                    AsyncMock(), run, scope, day_fact_map={},
                )
        assert ITEM_SKIPPED not in recorder.statuses()


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

    async def test_non_optional_board_scope_stays_plain_failure(self):
        """concept / industry_l2 不在 optional 集合 —— 不可用仍是 failure。"""
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
            with pytest.raises(ScopeSnapshotError) as ei:
                await resolve_scope_members(
                    AsyncMock(**{
                        "execute.return_value": type(
                            "R", (), {"scalar_one_or_none": lambda self=None: board},
                        )(),
                    }),
                    "concept",
                    board_id,
                    trade_date=TRADE_DATE,
                )
        assert not isinstance(ei.value, OptionalScopeUnavailableError)


# =============================================================================
# F / G. resume 生命周期
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

        async def fake_pipeline(_session, _run, scope, **_kwargs):
            redone.append((scope.scope_type, scope.scope_key))
            return 0

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
            },
        )()

        with patch.object(
            orch, "list_run_items", AsyncMock(return_value=items),
        ), patch.object(
            orch, "_compute_scope_pipeline", fake_pipeline,
        ), patch.object(
            orch, "evaluate_all_active_trackings", AsyncMock(return_value=0),
        ), patch.object(
            orch, "update_run_signal_count", AsyncMock(return_value=0),
        ), patch.object(
            orch, "_count_scope_status", AsyncMock(return_value=(len(items), 0)),
        ), patch.object(
            orch, "_aggregate_run_data_coverage", AsyncMock(return_value=0),
        ):
            session = AsyncMock()
            result = await resume_run(session, run, only_pending=True)
        return result, redone

    async def test_stale_optional_running_items_are_selected(self):
        """F. resume 精确选中 4 个 stale optional RUNNING，不含其他。"""
        items = [
            # 生产实况：lease_expires_at=NULL, attempt_count=1
            _FakeItem("major_index", "csi300", ITEM_RUNNING),
            _FakeItem("major_index", "csi500", ITEM_RUNNING),
            _FakeItem("style", "large_cap_style", ITEM_RUNNING),
            _FakeItem("style", "small_cap_style", ITEM_RUNNING),
            # G. 已 succeeded / skipped 不得重算
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
        # G. succeeded / skipped 严格不在重算集合内
        assert ("market", "ALL_A_SHARE") not in redone
        assert ("industry_l1", "board-a") not in redone
        assert ("industry_l1", "board-b") not in redone

    async def test_resume_leaves_no_running_residue(self):
        """F'. resume 走同一 ownership point，optional 不可用被终态化。

        _compute_scope_pipeline → _compute_scope_metrics_phase 是唯一 owner，
        因此 resume 与 compute 共享 SKIPPED 行为（不分别 patch 两套流程）。
        """
        scope = _scope("major_index", "csi300")
        exc = OptionalScopeUnavailableError(
            reason="population_not_ready",
            scope_type="major_index",
            scope_key="csi300",
            population_status="blocked_external_population",
            trade_date=TRADE_DATE,
        )
        snapshot, recorder = await _run_metrics_phase(scope, exc)
        assert snapshot is None
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
