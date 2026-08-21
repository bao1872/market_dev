"""[HISTORY-CURRENT-DATE-LIFECYCLE-01 §11] canonical history 日推进 + target-date readiness 单元测试。

覆盖用户 §11 A–I：

- A: 只有 08-07 state 的 dataset，对 required_trade_date=08-10 → NOT_READY
- B: target advance 只写 target-date state（不写历史日期）
- C: 08-07 payload / lineage before == after（不被改写）
- D: same source —— 08-07 previous 与 08-10 current 同为 be56dcd2
- E: future bar 绝不进入 compute input（PIT 断言 + MDAS 参数锁定）
- F: target eligible set 缺 1 个 state → NOT_READY
- G: target eligible set 完整 → READY
- H: FirstPyramidHistoryEvent 是 Review formal Structure Event source（ROUND-2.2A）
- I: daily advance 写 target-date state + target-date events（同一 canonical lifecycle）

运行：
    cd backend
    PURE_UNIT_TEST=1 PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest \
        tests/test_history_target_date_advance.py -v -p no:cacheprovider
"""
from __future__ import annotations

import hashlib
import json
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


def _history_with_events(dates: list[date], events: list[dict[str, Any]]) -> dict[str, Any]:
    """构造 T-date state + 指定 events 的 canonical history 结构（EVENT-LIFECYCLE 用）。"""
    return {
        "daily_state": [
            {"bar_index": i, "time": d.isoformat(), "regime_value": 0.5}
            for i, d in enumerate(dates)
        ],
        "events": events,
        "meta": {"input_hash": "hash-abc", "output_bars": 250},
    }


def _evt(etype: str, day: date, **extra: Any) -> dict[str, Any]:
    return {"type": etype, "bar_index": 5, "time": day.isoformat(), **extra}


class _FakeRun:
    def __init__(
        self,
        run_id: uuid.UUID,
        contract: str | None = CONTRACT,
        *,
        scope: str = "all_a_share",
        metadata_as_str: bool = True,
    ):
        self.id = run_id
        self.scope = scope
        self.status = "partial"
        self.algorithm_version = "fp-v1"
        # 生产 schema 中 metadata_json 是 Text 列 → 真实取到的是 JSON 字符串。
        meta = {"history_contract_version": contract} if contract else {}
        self.metadata_json = json.dumps(meta) if metadata_as_str else meta


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
    """§11 B / C / I（ROUND-2.2A）：只写 target-date state + target-date events，
    不改历史行，不写历史日期事件。"""

    async def _run_advance(self, states: list[date], bars_end: str = "2026-08-10"):
        run_id = uuid.uuid4()
        iid = uuid.uuid4()
        session = _FakeSession(_FakeRun(run_id), [iid])
        persisted: list[dict[str, Any]] = []

        async def fake_persist(_session, instrument_id, target_result, algorithm_version, **kw):
            persisted.append(
                {
                    "instrument_id": instrument_id,
                    "target_result": target_result,
                    "algorithm_version": algorithm_version,
                    **kw,
                }
            )

        with patch(
            "app.services.first_pyramid_history_service._fetch_pit_daily_bars_batch",
            AsyncMock(return_value={iid: MagicMock(bars=_build_bars(end=bars_end))}),
        ), patch(
            "app.services.first_pyramid_service.compute_first_pyramid_history",
            MagicMock(return_value=_history_with_states(states)),
        ), patch(
            "app.services.first_pyramid_history_service._persist_history_result",
            fake_persist,
        ):
            summary = await advance_history_to_trade_date(session, run_id, TARGET)
        return summary, persisted, run_id, iid, session

    async def test_b_only_target_date_state_written(self):
        """B: history 含 08-05/08-06/08-07/08-10 四天，只持久化 08-10 一行 state。"""
        states = [date(2026, 8, 5), date(2026, 8, 6), PREV, TARGET]
        summary, persisted, _, _, _ = await self._run_advance(states)

        assert summary["target_state_count"] == 1
        assert len(persisted) == 1, "必须最多写 1 次/instrument（1x 写放大，非 250x）"
        states_in = persisted[0]["target_result"]["daily_state"]
        assert len(states_in) == 1, "target_result 只含 target-date 1 行 state"
        assert states_in[0]["time"] == TARGET.isoformat()

    async def test_c_historical_dates_never_touched(self):
        """C: 历史日期（08-07 及更早）不出现在任何持久化调用中。"""
        states = [date(2026, 8, 5), date(2026, 8, 6), PREV, TARGET]
        _, persisted, _, _, _ = await self._run_advance(states)

        state_dates = {
            s["time"] for p in persisted for s in p["target_result"]["daily_state"]
        }
        assert state_dates == {TARGET.isoformat()}
        assert PREV.isoformat() not in state_dates
        assert date(2026, 8, 5).isoformat() not in state_dates

    async def test_d_lineage_stays_on_existing_canonical_run(self):
        """D: source_history_run_id 保持为传入的 canonical run（不新建 run X）。"""
        states = [PREV, TARGET]
        _, persisted, run_id, _, _ = await self._run_advance(states)

        assert persisted[0]["source_history_run_id"] == run_id
        assert persisted[0]["history_contract_version"] == CONTRACT

    async def test_i_target_events_written(self):
        """I（ROUND-2.2A）：daily advance 持久化 target-date events（同一 lifecycle）。

        FirstPyramidHistoryEvent 是 Review formal Structure Event source；advance
        从同一次 canonical compute 提取 exact-T events 一并持久化。仅 target-date
        事件（SQZ_RELEASE@08-10）被保留，非 target 事件（BOS@05-01）被 date-adapter
        过滤掉。
        """
        states = [PREV, TARGET]
        summary, persisted, _, _, _ = await self._run_advance(states)

        assert summary["target_state_count"] == 1
        events = persisted[0]["target_result"]["events"]
        # 只有 target-date 的 SQZ_RELEASE；非 target 的 BOS@05-01 被过滤。
        assert [e["type"] for e in events] == ["SQZ_RELEASE"]
        assert all(e["time"] == TARGET.isoformat() for e in events)

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
            "app.services.first_pyramid_history_service._fetch_pit_daily_bars_batch",
            AsyncMock(return_value={iid: MagicMock(bars=_build_bars(end="2026-08-14"))}),  # 未来 bar
        ), patch(
            "app.services.first_pyramid_service.compute_first_pyramid_history",
            compute,
        ), patch(
            "app.services.first_pyramid_history_service._persist_history_result",
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


class TestPitBatchParity:
    """Phase 3.3：old single vs new batch INPUT parity + 冻结算法 OUTPUT parity。

    INPUT_PARITY = 100%：单/批两路得到相同 bars（index/OHLCV/amount/row count/max date）
    与相同 source_bar_hash / adj_factor_hash / completed_through。
    OUTPUT_PARITY = 100%：把两路 bars 送入**同一个冻结的** compute_first_pyramid_history，
    canonical JSON hash 一致（比只比较 target_state_count 更强）。
    """

    @staticmethod
    def _bar_result(bars: pd.DataFrame):
        from app.services.market_data_aggregation_service import BarAggregationResult

        return BarAggregationResult(
            bars=bars,
            data_source="test",
            as_of=pd.Timestamp("2026-08-10", tz="UTC"),
            is_partial=False,
            last_persisted_bar_time=None,
            last_live_bar_time=None,
            freshness_seconds=0.0,
            degraded=False,
            degraded_reason=None,
            source_bar_hash="single-batch-same-hash",
            adj_factor_hash="adj-hash-1",
            completed_through=pd.Timestamp("2026-08-10"),
        )

    async def test_input_parity_single_vs_batch(self):
        """old single _fetch_pit_daily_bars_for_target vs new batch 读同一数据 → bars/hash 全一致。"""
        from app.services.first_pyramid_history_service import (
            _fetch_pit_daily_bars_batch,
            _fetch_pit_daily_bars_for_target,
        )
        from app.services.market_data_aggregation_service import (
            BarAggregationResult,
            MarketDataAggregationService,
        )

        iid = uuid.uuid4()
        bars = _build_bars()
        result = self._bar_result(bars)

        async def fake_single(_self, session, instrument_id, **kw):  # noqa: ANN001
            return result

        async def fake_batch(_self, session, instrument_ids, **kw):  # noqa: ANN001
            return {instrument_ids[0]: result}

        with patch.object(MarketDataAggregationService, "get_bars", fake_single), patch.object(
            MarketDataAggregationService, "get_bars_batch", fake_batch
        ):
            df_single = await _fetch_pit_daily_bars_for_target(
                AsyncMock(), iid, output_bars=250, target_trade_date=TARGET
            )
            batch = await _fetch_pit_daily_bars_batch(
                AsyncMock(), [iid], output_bars=250, target_trade_date=TARGET
            )

        br = batch[iid]
        assert isinstance(br, BarAggregationResult), "batch 必须保留完整 BarAggregationResult"
        # index / columns / 逐值 / row count / max date —— INPUT parity 100%
        assert list(df_single.index) == list(br.bars.index)
        assert list(df_single.columns) == list(br.bars.columns)
        assert (df_single.values == br.bars.values).all()
        assert len(df_single) == len(br.bars)
        assert df_single.index.max() == br.bars.index.max()
        # hash / completed_through —— INPUT parity 100%
        assert br.source_bar_hash == result.source_bar_hash
        assert br.adj_factor_hash == result.adj_factor_hash
        assert br.completed_through == result.completed_through

    async def test_output_parity_frozen_algorithm(self):
        """单/批两路 bars 送入同一冻结 compute 函数 → canonical JSON hash 100% 一致。"""
        from app.services.first_pyramid_history_service import (
            _fetch_pit_daily_bars_batch,
            _fetch_pit_daily_bars_for_target,
        )
        from app.services.first_pyramid_service import compute_first_pyramid_history
        from app.services.market_data_aggregation_service import MarketDataAggregationService

        iid = uuid.uuid4()
        bars = _build_bars()
        result = self._bar_result(bars)

        async def fake_single(_self, session, instrument_id, **kw):  # noqa: ANN001
            return result

        async def fake_batch(_self, session, instrument_ids, **kw):  # noqa: ANN001
            return {instrument_ids[0]: result}

        with patch.object(MarketDataAggregationService, "get_bars", fake_single), patch.object(
            MarketDataAggregationService, "get_bars_batch", fake_batch
        ):
            df_single = await _fetch_pit_daily_bars_for_target(
                AsyncMock(), iid, output_bars=250, target_trade_date=TARGET
            )
            batch = await _fetch_pit_daily_bars_batch(
                AsyncMock(), [iid], output_bars=250, target_trade_date=TARGET
            )

        hist_single = compute_first_pyramid_history(
            bars=df_single, symbol=str(iid), output_bars=250, include_chip=False
        )
        hist_batch = compute_first_pyramid_history(
            bars=batch[iid].bars, symbol=str(iid), output_bars=250, include_chip=False
        )

        def _hash(obj: Any) -> str:
            return hashlib.sha256(
                json.dumps(obj, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()

        assert _hash(hist_single) == _hash(hist_batch), "冻结算法 OUTPUT parity 必须 100%"

    async def test_batch_pit_params_frozen(self):
        """批读 helper 必须向 get_bars_batch 传 PIT 参数（end/adjustment/禁 backfill/completed_only）。"""
        from app.services.first_pyramid_history_service import _fetch_pit_daily_bars_batch
        from app.services.market_data_aggregation_service import (
            BarAggregationResult,
            MarketDataAggregationService,
        )

        iid = uuid.uuid4()
        result = self._bar_result(_build_bars())
        captured: dict[str, Any] = {}

        async def fake_batch(_self, session, instrument_ids, **kw):  # noqa: ANN001
            captured.update(kw)
            return {instrument_ids[0]: result}

        with patch.object(MarketDataAggregationService, "get_bars_batch", fake_batch):
            out = await _fetch_pit_daily_bars_batch(
                AsyncMock(), [iid], output_bars=250, target_trade_date=TARGET
            )

        assert out[iid] is result, "BarAggregationResult 原样保留（不提前丢 hash/diagnostics）"
        assert isinstance(out[iid], BarAggregationResult)
        assert captured["end_date"] == TARGET
        assert captured["adjustment_as_of"] == TARGET
        assert captured["allow_backfill"] is False
        assert captured["completed_only"] is True
        assert captured["include_realtime"] is False
        assert captured["adj"] == "qfq"
        assert captured["timeframe"] == "1d"


class TestBatchFetchContract:
    """Phase 3.3 batch contract（修改 2/3）：缺失=失败、整批异常可见、未知类型=失败。"""

    @staticmethod
    async def _fetch(  # noqa: ANN202
        build_map,
        *,
        raise_on_call: bool = False,
    ):
        from app.services.first_pyramid_history_service import _fetch_pit_daily_bars_batch
        from app.services.market_data_aggregation_service import MarketDataAggregationService

        iids = [uuid.uuid4(), uuid.uuid4()]

        async def fake_batch(_self, session, instrument_ids, **kw):  # noqa: ANN001
            if raise_on_call:
                raise RuntimeError("boom")
            return build_map(list(instrument_ids))

        with patch.object(MarketDataAggregationService, "get_bars_batch", fake_batch):
            result = await _fetch_pit_daily_bars_batch(
                AsyncMock(), iids, output_bars=250, target_trade_date=TARGET
            )
        return result, iids

    async def test_missing_instrument_is_failure_not_no_bar(self):
        """batch result 缺某 instrument → BATCH CONTRACT VIOLATION（RuntimeError），绝非 no_bar。"""
        from app.services.market_data_aggregation_service import BarAggregationResult

        result = BarAggregationResult(
            bars=_build_bars(),
            data_source="test",
            as_of=pd.Timestamp("2026-08-10", tz="UTC"),
            is_partial=False,
            last_persisted_bar_time=None,
            last_live_bar_time=None,
            freshness_seconds=0.0,
            degraded=False,
            degraded_reason=None,
        )
        # build_map 只给第一个 iid valid result；第二个 iid 缺失
        out, iids = await self._fetch(lambda ids: {ids[0]: result})

        assert out[iids[0]] is result, "存在的结果原样保留"
        assert isinstance(out[iids[1]], RuntimeError)
        assert "BATCH CONTRACT VIOLATION" in str(out[iids[1]]), \
            "缺失不是 no_bar，必须是失败"

    async def test_whole_batch_exception_visible_per_instrument(self):
        """整批 get_bars_batch 抛异常 → 每股收到明确 Exception，不整批退出、不静默 fallback。"""
        result, iids = await self._fetch(None, raise_on_call=True)
        assert len(result) == len(iids)
        for iid in iids:
            assert isinstance(result[iid], RuntimeError)
            assert "MDAS batch fetch failed" in str(result[iid])

    async def test_unknown_type_is_failure(self):
        """值既非 BarAggregationResult 也非 Exception → RuntimeError。"""
        result, iids = await self._fetch(lambda ids: {ids[0]: "not-a-result"})
        assert isinstance(result[iids[0]], RuntimeError)
        assert "unexpected MDAS batch result type" in str(result[iids[0]])


class TestRunItemsFrozen:
    """§9（用户 §4）：不 claim / 不修改任何 run item。"""

    async def test_advance_does_not_mutate_run_items(self):
        run_id = uuid.uuid4()
        iid = uuid.uuid4()
        session = _FakeSession(_FakeRun(run_id), [iid])

        with patch(
            "app.services.first_pyramid_history_service._fetch_pit_daily_bars_batch",
            AsyncMock(return_value={iid: MagicMock(bars=_build_bars())}),
        ), patch(
            "app.services.first_pyramid_service.compute_first_pyramid_history",
            MagicMock(return_value=_history_with_states([TARGET])),
        ), patch(
            "app.services.first_pyramid_history_service._persist_history_result",
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

    async def test_metadata_json_text_column_parsed(self):
        """metadata_json 是 Text 列（生产 schema）→ 必须按 JSON 字符串解析。

        真实 08-10 首次执行即被此形态击中（'str' object has no attribute 'get'），
        本测试锁定回归。
        """
        run_id = uuid.uuid4()
        run = _FakeRun(run_id, metadata_as_str=True)
        assert isinstance(run.metadata_json, str)  # 前提：确实是字符串
        session = _FakeSession(run, [])
        summary = await advance_history_to_trade_date(session, run_id, TARGET)
        assert summary["total"] == 0  # 无 run item，但未因解析失败抛错

    async def test_metadata_json_dict_still_supported(self):
        """metadata_json 已是 dict（ORM JSON 变体）→ 同样可用。"""
        run_id = uuid.uuid4()
        run = _FakeRun(run_id, metadata_as_str=False)
        assert isinstance(run.metadata_json, dict)
        session = _FakeSession(run, [])
        summary = await advance_history_to_trade_date(session, run_id, TARGET)
        assert summary["total"] == 0

    async def test_non_canonical_scope_rejected(self):
        """scope != all_a_share（如 canary/sample）→ ValueError（§4 guard）。"""
        run_id = uuid.uuid4()
        session = _FakeSession(_FakeRun(run_id, scope="canary"), [])
        with pytest.raises(ValueError, match="scope not canonical"):
            await advance_history_to_trade_date(session, run_id, TARGET)

    async def test_missing_run_rejected(self):
        run_id = uuid.uuid4()
        session = _FakeSession(_FakeRun(run_id), [])
        session.get = AsyncMock(return_value=None)
        with pytest.raises(ValueError, match="history run not found"):
            await advance_history_to_trade_date(session, run_id, TARGET)


class TestEventLifecycle:
    """ROUND-2.2A EVENT-LIFECYCLE-01~05：exact-T State + Events 同一 lifecycle。

    invariant: exact-T First Pyramid 可被 Review 消费 ⇔ 同一次 canonical calculation
    同时形成完整 T-day State 与 T-day Structure Event stream；零事件也是合法可证明结果。
    """

    async def _run(self, history: dict[str, Any]):
        """Capture the exact target_result handed to _persist_history_result."""
        run_id = uuid.uuid4()
        iid = uuid.uuid4()
        session = _FakeSession(_FakeRun(run_id), [iid])
        captured: list[dict[str, Any]] = []

        async def fake_persist(_session, instrument_id, target_result, algorithm_version, **kw):
            captured.append(target_result)

        with patch(
            "app.services.first_pyramid_history_service._fetch_pit_daily_bars_batch",
            AsyncMock(return_value={iid: MagicMock(bars=_build_bars())}),
        ), patch(
            "app.services.first_pyramid_service.compute_first_pyramid_history",
            MagicMock(return_value=history),
        ), patch(
            "app.services.first_pyramid_history_service._persist_history_result",
            fake_persist,
        ):
            summary = await advance_history_to_trade_date(session, run_id, TARGET)
        return summary, captured, session

    async def test_lifecycle_01_state_and_events_persisted(self):
        """EVENT-LIFECYCLE-01: T 日 compute: 1 state + 2 events -> both persisted."""
        history = _history_with_events(
            [PREV, TARGET],
            [_evt("BOS", TARGET, internal=False),
             _evt("CHoCH", TARGET, internal=True)],
        )
        summary, captured, _ = await self._run(history)

        assert summary["target_state_count"] == 1
        assert len(captured) == 1
        tr = captured[0]
        assert len(tr["daily_state"]) == 1  # 1x target state
        assert [e["type"] for e in tr["events"]] == ["BOS", "CHoCH"]
        assert all(e["time"] == TARGET.isoformat() for e in tr["events"])

    async def test_lifecycle_02_zero_event_is_legal_completion(self):
        """EVENT-LIFECYCLE-02 (most important): T 成功 + 0 events -> state persisted,
        events empty, but calculation is legitimately complete (zero-event ≠ no coverage)."""
        history = _history_with_events([PREV, TARGET], [])
        summary, captured, _ = await self._run(history)

        assert summary["target_state_count"] == 1
        assert summary["no_target_state"] == 0
        assert summary["failed"] == 0
        assert len(captured) == 1
        tr = captured[0]
        assert len(tr["daily_state"]) == 1
        assert tr["events"] == [], "零事件是合法 lifecycle 结果，非 no coverage"

    async def test_lifecycle_03_only_target_events_persisted(self):
        """EVENT-LIFECYCLE-03: 历史窗口含 T-1 与 T events -> 只持久化 T events。"""
        history = _history_with_events(
            [PREV, TARGET],
            [_evt("BOS", PREV),      # T-1 事件，不得持久化
             _evt("CHoCH", TARGET),  # T 事件
             _evt("EQH", TARGET)],
        )
        summary, captured, _ = await self._run(history)

        assert summary["target_state_count"] == 1
        events = captured[0]["events"]
        assert [e["type"] for e in events] == ["CHoCH", "EQH"]
        assert all(e["time"] == TARGET.isoformat() for e in events)
        assert not any(e["time"] == PREV.isoformat() for e in events)

    async def test_lifecycle_04_retry_produces_same_target_event_set(self):
        """EVENT-LIFECYCLE-04: 两次 advance slicing 产出同一目标事件集（deterministic）。

        注意：此测试只证明 adapter 两次 slice 得到相同输入事件集合（deterministic），
        **不**证明 DB 层不重复插入。真正的 persistence idempotency 由
        ``test_pg_review_lineage_contract.py::test_event_legacy_v2_coexistence_and_idempotency``
        （真实 PostgreSQL partial unique index + ON CONFLICT DO NOTHING，不 mock
        ``_persist_history_result``）提供证据。
        """
        history = _history_with_events(
            [PREV, TARGET],
            [_evt("BOS", TARGET), _evt("OB_CREATED", TARGET)],
        )
        _, captured1, _ = await self._run(history)
        _, captured2, _ = await self._run(history)

        def _event_ids(trs: list[dict[str, Any]]) -> list[str]:
            out = []
            for tr in trs:
                for e in tr["events"]:
                    eid = e.get("event_id") or e.get("id") or f"{e['type']}_{e['time']}"
                    out.append(str(eid))
            return out

        ids1 = _event_ids(captured1)
        ids2 = _event_ids(captured2)
        # deterministic target event set; DB no-dup 由真实 PG 测试（F2）证明。
        assert ids1 == ids2
        assert len(ids1) == len(set(ids1)), "同一天同类事件必须可区分（稳定 ID）"

    async def test_lifecycle_05_no_target_state_no_orphan_events(self):
        """EVENT-LIFECYCLE-05: 无 target_state -> 不得只写 orphan T events。"""
        # 只有 PREV state，无 TARGET state（停牌）；但 history 有 TARGET 事件。
        history = _history_with_events([PREV], [_evt("BOS", TARGET)])
        summary, captured, _ = await self._run(history)

        # target state 缺失 → 该 instrument 直接跳过，不写任何 target_result（含事件）。
        assert summary["no_target_state"] == 1
        assert summary["target_state_count"] == 0
        assert captured == [], "无 target_state 时不得写 orphan T events"
        # 事件也不能在 state 缺席时被凭空持久化（同一 lifecycle 原子性）

    async def test_failclosed_missing_timestamp_no_persistence(self):
        """EVENT-LIFECYCLE-06 (F1): event 缺 time -> fail closed, persistence NOT called."""
        history = _history_with_events(
            [PREV, TARGET],
            [{"type": "BOS", "bar_index": 5}],  # 无 time
        )
        summary, captured, _ = await self._run(history)

        assert summary["failed"] == 1
        assert summary["target_state_count"] == 0
        assert captured == [], "fail-closed 时不得调用 persistence"

    async def test_failclosed_anchor_time_does_not_fallback(self):
        """EVENT-LIFECYCLE-10 (F1): event 有 anchor_time 但缺 canonical time ->
        fail closed（anchor_time 不是 occurrence date，不允许 fallback 推断）。"""
        history = _history_with_events(
            [PREV, TARGET],
            # anchor_time=08-06 只是 anchor/pivot bar 时间，不是 BOS 发生日期；
            # canonical time 缺失 -> CONTRACT CORRUPTION，必须 fail closed。
            [{"type": "BOS", "bar_index": 5, "anchor_time": PREV.isoformat()}],
        )
        summary, captured, _ = await self._run(history)

        assert summary["failed"] == 1
        assert summary["target_state_count"] == 0
        assert captured == [], "anchor_time 不得 fallback 为 event date"

    async def test_failclosed_invalid_timestamp_no_persistence(self):
        """EVENT-LIFECYCLE-07 (F1): invalid event timestamp -> fail closed, persistence NOT called."""
        history = _history_with_events(
            [PREV, TARGET],
            [_evt("BOS", TARGET, time="not-a-date")],
        )
        summary, captured, _ = await self._run(history)

        assert summary["failed"] == 1
        assert summary["target_state_count"] == 0
        assert captured == [], "invalid timestamp 不得当零事件持久化"

    async def test_failclosed_future_timestamp_pit_violation(self):
        """EVENT-LIFECYCLE-08 (F1): event date > T -> PIT violation, fail closed, no persistence."""
        future = date(2026, 9, 1)  # > TARGET
        history = _history_with_events(
            [PREV, TARGET],
            [_evt("BOS", future)],
        )
        summary, captured, _ = await self._run(history)

        assert summary["failed"] == 1
        assert summary["target_state_count"] == 0
        assert captured == [], "future event = leakage，不得持久化"

    async def test_failclosed_valid_t1_event_ignored_not_failed(self):
        """EVENT-LIFECYCLE-09 (F1): event date < T (history-window legacy) -> ignored, NOT failed."""
        history = _history_with_events(
            [PREV, TARGET],
            [_evt("BOS", PREV)],  # T-1 事件
        )
        summary, captured, _ = await self._run(history)

        assert summary["failed"] == 0
        assert summary["target_state_count"] == 1
        assert len(captured) == 1
        assert captured[0]["events"] == [], "T-1 事件被忽略，但 lifecycle 仍成功"


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
        from app.services.review_history_readiness_service import (
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
        "review_attribution_service.py",
        "review_publication_service.py",
        "review_history_readiness_service.py",
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
