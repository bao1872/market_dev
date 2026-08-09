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
