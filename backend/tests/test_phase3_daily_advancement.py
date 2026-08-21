"""[CHANGE-20260821-001 Phase 3] canonical history terminalization + daily advancement 纯单元测试。

验证 ``advance_canonical_history_run_to_trade_date``（PRODUCER lifecycle owner，与 Review 完全独立）：

1. 已完成 canonical run（无 outstanding）→ 不重复 bootstrap，只 advance T
2. 新增 pending member        → 先 terminalize（dispatch backfill），再 advance
3. 退出 universe 的 pending    → 仍 terminalize（whole-run，不要求 current universe）；不要求其 T-state
4. 退出 universe 的 succeeded  → 不重新 bootstrap（无 outstanding 则 backfill 不 dispatch）
5. retryable failed           → backfill dispatched（claim 重领可恢复项）
6. non-expired running        → 不抢 lease；结果明确 incomplete（不谎报 success）
7. 同 T 重跑                  → advance 复用既有 canonical upsert 路径（幂等）
8. T 无事件                   → 合法（不要求 event_count > 0）
9. run counters               → 与实际 run_items（progress）一致
10. initial new run           → mode=INITIAL_BOOTSTRAP_FINALIZATION；bootstrap 后 finalize
11. long-lived run            → NORMAL_DAILY 无 outstanding 时不重 finalize（status 不重置）
12. Review                    → 零代码修改（绝不调用 readiness / resolver）

[Phase 3.1] claimability dispatch 门槛（TERMINALIZATION_DISPATCH 修正）：
   - 仅当存在可自动领取(claimable)的 terminalization work 时才 dispatch backfill：
     claimable = pending / retryable-failed(attempt<max) / expired-running(lease 过期)
   - 非过期 running、attempt 已达上限的 failed 属 BLOCKING_BUT_NOT_CLAIMABLE，worker 领取不到；
     此时不 dispatch backfill（否则其内部 finish_history_run 会无意义重写 status/completed_at），
     结果标记 incomplete 并暴露 active_lease_instruments / retry_exhausted_instruments 让 caller 知悉原因与可解性。
   - analyze_history_run_claimability 严格复刻 claim_history_items 的 WHERE 语义（分区单元测试）。
   - 混合场景：有 claimable 则仍 dispatch 处理可领取部分，并如实暴露剩余 blocker（不谎报 complete）。

运行：
    cd backend
    PURE_UNIT_TEST=1 PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest \
        tests/test_phase3_daily_advancement.py -v -p no:cacheprovider
"""
from __future__ import annotations

import types
import uuid
from contextlib import ExitStack, contextmanager
from datetime import date
from unittest.mock import AsyncMock, patch

import pytest


def _make_run(status: str, *, succeeded=0, failed=0, skipped=0, pending=0, running=0):
    rid = uuid.uuid4()
    total = succeeded + failed + skipped + pending + running
    run = types.SimpleNamespace(
        id=rid,
        status=status,
        algorithm_version="fp_core_v1",
        expected_count=total,
        succeeded_count=succeeded,
        failed_count=failed,
        skipped_count=skipped,
        updated_at=None,
    )
    return run, rid


class _FakeSession:
    """极简 async session：仅支持 orchestrator 直接调用的 session.get（返回注册 run）。"""

    def __init__(self, run):
        self._run = run

    async def get(self, _model, _ident):  # noqa: ANN001
        return self._run

    async def execute(self, _stmt):  # noqa: ANN001
        return None

    async def flush(self):  # noqa: ANN001
        pass

    def add(self, _obj):  # noqa: ANN001
        pass


@contextmanager
def _patch_helpers(progress: dict, advance: dict, *, nonterminal=None, claimability=None, backfill_result=None):
    """统一 mock Phase 3 依赖的既有 helper（PURE_UNIT_TEST 不连库、不真算）。

    Phase 3.1：用 ``analyze_history_run_claimability`` 的 mock 结果驱动 dispatch 门槛；
    默认把全部 nonterminal 视为可领取(claimable)，具体场景用 ``claimability`` 显式覆盖。
    """
    from app.services.first_pyramid_history_service import HistoryRunClaimabilityReport

    nonterminal = nonterminal or []
    backfill_result = backfill_result or {"status": "partial"}

    if claimability is None:
        claimability = HistoryRunClaimabilityReport(
            history_run_id=uuid.uuid4(),
            claimable_count=len(nonterminal),
            claimable_instruments=list(nonterminal),
            pending_nonterminal_instruments=list(nonterminal),
            active_lease_instruments=[],
            retry_exhausted_instruments=[],
        )

    with ExitStack() as stack:
        m_progress = stack.enter_context(
            patch(
                "app.services.first_pyramid_history_service.get_history_run_progress",
                new=AsyncMock(return_value=progress),
            )
        )
        m_backfill = stack.enter_context(
            patch(
                "app.services.first_pyramid_history_service.backfill_history_with_run_items",
                new=AsyncMock(return_value=backfill_result),
            )
        )
        m_refresh = stack.enter_context(
            patch(
                "app.services.first_pyramid_history_service.refresh_history_run_progress_counters",
                new=AsyncMock(),
            )
        )
        m_advance = stack.enter_context(
            patch(
                "app.services.first_pyramid_history_service.advance_history_to_trade_date",
                new=AsyncMock(return_value=advance),
            )
        )
        m_claim = stack.enter_context(
            patch(
                "app.services.first_pyramid_history_service.analyze_history_run_claimability",
                new=AsyncMock(return_value=claimability),
            )
        )
        m_ready = stack.enter_context(
            patch(
                "app.services.review_history_readiness_service.validate_canonical_history_run_readiness",
                new=AsyncMock(
                    side_effect=AssertionError("Review readiness must not be called")
                ),
            )
        )
        m_resolve = stack.enter_context(
            patch(
                "app.services.review_orchestrator_service._resolve_canonical_history_source",
                new=AsyncMock(
                    side_effect=AssertionError("Review resolver must not be called")
                ),
            )
        )
        yield {
            "progress": m_progress,
            "backfill": m_backfill,
            "refresh": m_refresh,
            "advance": m_advance,
            "claim": m_claim,
            "ready": m_ready,
            "resolve": m_resolve,
        }


def _progress(succeeded=0, failed=0, skipped=0, pending=0, running=0):
    total = succeeded + failed + skipped + pending + running
    cov = succeeded / total if total else 0.0
    return {
        "succeeded": succeeded, "failed": failed, "skipped": skipped,
        "pending": pending, "running": running, "total": total, "coverage": cov,
    }


def _advance(target_state_count, *, failed=0, failed_instruments=None, no_bar=0, no_target_state=0):
    return {
        "run_id": "ignored", "trade_date": "2026-08-21", "total": target_state_count,
        "processed": target_state_count, "target_state_count": target_state_count,
        "no_bar": no_bar, "no_target_state": no_target_state, "failed": failed,
        "failed_instruments": failed_instruments or [],
    }


def _claim_report(rid, *, claimable=None, active_lease=None, retry_exhausted=None):
    """构造 Phase 3.1 的 HistoryRunClaimabilityReport（供 _patch_helpers 覆盖默认 claimability）。"""
    from app.services.first_pyramid_history_service import HistoryRunClaimabilityReport

    claimable = list(claimable or [])
    active_lease = list(active_lease or [])
    retry_exhausted = list(retry_exhausted or [])
    nonterminal = claimable + active_lease + retry_exhausted
    return HistoryRunClaimabilityReport(
        history_run_id=rid,
        claimable_count=len(claimable),
        claimable_instruments=claimable,
        pending_nonterminal_instruments=nonterminal,
        active_lease_instruments=active_lease,
        retry_exhausted_instruments=retry_exhausted,
    )


class TestPhase3DailyAdvancement:
    """advance_canonical_history_run_to_trade_date 行为（mock 既有 helper）。"""

    @pytest.mark.asyncio
    async def test_completed_run_no_outstanding_only_advances(self):
        """已完成 canonical run（无 outstanding）→ 不重复 bootstrap，只 advance T。"""
        from app.services.first_pyramid_history_service import (
            advance_canonical_history_run_to_trade_date,
        )

        run, rid = _make_run("partial", succeeded=100)
        progress = _progress(succeeded=100)
        advance = _advance(100)
        session = _FakeSession(run)

        with _patch_helpers(progress, advance) as m:
            result = await advance_canonical_history_run_to_trade_date(
                session, history_run_id=rid, target_trade_date=date(2026, 8, 21),
            )
            m["backfill"].assert_not_called()          # 无 outstanding → 不重 bootstrap
            m["advance"].assert_awaited_once()          # 只 advance T
            m["refresh"].assert_awaited_once()          # 窄 counter refresh 仍执行
            assert result.mode == "NORMAL_DAILY_ADVANCEMENT"
            assert result.status == "complete"

    @pytest.mark.asyncio
    async def test_new_pending_member_terminalized_then_advanced(self):
        """新增 pending member → 先 terminalize（dispatch backfill），再 advance。"""
        from app.services.first_pyramid_history_service import (
            advance_canonical_history_run_to_trade_date,
        )

        run, rid = _make_run("running", succeeded=97, pending=3)
        progress = _progress(succeeded=97, pending=3)
        advance = _advance(97)
        nonterm = [uuid.uuid4() for _ in range(3)]
        session = _FakeSession(run)

        with _patch_helpers(progress, advance, nonterminal=nonterm) as m:
            result = await advance_canonical_history_run_to_trade_date(
                session, history_run_id=rid, target_trade_date=date(2026, 8, 21),
            )
            m["backfill"].assert_awaited_once()         # 有 outstanding → terminalize
            m["advance"].assert_awaited_once()
            assert result.terminalization_dispatched is True
            assert result.pending_nonterminal_instruments == nonterm
            # 真实 terminalization 后 progress 应清零（mock 下仍反映 outstanding → incomplete，属 mock 限制）
            assert result.status == "incomplete"

    @pytest.mark.asyncio
    async def test_exited_pending_still_terminalized_whole_run(self):
        """退出 universe 的 pending（非终态历史成员）→ 仍 terminalize（whole-run，不限 current universe）。"""
        from app.services.first_pyramid_history_service import (
            advance_canonical_history_run_to_trade_date,
        )

        # 该 pending 项已退出 universe（Phase 2 的 no_longer_current_nonterminal），但 Phase 3
        # 按整个 run 的 outstanding 处理，不依赖 universe，故 backfill 必被 dispatch。
        run, rid = _make_run("partial", succeeded=50, pending=1)
        progress = _progress(succeeded=50, pending=1)
        advance = _advance(50)
        nonterm = [uuid.uuid4()]
        session = _FakeSession(run)

        with _patch_helpers(progress, advance, nonterminal=nonterm) as m:
            await advance_canonical_history_run_to_trade_date(
                session, history_run_id=rid, target_trade_date=date(2026, 8, 21),
            )
            # whole-run terminalization 覆盖退出 universe 的非终态项
            m["backfill"].assert_awaited_once()
            args = m["backfill"].call_args.kwargs
            assert args["history_run_id"] == rid  # 不按 universe 过滤

    @pytest.mark.asyncio
    async def test_exited_succeeded_not_rebootstrapped(self):
        """退出 universe 的 succeeded（终态历史成员）→ 无 outstanding → 不重新 bootstrap。"""
        from app.services.first_pyramid_history_service import (
            advance_canonical_history_run_to_trade_date,
        )

        # 全部 succeeded（其中可能含已退出 universe 者）；无 pending/failed/running
        run, rid = _make_run("succeeded", succeeded=100)
        progress = _progress(succeeded=100)
        advance = _advance(100)
        session = _FakeSession(run)

        with _patch_helpers(progress, advance) as m:
            result = await advance_canonical_history_run_to_trade_date(
                session, history_run_id=rid, target_trade_date=date(2026, 8, 21),
            )
            m["backfill"].assert_not_called()          # 无 outstanding → 不重 bootstrap
            assert result.status == "complete"

    @pytest.mark.asyncio
    async def test_retryable_failed_recovered(self):
        """retryable failed → backfill dispatched（claim 重领可恢复项）。"""
        from app.services.first_pyramid_history_service import (
            advance_canonical_history_run_to_trade_date,
        )

        run, rid = _make_run("partial", succeeded=98, failed=2)
        progress = _progress(succeeded=98, failed=2)
        advance = _advance(98)
        nonterm = [uuid.uuid4() for _ in range(2)]
        session = _FakeSession(run)

        with _patch_helpers(progress, advance, nonterminal=nonterm) as m:
            result = await advance_canonical_history_run_to_trade_date(
                session, history_run_id=rid, target_trade_date=date(2026, 8, 21),
            )
            m["backfill"].assert_awaited_once()         # failed > 0 → dispatch terminalization
            assert result.status == "incomplete"        # mock progress 仍含 failed
            assert "failed=2" in (result.reason or "")

    @pytest.mark.asyncio
    async def test_non_expired_running_not_claimed_incomplete(self):
        """non-expired running → 不抢 lease；不 dispatch backfill；结果 incomplete + active_lease blocker。"""
        from app.services.first_pyramid_history_service import (
            advance_canonical_history_run_to_trade_date,
        )

        run, rid = _make_run("partial", succeeded=99, running=1)
        progress = _progress(succeeded=99, running=1)
        advance = _advance(99)
        nonterm = [uuid.uuid4()]
        # Phase 3.1：running 且 lease 未过期 → 不可领取（active_lease），属 BLOCKING_BUT_NOT_CLAIMABLE
        claimability = _claim_report(rid, active_lease=nonterm)
        session = _FakeSession(run)

        with _patch_helpers(progress, advance, claimability=claimability) as m:
            result = await advance_canonical_history_run_to_trade_date(
                session, history_run_id=rid, target_trade_date=date(2026, 8, 21),
            )
            # [Phase 3.1] 仅 active_lease（不可领取）→ 禁止 dispatch backfill（避免空转 finalize 重写 status/completed_at）
            m["backfill"].assert_not_called()
            assert result.terminalization_dispatched is False
            # 结构化结果暴露 blocker 类别与可解性（lease 过期后可由再次自动跑解决）
            assert result.active_lease_instruments == nonterm
            assert result.retry_exhausted_instruments == []
            assert result.status == "incomplete"
            assert "running=1" in (result.reason or "")
            assert "active_lease=1" in (result.reason or "")
            assert result.failed_instruments == []

    @pytest.mark.asyncio
    async def test_same_t_rerun_idempotent(self):
        """同 T 重跑 → advance 复用既有 canonical upsert 路径（幂等），两次均 advance 不报错。"""
        from app.services.first_pyramid_history_service import (
            advance_canonical_history_run_to_trade_date,
        )

        run, rid = _make_run("partial", succeeded=100)
        progress = _progress(succeeded=100)
        advance = _advance(100)
        session = _FakeSession(run)

        with _patch_helpers(progress, advance) as m:
            r1 = await advance_canonical_history_run_to_trade_date(
                session, history_run_id=rid, target_trade_date=date(2026, 8, 21),
            )
            r2 = await advance_canonical_history_run_to_trade_date(
                session, history_run_id=rid, target_trade_date=date(2026, 8, 21),
            )
            assert m["advance"].call_count == 2
            assert r1.status == "complete" and r2.status == "complete"

    @pytest.mark.asyncio
    async def test_zero_events_on_t_legal(self):
        """T 无事件 → 合法（不要求 event_count > 0）；只要求 target_state 存在。"""
        from app.services.first_pyramid_history_service import (
            advance_canonical_history_run_to_trade_date,
        )

        run, rid = _make_run("partial", succeeded=100)
        progress = _progress(succeeded=100)
        # target_state_count>0 但事件数为 0 合法（advance 结果不含 events 闸门）
        advance = _advance(100, no_bar=0, no_target_state=0)
        session = _FakeSession(run)

        with _patch_helpers(progress, advance) as m:
            result = await advance_canonical_history_run_to_trade_date(
                session, history_run_id=rid, target_trade_date=date(2026, 8, 21),
            )
            assert result.status == "complete"
            assert result.advance_summary["target_state_count"] == 100

    @pytest.mark.asyncio
    async def test_counters_consistent_with_items(self):
        """run counters 与实际 run_items（progress）一致。"""
        from app.services.first_pyramid_history_service import (
            advance_canonical_history_run_to_trade_date,
        )

        run, rid = _make_run("partial", succeeded=97, failed=2, skipped=1)
        progress = _progress(succeeded=97, failed=2, skipped=1)
        advance = _advance(97)
        session = _FakeSession(run)

        with _patch_helpers(progress, advance) as m:
            result = await advance_canonical_history_run_to_trade_date(
                session, history_run_id=rid, target_trade_date=date(2026, 8, 21),
            )
            assert result.expected_count == 100
            assert result.succeeded_count == 97
            assert result.failed_count == 2
            assert result.skipped_count == 1

    @pytest.mark.asyncio
    async def test_initial_new_run_finalization_mode(self):
        """initial new run（status=running）→ mode=INITIAL_BOOTSTRAP_FINALIZATION；bootstrap 后 finalize。"""
        from app.services.first_pyramid_history_service import (
            advance_canonical_history_run_to_trade_date,
        )

        run, rid = _make_run("running", succeeded=95, pending=5)
        progress = _progress(succeeded=95, pending=5)
        advance = _advance(95)
        nonterm = [uuid.uuid4() for _ in range(5)]
        session = _FakeSession(run)

        with _patch_helpers(progress, advance, nonterminal=nonterm) as m:
            result = await advance_canonical_history_run_to_trade_date(
                session, history_run_id=rid, target_trade_date=date(2026, 8, 21),
            )
            assert result.mode == "INITIAL_BOOTSTRAP_FINALIZATION"
            m["backfill"].assert_awaited_once()         # 初始 bootstrap → 一次性 finalize

    @pytest.mark.asyncio
    async def test_long_lived_run_no_lifecycle_reset(self):
        """existing long-lived run（status=partial）→ NORMAL_DAILY 无 outstanding 时不重 finalize。"""
        from app.services.first_pyramid_history_service import (
            advance_canonical_history_run_to_trade_date,
        )

        run, rid = _make_run("partial", succeeded=100)
        progress = _progress(succeeded=100)
        advance = _advance(100)
        session = _FakeSession(run)

        with _patch_helpers(progress, advance) as m:
            result = await advance_canonical_history_run_to_trade_date(
                session, history_run_id=rid, target_trade_date=date(2026, 8, 21),
            )
            # 无 outstanding → 不 dispatch backfill（其 finish_history_run 会重钉 status/completed_at）
            m["backfill"].assert_not_called()
            m["refresh"].assert_awaited_once()          # 窄 counter refresh 不碰 status/completed_at
            m["advance"].assert_awaited_once()
            assert run.status == "partial"               # execution lifecycle 不重置
            assert result.mode == "NORMAL_DAILY_ADVANCEMENT"

    @pytest.mark.asyncio
    async def test_review_zero_code_modification(self):
        """Review 零代码修改：绝不调用 validate_canonical_history_run_readiness / resolver。"""
        from app.services.first_pyramid_history_service import (
            advance_canonical_history_run_to_trade_date,
        )

        run, rid = _make_run("partial", succeeded=100)
        progress = _progress(succeeded=100)
        advance = _advance(100)
        session = _FakeSession(run)

        with _patch_helpers(progress, advance) as m:
            await advance_canonical_history_run_to_trade_date(
                session, history_run_id=rid, target_trade_date=date(2026, 8, 21),
            )
            m["ready"].assert_not_called()
            m["resolve"].assert_not_called()


class _FakeClaimSession:
    """支持 analyze_history_run_claimability 的极简 async session：execute 返回含 .all() 的结果。"""

    def __init__(self, rows):
        self._rows = rows

    async def execute(self, _stmt):  # noqa: ANN001
        rows = self._rows

        class _Res:
            def all(self):  # noqa: ANN001
                return rows

        return _Res()


class TestPhase31ClaimabilityAnalysis:
    """analyze_history_run_claimability 严格复刻 claim_history_items 的领取语义。

    直接对分区函数做纯单元测试（不连库），证明 RUN_TERMINALIZATION_SET 的拆分与 worker 实际
    能领取的集合一致：CLAIMABLE = pending / retryable-failed / expired-running；
    BLOCKING_BUT_NOT_CLAIMABLE = active-lease-running / exhausted-failed。
    """

    @pytest.mark.asyncio
    async def test_pending_is_claimable(self):
        from app.services.first_pyramid_history_service import analyze_history_run_claimability

        iid = uuid.uuid4()
        session = _FakeClaimSession([(iid, "pending", 0, None)])
        rep = await analyze_history_run_claimability(session, iid)
        assert rep.claimable_count == 1
        assert rep.claimable_instruments == [iid]
        assert rep.active_lease_instruments == []
        assert rep.retry_exhausted_instruments == []

    @pytest.mark.asyncio
    async def test_expired_running_is_claimable(self):
        from app.services.first_pyramid_history_service import analyze_history_run_claimability
        from datetime import datetime, timedelta, timezone

        iid = uuid.uuid4()
        expired = datetime.now(timezone.utc) - timedelta(seconds=10)
        session = _FakeClaimSession([(iid, "running", 1, expired)])
        rep = await analyze_history_run_claimability(session, iid)
        assert rep.claimable_count == 1
        assert rep.claimable_instruments == [iid]
        assert rep.active_lease_instruments == []

    @pytest.mark.asyncio
    async def test_active_lease_running_not_claimable(self):
        from app.services.first_pyramid_history_service import analyze_history_run_claimability
        from datetime import datetime, timedelta, timezone

        iid = uuid.uuid4()
        future = datetime.now(timezone.utc) + timedelta(seconds=300)
        session = _FakeClaimSession([(iid, "running", 1, future)])
        rep = await analyze_history_run_claimability(session, iid)
        assert rep.claimable_count == 0
        assert rep.active_lease_instruments == [iid]
        assert rep.retry_exhausted_instruments == []

    @pytest.mark.asyncio
    async def test_retryable_failed_is_claimable(self):
        from app.services.first_pyramid_history_service import analyze_history_run_claimability

        iid = uuid.uuid4()
        # attempt_count=2 < max(3) → retryable → claimable
        session = _FakeClaimSession([(iid, "failed", 2, None)])
        rep = await analyze_history_run_claimability(session, iid)
        assert rep.claimable_count == 1
        assert rep.claimable_instruments == [iid]
        assert rep.retry_exhausted_instruments == []

    @pytest.mark.asyncio
    async def test_exhausted_failed_not_claimable(self):
        from app.services.first_pyramid_history_service import analyze_history_run_claimability

        iid = uuid.uuid4()
        # attempt_count=3 >= max(3) → exhausted → 不 claimable
        session = _FakeClaimSession([(iid, "failed", 3, None)])
        rep = await analyze_history_run_claimability(session, iid)
        assert rep.claimable_count == 0
        assert rep.retry_exhausted_instruments == [iid]
        assert rep.active_lease_instruments == []

    @pytest.mark.asyncio
    async def test_mixed_partition(self):
        from app.services.first_pyramid_history_service import analyze_history_run_claimability
        from datetime import datetime, timedelta, timezone

        p = uuid.uuid4()
        rf = uuid.uuid4()   # retryable failed (attempt=1)
        ef = uuid.uuid4()   # exhausted failed (attempt=3)
        ar = uuid.uuid4()   # active-lease running
        er = uuid.uuid4()   # expired running
        now = datetime.now(timezone.utc)
        rows = [
            (p, "pending", 0, None),
            (rf, "failed", 1, None),
            (ef, "failed", 3, None),
            (ar, "running", 1, now + timedelta(seconds=300)),
            (er, "running", 1, now - timedelta(seconds=10)),
        ]
        session = _FakeClaimSession(rows)
        rep = await analyze_history_run_claimability(session, p)
        # claimable = p(pending) + rf(retryable) + er(expired running) = 3
        assert rep.claimable_count == 3
        assert set(rep.claimable_instruments) == {p, rf, er}
        assert rep.active_lease_instruments == [ar]
        assert rep.retry_exhausted_instruments == [ef]
        assert len(rep.pending_nonterminal_instruments) == 5


class TestPhase31TerminalizationDispatch:
    """advance_canonical_history_run_to_trade_date 的 backfill dispatch 门槛（Phase 3.1）。

    仅当存在可自动领取(claimable)的 terminalization work 时才 dispatch backfill；
    非过期 running / attempt 已达上限的 failed 不可领取，禁止 dispatch（避免空转 finalize）。
    """

    @pytest.mark.asyncio
    async def test_pending_dispatches_backfill(self):
        from app.services.first_pyramid_history_service import (
            advance_canonical_history_run_to_trade_date,
        )

        run, rid = _make_run("partial", succeeded=97, pending=3)
        progress = _progress(succeeded=97, pending=3)
        advance = _advance(97)
        nonterm = [uuid.uuid4() for _ in range(3)]
        claimability = _claim_report(rid, claimable=nonterm)
        session = _FakeSession(run)

        with _patch_helpers(progress, advance, claimability=claimability) as m:
            result = await advance_canonical_history_run_to_trade_date(
                session, history_run_id=rid, target_trade_date=date(2026, 8, 21),
            )
            m["backfill"].assert_awaited_once()          # pending 可领取 → terminalize
            assert result.terminalization_dispatched is True

    @pytest.mark.asyncio
    async def test_retryable_failed_dispatches_backfill(self):
        from app.services.first_pyramid_history_service import (
            advance_canonical_history_run_to_trade_date,
        )

        run, rid = _make_run("partial", succeeded=98, failed=2)
        progress = _progress(succeeded=98, failed=2)
        advance = _advance(98)
        nonterm = [uuid.uuid4() for _ in range(2)]
        # failed 且 attempt<max → retryable → claimable
        claimability = _claim_report(rid, claimable=nonterm)
        session = _FakeSession(run)

        with _patch_helpers(progress, advance, claimability=claimability) as m:
            result = await advance_canonical_history_run_to_trade_date(
                session, history_run_id=rid, target_trade_date=date(2026, 8, 21),
            )
            m["backfill"].assert_awaited_once()          # retryable failed 可领取 → retry
            assert result.terminalization_dispatched is True

    @pytest.mark.asyncio
    async def test_expired_running_dispatches_backfill(self):
        from app.services.first_pyramid_history_service import (
            advance_canonical_history_run_to_trade_date,
        )

        run, rid = _make_run("partial", succeeded=99, running=1)
        progress = _progress(succeeded=99, running=1)
        advance = _advance(99)
        iid = uuid.uuid4()
        # running 但 lease 过期 → claimable（reclaim）
        claimability = _claim_report(rid, claimable=[iid])
        session = _FakeSession(run)

        with _patch_helpers(progress, advance, claimability=claimability) as m:
            result = await advance_canonical_history_run_to_trade_date(
                session, history_run_id=rid, target_trade_date=date(2026, 8, 21),
            )
            m["backfill"].assert_awaited_once()          # expired running 可领取 → reclaim
            assert result.terminalization_dispatched is True
            assert result.active_lease_instruments == []  # 非 active-lease

    @pytest.mark.asyncio
    async def test_active_lease_running_no_dispatch(self):
        from app.services.first_pyramid_history_service import (
            advance_canonical_history_run_to_trade_date,
        )

        run, rid = _make_run("partial", succeeded=99, running=1)
        progress = _progress(succeeded=99, running=1)
        advance = _advance(99)
        iid = uuid.uuid4()
        # running 且 lease 未过期 → 不可领取（active_lease）
        claimability = _claim_report(rid, active_lease=[iid])
        session = _FakeSession(run)

        with _patch_helpers(progress, advance, claimability=claimability) as m:
            result = await advance_canonical_history_run_to_trade_date(
                session, history_run_id=rid, target_trade_date=date(2026, 8, 21),
            )
            m["backfill"].assert_not_called()            # 不可领取 → 禁止空转 finalize
            assert result.terminalization_dispatched is False
            assert result.active_lease_instruments == [iid]
            assert result.status == "incomplete"
            assert "active_lease=1" in (result.reason or "")

    @pytest.mark.asyncio
    async def test_exhausted_failed_no_dispatch(self):
        from app.services.first_pyramid_history_service import (
            advance_canonical_history_run_to_trade_date,
        )

        run, rid = _make_run("partial", succeeded=99, failed=1)
        progress = _progress(succeeded=99, failed=1)
        advance = _advance(99)
        iid = uuid.uuid4()
        # failed 且 attempt>=max → 不可领取（retry_exhausted）
        claimability = _claim_report(rid, retry_exhausted=[iid])
        session = _FakeSession(run)

        with _patch_helpers(progress, advance, claimability=claimability) as m:
            result = await advance_canonical_history_run_to_trade_date(
                session, history_run_id=rid, target_trade_date=date(2026, 8, 21),
            )
            m["backfill"].assert_not_called()            # 不可领取 → 禁止空转 finalize
            assert result.terminalization_dispatched is False
            assert result.retry_exhausted_instruments == [iid]
            assert result.status == "incomplete"
            assert "retry_exhausted=1" in (result.reason or "")

    @pytest.mark.asyncio
    async def test_mixed_processes_claimable_exposes_blockers(self):
        from app.services.first_pyramid_history_service import (
            advance_canonical_history_run_to_trade_date,
        )

        # 混合：pending(可领取) + active-lease running(不可领取) + exhausted failed(不可领取)
        run, rid = _make_run("partial", succeeded=97, pending=1, running=1, failed=1)
        progress = _progress(succeeded=97, pending=1, running=1, failed=1)
        advance = _advance(97)
        iid_p = uuid.uuid4()
        iid_a = uuid.uuid4()
        iid_e = uuid.uuid4()
        claimability = _claim_report(
            rid, claimable=[iid_p], active_lease=[iid_a], retry_exhausted=[iid_e],
        )
        session = _FakeSession(run)

        with _patch_helpers(progress, advance, claimability=claimability) as m:
            result = await advance_canonical_history_run_to_trade_date(
                session, history_run_id=rid, target_trade_date=date(2026, 8, 21),
            )
            # 有 claimable(pending) → 仍 dispatch backfill 处理可领取部分
            m["backfill"].assert_awaited_once()
            assert result.terminalization_dispatched is True
            # 但不可领取的 blocker 被如实暴露，不谎报 complete
            assert result.active_lease_instruments == [iid_a]
            assert result.retry_exhausted_instruments == [iid_e]
            assert result.status == "incomplete"
            assert "active_lease=1" in (result.reason or "")
            assert "retry_exhausted=1" in (result.reason or "")
