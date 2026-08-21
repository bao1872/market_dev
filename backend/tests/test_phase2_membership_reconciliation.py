"""[CHANGE-20260821-001 Phase 2] membership reconciliation 纯单元测试。

验证 ``reconcile_first_pyramid_history_membership`` / ``compute_membership_partition``：

1. universe == run_items        → no-op / 幂等（重跑不重复创建）
2. 新增股票                    → 识别为 new，NOT succeeded（pending）
3. skipped / failed 仍在 universe → 可复评（候选识别），不永久冻结
4. 股票退出 universe            → 不在当前参与集，历史 item 不删除
5. 不同 HistoryRun              → membership 不串 lineage（query scope 到 run_id）
6. Review                       → 零代码修改（绝不调用 readiness / resolver）
7. no-longer-current            → 不物理删除、不改 status
8. RUN-LEVEL COUNTER INVARIANT → expected_count = 累计 lineage membership 计数
   （fresh 0 → +3 =3 → +2 =5 → 退出 1 仍 5 → 重跑仍 5；不同 run 不串）
9. skipped rearm 策略           → 默认不无条件全量复评；rearm 仅对明确授权集
10. exited nonterminal          → 退出 universe 的 pending/failed/running 暴露进 RUN_TERMINALIZATION_SET
   （不进 current daily eligible，但不静默遗弃）；退出 universe 的 succeeded/skipped 为终态不进 terminalization

运行：
    cd backend
    PURE_UNIT_TEST=1 PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest \
        tests/test_phase2_membership_reconciliation.py -v -p no:cacheprovider
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.dialects import postgresql


def _compile(stmt: object) -> str:
    return str(
        stmt.compile(  # type: ignore[attr-defined]
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


# ---------------------------------------------------------------------------
# 纯分区函数测试（不连库、不 mock）：直接覆盖全部 membership 生命周期决策
# ---------------------------------------------------------------------------

class TestMembershipPartitionPure:
    """compute_membership_partition 决策逻辑（无 DB）。"""

    def test_universe_matches_items_is_idempotent(self):
        """universe 与 run_items 完全一致 → no-op（added / no_longer_current 均为空）。"""
        from app.services.first_pyramid_history_service import (
            compute_membership_partition,
        )

        a, b = uuid.uuid4(), uuid.uuid4()
        existing = {a: "succeeded", b: "succeeded"}
        p = compute_membership_partition(existing, [a, b])

        assert p.added == []
        assert p.no_longer_current == []
        assert set(p.retained) == {a, b}
        assert set(p.daily_ready) == {a, b}
        assert p.not_daily_ready == []
        # 重跑确定性一致（幂等的基础）
        assert p == compute_membership_partition(existing, [a, b])

    def test_new_stock_identified_not_succeeded(self):
        """universe 新增股票 → 识别为 new/missing，状态为 pending（NOT succeeded）。"""
        from app.services.first_pyramid_history_service import (
            compute_membership_partition,
        )

        a, b = uuid.uuid4(), uuid.uuid4()
        existing = {a: "succeeded"}
        p = compute_membership_partition(existing, [a, b])

        assert p.added == [b]
        assert b not in p.daily_ready           # 新成员 NOT daily-ready
        assert b in p.not_daily_ready
        assert p.reevaluation_candidates == []  # 无 failed/skipped
        assert p.rearmed_skipped == []
        assert p.skipped_reevaluation_candidates == []

    def test_skipped_failed_can_reevaluate_not_frozen(self):
        """skipped / failed 仍在 universe → 候选识别（可复评），不永久冻结。

        reevaluate_instrument_ids=[b, c] 精确授权：仅 skipped 的 b 被重置为 pending；
        failed 的 c 不在此路径（由 claim_history_items 自动复领）。
        """
        from app.services.first_pyramid_history_service import (
            compute_membership_partition,
        )

        a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        existing = {a: "succeeded", b: "skipped", c: "failed"}
        p = compute_membership_partition(
            existing, [a, b, c], reevaluate_instrument_ids=[b, c],
        )

        # 候选始终识别（不依赖 rearm 策略）
        assert set(p.reevaluation_candidates) == {b, c}
        assert p.skipped_reevaluation_candidates == [b]
        # 实际 rearm：仅被授权的 skipped b
        assert p.rearmed_skipped == [b]
        # 重置后 b 不再 succeeded → 非 daily-ready（待 Phase 3 bootstrap）
        assert b not in p.daily_ready
        assert b in p.not_daily_ready
        # failed 项仍属复评候选（claim_history_items 自动复领）
        assert c in p.reevaluation_candidates

    def test_exited_universe_excluded_history_preserved(self):
        """股票退出 universe → 不在当前参与集，历史 item 保留（不删除、不改 status）。"""
        from app.services.first_pyramid_history_service import (
            compute_membership_partition,
        )

        a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        # c 已 skipped（历史），但已退出 universe
        existing = {a: "succeeded", b: "succeeded", c: "skipped"}
        p = compute_membership_partition(existing, [a, b])

        assert p.no_longer_current == [c]
        assert c not in p.current_expected_participating_set  # 排除出参与集
        # 退出 universe 的 skipped 不复评、不重置（避免重新 bootstrap 已退市标的）
        assert c not in p.reevaluation_candidates
        assert c not in p.skipped_reevaluation_candidates
        assert c not in p.rearmed_skipped
        # 历史 item 的 status 由本函数原样保留（不删除、不改）
        assert existing[c] == "skipped"

    def test_rearm_skipped_false_keeps_skipped(self):
        """rearm_skipped=False（默认）→ skipped 保持 skipped，不进入复评入口。"""
        from app.services.first_pyramid_history_service import (
            compute_membership_partition,
        )

        a, b = uuid.uuid4(), uuid.uuid4()
        existing = {a: "succeeded", b: "skipped"}
        p = compute_membership_partition(existing, [a, b], rearm_skipped=False)

        # 候选仍被识别（不依赖 rearm 策略）
        assert p.skipped_reevaluation_candidates == [b]
        assert b in p.reevaluation_candidates
        # 但默认不重置（actual rearm 为空）
        assert p.rearmed_skipped == []
        assert b not in p.daily_ready  # 仍是 skipped（非 succeeded）

    def test_skipped_reevaluation_candidates_identified_without_rearm(self):
        """候选识别独立于 rearm 策略：rearm_skipped=False 时仍报告 skipped 候选。"""
        from app.services.first_pyramid_history_service import (
            compute_membership_partition,
        )

        a, b = uuid.uuid4(), uuid.uuid4()
        existing = {a: "succeeded", b: "skipped"}
        p = compute_membership_partition(existing, [a, b], rearm_skipped=False)

        assert p.skipped_reevaluation_candidates == [b]
        assert p.reevaluation_candidates == [b]
        # 但默认不重置（actual rearm 为空）
        assert p.rearmed_skipped == []

    def test_reevaluate_instrument_ids_rearms_only_listed_skipped(self):
        """reevaluate_instrument_ids 精确授权：仅重置列表中的 skipped。"""
        from app.services.first_pyramid_history_service import (
            compute_membership_partition,
        )

        a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        existing = {a: "succeeded", b: "skipped", c: "skipped"}
        p = compute_membership_partition(
            existing, [a, b, c], reevaluate_instrument_ids=[b],
        )

        assert set(p.skipped_reevaluation_candidates) == {b, c}
        assert p.rearmed_skipped == [b]   # 仅 b 被授权
        assert c not in p.rearmed_skipped

    def test_exited_terminal_items_excluded_from_daily_eligible_no_terminalization(self):
        """退出 universe 的终态(succeeded/skipped)历史成员：不在 current daily eligible，
        且不属于 terminalization 候选（无需再次 bootstrap）。

        对应契约：today eligible set 不含退市标的；其历史 succeeded/skipped item 已是合法终态，
        Phase 3 不必为它重新生成 target-T state。
        """
        from app.services.first_pyramid_history_service import (
            compute_membership_partition,
        )

        a, b = uuid.uuid4(), uuid.uuid4()
        # 两者均已退出 universe，且均为终态（succeeded / skipped）
        existing = {a: "succeeded", b: "skipped"}
        p = compute_membership_partition(existing, eligible_instrument_ids=[])

        assert set(p.no_longer_current) == {a, b}
        # 不属 current daily eligible 参与集
        assert a not in p.current_expected_participating_set
        assert b not in p.current_expected_participating_set
        # 终态 → 不属于 terminalization 候选（无需 re-bootstrap）
        assert p.no_longer_current_nonterminal == []
        # status 由本函数原样保留（不删除、不改）
        assert existing[a] == "succeeded"
        assert existing[b] == "skipped"

    def test_exited_nonterminal_items_surfaced_not_abandoned(self):
        """退出 universe 的 nonterminal(pending/failed/running)历史成员：不在 current daily eligible，
        但必须暴露进入 RUN_TERMINALIZATION_SET，不得静默遗弃。

        对应契约：若只把 daily advance 限定在 current eligible set，这些已退市但仍 pending/failed/running
        的 item 永远无法 terminalize，整个 canonical run 会因 readiness 检查全 run_items
        (pending/running/failed → NOT READY) 永久卡死。故必须显式识别并交给 Phase 3 terminalize。
        """
        from app.services.first_pyramid_history_service import (
            compute_membership_partition,
        )

        c, d, e = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        # 三者均已退出 universe，且均为非终态
        existing = {c: "pending", d: "failed", e: "running"}
        p = compute_membership_partition(existing, eligible_instrument_ids=[])

        assert set(p.no_longer_current) == {c, d, e}
        # 不属 current daily eligible 参与集（不要求今天 T-state）
        assert c not in p.current_expected_participating_set
        assert d not in p.current_expected_participating_set
        assert e not in p.current_expected_participating_set
        # 但必须进入 terminalization 候选（被显式暴露，未被静默遗弃）
        assert set(p.no_longer_current_nonterminal) == {c, d, e}

    def test_exited_mixed_terminal_vs_nonterminal_split(self):
        """混合场景：退出 universe 的成员里，终态(succeeded)与 nonterminal(pending/failed)必须正确拆分。"""
        from app.services.first_pyramid_history_service import (
            compute_membership_partition,
        )

        a, b, c, d, e = (uuid.uuid4() for _ in range(5))
        # a=在 universe 且 succeeded；b=在 universe 且 skipped
        # c=退出 universe 且 succeeded（终态）；d=退出 universe 且 pending（非终态）；e=退出 universe 且 failed（非终态）
        existing = {
            a: "succeeded", b: "skipped",
            c: "succeeded", d: "pending", e: "failed",
        }
        p = compute_membership_partition(existing, eligible_instrument_ids=[a, b])

        assert set(p.no_longer_current) == {c, d, e}
        assert set(p.no_longer_current_nonterminal) == {d, e}  # c(succeeded) 为终态，不进 terminalization
        assert c not in p.no_longer_current_nonterminal
        # 仍在 universe 的 a/b 不受退出集合影响
        assert set(p.current_expected_participating_set) == {a, b}


# ---------------------------------------------------------------------------
# FakeSession：按 run_id 路由 seed，支持 count / run-load / items-load / UPDATE
# ---------------------------------------------------------------------------

class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def scalar_one(self):
        return self._rows[0] if self._rows else None


class _FakeScalar:
    def __init__(self, value):
        self._value = value

    def scalar_one(self):
        return self._value


class _FakeRun:
    def __init__(self, run_id, expected_count=0):
        self.id = run_id
        self.expected_count = expected_count


class _FakeSession:
    """极简 async session：按 run_id 路由 seed，记录执行的 statement。"""

    def __init__(self, seed_by_run: dict[uuid.UUID, list[tuple[uuid.UUID, str]]]):
        self._seed_by_run = dict(seed_by_run)
        self._runs: dict[uuid.UUID, _FakeRun] = {}
        self.executed: list[object] = []

    def _find_run_id(self, sql: str) -> uuid.UUID | None:
        for rid in set(self._seed_by_run) | set(self._runs):
            if str(rid) in sql:
                return rid
        return None

    async def execute(self, stmt):  # noqa: ANN001
        self.executed.append(stmt)
        sql = _compile(stmt)
        if "first_pyramid_history_run_items" in sql and "count(" in sql:
            run_id = self._find_run_id(sql)
            return _FakeScalar(len(self._seed_by_run.get(run_id, [])))
        if "first_pyramid_history_run_items" in sql:
            run_id = self._find_run_id(sql)
            return _FakeResult(list(self._seed_by_run.get(run_id, [])))
        if "first_pyramid_history_run" in sql:  # run load
            run_id = self._find_run_id(sql)
            if run_id is None:
                return _FakeResult([])
            if run_id not in self._runs:
                self._runs[run_id] = _FakeRun(run_id)
            return _FakeResult([self._runs[run_id]])
        return _FakeResult([])

    async def flush(self):  # noqa: ANN001
        pass

    def add(self, obj):  # noqa: ANN001
        if getattr(obj, "id", None) is not None:
            self._runs[obj.id] = obj


class TestMembershipReconciliationOrchestrator:
    """reconcile_first_pyramid_history_membership 落地动作（FakeSession，不连库）。"""

    @pytest.mark.asyncio
    async def test_new_member_created_pending_rearm_skipped_no_delete(self):
        """新成员建 pending（rearm_skipped=True 显式）；no_longer_current 不删除。"""
        from app.services.first_pyramid_history_service import (
            reconcile_first_pyramid_history_membership,
        )

        run_id = uuid.uuid4()
        a, b, c, d = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        # a=已在 universe 且 succeeded；b=新成员；c=in-universe skipped（复评）；
        # d=已退出 universe 的 skipped 历史 item（保留、不删、不复评）
        seed = {
            run_id: [
                (a, "succeeded"),
                (c, "skipped"),
                (d, "skipped"),
            ]
        }
        session = _FakeSession(seed)

        # create_history_run_items 模拟真实幂等：把 added 落为 pending
        async def _fake_create(sess, rid, ids, **_kw):  # noqa: ANN001
            for iid in ids:
                session._seed_by_run.setdefault(rid, []).append((iid, "pending"))
            return len(ids)

        with patch(
            "app.services.first_pyramid_history_service.create_history_run_items",
            new=_fake_create,
        ), patch(
            "app.services.review_history_readiness_service.validate_canonical_history_run_readiness",
            new=AsyncMock(
                side_effect=AssertionError("Review readiness must not be called")
            ),
        ) as mock_ready, patch(
            "app.services.review_orchestrator_service._resolve_canonical_history_source",
            new=AsyncMock(
                side_effect=AssertionError("Review resolver must not be called")
            ),
        ) as mock_resolve:
            result = await reconcile_first_pyramid_history_membership(
                session,
                history_run_id=run_id,
                eligible_instrument_ids=[a, b, c],  # d 不在 universe
                rearm_skipped=True,
            )

            # Review 零调用
            mock_ready.assert_not_called()
            mock_resolve.assert_not_called()

        p = result.partition
        assert p.added == [b]
        assert p.no_longer_current == [d]
        assert p.rearmed_skipped == [c]
        assert d not in p.current_expected_participating_set

        # 硬边界：不得发出任何 DELETE（no_longer_current 历史保留）
        for stmt in session.executed:
            assert "DELETE FROM first_pyramid_history_run_items" not in _compile(stmt)

        # rearm UPDATE 仅限 in-universe skipped（c），不含已退出 universe 的 d
        rearm_sql = next(
            s for s in session.executed
            if "UPDATE first_pyramid_history_run_items" in _compile(s)
        )
        rearm_sql = _compile(rearm_sql)
        assert str(c) in rearm_sql
        assert str(d) not in rearm_sql
        assert "status = 'skipped'" in rearm_sql

    @pytest.mark.asyncio
    async def test_reconciliation_rerun_is_idempotent_no_duplicate_create(self):
        """reconciliation 重跑 → 不重复创建 run_item（依赖 create_history_run_items 幂等）。"""
        from app.services.first_pyramid_history_service import (
            reconcile_first_pyramid_history_membership,
        )

        run_id = uuid.uuid4()
        a, b = uuid.uuid4(), uuid.uuid4()
        seed = {run_id: [(a, "succeeded")]}
        session = _FakeSession(seed)

        created_calls: list[list[uuid.UUID]] = []

        async def _fake_create(sess, rid, ids, **_kw):  # noqa: ANN001
            created_calls.append(list(ids))
            for iid in ids:
                session._seed_by_run.setdefault(rid, []).append((iid, "pending"))
            return len(ids)

        with patch(
            "app.services.first_pyramid_history_service.create_history_run_items",
            new=_fake_create,
        ):
            r1 = await reconcile_first_pyramid_history_membership(
                session, history_run_id=run_id, eligible_instrument_ids=[a, b],
            )
            # 第二次重跑：seed 已含 b，added 应为空
            r2 = await reconcile_first_pyramid_history_membership(
                session, history_run_id=run_id, eligible_instrument_ids=[a, b],
            )

        # 第一次创建了 b；第二次重跑 added 为空（幂等），create 仅首次调用一次
        assert r1.partition.added == [b]
        assert r2.partition.added == []
        assert created_calls == [[b]]

    @pytest.mark.asyncio
    async def test_load_query_scoped_to_history_run_id_lineage_isolated(self):
        """membership reconciliation 的载入 query 必须 scope 到 history_run_id（不串 lineage）。"""
        from app.services.first_pyramid_history_service import (
            reconcile_first_pyramid_history_membership,
        )

        run_a = uuid.uuid4()
        run_b = uuid.uuid4()
        a, b = uuid.uuid4(), uuid.uuid4()
        seed = {
            run_a: [(a, "succeeded")],
            run_b: [(b, "succeeded")],
        }
        session = _FakeSession(seed)

        with patch(
            "app.services.first_pyramid_history_service.create_history_run_items",
            new=AsyncMock(return_value=0),
        ):
            await reconcile_first_pyramid_history_membership(
                session, history_run_id=run_a, eligible_instrument_ids=[a, b],
            )

        # 载入 query 必须包含 run_a 的 id，且不包含 run_b 的 id（lineage 隔离）
        load_sqls = [
            _compile(s) for s in session.executed
            if "SELECT" in _compile(s) and "first_pyramid_history_run_items" in _compile(s)
            and "count(" not in _compile(s)
        ]
        assert load_sqls, "必须发出 scoped 载入 query"
        for sql in load_sqls:
            assert str(run_a) in sql
            assert str(run_b) not in sql

    @pytest.mark.asyncio
    async def test_expected_count_lifecycle_cumulative_membership(self):
        """RUN-LEVEL COUNTER INVARIANT：expected_count = 累计 lineage membership。

        fresh run(0) → reconcile 3 成员 (3) → 新增 2 (+2 =5) → 退出 1 仍 5（item 不删）
        → 重跑仍 5（幂等）。不更新 succeeded_count/skipped_count。
        """
        from app.services.first_pyramid_history_service import (
            reconcile_first_pyramid_history_membership,
        )

        run_id = uuid.uuid4()
        a, b, c, d, e = (uuid.uuid4() for _ in range(5))
        session = _FakeSession({run_id: []})
        session._runs[run_id] = _FakeRun(run_id, expected_count=0)

        async def _fake_create(sess, rid, ids, **_kw):  # noqa: ANN001
            for iid in ids:
                session._seed_by_run.setdefault(rid, []).append((iid, "pending"))
            return len(ids)

        with patch(
            "app.services.first_pyramid_history_service.create_history_run_items",
            new=_fake_create,
        ), patch(
            "app.services.review_history_readiness_service.validate_canonical_history_run_readiness",
            new=AsyncMock(
                side_effect=AssertionError("Review readiness must not be called")
            ),
        ) as mock_ready, patch(
            "app.services.review_orchestrator_service._resolve_canonical_history_source",
            new=AsyncMock(
                side_effect=AssertionError("Review resolver must not be called")
            ),
        ) as mock_resolve:
            r1 = await reconcile_first_pyramid_history_membership(
                session, history_run_id=run_id, eligible_instrument_ids=[a, b, c],
            )
            assert r1.expected_count == 3
            assert session._runs[run_id].expected_count == 3

            # 新增 2 个成员 → 5
            r2 = await reconcile_first_pyramid_history_membership(
                session, history_run_id=run_id,
                eligible_instrument_ids=[a, b, c, d, e],
            )
            assert r2.expected_count == 5
            assert session._runs[run_id].expected_count == 5

            # 退出 1 个（e 不在 universe）：item 保留，expected_count 不降
            r3 = await reconcile_first_pyramid_history_membership(
                session, history_run_id=run_id, eligible_instrument_ids=[a, b, c, d],
            )
            assert r3.expected_count == 5
            assert session._runs[run_id].expected_count == 5

            # 重跑：仍 5（幂等）
            r4 = await reconcile_first_pyramid_history_membership(
                session, history_run_id=run_id, eligible_instrument_ids=[a, b, c, d],
            )
            assert r4.expected_count == 5

            # Review 零调用（不通过改 Review 来迎合 producer 的 counter 维护）
            mock_ready.assert_not_called()
            mock_resolve.assert_not_called()

        # 硬边界：不得发出任何 DELETE（no-longer-current 历史保留 → expected_count 不降）
        for stmt in session.executed:
            assert "DELETE FROM first_pyramid_history_run_items" not in _compile(stmt)

    @pytest.mark.asyncio
    async def test_expected_count_isolated_across_runs(self):
        """不同 HistoryRun 的 expected_count 不串线（各自累计 membership）。"""
        from app.services.first_pyramid_history_service import (
            reconcile_first_pyramid_history_membership,
        )

        run_a, run_b = uuid.uuid4(), uuid.uuid4()
        a, b = uuid.uuid4(), uuid.uuid4()
        session = _FakeSession({run_a: [], run_b: []})
        session._runs[run_a] = _FakeRun(run_a, 0)
        session._runs[run_b] = _FakeRun(run_b, 0)

        async def _fake_create(sess, rid, ids, **_kw):  # noqa: ANN001
            for iid in ids:
                session._seed_by_run.setdefault(rid, []).append((iid, "pending"))
            return len(ids)

        with patch(
            "app.services.first_pyramid_history_service.create_history_run_items",
            new=_fake_create,
        ), patch(
            "app.services.review_history_readiness_service.validate_canonical_history_run_readiness",
            new=AsyncMock(
                side_effect=AssertionError("Review readiness must not be called")
            ),
        ) as mock_ready, patch(
            "app.services.review_orchestrator_service._resolve_canonical_history_source",
            new=AsyncMock(
                side_effect=AssertionError("Review resolver must not be called")
            ),
        ) as mock_resolve:
            ra = await reconcile_first_pyramid_history_membership(
                session, history_run_id=run_a, eligible_instrument_ids=[a, b],
            )
            rb = await reconcile_first_pyramid_history_membership(
                session, history_run_id=run_b, eligible_instrument_ids=[a],
            )

            assert ra.expected_count == 2
            assert rb.expected_count == 1
            assert session._runs[run_a].expected_count == 2
            assert session._runs[run_b].expected_count == 1
            mock_ready.assert_not_called()
            mock_resolve.assert_not_called()
