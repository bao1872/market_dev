from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects.postgresql.dml import Insert as PgInsert

from app.services import review_bootstrap_service as bootstrap
from app.services.review_metric_observation_service import (
    build_member_input_hash,
    load_metric_history,
    persist_metric_observations,
)
from app.services.review_scope_service import ScopeDefinition, fetch_historical_member_facts


class _ScalarResult:
    def __init__(self, values: list[Any]) -> None:
        self.values = values

    def scalars(self) -> _ScalarResult:
        return self

    def all(self) -> list[Any]:
        return self.values

    def __iter__(self):
        return iter(self.values)


def test_member_input_hash_is_order_stable_and_ignores_snapshot_projection() -> None:
    first = [
        {"_instrument_id": "b", "_snapshot_id": uuid.uuid4(), "value": Decimal("1")},
        {"_instrument_id": "a", "_snapshot_id": uuid.uuid4(), "value": Decimal("2")},
    ]
    second = [
        {"value": Decimal("2"), "_snapshot_id": uuid.uuid4(), "_instrument_id": "a"},
        {"value": Decimal("1"), "_snapshot_id": uuid.uuid4(), "_instrument_id": "b"},
    ]
    assert build_member_input_hash(first) == build_member_input_hash(second)


@pytest.mark.asyncio
async def test_history_reader_is_version_exact_and_strictly_pit() -> None:
    observations = [
        SimpleNamespace(
            trade_date=date(2026, 7, 30), scope_type="market", scope_key="market",
            metric_code="P", component_name="scope_return_1d", raw_value=Decimal("2.5"),
            source_kind="history_replay",
        ),
        SimpleNamespace(
            trade_date=date(2026, 7, 30), scope_type="market", scope_key="market",
            metric_code="P", component_name="_metric_value", raw_value=Decimal("70"),
            source_kind="history_replay",
        ),
        SimpleNamespace(
            trade_date=date(2026, 7, 29), scope_type="market", scope_key="market",
            metric_code="P", component_name="scope_return_1d", raw_value=Decimal("1.5"),
            source_kind="history_replay",
        ),
    ]
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_ScalarResult(observations))

    history, previous, previous5 = await load_metric_history(
        session,
        scope_type="market",
        scope_key="market",
        trade_date=date(2026, 7, 31),
        algorithm_version="review-2.0.0",
        baseline_window=120,
    )

    assert history is not None
    assert history["P"]["scope_return_1d"] == [1.5, 2.5]
    assert previous == {"P": 70.0}
    assert previous5 is None
    sql = str(session.execute.call_args.args[0].compile(compile_kwargs={"literal_binds": True}))
    assert "algorithm_version = 'review-2.0.0'" in sql
    assert "trade_date < '2026-07-31'" in sql


@pytest.mark.asyncio
async def test_observation_persistence_emits_idempotent_component_rows() -> None:
    session = AsyncMock()
    session.execute = AsyncMock()
    session.flush = AsyncMock()
    count = await persist_metric_observations(
        session,
        review_run_id=uuid.uuid4(),
        trade_date=date(2026, 7, 31),
        scope_type="market",
        scope_key="market",
        membership_version="pit-v1",
        algorithm_version="review-2.0.0",
        flat_list=[{"_instrument_id": "one", "review_return_1d": 1.0}],
        payloads={
            "P": {
                "value": 60.0,
                "status": "ready",
                "components": [{
                    "name": "scope_return_1d",
                    "rawValue": 1.0,
                    "denominator": 1,
                    "fieldSource": "bars_daily.close",
                    "weightMode": "equal_weight",
                    "status": "ready",
                }],
            },
        },
        taxonomy_compatibility_key=None,
    )
    assert count == 2
    assert session.execute.call_count == 2
    assert all(
        isinstance(call.args[0], PgInsert)
        for call in session.execute.call_args_list
    )
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_historical_member_fact_uses_target_and_previous_pit_state() -> None:
    instrument_id = uuid.uuid4()
    current_state = SimpleNamespace(
        id=uuid.uuid4(), instrument_id=instrument_id, trade_date=date(2026, 7, 31),
        state_payload={"regime_value": 1, "swing_bias": "up"}, input_hash="current",
    )
    previous_state = SimpleNamespace(
        id=uuid.uuid4(), instrument_id=instrument_id, trade_date=date(2026, 7, 30),
        state_payload={"regime_value": 0, "swing_bias": "sideways"}, input_hash="previous",
    )
    bars = [
        SimpleNamespace(
            instrument_id=instrument_id, trade_date=date(2026, 7, 30),
            open=Decimal("100"), high=Decimal("101"), low=Decimal("99"),
            close=Decimal("100"), volume=Decimal("10"), amount=Decimal("1000"),
        ),
        SimpleNamespace(
            instrument_id=instrument_id, trade_date=date(2026, 7, 31),
            open=Decimal("100"), high=Decimal("111"), low=Decimal("99"),
            close=Decimal("110"), volume=Decimal("20"), amount=Decimal("2200"),
        ),
    ]
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[
        _ScalarResult([SimpleNamespace(id=instrument_id, symbol="600000", name="浦发银行")]),
        _ScalarResult([current_state]),
        _ScalarResult([previous_state]),
        _ScalarResult(bars),
    ])

    facts = await fetch_historical_member_facts(
        session, [instrument_id], trade_date=date(2026, 7, 31),
    )

    assert len(facts) == 1
    assert facts[0]["review_return_1d"] == pytest.approx(10.0)
    # [CHANGE-20260808] LIVE parity：fp_trend_direction 用中文标签（上行/下行/震荡）
    assert facts[0]["fp_trend_direction"] == "上行"
    assert facts[0]["review_previous_first_pyramid"]["fp_trend_direction"] == "震荡"
    assert facts[0]["_history_state_id"] == str(current_state.id)
    assert facts[0]["_history_input_hash"] == "current"


@pytest.mark.asyncio
async def test_bootstrap_dry_run_is_strictly_read_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = ScopeDefinition("market", "market", "全市场", membership_version="fp-history")
    instrument_id = uuid.uuid4()
    fact = {
        "_instrument_id": str(instrument_id),
        "fp_trend_direction": "上行",
        "review_return_1d": 1.0,
    }
    src_run_id = uuid.uuid4()
    # [CHANGE-20260808] 当前 bootstrap_single_date 用 load_day_fact_maps + canonical
    # source run + persist_history_replay_observations（不再用旧 _market_history_members
    # / fetch_historical_member_facts / persist_metric_observations）。
    monkeypatch.setattr(bootstrap, "_list_bootstrap_scopes", AsyncMock(return_value=[scope]))
    monkeypatch.setattr(
        bootstrap, "load_day_fact_maps",
        AsyncMock(return_value={instrument_id: fact}),
    )
    # _collect_canonical_source_run 是 sync；_validate_canonical_history_run 是 async
    monkeypatch.setattr(
        bootstrap, "_collect_canonical_source_run",
        lambda *a, **kw: {"status": "one", "run_id": src_run_id},
    )
    monkeypatch.setattr(
        bootstrap, "_validate_canonical_history_run",
        AsyncMock(return_value={"status": "ok", "run_id": src_run_id}),
    )
    monkeypatch.setattr(
        bootstrap, "load_metric_history",
        AsyncMock(return_value=({}, {}, {})),
    )
    monkeypatch.setattr(bootstrap, "compute_all_metrics", lambda *a, **kw: {
        "P": {"value": 60.0, "status": "ready", "components": []},
    })
    write_run = AsyncMock(side_effect=AssertionError("dry-run wrote a run"))
    write_observation = AsyncMock(side_effect=AssertionError("dry-run wrote observations"))
    monkeypatch.setattr(bootstrap, "_upsert_bootstrap_run", write_run)
    monkeypatch.setattr(bootstrap, "persist_history_replay_observations", write_observation)

    result = await bootstrap.bootstrap_single_date(
        AsyncMock(),
        trade_date=date(2026, 7, 31),
        source_core_run_id=None,
        source_board_run_id=None,
        dry_run=True,
    )

    assert result["status"] == "dry_run"
    assert result["run_id"] is None
    assert result["written"] is False
    write_run.assert_not_awaited()
    write_observation.assert_not_awaited()


# =============================================================================
# 历史序列兼容性契约
#
# 兼容性判定维度 = scope identity (scope_type + scope_key)
#                + compatible taxonomy
#                + algorithm_version
#                + metric definition version
#
# membership_version **随每条观测存储，但不参与历史序列过滤**：
# 成分股的正常增减是常态，若按 membership_version 过滤，任何一次调仓
# 都会让 60 日历史被截断并重新冷启动。以下测试固化该契约，防止后续
# 有人"顺手"把 membership_version 加进 where 条件。
# =============================================================================


@pytest.mark.asyncio
async def test_history_query_does_not_filter_by_membership_version() -> None:
    """历史序列查询不得按 membership_version 过滤（否则调仓即冷启动）。"""
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_ScalarResult([]))

    await load_metric_history(
        session,
        scope_type="industry_l1",
        scope_key="board-1",
        trade_date=date(2026, 7, 31),
        algorithm_version="review-2.0.0",
        baseline_window=120,
    )

    sql = str(
        session.execute.call_args.args[0].compile(compile_kwargs={"literal_binds": True}),
    )
    # 只看过滤条件：membership_version 出现在 SELECT 投影列中是正常的
    # （随观测存储、可追溯当日成员），关键是它不得进入 WHERE。
    where_clause = sql.split("WHERE", 1)[1]
    assert "membership_version" not in where_clause, (
        "历史序列不得按 membership_version 过滤："
        "成员正常变化不应导致 60 日历史重新冷启动"
    )
    # 兼容性维度必须仍然生效
    assert "scope_type = 'industry_l1'" in where_clause
    assert "scope_key = 'board-1'" in where_clause
    assert "algorithm_version = 'review-2.0.0'" in where_clause


@pytest.mark.asyncio
async def test_history_sequence_spans_multiple_membership_versions() -> None:
    """同一 scope 跨多个 membership_version 的历史必须连续可比。"""
    observations = [
        SimpleNamespace(
            trade_date=date(2026, 7, 30), scope_type="industry_l1", scope_key="board-1",
            metric_code="P", component_name="_metric_value", raw_value=Decimal("70"),
            membership_version="pit-v3",
            source_kind="history_replay",
        ),
        SimpleNamespace(
            trade_date=date(2026, 7, 29), scope_type="industry_l1", scope_key="board-1",
            metric_code="P", component_name="_metric_value", raw_value=Decimal("65"),
            membership_version="pit-v2",
            source_kind="history_replay",
        ),
        SimpleNamespace(
            trade_date=date(2026, 7, 28), scope_type="industry_l1", scope_key="board-1",
            metric_code="P", component_name="_metric_value", raw_value=Decimal("60"),
            membership_version="pit-v1",
            source_kind="history_replay",
        ),
    ]
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_ScalarResult(observations))

    history, previous, _ = await load_metric_history(
        session,
        scope_type="industry_l1",
        scope_key="board-1",
        trade_date=date(2026, 7, 31),
        algorithm_version="review-2.0.0",
        baseline_window=120,
    )

    assert history is not None
    # 三个不同 membership_version 的观测全部保留，历史序列不被截断
    assert history["P"]["_metric_value"] == [60.0, 65.0, 70.0]
    assert previous == {"P": 70.0}


@pytest.mark.asyncio
async def test_observation_persist_stores_membership_version() -> None:
    """membership_version 必须随观测写入（可追溯当日成员），只是不参与过滤。"""
    session = AsyncMock()
    session.execute = AsyncMock()
    session.flush = AsyncMock()

    await persist_metric_observations(
        session,
        review_run_id=uuid.uuid4(),
        trade_date=date(2026, 7, 31),
        scope_type="industry_l1",
        scope_key="board-1",
        membership_version="pit-v7",
        algorithm_version="review-2.0.0",
        flat_list=[{"_instrument_id": "one", "review_return_1d": 1.0}],
        payloads={
            "P": {
                "value": 60.0,
                "status": "ready",
                "components": [{
                    "name": "scope_return_1d",
                    "rawValue": 1.0,
                    "denominator": 1,
                    "fieldSource": "bars_daily.close",
                    "weightMode": "equal_weight",
                    "status": "ready",
                }],
            },
        },
        taxonomy_compatibility_key=None,
    )

    stmt = session.execute.call_args_list[0].args[0]
    assert isinstance(stmt, PgInsert)
    # JSONB 无法 literal_binds 渲染，改为结构化读取绑定参数
    values = stmt.compile().params
    assert "pit-v7" in values.values(), (
        "membership_version 必须随观测持久化（可追溯当日成员）"
    )
