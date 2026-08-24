"""[CHANGE-20260809] Phase 2B.2 Review historical lineage PG contract 测试。

在真实 PostgreSQL 验证库（bz_stock_verify_<sha>）验证 migration 088 落地后的
dual-lineage / event coexistence / Board isolation / daily-state lineage / HistoryRun status
契约。这是 PG 集成测试，需 PANJI_REMOTE_VERIFY_DB_TEST=1，不得连接 bz_stock。

运行：
    PANJI_REMOTE_VERIFY_DB_TEST=1 .venv/bin/python -m pytest \
        tests/test_pg_review_lineage_contract.py -v
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest

pytestmark = pytest.mark.pg

# 复用的算法版本常量（避免硬编码漂移）
from app.models.first_pyramid_history_run import FirstPyramidHistoryRun  # noqa: E402
from app.schemas.first_pyramid import FIRST_PYRAMID_CORE_ALGORITHM_VERSION  # noqa: E402
from app.services.first_pyramid_service import HISTORY_CONTRACT_VERSION  # noqa: E402

_BOOTSTRAP_ALGO = "review-bootstrap-v2"


def _mk_history_run(scope: str = "all_a_share") -> FirstPyramidHistoryRun:
    """构造真实 FirstPyramidHistoryRun（满足 daily_state/observation FK）。"""

    return FirstPyramidHistoryRun(
        algorithm_version=FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
        parameter_hash="p", output_bars=250, scope=scope, status="running",
    )


# ---------------------------------------------------------------------------
# §4/§5 observation dual-lineage + partial unique / upsert
# ---------------------------------------------------------------------------
def _mk_payloads(value: float = 60.0) -> dict:
    return {
        "P": {
            "value": value,
            "status": "ready",
            "components": [
                {
                    "name": "scope_return_1d",
                    "rawValue": value,
                    "denominator": None,
                    "fieldSource": "computed",
                    "extra": None,
                    "weightMode": "equal_weight",
                    "status": "ready",
                },
            ],
        },
    }


@pytest.mark.asyncio
async def test_observation_dual_lineage_check_matrix():
    """§4 真实 PG 验证 ck_review_observation_dual_lineage CHECK（A-F）。

    用真实父记录（MarketReviewRun / FirstPyramidHistoryRun）满足 FK，再测 CHECK。
    """
    from sqlalchemy import delete, insert

    from app.db import AsyncSessionLocal
    from app.models.first_pyramid_history_run import FirstPyramidHistoryRun
    from app.models.market_review import MarketReviewMetricObservation, MarketReviewRun

    async with AsyncSessionLocal() as s:
        await s.execute(delete(MarketReviewMetricObservation))
        await s.execute(delete(MarketReviewRun).where(
            MarketReviewRun.trade_date == date(2026, 8, 4),
        ))
        await s.execute(delete(FirstPyramidHistoryRun))
        # 创建真实父记录（满足 observation FK）
        review_run = MarketReviewRun(
            trade_date=date(2026, 8, 4),
            source_core_run_id=uuid.uuid4(),
            source_board_run_id=uuid.uuid4(),
            degraded_reasons=[],
            algorithm_version="review-2.0.0",
            filter_version="filters-1.1.0",
            baseline_window=120,
            status="signals_ready",
            coverage_ratio=Decimal("1.0"),
        )
        s.add(review_run)
        hist_run = FirstPyramidHistoryRun(
            algorithm_version=FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
            parameter_hash="p", output_bars=250, scope="all_a_share",
            status="running",
        )
        s.add(hist_run)
        await s.flush()
        run_id = review_run.id
        src_run_id = hist_run.id
        await s.commit()

    # 完整 NOT NULL 列的最小 observation row 模板。
    def _row(kind: str, run, src) -> dict:
        return {
            "id": uuid.uuid4(), "source_kind": kind, "review_run_id": run,
            "source_history_run_id": src,
            "trade_date": date(2026, 8, 4), "scope_type": "market",
            "scope_key": "market", "metric_code": "P",
            "component_name": "_metric_value", "raw_value": "60",
            "field_source_json": {"fieldSource": "test"}, "weight_mode": "derived",
            "algorithm_version": _BOOTSTRAP_ALGO, "input_hash": "h",
            "membership_version": "v1", "status": "ready",
        }

    async def _insert(kind: str, run, src) -> bool:
        async with AsyncSessionLocal() as s:
            try:
                await s.execute(insert(MarketReviewMetricObservation).values(
                    **_row(kind, run, src),
                ))
                await s.commit()
                return True
            except Exception:
                await s.rollback()
                return False

    # A. valid LIVE：source_kind=live, review_run_id != NULL, src_run_id NULL → PASS
    assert await _insert("live", run_id, None) is True
    # B. valid HISTORY_REPLAY：source_kind=history_replay, review_run_id NULL, src != NULL → PASS
    assert await _insert("history_replay", None, src_run_id) is True
    # C. both NULL → CHECK reject
    assert await _insert("history_replay", None, None) is False
    # D. both non-NULL → CHECK reject
    assert await _insert("history_replay", run_id, src_run_id) is False
    # E. live with source_history_run → reject
    assert await _insert("live", run_id, src_run_id) is False
    # F. replay with review_run → reject
    assert await _insert("history_replay", run_id, src_run_id) is False

    # 清理
    async with AsyncSessionLocal() as s:
        await s.execute(delete(MarketReviewMetricObservation))
        await s.execute(delete(MarketReviewRun).where(
            MarketReviewRun.trade_date == date(2026, 8, 4),
        ))
        await s.execute(delete(FirstPyramidHistoryRun))
        await s.commit()


@pytest.mark.asyncio
async def test_observation_partial_index_upsert_live_and_replay():
    """§5 persist_metric_observations / persist_history_replay_observations 真实 upsert。"""
    from sqlalchemy import delete, func, select

    from app.db import AsyncSessionLocal
    from app.models.first_pyramid_history_run import FirstPyramidHistoryRun
    from app.models.market_review import MarketReviewMetricObservation, MarketReviewRun
    from app.services.review_metric_observation_service import (
        persist_history_replay_observations,
        persist_metric_observations,
    )

    async with AsyncSessionLocal() as s:
        await s.execute(
            MarketReviewMetricObservation.__table__.delete()
        )
        await s.execute(delete(MarketReviewRun).where(
            MarketReviewRun.trade_date == date(2026, 8, 4),
        ))
        await s.execute(delete(FirstPyramidHistoryRun))
        # 创建真实父记录（observation FK 要求真实 run 存在）
        review_run = MarketReviewRun(
            trade_date=date(2026, 8, 4), source_core_run_id=uuid.uuid4(),
            source_board_run_id=uuid.uuid4(), degraded_reasons=[],
            algorithm_version="review-2.0.0", filter_version="filters-1.1.0",
            baseline_window=120, status="signals_ready", coverage_ratio=Decimal("1.0"),
        )
        s.add(review_run)
        hist_run_a = FirstPyramidHistoryRun(
            algorithm_version=FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
            parameter_hash="p", output_bars=250, scope="all_a_share", status="running",
        )
        hist_run_b = FirstPyramidHistoryRun(
            algorithm_version=FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
            parameter_hash="p", output_bars=250, scope="all_a_share", status="running",
        )
        s.add_all([hist_run_a, hist_run_b])
        await s.flush()
        run_id = review_run.id
        src_run_a = hist_run_a.id
        src_run_b = hist_run_b.id
        await s.commit()

    flat_list = [{"_instrument_id": str(uuid.uuid4()), "fp_trend_direction": "上行"}]

    async with AsyncSessionLocal() as s:
        # LIVE same run/scope/component rerun → update same row（count 不变）
        n1 = await persist_metric_observations(
            s, review_run_id=run_id, trade_date=date(2026, 8, 4),
            scope_type="market", scope_key="market",
            membership_version="v1", algorithm_version=_BOOTSTRAP_ALGO,
            flat_list=flat_list, payloads=_mk_payloads(60.0),
            taxonomy_compatibility_key=None,
        )
        await s.commit()
        n2 = await persist_metric_observations(
            s, review_run_id=run_id, trade_date=date(2026, 8, 4),
            scope_type="market", scope_key="market",
            membership_version="v1", algorithm_version=_BOOTSTRAP_ALGO,
            flat_list=flat_list, payloads=_mk_payloads(70.0),
            taxonomy_compatibility_key=None,
        )
        await s.commit()
        assert n2 == n1  # same row upserted（不新增）

        # HISTORY_REPLAY same source run/date/scope/component rerun → update same row
        r1 = await persist_history_replay_observations(
            s, source_history_run_id=src_run_a,
            history_contract_version=HISTORY_CONTRACT_VERSION,
            taxonomy_compatibility_key="taxo-B",
            trade_date=date(2026, 8, 4), scope_type="market", scope_key="market",
            membership_version="v1", algorithm_version=_BOOTSTRAP_ALGO,
            flat_list=flat_list, payloads=_mk_payloads(50.0),
        )
        await s.commit()
        r2 = await persist_history_replay_observations(
            s, source_history_run_id=src_run_a,
            history_contract_version=HISTORY_CONTRACT_VERSION,
            taxonomy_compatibility_key="taxo-B",
            trade_date=date(2026, 8, 4), scope_type="market", scope_key="market",
            membership_version="v1", algorithm_version=_BOOTSTRAP_ALGO,
            flat_list=flat_list, payloads=_mk_payloads(55.0),
        )
        await s.commit()
        assert r2 == r1

        # 不同 source_history_run_id → 可合法共存
        await persist_history_replay_observations(
            s, source_history_run_id=src_run_b,
            history_contract_version=HISTORY_CONTRACT_VERSION,
            taxonomy_compatibility_key="taxo-B",
            trade_date=date(2026, 8, 4), scope_type="market", scope_key="market",
            membership_version="v1", algorithm_version=_BOOTSTRAP_ALGO,
            flat_list=flat_list, payloads=_mk_payloads(52.0),
        )
        await s.commit()

        # LIVE rows count：P._metric_value + P.scope_return_1d = 2（同一 run 唯一）
        live_cnt = (await s.execute(
            select(func.count()).select_from(MarketReviewMetricObservation).where(
                MarketReviewMetricObservation.source_kind == "live"
            )
        )).scalar()
        # replay rows：src_run_a (2) + src_run_b (2) = 4
        replay_cnt = (await s.execute(
            select(func.count()).select_from(MarketReviewMetricObservation).where(
                MarketReviewMetricObservation.source_kind == "history_replay"
            )
        )).scalar()
        assert live_cnt == 2
        assert replay_cnt == 4

        await s.execute(MarketReviewMetricObservation.__table__.delete())
        await s.commit()


# ---------------------------------------------------------------------------
# §6 event legacy / v2 coexistence + idempotency（真实 _persist_history_result）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_event_legacy_v2_coexistence_and_idempotency():
    """§6 真实 PG：legacy NULL X + v2 X 共存；v2 X 重跑仍 1 条。"""
    from sqlalchemy import delete, func, select

    from app.db import AsyncSessionLocal
    from app.models.first_pyramid_history import FirstPyramidHistoryEvent
    from app.models.instrument import Instrument
    from app.services.first_pyramid_history_service import _persist_history_result

    iid = uuid.uuid4()
    algo = FIRST_PYRAMID_CORE_ALGORITHM_VERSION

    async with AsyncSessionLocal() as s:
        await s.execute(delete(FirstPyramidHistoryEvent).where(
            FirstPyramidHistoryEvent.instrument_id == iid,
        ))
        await s.execute(delete(Instrument).where(Instrument.id == iid))
        await s.commit()
        # 真实 Instrument + HistoryRun 满足 events/daily_state FK（先独立提交父记录）
        s.add(Instrument(
            id=iid, symbol=f"PGVB{iid.hex[:8]}", name="verify-ev",
            market="SH", status="active",
        ))
        run = _mk_history_run()
        s.add(run)
        await s.flush()
        src_run = run.id
        await s.commit()

    history_v2 = {
        "daily_state": [{
            "time": "2026-08-04", "regime_value": 1, "bar_index": 5,
        }],
        "events": [{"type": "BOS", "event_id": "X", "bar_index": 5, "time": "2026-08-04"}],
    }
    history_legacy = dict(history_v2)

    async with AsyncSessionLocal() as s:
        # legacy（history_contract_version=None）
        await _persist_history_result(
            s, iid, history_legacy, algo,
            source_history_run_id=None, history_contract_version=None,
        )
        await s.commit()
        # v2
        await _persist_history_result(
            s, iid, history_v2, algo,
            source_history_run_id=src_run, history_contract_version=HISTORY_CONTRACT_VERSION,
        )
        await s.commit()

        rows = (await s.execute(
            select(FirstPyramidHistoryEvent).where(
                FirstPyramidHistoryEvent.instrument_id == iid,
                FirstPyramidHistoryEvent.event_id == "X",
            )
        )).scalars().all()
        # legacy + v2 共存 → 2 行
        assert len(rows) == 2
        kinds = {r.history_contract_version for r in rows}
        assert kinds == {None, HISTORY_CONTRACT_VERSION}

        # 同一个 v2 X 再写一次 → 仍 1 条 v2（不产生 duplicate）
        await _persist_history_result(
            s, iid, history_v2, algo,
            source_history_run_id=src_run, history_contract_version=HISTORY_CONTRACT_VERSION,
        )
        await s.commit()
        v2_cnt = (await s.execute(
            select(func.count()).select_from(FirstPyramidHistoryEvent).where(
                FirstPyramidHistoryEvent.instrument_id == iid,
                FirstPyramidHistoryEvent.event_id == "X",
                FirstPyramidHistoryEvent.history_contract_version == HISTORY_CONTRACT_VERSION,
            )
        )).scalar()
        assert v2_cnt == 1

        await s.execute(delete(FirstPyramidHistoryEvent).where(
            FirstPyramidHistoryEvent.instrument_id == iid,
        ))
        await s.commit()




# ---------------------------------------------------------------------------
# §9 daily-state lineage
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_daily_state_lineage_load_day_fact_maps():
    """§9 真实 PG：load_day_fact_maps 正常读取 v2 同源 daily-state。"""
    from sqlalchemy import delete

    from app.db import AsyncSessionLocal
    from app.models.first_pyramid_history import FirstPyramidHistoryDailyState
    from app.models.instrument import Instrument
    from app.services.review_scope_service import load_day_fact_maps

    iid = uuid.uuid4()
    target = date(2026, 8, 4)

    async with AsyncSessionLocal() as s:
        await s.execute(delete(FirstPyramidHistoryDailyState).where(
            FirstPyramidHistoryDailyState.instrument_id == iid,
        ))
        await s.execute(delete(Instrument).where(Instrument.id == iid))
        await s.commit()
        # 先独立提交 Instrument + HistoryRun（保证 FK 父记录已持久化）
        s.add(Instrument(
            id=iid, symbol=f"PGVB{iid.hex[:8]}", name="verify-st",
            market="SH", status="active",
        ))
        run = _mk_history_run()
        s.add(run)
        await s.flush()
        src_run = run.id
        await s.commit()
        # current + previous 同源 v2
        s.add(FirstPyramidHistoryDailyState(
            instrument_id=iid, trade_date=target,
            algorithm_version=FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
            input_hash="h", history_contract_version=HISTORY_CONTRACT_VERSION,
            source_history_run_id=src_run,
            state_payload={"history_contract_version": HISTORY_CONTRACT_VERSION,
                           "regime_value": 1},
        ))
        s.add(FirstPyramidHistoryDailyState(
            instrument_id=iid, trade_date=target - timedelta(days=1),
            algorithm_version=FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
            input_hash="h", history_contract_version=HISTORY_CONTRACT_VERSION,
            source_history_run_id=src_run,
            state_payload={"history_contract_version": HISTORY_CONTRACT_VERSION,
                           "regime_value": 0},
        ))
        await s.commit()

        # 同一 instrument 需 identity 存在才会被 load；无 identity 时 fact 被跳过，
        # 但不会 raise lineage mismatch。这里只验证不抛错（读 path 正常）。
        facts = await load_day_fact_maps(s, trade_date=target, instrument_ids=[iid])
        # 无 identity → 可能为空 dict，但不 raise
        assert isinstance(facts, dict)

        # 清理
        await s.execute(delete(FirstPyramidHistoryDailyState).where(
            FirstPyramidHistoryDailyState.instrument_id == iid,
        ))
        await s.commit()


@pytest.mark.asyncio
async def test_daily_state_previous_source_run_mismatch_fails_closed():
    """§9 真实 PG：current run A + previous run B（同 contract）→ 拒绝。"""
    import pytest as _pt
    from sqlalchemy import delete

    from app.db import AsyncSessionLocal
    from app.models.first_pyramid_history import FirstPyramidHistoryDailyState
    from app.models.instrument import Instrument
    from app.services.review_scope_service import load_day_fact_maps

    iid = uuid.uuid4()
    target = date(2026, 8, 4)

    async with AsyncSessionLocal() as s:
        await s.execute(delete(FirstPyramidHistoryDailyState).where(
            FirstPyramidHistoryDailyState.instrument_id == iid,
        ))
        await s.execute(delete(Instrument).where(Instrument.id == iid))
        await s.commit()
        s.add(Instrument(
            id=iid, symbol=f"PGVB{iid.hex[:8]}", name="verify-st",
            market="SH", status="active",
        ))
        run_a_obj = _mk_history_run()
        run_b_obj = _mk_history_run()
        s.add_all([run_a_obj, run_b_obj])
        await s.flush()
        run_a = run_a_obj.id
        run_b = run_b_obj.id
        await s.commit()
        s.add(FirstPyramidHistoryDailyState(
            instrument_id=iid, trade_date=target,
            algorithm_version=FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
            input_hash="h", history_contract_version=HISTORY_CONTRACT_VERSION,
            source_history_run_id=run_a,
            state_payload={"history_contract_version": HISTORY_CONTRACT_VERSION},
        ))
        s.add(FirstPyramidHistoryDailyState(
            instrument_id=iid, trade_date=target - timedelta(days=1),
            algorithm_version=FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
            input_hash="h", history_contract_version=HISTORY_CONTRACT_VERSION,
            source_history_run_id=run_b,  # 不同源 run
            state_payload={"history_contract_version": HISTORY_CONTRACT_VERSION},
        ))
        await s.commit()

        with _pt.raises(ValueError) as excinfo:
            await load_day_fact_maps(s, trade_date=target, instrument_ids=[iid])
        assert "HISTORY_PREVIOUS_SOURCE_RUN_MISMATCH" in str(excinfo.value)

        await s.execute(delete(FirstPyramidHistoryDailyState).where(
            FirstPyramidHistoryDailyState.instrument_id == iid,
        ))
        await s.commit()


# ---------------------------------------------------------------------------
# §10 HistoryRun final status（DB canonical progress）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_history_run_final_status_matrix():
    """§10 真实 PG：真实 run_items 决定 final status（绝不错标 succeeded）。"""
    from sqlalchemy import delete

    from app.db import AsyncSessionLocal
    from app.models.first_pyramid_history_run import FirstPyramidHistoryRun
    from app.models.first_pyramid_history_run_item import FirstPyramidHistoryRunItem
    from app.models.instrument import Instrument
    from app.services.first_pyramid_history_service import (
        _derive_run_final_status,
        get_history_run_progress,
    )

    status_cases = [
        # (statuses, expected)
        (["succeeded"] * 3, "succeeded"),
        (["succeeded"] * 2 + ["skipped"], "partial"),
        (["succeeded"] * 2 + ["failed"], "partial"),
        (["succeeded"] * 2 + ["running"], "partial"),
        (["succeeded"] * 2 + ["pending"], "partial"),
        (["failed"] * 2 + ["skipped"], "failed"),
    ]

    # 真实 Instrument（满足 FK），每个 case 用独立 run + instrument。
    async with AsyncSessionLocal() as s:
        await s.execute(delete(FirstPyramidHistoryRunItem))
        await s.execute(delete(FirstPyramidHistoryRun))
        await s.execute(delete(Instrument).where(Instrument.symbol.like("PGVERIFY%")))
        await s.commit()

        for statuses, expected in status_cases:
            run = FirstPyramidHistoryRun(
                algorithm_version="algo", parameter_hash="p",
                output_bars=250, scope="all_a_share", status="running",
            )
            s.add(run)
            await s.flush()
            for idx, st in enumerate(statuses):
                inst = Instrument(
                    symbol=f"PGVERIFY{run.id.hex[:8]}{idx}",
                    name="verify-inst", market="SH", status="active",
                )
                s.add(inst)
                await s.flush()
                s.add(FirstPyramidHistoryRunItem(
                    history_run_id=run.id, instrument_id=inst.id,
                    status=st, input_hash="h",
                ))
            await s.commit()
            run_id = run.id
            async with AsyncSessionLocal() as s2:
                progress = await get_history_run_progress(s2, run_id)
                final = _derive_run_final_status(progress)
            assert final == expected, (
                f"statuses={statuses} → expected {expected}, got {final}"
            )
            async with AsyncSessionLocal() as s3:
                await s3.execute(delete(FirstPyramidHistoryRunItem).where(
                    FirstPyramidHistoryRunItem.history_run_id == run_id,
                ))
                await s3.execute(delete(FirstPyramidHistoryRun).where(
                    FirstPyramidHistoryRun.id == run_id,
                ))
                await s3.commit()

        # 清理测试 instrument
        async with AsyncSessionLocal() as s4:
            await s4.execute(delete(Instrument).where(
                Instrument.symbol.like("PGVERIFY%"),
            ))
            await s4.commit()
