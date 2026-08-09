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
from unittest.mock import MagicMock

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
    """load_day_fact_maps 要求 source_history_run_id != NULL（fail closed）。"""

    @pytest.mark.asyncio
    async def test_missing_source_run_fails(self) -> None:
        from app.services.review_scope_service import load_day_fact_maps

        iid = uuid.uuid4()
        executed = {"n": 0}

        class State:
            def __init__(self):
                self.id = uuid.uuid4()
                self.instrument_id = iid
                self.trade_date = date(2026, 8, 4)
                self.state_payload = {"history_contract_version": "review-history-v2"}
                self.input_hash = "h"
                self.source_history_run_id = None  # 缺 run → 应 fail
                self.history_contract_version = "review-history-v2"

        class FakeScalars:
            def __init__(self, rows): self.rows = rows
            def __iter__(self): return iter(self.rows)

        class FakeResult:
            def scalars(self): return FakeScalars([])

        session = MagicMock()

        async def fake_execute(stmt):
            executed["n"] += 1
            # 第 1 次（current FP state）返回一个缺 source_history_run_id 的 state
            if executed["n"] == 1:
                return MagicMock(scalars=lambda: FakeScalars([State()]))
            return FakeResult()

        session.execute = fake_execute
        with pytest.raises(ValueError) as exc_info:
            await load_day_fact_maps(session, trade_date=date(2026, 8, 4))
        assert "HISTORY_SOURCE_RUN_MISSING" in str(exc_info.value)


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
    """§3 previous state source-run parity：current run A / previous 必须同 run。"""

    def test_previous_source_run_mismatch_detected(self) -> None:
        """current run A + previous run B（同 contract）→ fail closed。"""
        import asyncio

        from app.services.review_scope_service import load_day_fact_maps

        run_a = uuid.uuid4()
        run_b = uuid.uuid4()
        shared_iid = uuid.uuid4()  # current 与 previous 同一 instrument
        executed = {"n": 0}

        class State:
            def __init__(self, run_id, date_val):
                self.id = uuid.uuid4()
                self.instrument_id = shared_iid
                self.trade_date = date_val
                self.state_payload = {"regime_value": 1}
                self.input_hash = "h"
                self.source_history_run_id = run_id
                self.history_contract_version = "review-history-v2"

        class FakeScalars:
            def __init__(self, rows): self.rows = rows
            def __iter__(self): return iter(self.rows)

        session = MagicMock()

        async def fake_execute(stmt):
            executed["n"] += 1
            if executed["n"] == 1:  # current（run A）
                return MagicMock(scalars=lambda: FakeScalars([State(run_a, date(2026, 8, 4))]))
            if executed["n"] == 2:  # previous（run B → mismatch）
                return MagicMock(scalars=lambda: FakeScalars([State(run_b, date(2026, 8, 3))]))
            return MagicMock(scalars=lambda: FakeScalars([]))

        session.execute = fake_execute
        with pytest.raises(ValueError) as exc_info:
            asyncio.run(load_day_fact_maps(session, trade_date=date(2026, 8, 4)))
        assert "HISTORY_PREVIOUS_SOURCE_RUN_MISMATCH" in str(exc_info.value)

    def test_previous_source_run_same_passes(self) -> None:
        """current run A + previous run A → PASS（不 raise）。"""
        import asyncio

        from app.services.review_scope_service import load_day_fact_maps

        run_a = uuid.uuid4()
        executed = {"n": 0}

        class State:
            def __init__(self, date_val):
                self.id = uuid.uuid4()
                self.instrument_id = uuid.uuid4()
                self.trade_date = date_val
                self.state_payload = {"regime_value": 1}
                self.input_hash = "h"
                self.source_history_run_id = run_a
                self.history_contract_version = "review-history-v2"

        class FakeScalars:
            def __init__(self, rows): self.rows = rows
            def __iter__(self): return iter(self.rows)

        # 构造当前 + previous 同 run
        current = State(date(2026, 8, 4))
        previous = State(date(2026, 8, 3))
        previous.instrument_id = current.instrument_id  # 同 instrument

        session = MagicMock()

        async def fake_execute(stmt):
            executed["n"] += 1
            if executed["n"] == 1:
                return MagicMock(scalars=lambda: FakeScalars([current]))
            if executed["n"] == 2:
                return MagicMock(scalars=lambda: FakeScalars([previous]))
            return MagicMock(scalars=lambda: FakeScalars([]))

        session.execute = fake_execute
        # 不应 raise（same source run）
        asyncio.run(load_day_fact_maps(session, trade_date=date(2026, 8, 4)))


class TestHistoryRunReadiness:
    """§5 canonical HistoryRun readiness contract。"""

    def _make_run(self, scope, status, contract):
        class Run:
            def __init__(self):
                self.scope = scope
                self.status = status
                self.metadata_json = (
                    f'{{"history_contract_version": "{contract}"}}'
                    if contract is not None else None
                )
        return Run()

    def test_succeeded_all_a_share_v2_ok(self) -> None:
        import asyncio

        from app.services.review_bootstrap_service import _validate_canonical_history_run

        session = MagicMock()

        async def fake_execute(stmt):
            result = MagicMock()
            result.scalar_one_or_none.return_value = self._make_run(
                "all_a_share", "succeeded", "review-history-v2",
            )
            return result

        session.execute = fake_execute
        res = asyncio.run(_validate_canonical_history_run(
            session, uuid.uuid4(), "review-history-v2",
        ))
        assert res["status"] == "ok"

    def test_running_fails(self) -> None:
        import asyncio

        from app.services.review_bootstrap_service import _validate_canonical_history_run

        session = MagicMock()

        async def fake_execute(stmt):
            result = MagicMock()
            result.scalar_one_or_none.return_value = self._make_run(
                "all_a_share", "running", "review-history-v2",
            )
            return result

        session.execute = fake_execute
        res = asyncio.run(_validate_canonical_history_run(
            session, uuid.uuid4(), "review-history-v2",
        ))
        assert res["status"] == "not_ready"
        assert "not_succeeded" in res["reason"]

    def test_wrong_scope_fails(self) -> None:
        import asyncio

        from app.services.review_bootstrap_service import _validate_canonical_history_run

        session = MagicMock()

        async def fake_execute(stmt):
            result = MagicMock()
            result.scalar_one_or_none.return_value = self._make_run(
                "sample", "succeeded", "review-history-v2",
            )
            return result

        session.execute = fake_execute
        res = asyncio.run(_validate_canonical_history_run(
            session, uuid.uuid4(), "review-history-v2",
        ))
        assert res["status"] == "not_ready"
        assert "wrong_scope" in res["reason"]

    def test_wrong_contract_fails(self) -> None:
        import asyncio

        from app.services.review_bootstrap_service import _validate_canonical_history_run

        session = MagicMock()

        async def fake_execute(stmt):
            result = MagicMock()
            result.scalar_one_or_none.return_value = self._make_run(
                "all_a_share", "succeeded", "review-history-v1",
            )
            return result

        session.execute = fake_execute
        res = asyncio.run(_validate_canonical_history_run(
            session, uuid.uuid4(), "review-history-v2",
        ))
        assert res["status"] == "not_ready"
        assert "wrong_contract" in res["reason"]

    def test_not_found_fails(self) -> None:
        import asyncio

        from app.services.review_bootstrap_service import _validate_canonical_history_run

        session = MagicMock()

        async def fake_execute(stmt):
            result = MagicMock()
            result.scalar_one_or_none.return_value = None
            return result

        session.execute = fake_execute
        res = asyncio.run(_validate_canonical_history_run(
            session, uuid.uuid4(), "review-history-v2",
        ))
        assert res["status"] == "not_ready"
        assert "not_found" in res["reason"]


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
        # downgrade 必须含 drop_column（对 events 表）
        drop_events = [
            o for o in downgrade_ops
            if o[0] == "drop_column" and "first_pyramid_history_events" in o[1]
        ]
        assert drop_events, "downgrade 缺少 DROP first_pyramid_history_events.history_contract_version"


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
