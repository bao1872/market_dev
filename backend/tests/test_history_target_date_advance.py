"""[HISTORY-CURRENT-DATE-LIFECYCLE-01 §11] canonical history 日推进 + target-date readiness 单元测试。

覆盖用户 §11 A–I：

- A: 只有 08-07 state 的 dataset，对 required_trade_date=08-10 → NOT_READY
- B: target advance 只写 target-date state（不写历史日期）
- C: 08-07 payload / lineage before == after（不被改写）
- D: same source —— 08-07 previous 与 08-10 current 同为 be56dcd2
- E: future bar 绝不进入 compute input（PIT 断言 + MDAS 参数锁定）
- F: target eligible set 缺 1 个 state → NOT_READY
- G: target eligible set 完整 → READY
- H: formal Review 不消费 FirstPyramidHistoryEvent（静态证据测试）
- I: daily advance 不写 event table

运行：
    cd backend
    PURE_UNIT_TEST=1 PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest \
        tests/test_history_target_date_advance.py -v -p no:cacheprovider
"""
from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from app.services.first_pyramid_history_service import (
    _max_bar_trade_date,
    advance_history_to_trade_date,
)

TARGET = date(2026, 8, 10)
PREV = date(2026, 8, 7)
CONTRACT = "review-history-v2"


def _build_bars(n: int = 300, end: str = "2026-08-10") -> pd.DataFrame:
    """构造 DatetimeIndex OHLCV 日线 fixture（history SSOT 的 bars 契约）。"""
    np.random.seed(7)
    dates = pd.bdate_range(end=end, periods=n)
    close = 10.0 + np.cumsum(np.random.randn(n) * 0.15 + 0.05)
    return pd.DataFrame(
        {
            "open": close - 0.05,
            "high": close + 0.1,
            "low": close - 0.1,
            "close": close,
            "volume": np.random.randint(100000, 500000, n).astype(float),
            "amount": close * 100000,
        },
        index=dates,
    )


def _history_with_states(dates: list[date]) -> dict[str, Any]:
    """构造含多日 daily_state 的 compute_first_pyramid_history 返回结构。"""
    return {
        "daily_state": [
            {"bar_index": i, "time": d.isoformat(), "regime_value": 0.5}
            for i, d in enumerate(dates)
        ],
        "events": [
            {"type": "BOS", "bar_index": 3, "time": "2026-05-01"},
            {"type": "SQZ_RELEASE", "bar_index": 9, "time": TARGET.isoformat()},
        ],
        "meta": {"input_hash": "hash-abc", "output_bars": 250},
    }


class _FakeRun:
    def __init__(self, run_id: uuid.UUID, contract: str = CONTRACT):
        self.id = run_id
        self.scope = "all_a_share"
        self.status = "partial"
        self.algorithm_version = "fp-v1"
        self.metadata_json = {"history_contract_version": contract}


class _FakeSession:
    """最小 AsyncSession 替身：记录所有 execute 的 upsert 语句。"""

    def __init__(self, run: _FakeRun, instrument_ids: list[uuid.UUID]):
        self._run = run
        self._instrument_ids = instrument_ids
        self.executed: list[Any] = []
        self.commits = 0

    async def get(self, model, pk):  # noqa: ANN001
        return self._run

    async def execute(self, stmt):  # noqa: ANN001
        self.executed.append(stmt)
        rows = [(iid,) for iid in self._instrument_ids]
        result = MagicMock()
        result.all.return_value = rows
        return result

    async def commit(self):
        self.commits += 1


class TestAdvanceWritesOnlyTargetDate:
    """§11 B / C / I：只写 target-date state，不改历史行，不写 events。"""

    async def _run_advance(self, states: list[date], bars_end: str = "2026-08-10"):
        run_id = uuid.uuid4()
        iid = uuid.uuid4()
        session = _FakeSession(_FakeRun(run_id), [iid])
        persisted: list[dict[str, Any]] = []

        async def fake_persist(_session, instrument_id, state, algorithm_version, **kw):
            persisted.append(
                {
                    "instrument_id": instrument_id,
                    "state": state,
                    "algorithm_version": algorithm_version,
                    **kw,
                }
            )

        with patch(
            "app.services.first_pyramid_history_service._fetch_pit_daily_bars_for_target",
            AsyncMock(return_value=_build_bars(end=bars_end)),
        ), patch(
            "app.services.first_pyramid_service.compute_first_pyramid_history",
            MagicMock(return_value=_history_with_states(states)),
        ), patch(
            "app.services.first_pyramid_history_service._persist_target_daily_state",
            fake_persist,
        ):
            summary = await advance_history_to_trade_date(session, run_id, TARGET)
        return summary, persisted, run_id, iid, session

    async def test_b_only_target_date_state_written(self):
        """B: history 含 08-05/08-06/08-07/08-10 四天，只持久化 08-10 一行。"""
        states = [date(2026, 8, 5), date(2026, 8, 6), PREV, TARGET]
        summary, persisted, _, _, _ = await self._run_advance(states)

        assert summary["target_state_count"] == 1
        assert len(persisted) == 1, "必须最多写 1 行/instrument（1x 写放大，非 250x）"
        assert persisted[0]["trade_date_val"] == TARGET
        assert persisted[0]["state"]["time"] == TARGET.isoformat()

    async def test_c_historical_dates_never_touched(self):
        """C: 历史日期（08-07 及更早）不出现在任何持久化调用中。"""
        states = [date(2026, 8, 5), date(2026, 8, 6), PREV, TARGET]
        _, persisted, _, _, _ = await self._run_advance(states)

        written_dates = {p["trade_date_val"] for p in persisted}
        assert written_dates == {TARGET}
        assert PREV not in written_dates
        assert date(2026, 8, 5) not in written_dates

    async def test_d_lineage_stays_on_existing_canonical_run(self):
        """D: source_history_run_id 保持为传入的 canonical run（不新建 run X）。"""
        states = [PREV, TARGET]
        _, persisted, run_id, _, _ = await self._run_advance(states)

        assert persisted[0]["source_history_run_id"] == run_id
        assert persisted[0]["history_contract_version"] == CONTRACT

    async def test_i_no_event_rows_written(self):
        """I: Review 不消费 HistoryEvent → daily advance 完全不写 event 表。"""
        states = [PREV, TARGET]
        summary, _, _, _, session = await self._run_advance(states)

        # advance 内唯一的 session.execute 是 run-item 查询（select），
        # 不含任何 FirstPyramidHistoryEvent insert。
        assert summary["target_state_count"] == 1
        compiled = " ".join(str(s) for s in session.executed).lower()
        assert "first_pyramid_history_events" not in compiled

    async def test_missing_target_state_is_counted_not_written(self):
        """target date 无 state（停牌）→ 不写行，计入 no_target_state。"""
        states = [date(2026, 8, 5), date(2026, 8, 6), PREV]
        summary, persisted, _, _, _ = await self._run_advance(states)

        assert persisted == []
        assert summary["target_state_count"] == 0
        assert summary["no_target_state"] == 1


class TestPitBoundary:
    """§5 / §11 E：future bar 绝不进入 compute input。"""

    def test_max_bar_trade_date_from_datetime_index(self):
        bars = _build_bars(end="2026-08-10")
        assert _max_bar_trade_date(bars) == TARGET

    def test_max_bar_trade_date_empty(self):
        assert _max_bar_trade_date(pd.DataFrame()) is None

    async def test_e_future_bar_raises_pit_violation(self):
        """bars 含 target 之后的 bar → 抛 PIT violation，不进入 compute。"""
        run_id = uuid.uuid4()
        iid = uuid.uuid4()
        session = _FakeSession(_FakeRun(run_id), [iid])
        compute = MagicMock(return_value=_history_with_states([TARGET]))

        with patch(
            "app.services.first_pyramid_history_service._fetch_pit_daily_bars_for_target",
            AsyncMock(return_value=_build_bars(end="2026-08-14")),  # 未来 bar
        ), patch(
            "app.services.first_pyramid_service.compute_first_pyramid_history",
            compute,
        ), patch(
            "app.services.first_pyramid_history_service._persist_target_daily_state",
            AsyncMock(),
        ):
            summary = await advance_history_to_trade_date(session, run_id, TARGET)

        assert summary["failed"] == 1
        assert "PIT violation" in summary["failed_instruments"][0]["error"]
        assert compute.call_count == 0, "PIT 违规必须在调用 compute 之前拦截"

    async def test_e_mdas_receives_pit_parameters(self):
        """PIT fetch helper 必须向 MDAS 传 end_date / adjustment_as_of 且禁 backfill。"""
        from app.services.first_pyramid_history_service import (
            _fetch_pit_daily_bars_for_target,
        )

        captured: dict[str, Any] = {}

        class _FakeMdas:
            async def get_bars(self, session, instrument_id, **kwargs):  # noqa: ANN001
                captured.update(kwargs)
                agg = MagicMock()
                agg.bars = _build_bars(end="2026-08-10")
                return agg

        with patch(
            "app.services.market_data_aggregation_service.MarketDataAggregationService",
            _FakeMdas,
        ):
            await _fetch_pit_daily_bars_for_target(
                AsyncMock(), uuid.uuid4(), output_bars=250, target_trade_date=TARGET
            )

        assert captured["end_date"] == TARGET
        assert captured["adjustment_as_of"] == TARGET
        assert captured["allow_backfill"] is False
        assert captured["completed_only"] is True
        assert captured["include_realtime"] is False


class TestRunItemsFrozen:
    """§9（用户 §4）：不 claim / 不修改任何 run item。"""

    async def test_advance_does_not_mutate_run_items(self):
        run_id = uuid.uuid4()
        iid = uuid.uuid4()
        session = _FakeSession(_FakeRun(run_id), [iid])

        with patch(
            "app.services.first_pyramid_history_service._fetch_pit_daily_bars_for_target",
            AsyncMock(return_value=_build_bars()),
        ), patch(
            "app.services.first_pyramid_service.compute_first_pyramid_history",
            MagicMock(return_value=_history_with_states([TARGET])),
        ), patch(
            "app.services.first_pyramid_history_service._persist_target_daily_state",
            AsyncMock(),
        ):
            await advance_history_to_trade_date(session, run_id, TARGET)

        compiled = " ".join(str(s) for s in session.executed).lower()
        assert "update" not in compiled, "advance 不得 UPDATE run items"
        assert "insert" not in compiled, "advance 不得 INSERT run items"

    async def test_contract_mismatch_rejected(self):
        """run contract 与当前算法 contract 不符 → ValueError（fail closed）。"""
        run_id = uuid.uuid4()
        session = _FakeSession(_FakeRun(run_id, contract="review-history-v1"), [])
        with pytest.raises(ValueError, match="contract mismatch"):
            await advance_history_to_trade_date(session, run_id, TARGET)

    async def test_missing_run_rejected(self):
        run_id = uuid.uuid4()
        session = _FakeSession(_FakeRun(run_id), [])
        session.get = AsyncMock(return_value=None)
        with pytest.raises(ValueError, match="history run not found"):
            await advance_history_to_trade_date(session, run_id, TARGET)


class TestTargetDateReadiness:
    """§9 / §11 A / F / G：required_trade_date readiness predicate I。"""

    def _make_run(self):
        run = MagicMock()
        run.scope = "all_a_share"
        run.status = "partial"
        run.expected_count = 100
        run.succeeded_count = 91
        run.skipped_count = 9
        run.failed_count = 0
        run.metadata_json = f'{{"history_contract_version": "{CONTRACT}"}}'
        return run

    async def _readiness(
        self,
        *,
        required_trade_date: date | None,
        eligible: int = 91,
        state: int = 91,
        missing_target: int = 0,
        extra_target: int = 0,
    ):
        """按 predicate 实际查询顺序提供 sequenced fake results。

        A–H 顺序：run → item group-by → skip reasons → missing-state count；
        predicate I 追加：eligible count → state count → missing → extra。
        """
        from app.services.review_bootstrap_service import (
            validate_canonical_history_run_readiness,
        )

        run = self._make_run()
        calls = {"n": 0}
        session = MagicMock()
        tail = [eligible, state, missing_target, extra_target]

        async def fake_execute(stmt):  # noqa: ANN001
            calls["n"] += 1
            result = MagicMock()
            n = calls["n"]
            if n == 1:
                result.scalar_one_or_none.return_value = run
                return result
            if n == 2:
                result.all.return_value = [("succeeded", 91), ("skipped", 9)]
                return result
            if n == 3:
                result.all.return_value = [("INSUFFICIENT_HISTORY",)] * 9
                return result
            if n == 4:
                result.scalar_one.return_value = 0  # missing_state_count (A–H)
                return result
            result.scalar_one.return_value = tail[min(n - 5, len(tail) - 1)]
            return result

        session.execute = fake_execute
        return await validate_canonical_history_run_readiness(
            session, uuid.uuid4(), CONTRACT, required_trade_date=required_trade_date
        )

    async def test_a_stale_dataset_not_ready_for_target_date(self):
        """A: 只有 08-07 state 的 dataset，对 required_trade_date=08-10 → NOT_READY。

        eligible=91（08-10 有 bar），state=0（无 08-10 state）→ missing=91。
        """
        res = await self._readiness(
            required_trade_date=TARGET, eligible=91, state=0, missing_target=91
        )
        assert res["status"] == "not_ready"
        assert "target_date_state_mismatch" in res["reason"]
        assert "2026-08-10" in res["reason"]

    async def test_f_missing_one_target_state_not_ready(self):
        """F: eligible set 缺 1 个 state → NOT_READY（不因 90 行存在就放行）。"""
        res = await self._readiness(
            required_trade_date=TARGET, eligible=91, state=90, missing_target=1
        )
        assert res["status"] == "not_ready"
        assert "missing=1" in res["reason"]

    async def test_g_complete_target_set_ready(self):
        """G: eligible set 完整 → READY，并回报 target 计数。"""
        res = await self._readiness(
            required_trade_date=TARGET, eligible=91, state=91
        )
        assert res["status"] == "ok"
        assert res["required_trade_date"] == "2026-08-10"
        assert res["target_date_eligible_count"] == 91
        assert res["target_date_state_count"] == 91

    async def test_extra_target_state_rejected(self):
        """多出不属于 eligible set 的 target state → NOT_READY（双向差集）。"""
        res = await self._readiness(
            required_trade_date=TARGET, eligible=91, state=92, extra_target=1
        )
        assert res["status"] == "not_ready"
        assert "extra=1" in res["reason"]

    async def test_backward_compatible_without_target_date(self):
        """required_trade_date=None → 行为与扩展前一致，不含 target 字段。"""
        res = await self._readiness(required_trade_date=None)
        assert res["status"] == "ok"
        assert "required_trade_date" not in res
        assert "target_date_eligible_count" not in res

    async def test_single_target_row_does_not_pass(self):
        """反 'target rows > 0 即 ready'：1 行 state 对 91 eligible 必须 NOT_READY。"""
        res = await self._readiness(
            required_trade_date=TARGET, eligible=91, state=1, missing_target=90
        )
        assert res["status"] == "not_ready"


class TestFormalReviewEventDependency:
    """§11 H：formal Review path 不消费 FirstPyramidHistoryEvent（静态证据）。"""

    FORMAL_REVIEW_FILES = [
        "review_orchestrator_service.py",
        "review_scope_service.py",
        "review_metric_observation_service.py",
        "review_signal_service.py",
        "review_attribution_service.py",
        "review_tracking_service.py",
        "review_publication_service.py",
        "review_bootstrap_service.py",
        "metric_engine.py",
        "metric_registry.py",
    ]

    def test_h_no_formal_review_file_reads_history_events(self):
        services = Path(__file__).resolve().parents[1] / "app" / "services"
        offenders = []
        for name in self.FORMAL_REVIEW_FILES:
            path = services / name
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            if (
                "FirstPyramidHistoryEvent" in text
                or "first_pyramid_history_events" in text
            ):
                offenders.append(name)
        assert offenders == [], (
            "formal Review path 出现 HistoryEvent 依赖 → "
            f"EVENT-IDENTITY-NOT-DATE-ANCHORED 须重新升级为 blocker: {offenders}"
        )
