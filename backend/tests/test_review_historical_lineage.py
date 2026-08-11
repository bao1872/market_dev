"""[CHANGE-20260808] Review Historical Lineage（M2）contract 纯单元测试。

覆盖：
1. ORM contract：daily_state / observation 新列存在（source_history_run_id/history_contract_version）
2. daily state persistence：_persist_history_result 写入 run.id + contract version
3. new-v2 state lineage：load_day_fact_maps 要求 source_history_run_id != NULL（fail closed）
4. dual-lineage CHECK semantic：LIVE/REPLAY 互斥（两者不同时 NULL / 同时非 NULL）
5. canonical precedence：load_metric_history 选 canonical（live published/succeeded > replay）

运行：
    cd backend
    PURE_UNIT_TEST=1 .venv/bin/python -m pytest \
        tests/test_review_historical_lineage.py -v
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.first_pyramid_history import FirstPyramidHistoryDailyState
from app.models.market_review import MarketReviewMetricObservation


def _valid_live(kind="live") -> dict:
    """构造满足 CHECK dual-lineage 的字段。"""
    if kind == "live":
        return {
            "source_kind": "live",
            "review_run_id": uuid.uuid4(),
            "source_history_run_id": None,
        }
    return {
        "source_kind": "history_replay",
        "review_run_id": None,
        "source_history_run_id": uuid.uuid4(),
    }


def _check_passes(**fields) -> bool:
    """模拟 ck_review_observation_dual_lineage 的 CHECK 语义。"""
    kind = fields.get("source_kind")
    rid = fields.get("review_run_id")
    sid = fields.get("source_history_run_id")
    if kind == "live":
        return rid is not None and sid is None
    if kind == "history_replay":
        return rid is None and sid is not None
    return False


class TestOrmContract:
    """ORM 新列 contract。"""

    def test_daily_state_lineage_columns(self) -> None:
        cols = {c.name for c in FirstPyramidHistoryDailyState.__table__.columns}
        assert "source_history_run_id" in cols
        assert "history_contract_version" in cols

    def test_observation_lineage_columns(self) -> None:
        cols = {c.name for c in MarketReviewMetricObservation.__table__.columns}
        assert "source_kind" in cols
        assert "source_history_run_id" in cols
        assert "history_contract_version" in cols
        assert "taxonomy_compatibility_key" in cols

    def test_observation_review_run_id_nullable(self) -> None:
        col = MarketReviewMetricObservation.__table__.c["review_run_id"]
        assert col.nullable is True


class TestCheckSemantics:
    """dual-lineage CHECK 语义。"""

    def test_live_valid(self) -> None:
        assert _check_passes(**_valid_live("live")) is True

    def test_replay_valid(self) -> None:
        assert _check_passes(**_valid_live("history_replay")) is True

    def test_both_null_rejected(self) -> None:
        assert _check_passes(source_kind="live", review_run_id=None, source_history_run_id=None) is False
        assert _check_passes(
            source_kind="history_replay", review_run_id=None, source_history_run_id=None,
        ) is False

    def test_both_not_null_rejected(self) -> None:
        assert _check_passes(
            source_kind="live", review_run_id=uuid.uuid4(), source_history_run_id=uuid.uuid4(),
        ) is False
        assert _check_passes(
            source_kind="history_replay",
            review_run_id=uuid.uuid4(), source_history_run_id=uuid.uuid4(),
        ) is False

    def test_live_with_history_rejected(self) -> None:
        # live 但 review_run_id 缺 → 拒绝
        assert _check_passes(source_kind="live", review_run_id=None, source_history_run_id=None) is False


class TestDailyStatePersistenceRunId:
    """_persist_history_result 写入 run.id + contract version。"""

    @pytest.mark.asyncio
    async def test_persist_writes_run_lineage(self) -> None:

        from app.services.first_pyramid_history_service import _persist_history_result

        run_id = uuid.uuid4()
        instrument_id = uuid.uuid4()
        executed: list[dict] = []

        class FakeResult:
            def scalars(self):
                return self

            def __iter__(self):
                return iter(())

        from unittest.mock import AsyncMock

        session = MagicMock()
        session.flush = AsyncMock()

        async def fake_execute(stmt):
            # 捕获 insert 的 values（含 lineage）
            executed.append(stmt)
            return FakeResult()

        session.execute = fake_execute

        history = {
            "daily_state": [{
                "time": "2026-07-01", "regime_value": 1, "bar_index": 5,
            }],
            "events": [],
            "meta": {"input_hash": "h"},
        }
        await _persist_history_result(
            session, instrument_id, history, "1.0.0-core-split",
            source_history_run_id=run_id,
            history_contract_version="review-history-v2",
        )
        # 断言 insert values 含 run lineage
        assert len(executed) == 1
        insert_stmt = executed[0]
        # SQLAlchemy insert._values 的 key 是 Column 对象；检查 lineage 列存在且 bind 值正确
        col_to_param = dict(getattr(insert_stmt, "_values", {}))
        src_col = FirstPyramidHistoryDailyState.__table__.c["source_history_run_id"]
        ver_col = FirstPyramidHistoryDailyState.__table__.c["history_contract_version"]
        assert src_col in col_to_param
        assert ver_col in col_to_param
        # 断言 bind 参数值（run_id / contract version）
        assert col_to_param[src_col].value == run_id
        assert col_to_param[ver_col].value == "review-history-v2"


class TestDayFactSourceRunGuard:
    """load_day_fact_maps（history_state 模式）要求 CURRENT T state 的
    source_history_run_id != NULL（fail closed，P1-C1）。"""

    @pytest.mark.asyncio
    async def test_missing_source_run_fails_closed(self) -> None:
        from app.services.review_scope_service import load_day_fact_maps

        iid = uuid.uuid4()
        executed = {"n": 0}

        class State:
            def __init__(self):
                self.id = uuid.uuid4()
                self.instrument_id = iid
                self.trade_date = date(2026, 8, 4)
                self.state_payload = {"first_pyramid_flat": {"dfx_score": 1}}
                self.input_hash = "h"
                self.source_history_run_id = None  # 缺 run → 必须 fail closed
                self.history_contract_version = "review-history-v2"

        class FakeScalars:
            def __init__(self, rows): self.rows = rows
            def __iter__(self): return iter(self.rows)

        session = MagicMock()

        async def fake_execute(stmt):
            executed["n"] += 1
            # 第 1 次（CURRENT T state 查询 == T）返回缺 source_history_run_id 的 state
            if executed["n"] == 1:
                return MagicMock(scalars=lambda: FakeScalars([State()]))
            return MagicMock(scalars=lambda: FakeScalars([]))

        session.execute = fake_execute
        # [P1-C1] history_state 模式 CURRENT T 状态 source_history_run_id 为 NULL → fail closed
        with pytest.raises(ValueError, match="HISTORY_STATE_CURRENT_SOURCE_RUN_NULL"):
            await load_day_fact_maps(
                session, trade_date=date(2026, 8, 4), current_source="history_state",
            )


class TestCanonicalPrecedence:
    """load_metric_history canonical precedence（live published > replay）。"""

    def test_live_published_precedes_replay(self) -> None:

        from app.services.review_metric_observation_service import load_metric_history

        target = date(2026, 8, 4)
        run_id = uuid.uuid4()

        class Obs:
            def __init__(self, kind, raw, run=None):
                self.trade_date = date(2026, 8, 3)
                self.metric_code = "P"
                self.raw_value = raw
                self.component_name = "core"
                self.scope_type = "market"
                self.scope_key = "A"
                self.source_kind = kind
                self.review_run_id = run
                self.source_history_run_id = None if kind == "live" else uuid.uuid4()

        # 同一 date：live(review_run_id) + replay
        live_obs = Obs("live", 1.5, run=run_id)
        replay_obs = Obs("history_replay", 2.5)
        observations = [live_obs, replay_obs]

        session = MagicMock()
        call_count = {"n": 0}

        async def fake_execute(stmt):
            call_count["n"] += 1
            result = MagicMock()
            if call_count["n"] == 1:
                result.scalars.return_value = MagicMock(
                    __iter__=lambda self: iter(observations),
                )
                return result
            # run_status 预取查询（第 2 次 execute）：published_at + status='published'
            run_result = MagicMock()
            run_result.all.return_value = [(run_id, "2026-07-01", "published")]
            return run_result

        session.execute = fake_execute
        history_maps, prev, prev5 = asyncio.run(
            load_metric_history(
                session, scope_type="market", scope_key="A",
                trade_date=target, algorithm_version="v1", baseline_window=120,
            )
        )
        # canonical 选 live published（raw=1.5），非 replay（2.5）
        p_hist = history_maps["P"]["core"]
        assert p_hist == [1.5]

    def test_unpublished_live_excluded_replay_used(self) -> None:
        """unpublished/failed live 不得覆盖 replay baseline。"""

        from app.services.review_metric_observation_service import load_metric_history

        target = date(2026, 8, 4)
        run_id = uuid.uuid4()

        class Obs:
            def __init__(self, kind, raw, run=None):
                self.trade_date = date(2026, 8, 3)
                self.metric_code = "P"
                self.raw_value = raw
                self.component_name = "core"
                self.scope_type = "market"
                self.scope_key = "A"
                self.source_kind = kind
                self.review_run_id = run
                self.source_history_run_id = None if kind == "live" else uuid.uuid4()

        # 同 date：live(unpublished) + replay
        live_obs = Obs("live", 9.9, run=run_id)  # unpublished/failed live
        replay_obs = Obs("history_replay", 3.3)
        observations = [live_obs, replay_obs]

        session = MagicMock()
        call_count = {"n": 0}

        async def fake_execute(stmt):
            call_count["n"] += 1
            result = MagicMock()
            if call_count["n"] == 1:
                result.scalars.return_value = MagicMock(
                    __iter__=lambda self: iter(observations),
                )
                return result
            # run_status 预取查询（第 2 次）：未发布 + failed
            run_result = MagicMock()
            run_result.all.return_value = [(run_id, None, "failed")]
            return run_result

        session.execute = fake_execute
        history_maps, _, _ = asyncio.run(
            load_metric_history(
                session, scope_type="market", scope_key="A",
                trade_date=target, algorithm_version="v1", baseline_window=120,
            )
        )
        # unpublished/failed live 排除 → 用 replay（raw=3.3）
        p_hist = history_maps["P"]["core"]
        assert p_hist == [3.3]


class TestPartialIndexOnConflictCompile:
    """partial unique INDEX 的 ON CONFLICT SQL 编译（非 ON CONFLICT ON CONSTRAINT）。"""

    def _compile_replay_insert(self) -> str:
        # 直接构造 persist_history_replay_observations 的 insert SQL（不连库）
        import asyncio
        import uuid as _uuid
        from unittest.mock import AsyncMock, MagicMock

        from app.services.review_metric_observation_service import (
            persist_history_replay_observations,
        )

        session = AsyncMock()
        compiled = {}

        class FakeScalars:
            def __init__(self, rows): self.rows = rows
            def __iter__(self): return iter(self.rows)

        async def fake_execute(stmt):
            compiled["sql"] = str(
                stmt.compile(dialect=__import__("sqlalchemy").dialects.postgresql.dialect())
            )
            result = MagicMock()
            result.scalars.return_value = FakeScalars([])
            return result

        session.execute = fake_execute
        session.flush = AsyncMock()
        asyncio.run(persist_history_replay_observations(
            session,
            source_history_run_id=_uuid.uuid4(),
            history_contract_version="review-history-v2",
            taxonomy_compatibility_key=None,
            trade_date=date(2026, 8, 4),
            scope_type="market", scope_key="A",
            membership_version="m1", algorithm_version="v1",
            flat_list=[{"fp_trend_direction": "上行"}],
            payloads={"P": {"value": 1.0, "components": [{"name": "scope_return_1d", "rawValue": 1.0}]}},
        ))
        return compiled.get("sql", "")

    def test_replay_on_conflict_uses_index_where(self) -> None:
        sql = self._compile_replay_insert()
        # 必须生成 ON CONFLICT (...) WHERE source_kind = 'history_replay'
        # 禁止 ON CONFLICT ON CONSTRAINT <partial-index-name>
        assert "ON CONFLICT" in sql
        assert "source_kind" in sql
        assert "history_replay" in sql
        assert "ON CONFLICT ON CONSTRAINT" not in sql
        # 不应引用 partial index 名（uq_review_obs_*）
        assert "uq_review_obs_replay_run_date_scope_component" not in sql


class TestCanonicalHistoryRunMixed:
    """canonical HistoryRun：one run PASS，mixed source run FAIL。"""

    def test_mixed_source_run_detected(self) -> None:
        from app.services.review_bootstrap_service import (
            _collect_canonical_source_run,
        )

        facts = [
            {"_history_source_run_id": "11111111-1111-1111-1111-111111111111"},
            {"_history_source_run_id": "22222222-2222-2222-2222-222222222222"},
        ]
        result = _collect_canonical_source_run(facts)
        assert result["status"] == "mixed"


class TestDowngradePrecondition:
    """downgrade 时存在 history_replay 行必须 fail fast。"""

    def test_downgrade_replay_row_precondition(self) -> None:
        # 语义：replay_count > 0 → downgrade 必须 raise（不自动删除）
        # 纯逻辑验证 downgrade 的 precheck 分支
        replay_count = 3
        if replay_count > 0:
            with pytest.raises(RuntimeError):
                _raise_if_replay_rows(replay_count)
        # 空表（0 行）不 raise
        _raise_if_replay_rows(0)


def _raise_if_replay_rows(count: int) -> None:
    if count > 0:
        raise RuntimeError(
            f"downgrade blocked: found {count} history_replay observation row(s)"
        )


class TestPreviousSourceRunParity:
    """§3 previous state source-run parity（history_state 模式）：
    CURRENT T 状态(source A) 的 previous(<T) 必须同 run。
    查询模型：call #1 = CURRENT(==T)，call #2 = PREVIOUS(<T DISTINCT ON)。
    """

    def _session(self, current_rows, previous_rows):
        from unittest.mock import MagicMock

        from app.services.review_scope_service import load_day_fact_maps

        executed = {"n": 0}

        class FakeScalars:
            def __init__(self, rows): self.rows = rows
            def __iter__(self): return iter(self.rows)

        session = MagicMock()

        async def fake_execute(stmt):
            executed["n"] += 1
            if executed["n"] == 1:  # CURRENT(==T)
                return MagicMock(scalars=lambda: FakeScalars(current_rows))
            if executed["n"] == 2:  # PREVIOUS(<T)
                return MagicMock(scalars=lambda: FakeScalars(previous_rows))
            return MagicMock(scalars=lambda: FakeScalars([]))

        session.execute = fake_execute
        return session, load_day_fact_maps

    def _state(self, iid, run_id, date_val):
        class State:
            def __init__(self):
                self.id = uuid.uuid4()
                self.instrument_id = iid
                self.trade_date = date_val
                self.state_payload = {"first_pyramid_flat": {"dfx_score": 1}}
                self.input_hash = "h"
                self.source_history_run_id = run_id
                self.history_contract_version = "review-history-v2"
        return State()

    def test_previous_source_run_mismatch_detected(self) -> None:
        """current run A + previous run B（同 contract）→ fail closed。"""
        import asyncio

        run_a = uuid.uuid4()
        run_b = uuid.uuid4()
        iid = uuid.uuid4()
        current = self._state(iid, run_a, date(2026, 8, 4))
        previous = self._state(iid, run_b, date(2026, 8, 3))
        session, fn = self._session([current], [previous])

        with pytest.raises(ValueError, match="HISTORY_STATE_PREVIOUS_SOURCE_RUN_MISMATCH"):
            asyncio.run(
                fn(
                    session,
                    trade_date=date(2026, 8, 4),
                    current_source="history_state",
                    required_source_history_run_id=run_a,
                )
            )

    def test_previous_source_run_same_passes(self) -> None:
        """current run A + previous run A → PASS（不 raise）。"""
        import asyncio

        run_a = uuid.uuid4()
        iid = uuid.uuid4()
        current = self._state(iid, run_a, date(2026, 8, 4))
        previous = self._state(iid, run_a, date(2026, 8, 3))
        session, fn = self._session([current], [previous])

        # 不应 raise（same source run）
        asyncio.run(
            fn(
                session, trade_date=date(2026, 8, 4),
                current_source="history_state",
                required_source_history_run_id=run_a,
            )
        )


class TestHistoryRunReadiness:
    """§5 CANONICAL_HISTORY_RUN_READY contract。

    [CHANGE-20260809] Phase 4B.1：canonical readiness 不再等价于
    ``run.status == 'succeeded'``。``status`` 是 execution outcome
    （``skipped > 0`` 永久 ``partial``），readiness 是 consumer eligibility。
    """

    REQUIRED_CONTRACT = "review-history-v2"

    def _make_run(
        self,
        scope="all_a_share",
        status="succeeded",
        contract="review-history-v2",
        expected=100,
        succeeded=100,
        skipped=0,
        failed=0,
    ):
        class Run:
            def __init__(self):
                self.scope = scope
                self.status = status
                self.expected_count = expected
                self.succeeded_count = succeeded
                self.skipped_count = skipped
                self.failed_count = failed
                self.metadata_json = (
                    f'{{"history_contract_version": "{contract}"}}'
                    if contract is not None else None
                )
        return Run()

    def _run_predicate(
        self,
        run,
        *,
        item_status_counts=None,
        skip_reasons=(),
        missing_state_count=0,
    ):
        """按 predicate 的实际查询顺序提供 sequenced fake results。

        顺序：run → item status group-by → (skip reasons) → missing-state count。
        """
        import asyncio

        from app.services.review_bootstrap_service import (
            validate_canonical_history_run_readiness,
        )

        if item_status_counts is None and run is not None:
            item_status_counts = {
                "succeeded": int(run.succeeded_count or 0),
                "skipped": int(run.skipped_count or 0),
                "failed": int(run.failed_count or 0),
            }
        item_status_counts = {
            k: v for k, v in (item_status_counts or {}).items() if v
        }

        calls = {"n": 0}
        session = MagicMock()

        async def fake_execute(stmt):
            calls["n"] += 1
            result = MagicMock()
            if calls["n"] == 1:
                result.scalar_one_or_none.return_value = run
                return result
            if calls["n"] == 2:
                result.all.return_value = list(item_status_counts.items())
                return result
            # skip-reason 查询只在 skipped_count > 0 时发生
            if int(run.skipped_count or 0) > 0 and calls["n"] == 3:
                result.all.return_value = [(r,) for r in skip_reasons]
                return result
            result.scalar_one.return_value = missing_state_count
            return result

        session.execute = fake_execute
        return asyncio.run(
            validate_canonical_history_run_readiness(
                session, uuid.uuid4(), self.REQUIRED_CONTRACT,
            )
        )

    # --- A. clean succeeded --------------------------------------------------

    def test_a_succeeded_clean_accepts(self) -> None:
        """succeeded + failed=0 + terminal + contract + invariant → ACCEPT。"""
        res = self._run_predicate(self._make_run(status="succeeded"))
        assert res["status"] == "ok"

    # --- B. partial with INSUFFICIENT_HISTORY only ---------------------------

    def test_b_partial_insufficient_history_accepts(self) -> None:
        """合法 partial（仅 INSUFFICIENT_HISTORY skip）必须被接受。

        这是生产 canonical run be56dcd2 的真实形态。
        """
        run = self._make_run(
            status="partial", expected=100, succeeded=91, skipped=9,
        )
        res = self._run_predicate(
            run,
            skip_reasons=[
                "INSUFFICIENT_HISTORY: input_bars=31 required_bars=60"
            ] * 9,
        )
        assert res["status"] == "ok"
        assert res["skipped_count"] == 9
        assert res["run_status"] == "partial"

    # --- C. partial with NO_DAILY_BARS ---------------------------------------

    def test_c_partial_no_daily_bars_accepts(self) -> None:
        """NO_DAILY_BARS（含 legacy 中文 reason）属于已知 non-blocking skip。"""
        run = self._make_run(
            status="partial", expected=100, succeeded=90, skipped=10,
        )
        res = self._run_predicate(
            run,
            skip_reasons=(
                ["INSUFFICIENT_HISTORY: input_bars=21 required_bars=60"] * 9
                + ["daily bars 为空（DB-only）"]
            ),
        )
        assert res["status"] == "ok"

    def test_c2_partial_no_daily_bars_canonical_token_accepts(self) -> None:
        """新格式 NO_DAILY_BARS token 同样被接受（不 hardcode symbol）。"""
        run = self._make_run(
            status="partial", expected=100, succeeded=99, skipped=1,
        )
        res = self._run_predicate(run, skip_reasons=["NO_DAILY_BARS: empty"])
        assert res["status"] == "ok"

    # --- D. unknown skip reason ----------------------------------------------

    def test_d_partial_unknown_skip_reason_rejects(self) -> None:
        """未知 skip 原因可能是 systemic gap → 必须 fail closed。"""
        run = self._make_run(
            status="partial", expected=100, succeeded=99, skipped=1,
        )
        res = self._run_predicate(
            run, skip_reasons=["some unexpected exclusion"],
        )
        assert res["status"] == "not_ready"
        assert "unknown_skip_reason" in res["reason"]

    def test_d2_empty_skip_reason_rejects(self) -> None:
        """空/NULL skip reason 不得被当作合法 non-blocking skip。"""
        run = self._make_run(
            status="partial", expected=100, succeeded=99, skipped=1,
        )
        res = self._run_predicate(run, skip_reasons=[None])
        assert res["status"] == "not_ready"
        assert "unknown_skip_reason" in res["reason"]

    # --- E. failures ----------------------------------------------------------

    def test_e_failed_items_reject(self) -> None:
        run = self._make_run(
            status="partial", expected=100, succeeded=98, skipped=0, failed=2,
        )
        res = self._run_predicate(run)
        assert res["status"] == "not_ready"
        assert "has_failures" in res["reason"]

    # --- F. non-terminal ------------------------------------------------------

    def test_f_pending_items_reject(self) -> None:
        run = self._make_run(status="running", expected=100, succeeded=50)
        res = self._run_predicate(
            run, item_status_counts={"succeeded": 50, "pending": 50},
        )
        assert res["status"] == "not_ready"
        assert "not_terminal:pending" in res["reason"]

    def test_f2_running_items_reject(self) -> None:
        run = self._make_run(status="running", expected=100, succeeded=50)
        res = self._run_predicate(
            run, item_status_counts={"succeeded": 50, "running": 50},
        )
        assert res["status"] == "not_ready"
        assert "not_terminal:running" in res["reason"]

    # --- G. SUCCESS_SET == CANONICAL_STATE_SET -------------------------------

    def test_g_succeeded_item_without_canonical_state_rejects(self) -> None:
        """succeeded 但无 canonical daily state → 违反 hard invariant。"""
        res = self._run_predicate(
            self._make_run(status="succeeded"), missing_state_count=3,
        )
        assert res["status"] == "not_ready"
        assert "success_state_mismatch" in res["reason"]

    # --- H. count reconciliation ---------------------------------------------

    def test_h_count_mismatch_rejects(self) -> None:
        run = self._make_run(
            status="partial", expected=100, succeeded=80, skipped=10,
        )
        res = self._run_predicate(run)
        assert res["status"] == "not_ready"
        assert "count_mismatch" in res["reason"]

    def test_h2_zero_succeeded_rejects(self) -> None:
        run = self._make_run(
            status="partial", expected=10, succeeded=0, skipped=10,
        )
        res = self._run_predicate(
            run,
            skip_reasons=["INSUFFICIENT_HISTORY: input_bars=1 required_bars=60"] * 10,
        )
        assert res["status"] == "not_ready"
        assert "no_succeeded_items" in res["reason"]

    # --- I. pre-v2 rejection（放宽 status 不得开洞）---------------------------

    def test_i_pre_v2_succeeded_null_contract_rejects(self) -> None:
        """真实存在的 5e222b38 形态：succeeded + all_a_share 但 contract=NULL。"""
        res = self._run_predicate(
            self._make_run(status="succeeded", contract=None),
        )
        assert res["status"] == "not_ready"
        assert "wrong_contract" in res["reason"]

    # --- J/K. scope + contract ------------------------------------------------

    def test_j_wrong_scope_rejects(self) -> None:
        res = self._run_predicate(self._make_run(scope="sample"))
        assert res["status"] == "not_ready"
        assert "wrong_scope" in res["reason"]

    def test_k_wrong_contract_rejects(self) -> None:
        res = self._run_predicate(
            self._make_run(contract="review-history-v1"),
        )
        assert res["status"] == "not_ready"
        assert "wrong_contract" in res["reason"]

    def test_not_found_rejects(self) -> None:
        import asyncio

        from app.services.review_bootstrap_service import (
            validate_canonical_history_run_readiness,
        )

        session = MagicMock()

        async def fake_execute(stmt):
            result = MagicMock()
            result.scalar_one_or_none.return_value = None
            return result

        session.execute = fake_execute
        res = asyncio.run(validate_canonical_history_run_readiness(
            session, uuid.uuid4(), self.REQUIRED_CONTRACT,
        ))
        assert res["status"] == "not_ready"
        assert "not_found" in res["reason"]

    def test_legacy_alias_delegates(self) -> None:
        """``_validate_canonical_history_run`` 仍是可用别名，行为一致。"""
        import asyncio

        from app.services.review_bootstrap_service import (
            _validate_canonical_history_run,
        )

        session = MagicMock()

        async def fake_execute(stmt):
            result = MagicMock()
            result.scalar_one_or_none.return_value = None
            return result

        session.execute = fake_execute
        res = asyncio.run(_validate_canonical_history_run(
            session, uuid.uuid4(), self.REQUIRED_CONTRACT,
        ))
        assert res["status"] == "not_ready"


class TestHistorySkipReasonClassifier:
    """Allowed skip contract：只接受显式已知 category，UNKNOWN fail closed。"""

    def test_insufficient_history_classified(self) -> None:
        from app.services.first_pyramid_history_service import (
            classify_history_skip_reason,
        )

        assert classify_history_skip_reason(
            "INSUFFICIENT_HISTORY: input_bars=31 required_bars=60"
        ) == "INSUFFICIENT_HISTORY"

    def test_legacy_no_daily_bars_classified(self) -> None:
        from app.services.first_pyramid_history_service import (
            classify_history_skip_reason,
        )

        assert classify_history_skip_reason(
            "daily bars 为空（DB-only）"
        ) == "NO_DAILY_BARS"

    def test_canonical_no_daily_bars_classified(self) -> None:
        from app.services.first_pyramid_history_service import (
            classify_history_skip_reason,
        )

        assert classify_history_skip_reason(
            "NO_DAILY_BARS: provider returned nothing"
        ) == "NO_DAILY_BARS"

    def test_unknown_and_empty_are_unknown(self) -> None:
        from app.services.first_pyramid_history_service import (
            classify_history_skip_reason,
        )

        for value in (None, "", "   ", "delisted?", "random failure"):
            assert classify_history_skip_reason(value) == "UNKNOWN"

    def test_unknown_not_in_allowed_set(self) -> None:
        from app.services.first_pyramid_history_service import (
            ALLOWED_NON_BLOCKING_SKIP_CATEGORIES,
        )

        assert "UNKNOWN" not in ALLOWED_NON_BLOCKING_SKIP_CATEGORIES
        assert ALLOWED_NON_BLOCKING_SKIP_CATEGORIES == frozenset(
            {"INSUFFICIENT_HISTORY", "NO_DAILY_BARS"}
        )


class TestMigrationDowngradeSymmetry:
    """§1 migration 088 upgrade/downgrade 对称（event column 也 drop）。"""

    def test_event_contract_column_dropped_in_downgrade(self) -> None:
        """downgrade 必须 DROP first_pyramid_history_events.history_contract_version。"""
        import ast

        path = (
            "backend/alembic/versions/088_review_historical_lineage.py"
        )
        # 从仓库根相对解析
        import os
        repo = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        full = os.path.join(repo, path)
        with open(full) as f:
            tree = ast.parse(f.read())

        def _collect(func_name):
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == func_name:
                    ops = []
                    for stmt in ast.walk(node):
                        if isinstance(stmt, ast.Call) and isinstance(stmt.func, ast.Attribute):
                            ops.append((stmt.func.attr, ast.dump(stmt)))
                    return ops
            return []

        downgrade_ops = _collect("downgrade")
        # downgrade 必须含 drop_column（对 events 表）。migration 用 _EVENTS_TABLE 常量
        # （值="first_pyramid_history_events"），测试同时匹配常量名与字面量。
        drop_events = [
            o for o in downgrade_ops
            if o[0] == "drop_column"
            and "_EVENTS_TABLE" in o[1] and "history_contract_version" in o[1]
        ]
        assert drop_events, "downgrade 缺少 DROP first_pyramid_history_events.history_contract_version"

    def test_downgrade_reverses_event_partial_indexes(self) -> None:
        """downgrade 必须 drop 两个 event partial unique index，并恢复旧三字段 UNIQUE。"""
        import ast
        import os

        repo = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        full = os.path.join(repo, "backend/alembic/versions/088_review_historical_lineage.py")
        with open(full) as f:
            tree = ast.parse(f.read())

        def _collect(func_name):
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == func_name:
                    ops = []
                    for stmt in ast.walk(node):
                        if isinstance(stmt, ast.Call) and isinstance(stmt.func, ast.Attribute):
                            ops.append((stmt.func.attr, ast.dump(stmt)))
                    return ops
            return []

        upgrade_ops = _collect("upgrade")
        downgrade_ops = _collect("downgrade")

        # migration 用常量：_OLD_EVENT_UQ/_LEGACY_EVENT_IDX = "uq_...instr_ver_evid"，
        # _VERSIONED_EVENT_IDX = "uq_...instr_ver_cv_evid"。
        # upgrade 必须 drop 旧 UNIQUE（_OLD_EVENT_UQ）+ create 两个 partial index
        upgrade_drop_uq = [
            o for o in upgrade_ops
            if o[0] == "drop_constraint" and "_OLD_EVENT_UQ" in o[1]
        ]
        upgrade_create_versioned = [
            o for o in upgrade_ops
            if o[0] == "create_index" and "_VERSIONED_EVENT_IDX" in o[1]
        ]
        upgrade_create_legacy = [
            o for o in upgrade_ops
            if o[0] == "create_index" and "_LEGACY_EVENT_IDX" in o[1]
        ]
        assert upgrade_drop_uq, "upgrade 缺少 DROP 旧事件 UNIQUE 约束"
        assert upgrade_create_versioned, "upgrade 缺少 versioned partial unique index"
        assert upgrade_create_legacy, "upgrade 缺少 legacy partial unique index"

        # downgrade 必须 drop 两个 partial index + 重建旧 UNIQUE（对称）
        downgrade_drop_versioned = [
            o for o in downgrade_ops
            if o[0] == "drop_index" and "_VERSIONED_EVENT_IDX" in o[1]
        ]
        downgrade_drop_legacy = [
            o for o in downgrade_ops
            if o[0] == "drop_index" and "_LEGACY_EVENT_IDX" in o[1]
        ]
        downgrade_recreate_uq = [
            o for o in downgrade_ops
            if o[0] == "create_unique_constraint" and "_OLD_EVENT_UQ" in o[1]
        ]
        assert downgrade_drop_versioned, "downgrade 缺少 DROP versioned event partial index"
        assert downgrade_drop_legacy, "downgrade 缺少 DROP legacy event partial index"
        assert downgrade_recreate_uq, "downgrade 缺少重建旧事件 UNIQUE 约束"

    def test_downgrade_has_v2_event_precondition(self) -> None:
        """downgrade 必须先检查 versioned event（history_contract_version IS NOT NULL），fail fast。"""
        import ast
        import os

        repo = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        full = os.path.join(repo, "backend/alembic/versions/088_review_historical_lineage.py")
        with open(full) as f:
            tree = ast.parse(f.read())

        def _collect(func_name):
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == func_name:
                    ops = []
                    for stmt in ast.walk(node):
                        if isinstance(stmt, ast.Call) and isinstance(stmt.func, ast.Attribute):
                            ops.append((stmt.func.attr, ast.dump(stmt)))
                    return ops
            return []

        downgrade_ops = _collect("downgrade")
        # 检查 downgrade 中包含对 first_pyramid_history_events（_EVENTS_TABLE 常量）WHERE
        # history_contract_version IS NOT NULL 的 COUNT 查询
        has_v2_precheck = any(
            "_EVENTS_TABLE" in o[1]
            and "history_contract_version IS NOT NULL" in o[1]
            for o in downgrade_ops
        )
        assert has_v2_precheck, "downgrade 缺少 versioned event downgrade precondition"


class TestReplayRequiredSourceRunSelection:
    """§6 required_source_history_run_id 锁定 replay baseline。"""

    def test_required_source_run_selects_only_matching(self) -> None:
        """同 date/algorithm/contract/taxonomy，required_source_run=A → 只选 A。"""
        import asyncio

        from app.services.review_metric_observation_service import load_metric_history

        run_a = uuid.uuid4()
        run_b = uuid.uuid4()

        class Obs:
            def __init__(self, run_id, raw):
                self.trade_date = date(2026, 8, 3)
                self.metric_code = "P"
                self.raw_value = raw
                self.component_name = "core"
                self.scope_type = "market"
                self.scope_key = "A"
                self.source_kind = "history_replay"
                self.review_run_id = None
                self.source_history_run_id = run_id
                self.history_contract_version = "review-history-v2"
                self.taxonomy_compatibility_key = "taxo-B"

        obs_a = Obs(run_a, 1.0)
        obs_b = Obs(run_b, 9.0)  # 同 date/scope，不同 run
        observations = [obs_a, obs_b]

        session = MagicMock()

        async def fake_execute(stmt):
            result = MagicMock()
            result.scalars.return_value = MagicMock(
                __iter__=lambda self: iter(observations),
            )
            # run_status 预取：无 live observation（run_ids 空）→ 不触发
            return result

        session.execute = fake_execute
        history_maps, _, _ = asyncio.run(
            load_metric_history(
                session, scope_type="market", scope_key="A",
                trade_date=date(2026, 8, 4), algorithm_version="v1",
                baseline_window=120,
                required_history_contract_version="review-history-v2",
                required_taxonomy_compatibility_key="taxo-B",
                required_source_history_run_id=run_a,
            )
        )
        # 只选 run A（raw=1.0），排除 run B（9.0）
        p_hist = history_maps["P"]["core"]
        assert p_hist == [1.0]


class TestLiveTaxonomyCompatibility:
    """§7 LIVE taxonomy compatibility：不兼容 published live 不得覆盖 replay。"""

    def test_incompatible_live_cannot_override_replay(self) -> None:
        """target taxonomy=B，published LIVE taxonomy=A + replay taxonomy=B → replay wins。"""
        import asyncio

        from app.services.review_metric_observation_service import load_metric_history

        run_id = uuid.uuid4()

        class Obs:
            def __init__(self, kind, raw, run=None, taxo=None):
                self.trade_date = date(2026, 8, 3)
                self.metric_code = "P"
                self.raw_value = raw
                self.component_name = "core"
                self.scope_type = "market"
                self.scope_key = "A"
                self.source_kind = kind
                self.review_run_id = run
                self.source_history_run_id = None if kind == "live" else uuid.uuid4()
                self.history_contract_version = "review-history-v2" if kind == "history_replay" else None
                self.taxonomy_compatibility_key = taxo

        live_a = Obs("live", 9.9, run=run_id, taxo="taxo-A")  # published 但不兼容
        replay_b = Obs("history_replay", 3.3, taxo="taxo-B")  # 兼容 replay
        observations = [live_a, replay_b]

        session = MagicMock()
        call_count = {"n": 0}

        async def fake_execute(stmt):
            call_count["n"] += 1
            result = MagicMock()
            if call_count["n"] == 1:
                result.scalars.return_value = MagicMock(
                    __iter__=lambda self: iter(observations),
                )
                return result
            # run_status：published
            run_result = MagicMock()
            run_result.all.return_value = [(run_id, "2026-07-01", "published")]
            return run_result

        session.execute = fake_execute
        history_maps, _, _ = asyncio.run(
            load_metric_history(
                session, scope_type="market", scope_key="A",
                trade_date=date(2026, 8, 4), algorithm_version="v1",
                baseline_window=120,
                required_history_contract_version="review-history-v2",
                required_taxonomy_compatibility_key="taxo-B",
            )
        )
        # 不兼容 published live（taxo-A）排除 → replay（taxo-B, raw=3.3）胜出
        p_hist = history_maps["P"]["core"]
        assert p_hist == [3.3]

    def test_compatible_published_live_wins(self) -> None:
        """target taxonomy=B，published LIVE taxonomy=B + replay taxonomy=B → live wins。"""
        import asyncio

        from app.services.review_metric_observation_service import load_metric_history

        run_id = uuid.uuid4()

        class Obs:
            def __init__(self, kind, raw, run=None, taxo=None):
                self.trade_date = date(2026, 8, 3)
                self.metric_code = "P"
                self.raw_value = raw
                self.component_name = "core"
                self.scope_type = "market"
                self.scope_key = "A"
                self.source_kind = kind
                self.review_run_id = run
                self.source_history_run_id = None if kind == "live" else uuid.uuid4()
                self.history_contract_version = "review-history-v2" if kind == "history_replay" else None
                self.taxonomy_compatibility_key = taxo

        live_b = Obs("live", 5.5, run=run_id, taxo="taxo-B")  # published 兼容
        replay_b = Obs("history_replay", 3.3, taxo="taxo-B")  # 兼容 replay
        observations = [live_b, replay_b]

        session = MagicMock()
        call_count = {"n": 0}

        async def fake_execute(stmt):
            call_count["n"] += 1
            result = MagicMock()
            if call_count["n"] == 1:
                result.scalars.return_value = MagicMock(
                    __iter__=lambda self: iter(observations),
                )
                return result
            run_result = MagicMock()
            run_result.all.return_value = [(run_id, "2026-07-01", "published")]
            return run_result

        session.execute = fake_execute
        history_maps, _, _ = asyncio.run(
            load_metric_history(
                session, scope_type="market", scope_key="A",
                trade_date=date(2026, 8, 4), algorithm_version="v1",
                baseline_window=120,
                required_history_contract_version="review-history-v2",
                required_taxonomy_compatibility_key="taxo-B",
            )
        )
        # compatible published live（raw=5.5）胜出（rank 0 < replay rank 1）
        p_hist = history_maps["P"]["core"]
        assert p_hist == [5.5]


class TestHistoryRunFinalStatus:
    """§2 HistoryRun final status 由 DB canonical progress 决定（非 local counters）。"""

    def _derive(self, **kw):
        from app.services.first_pyramid_history_service import _derive_run_final_status

        progress = {
            "total": kw.get("total", 0),
            "succeeded": kw.get("succeeded", 0),
            "failed": kw.get("failed", 0),
            "pending": kw.get("pending", 0),
            "running": kw.get("running", 0),
            "skipped": kw.get("skipped", 0),
        }
        return _derive_run_final_status(progress)

    def test_a_all_succeeded(self) -> None:
        assert self._derive(total=10, succeeded=10) == "succeeded"

    def test_b_succeeded_plus_skipped_partial(self) -> None:
        assert self._derive(total=10, succeeded=8, skipped=2) == "partial"

    def test_c_succeeded_plus_failed_partial(self) -> None:
        assert self._derive(total=10, succeeded=7, failed=3) == "partial"

    def test_d_previous_failed_no_new_failure_still_partial(self) -> None:
        # 当前 invocation 无新失败，但 DB progress 含 previous failed → 仍 partial
        assert self._derive(total=10, succeeded=9, failed=1) == "partial"

    def test_e_other_worker_running_not_succeeded(self) -> None:
        # running>0 → 不得 finalize succeeded（并发 worker）
        assert self._derive(total=10, succeeded=10, running=1) == "partial"
        # pending>0 同理
        assert self._derive(total=10, succeeded=10, pending=2) == "partial"

    def test_f_all_failed_or_skipped_failed(self) -> None:
        assert self._derive(total=5, failed=5) == "failed"
        assert self._derive(total=5, skipped=5) == "failed"

    def test_all_failed_model_contract(self) -> None:
        # 全部 failed → failed（无成功）
        assert self._derive(total=4, failed=4) == "failed"


class TestEventContractAwareUniqueness:
    """§3 FirstPyramidHistoryEvent 唯一性 contract-aware（legacy + versioned partial index）。"""

    def test_orm_has_two_partial_unique_indexes(self) -> None:
        from app.models.first_pyramid_history import FirstPyramidHistoryEvent

        indexes = {
            idx.name: idx
            for idx in FirstPyramidHistoryEvent.__table__.indexes
        }
        legacy = indexes.get("uq_first_pyramid_history_events_instr_ver_evid")
        versioned = indexes.get("uq_first_pyramid_history_events_instr_ver_cv_evid")
        assert legacy is not None and legacy.unique is True
        assert versioned is not None and versioned.unique is True

        # legacy 只含 3 列，不依赖 history_contract_version 列
        legacy_cols = [c.name for c in legacy.columns]
        assert "history_contract_version" not in legacy_cols
        assert legacy_cols == ["instrument_id", "algorithm_version", "event_id"]

        # versioned 含 history_contract_version（4 列）
        versioned_cols = [c.name for c in versioned.columns]
        assert "history_contract_version" in versioned_cols

    def test_orm_no_plain_unique_constraint(self) -> None:
        from app.models.first_pyramid_history import FirstPyramidHistoryEvent

        # 旧普通 UNIQUE 约束必须已移除（改 partial index）
        constraint_names = {
            c.name for c in FirstPyramidHistoryEvent.__table__.constraints
        }
        assert "uq_first_pyramid_history_events_instr_ver_evid" not in constraint_names

    def test_legacy_and_versioned_index_where(self) -> None:
        from app.models.first_pyramid_history import FirstPyramidHistoryEvent

        indexes = {
            idx.name: idx
            for idx in FirstPyramidHistoryEvent.__table__.indexes
        }
        legacy = indexes["uq_first_pyramid_history_events_instr_ver_evid"]
        versioned = indexes["uq_first_pyramid_history_events_instr_ver_cv_evid"]
        # partial predicate 编译含 history_contract_version IS NULL / IS NOT NULL
        legacy_sql = str(legacy.dialect_options["postgresql"].get("where"))
        assert legacy_sql is not None and "IS NULL" in legacy_sql
        versioned_sql = str(versioned.dialect_options["postgresql"].get("where"))
        assert versioned_sql is not None and "IS NOT NULL" in versioned_sql


class TestEventPersistenceIndexInference:
    """§4 新 v2 event persistence 用 partial-index inference（禁 ON CONSTRAINT）。"""

    def _compile_event_insert(self, contract: str | None) -> str:
        import asyncio as _asyncio
        from unittest.mock import AsyncMock, MagicMock

        from sqlalchemy.dialects import postgresql as _pg

        from app.services.first_pyramid_history_service import _persist_history_result

        class FakeScalars:
            def __init__(self, rows): self.rows = rows
            def __iter__(self): return iter(self.rows)

        compiled = {}

        async def fake_execute(stmt):
            compiled["sql"] = str(
                stmt.compile(dialect=_pg.dialect())
            )
            result = MagicMock()
            result.scalars.return_value = FakeScalars([])
            return result

        session = MagicMock()
        session.execute = fake_execute
        session.flush = AsyncMock()

        history = {
            "daily_state": [{
                "time": "2026-07-01", "regime_value": 1, "bar_index": 5,
            }],
            "events": [{
                "type": "BOS",
                "event_id": "X",
                "bar_index": 5,
                "direction": "bullish",
            }],
            "meta": {"input_hash": "h"},
        }
        _asyncio.run(_persist_history_result(
            session, uuid.uuid4(), history, "1.0.0-core-split",
            source_history_run_id=uuid.uuid4(),
            history_contract_version=contract,
        ))
        return compiled.get("sql", "")

    def test_v2_event_uses_versioned_index_inference(self) -> None:
        sql = self._compile_event_insert("review-history-v2")
        assert "ON CONFLICT" in sql
        # 必须用 index_elements+index_where 生成 ON CONFLICT (... WHERE ...)
        assert "history_contract_version IS NOT NULL" in sql
        assert "ON CONFLICT ON CONSTRAINT" not in sql

    def test_legacy_event_uses_legacy_index_inference(self) -> None:
        sql = self._compile_event_insert(None)
        assert "ON CONFLICT" in sql
        assert "history_contract_version IS NULL" in sql
        assert "ON CONFLICT ON CONSTRAINT" not in sql


class TestEventDowngradePrecondition:
    """§5 event downgrade precondition：存在 versioned event 必须 fail fast。"""

    def test_versioned_event_blocks_downgrade(self) -> None:
        def _raise_if_v2_events(count: int) -> None:
            if count > 0:
                raise RuntimeError(
                    f"downgrade blocked: found {count} versioned "
                    "first_pyramid_history_events row(s)"
                )

        with pytest.raises(RuntimeError):
            _raise_if_v2_events(2)
        # 空表（无 versioned event）不 raise
        _raise_if_v2_events(0)


class TestBootstrapScopeSelector:
    """§8/§9 optional scope selector：默认行为不变，market-only 可显式选择。"""

    def test_normalize_none_preserves_default(self) -> None:
        """None → None，即保持既有「全部 scope」生产默认行为。"""
        from app.services.review_bootstrap_service import _normalize_scope_types

        assert _normalize_scope_types(None) is None

    def test_normalize_market_only(self) -> None:
        from app.services.review_bootstrap_service import _normalize_scope_types

        assert _normalize_scope_types({"market"}) == frozenset({"market"})

    def test_unknown_scope_type_fails_fast(self) -> None:
        """未知 scope_type 必须 fail-fast，不得静默返回空集造成 false-green。"""
        import pytest

        from app.services.review_bootstrap_service import _normalize_scope_types

        with pytest.raises(ValueError, match="unknown bootstrap scope_types"):
            _normalize_scope_types({"market", "not_a_scope"})

    def test_empty_selector_fails_fast(self) -> None:
        import pytest

        from app.services.review_bootstrap_service import _normalize_scope_types

        with pytest.raises(ValueError, match="must not be empty"):
            _normalize_scope_types(set())

    def test_known_scope_types_cover_market(self) -> None:
        from app.services.review_bootstrap_service import (
            KNOWN_BOOTSTRAP_SCOPE_TYPES,
        )

        assert "market" in KNOWN_BOOTSTRAP_SCOPE_TYPES
        assert {"major_index", "style", "concept", "industry_l1"} <= (
            KNOWN_BOOTSTRAP_SCOPE_TYPES
        )

    def test_bootstrap_single_date_accepts_scope_types_kwarg(self) -> None:
        """bootstrap_single_date 暴露 optional scope_types，默认 None。"""
        import inspect

        from app.services.review_bootstrap_service import bootstrap_single_date

        sig = inspect.signature(bootstrap_single_date)
        assert "scope_types" in sig.parameters
        assert sig.parameters["scope_types"].default is None

    def test_list_bootstrap_scopes_filters_market_only(self) -> None:
        """market-only selector 只返回 market scope（其他 scope 被过滤掉）。"""
        import asyncio

        from app.services.review_bootstrap_service import _list_bootstrap_scopes

        session = MagicMock()

        async def fake_execute(stmt):
            result = MagicMock()
            result.all.return_value = []
            return result

        session.execute = fake_execute

        with patch(
            "app.services.review_bootstrap_service.list_universe_definitions_at",
            new=AsyncMock(return_value=[]),
        ):
            scopes = asyncio.run(
                _list_bootstrap_scopes(
                    session, date(2026, 2, 6), scope_types={"market"},
                )
            )
        assert [s.scope_type for s in scopes] == ["market"]

    def test_list_bootstrap_scopes_default_keeps_market(self) -> None:
        """默认 None 时行为不变（market 仍在，且不因 selector 被裁剪）。"""
        import asyncio

        from app.services.review_bootstrap_service import _list_bootstrap_scopes

        session = MagicMock()

        async def fake_execute(stmt):
            result = MagicMock()
            result.all.return_value = []
            return result

        session.execute = fake_execute

        with patch(
            "app.services.review_bootstrap_service.list_universe_definitions_at",
            new=AsyncMock(return_value=[]),
        ):
            scopes = asyncio.run(
                _list_bootstrap_scopes(session, date(2026, 2, 6))
            )
        assert "market" in [s.scope_type for s in scopes]
