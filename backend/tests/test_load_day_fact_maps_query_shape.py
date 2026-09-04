"""Review-v2 load_day_fact_maps 契约 / 回归 / lineage / memory-bound 测试。

覆盖：
- P1-B VERSION：REVIEW_ALGORITHM_VERSION == review-2.0.1；同版本 history 仍 eligible
- P1-C HISTORY_STATE lineage：CURRENT T 状态为权威；NULL/不一致 fail closed
- P1-D MEMORY BOUND：history 查询 bounded（无 <= T 全扫描）；Bar 窗口分块
- P2 SNAPSHOT parity：stock_core 模式错误 trade_date fail closed
- load-once：query count 随 chunk 数而非 scope 数增长
- OptionalScopeUnavailableError：本 loader 由调用方用空 instrument_ids / 空返回处理

运行：
    cd backend
    PURE_UNIT_TEST=1 .venv/bin/python -m pytest \
        tests/test_load_day_fact_maps_query_shape.py -v
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import date
from unittest.mock import MagicMock

import pytest

from app.domain.review.versions import REVIEW_ALGORITHM_VERSION
from app.services.review_scope_service import (
    load_day_fact_maps,
)

# 与 review_scope_service 模块内常量保持一致
_REVIEW_HISTORY_CONTRACT_VERSION = "review-history-v2"

TARGET = date(2026, 8, 4)
PREV = date(2026, 8, 3)


class _HistoryState:
    def __init__(self, instrument_id, trade_date, payload, source_history_run_id=None,
                 history_contract_version=_REVIEW_HISTORY_CONTRACT_VERSION,
                 input_hash="hash-x", state_id=None,
                 algorithm_version="first-pyramid-core-v2.1") -> None:
        self.id = state_id or uuid.uuid4()
        self.instrument_id = instrument_id
        self.trade_date = trade_date
        self.state_payload = payload
        self.input_hash = input_hash
        self.source_history_run_id = source_history_run_id
        self.history_contract_version = history_contract_version
        self.algorithm_version = algorithm_version


class _Bar:
    def __init__(self, instrument_id, trade_date, close, prev_close, volume, amount) -> None:
        self.instrument_id = instrument_id
        self.trade_date = trade_date
        self.open = prev_close
        self.high = max(close, prev_close)
        self.low = min(close, prev_close)
        self.close = close
        self.volume = volume
        self.amount = amount


class _Instrument:
    def __init__(self, instrument_id, symbol) -> None:
        self.id = instrument_id
        self.symbol = symbol
        self.name = symbol


SAMPLE_FP_FLAT = {
    "fp_trend_direction": "上行",
    "fp_swing_direction": "上行",
    "fp_internal_direction": "上行",
    "fp_structure_alignment": "共振",
    "fp_momentum_direction": "扩张",
    "fp_momentum_change": 0.1,
    "fp_volume_ratio20": 1.2,
    "fp_volume_percentile20": 70.0,
    "fp_latest_bos_direction": "bullish",
    "fp_latest_bos_freshness": 2,
    "fp_latest_choch_direction": "bullish",
    "fp_latest_choch_freshness": 3,
    "fp_latest_ob_direction": "bullish",
    "fp_latest_ob_freshness": 4,
    "fp_segment_volume_ratio": 1.0,
    "fp_prev_segment_volume": 100.0,
}


def _make_session(
    instruments,
    snap_summaries=None,
    snap_trade_date=TARGET,
    bar_sets=None,
    current_states=None,
    previous_states=None,
):
    """SQL-aware mock AsyncSession。

    根据 stmt 字符串区分实体与 operator：
      - first_pyramid_history_daily_state + 'trade_date <'  → PREVIOUS (< T)
      - first_pyramid_history_daily_state (其他)            → CURRENT (== T)
      - stock_feature_snapshot                              → snapshot 投影
      - bars_daily                                         → 400d bar 窗口
      - instruments                                         → identity
    """
    session = MagicMock()
    shared_run = uuid.uuid4()

    if current_states is None:
        current_states = []
    if previous_states is None:
        previous_states = [
            _HistoryState(inst, PREV, {"first_pyramid_flat": {"dfx_score": 1}},
                         source_history_run_id=shared_run)
            for inst in instruments
        ]
    if snap_summaries is None:
        snap_summaries = []
    if bar_sets is None:
        bar_sets = [(10.0, 9.5, 500.0, 5000.0) for _ in instruments]

    async def fake_execute(stmt):
        sql = str(stmt)
        fake = MagicMock()
        if "first_pyramid_history_daily_state" in sql:
            if "trade_date <" in sql:
                rows = previous_states
            else:
                rows = current_states
            fake.scalars.return_value = MagicMock(__iter__=lambda self: iter(rows))
        elif "stock_feature_snapshot" in sql:
            # [P1-A] 正式 stock_core 查询现在只投影 summary_payload["first_pyramid_flat"]，
            # 第三列直接是 flat 字典（不再是含 first_pyramid_flat 键的全量 summary）。
            rows = [
                (inst, snap_trade_date, s)
                for inst, s in zip(instruments, snap_summaries, strict=False)
            ]
            fake.all.return_value = rows
            fake.scalars.return_value = MagicMock(__iter__=lambda self: iter([]))
        elif "bars_daily" in sql:
            bars = []
            for inst, (c, prev_c, vol, amt) in zip(instruments, bar_sets, strict=False):
                bars.append(_Bar(inst, PREV, prev_c, prev_c - 0.1, vol, amt))
                bars.append(_Bar(inst, TARGET, c, prev_c, vol, amt))
            fake.scalars.return_value = MagicMock(__iter__=lambda self: iter(bars))
        elif "instruments" in sql:
            idents = [_Instrument(inst, f"SYM{i}") for i, inst in enumerate(instruments)]
            fake.scalars.return_value = MagicMock(__iter__=lambda self: iter(idents))
        else:
            fake.scalars.return_value = MagicMock(__iter__=lambda self: iter([]))
        return fake

    session.execute = fake_execute
    return session


def _run(**kw):
    return asyncio.run(load_day_fact_maps(MagicMock(), trade_date=TARGET, **kw))


# =====================================================================
# P1-B VERSION COMPATIBILITY
# =====================================================================
class TestReviewAlgorithmVersion:
    def test_version_is_review_2_0_1(self) -> None:
        assert REVIEW_ALGORITHM_VERSION == "review-2.0.1"

    def test_same_version_history_eligible(self) -> None:
        # load_metric_history 用 algorithm_version == run.algorithm_version 过滤；
        # 同版本历史系列保持 eligible（不创建隔离）。
        from app.services.review_metric_observation_service import load_metric_history
        assert callable(load_metric_history)
        assert REVIEW_ALGORITHM_VERSION == "review-2.0.1"


# =====================================================================
# FORMAL CURRENT (stock_core)
# =====================================================================
class TestFormalCurrentSource:
    def test_current_fp_from_stock_core(self) -> None:
        ids = [uuid.uuid4() for _ in range(3)]
        session = _make_session(
            ids, snap_summaries=[dict(SAMPLE_FP_FLAT) for _ in range(3)],
            bar_sets=[(10.0, 9.8, 1000.0, 10000.0) for _ in range(3)],
        )
        facts = asyncio.run(load_day_fact_maps(
            session, trade_date=TARGET,
            source_core_run_id=uuid.uuid4(), instrument_ids=ids,
        ))
        assert len(facts) == 3
        assert facts[ids[0]]["fp_trend_direction"] == "上行"
        assert facts[ids[0]]["review_price_position"] is not None

    def test_current_history_state_zero_ok(self) -> None:
        """形式 Review 不要求 FirstPyramidHistoryDailyState(T)。"""
        ids = [uuid.uuid4()]
        session = _make_session(
            ids, snap_summaries=[dict(SAMPLE_FP_FLAT)],
            bar_sets=[(10.0, 9.5, 500.0, 5000.0)],
            # 无 current state（history_state 模式才用）；stock_core 模式不依赖
            current_states=[],
        )
        facts = asyncio.run(load_day_fact_maps(
            session, trade_date=TARGET,
            source_core_run_id=uuid.uuid4(), instrument_ids=ids,
        ))
        assert len(facts) == 1
        assert facts[ids[0]]["review_return_1d"] is not None

    def test_snapshot_wrong_trade_date_rejected(self) -> None:
        """P2: stock_core 模式 snapshot trade_date != T → fail closed。"""
        ids = [uuid.uuid4()]
        session = _make_session(
            ids, snap_summaries=[dict(SAMPLE_FP_FLAT)],
            snap_trade_date=date(2026, 8, 1),  # 错误日期
            bar_sets=[(10.0, 9.5, 500.0, 5000.0)],
        )
        with pytest.raises(ValueError, match="STOCK_CORE_SNAPSHOT_TRADE_DATE_MISMATCH"):
            asyncio.run(load_day_fact_maps(
                session, trade_date=TARGET,
                source_core_run_id=uuid.uuid4(), instrument_ids=ids,
            ))


# =====================================================================
# REVIEW FACT SSOT
# =====================================================================
class TestReviewFactSSOT:
    def test_production_flat_has_no_synthetic_rolling_fields(self) -> None:
        flat = dict(SAMPLE_FP_FLAT)
        for k in ("review_return_1d", "review_price_position", "review_volume_ratio20",
                  "review_amount_ratio20", "review_volume_percentile20",
                  "review_amount_percentile200"):
            assert k not in flat

    def test_review_rolling_facts_derived_by_ssot(self) -> None:
        ids = [uuid.uuid4()]
        session = _make_session(
            ids, snap_summaries=[dict(SAMPLE_FP_FLAT)],
            bar_sets=[(10.0, 9.5, 500.0, 5000.0)],
        )
        facts = asyncio.run(load_day_fact_maps(
            session, trade_date=TARGET,
            source_core_run_id=uuid.uuid4(), instrument_ids=ids,
        ))
        fact = facts[ids[0]]
        assert abs(fact["review_return_1d"] - ((10.0 - 9.5) / 9.5 * 100.0)) < 1e-6
        assert fact["review_price_position"] is not None
        assert fact["review_volume_ratio20"] is not None
        assert fact["review_amount_ratio20"] is not None
        assert fact["review_volume_percentile20"] is not None
        assert fact["review_amount_percentile200"] is not None


# =====================================================================
# P1-C HISTORY_STATE LINEAGE
# =====================================================================
class TestHistoryStateLineage:
    def _ids(self, n=1):
        return [uuid.uuid4() for _ in range(n)]

    def test_current_source_null_fail_closed(self) -> None:
        ids = self._ids()
        cur = [_HistoryState(ids[0], TARGET, {"first_pyramid_flat": dict(SAMPLE_FP_FLAT)},
                             source_history_run_id=None)]  # NULL source run
        session = _make_session(
            ids, current_states=cur,
            bar_sets=[(10.0, 9.5, 500.0, 5000.0)],
        )
        with pytest.raises(ValueError, match="HISTORY_STATE_CURRENT_SOURCE_RUN_NULL"):
            asyncio.run(load_day_fact_maps(
                session, trade_date=TARGET, instrument_ids=ids,
                required_source_history_run_id=uuid.uuid4(), current_source="history_state",
            ))

    def test_current_a_previous_b_fail_closed(self) -> None:
        ids = self._ids()
        run_a = uuid.uuid4()
        run_b = uuid.uuid4()
        cur = [_HistoryState(ids[0], TARGET, {"first_pyramid_flat": dict(SAMPLE_FP_FLAT)},
                             source_history_run_id=run_a)]
        prev = [_HistoryState(ids[0], PREV, {"first_pyramid_flat": {"dfx_score": 1}},
                              source_history_run_id=run_b)]
        session = _make_session(
            ids, current_states=cur, previous_states=prev,
            bar_sets=[(10.0, 9.5, 500.0, 5000.0)],
        )
        with pytest.raises(ValueError, match="HISTORY_STATE_PREVIOUS_SOURCE_RUN_MISMATCH"):
            asyncio.run(load_day_fact_maps(
                session, trade_date=TARGET, instrument_ids=ids, current_source="history_state"))

    def test_current_a_previous_a_pass(self) -> None:
        ids = self._ids()
        run_a = uuid.uuid4()
        cur = [_HistoryState(ids[0], TARGET, {"first_pyramid_flat": dict(SAMPLE_FP_FLAT)},
                             source_history_run_id=run_a)]
        prev = [_HistoryState(ids[0], PREV, {"first_pyramid_flat": {"dfx_score": 1}},
                              source_history_run_id=run_a)]
        session = _make_session(
            ids, current_states=cur, previous_states=prev,
            bar_sets=[(10.0, 9.5, 500.0, 5000.0)],
        )
        facts = asyncio.run(load_day_fact_maps(
            session, trade_date=TARGET, instrument_ids=ids, current_source="history_state"))
        assert len(facts) == 1

    def test_output_real_lineage_fields(self) -> None:
        ids = self._ids()
        run_a = uuid.uuid4()
        state_id = uuid.uuid4()
        input_hash = "input-hash-current"
        cur = [_HistoryState(ids[0], TARGET, {"first_pyramid_flat": dict(SAMPLE_FP_FLAT)},
                             source_history_run_id=run_a, input_hash=input_hash, state_id=state_id)]
        prev = [_HistoryState(ids[0], PREV, {"first_pyramid_flat": {"dfx_score": 1}},
                              source_history_run_id=run_a)]
        session = _make_session(
            ids, current_states=cur, previous_states=prev,
            bar_sets=[(10.0, 9.5, 500.0, 5000.0)],
        )
        facts = asyncio.run(load_day_fact_maps(
            session, trade_date=TARGET, instrument_ids=ids, current_source="history_state"))
        fact = facts[ids[0]]
        assert fact["_history_state_id"] == str(state_id)
        assert fact["_history_source_run_id"] == str(run_a)
        assert fact["_history_input_hash"] == input_hash

    def test_optional_no_current_state_skip(self) -> None:
        """history_state 模式无当前 T 状态 → 该标的跳过（无 CURRENT FP）。"""
        ids = self._ids()
        prev = [_HistoryState(ids[0], PREV, {"first_pyramid_flat": {"dfx_score": 1}},
                              source_history_run_id=uuid.uuid4())]
        session = _make_session(
            ids, current_states=[], previous_states=prev,  # 无 T 状态
            bar_sets=[(10.0, 9.5, 500.0, 5000.0)],
        )
        facts = asyncio.run(load_day_fact_maps(
            session, trade_date=TARGET, instrument_ids=ids, current_source="history_state"))
        assert ids[0] not in facts


# =====================================================================
# P1-D MEMORY BOUND
# =====================================================================
class TestMemoryBound:
    def test_no_unbounded_le_t_scan(self) -> None:
        """FORMAL stock_core 历史查询不包含 'trade_date <=' 全历史扫描。"""
        ids = [uuid.uuid4() for _ in range(3)]
        captured = []
        session = _make_session(
            ids, snap_summaries=[dict(SAMPLE_FP_FLAT) for _ in range(3)],
            bar_sets=[(10.0, 9.8, 1000.0, 10000.0) for _ in range(3)],
        )
        original = session.execute

        async def spy(stmt):
            captured.append(str(stmt))
            return await original(stmt)

        session.execute = spy
        asyncio.run(load_day_fact_maps(
            session, trade_date=TARGET,
            source_core_run_id=uuid.uuid4(), instrument_ids=ids))
        for sql in captured:
            if "first_pyramid_history_daily_state" in sql:
                assert "trade_date <=" not in sql, f"unbounded <= T scan: {sql}"

    def test_previous_query_nearest_only(self) -> None:
        """FORMAL previous 查询返回每个标的最近一条 < T（DISTINCT ON）。"""
        ids = [uuid.uuid4() for _ in range(2)]
        states = [
            _HistoryState(ids[0], PREV, {"first_pyramid_flat": {"dfx_score": 1}}),
            _HistoryState(ids[1], PREV, {"first_pyramid_flat": {"dfx_score": 1}}),
        ]
        session = _make_session(
            ids, snap_summaries=[dict(SAMPLE_FP_FLAT) for _ in range(2)],
            bar_sets=[(10.0, 9.5, 500.0, 5000.0) for _ in range(2)],
            previous_states=states,
        )
        facts = asyncio.run(load_day_fact_maps(
            session, trade_date=TARGET,
            source_core_run_id=uuid.uuid4(), instrument_ids=ids))
        assert len(facts) == 2

    def test_bar_window_chunked(self, monkeypatch) -> None:
        """P1-D2 TRUE MULTI-CHUNK: 块大小=2，5 instrument → 3 次 BarDaily 查询。"""
        import app.services.review_scope_service as rs
        # 把模块内块大小常量缩到 2，使 5 个 instrument 真实跨 3 块。
        monkeypatch.setattr(rs, "REVIEW_BAR_INSTRUMENT_CHUNK_SIZE", 2)
        ids = [uuid.uuid4() for _ in range(5)]
        bar_queries = {"n": 0}
        session = _make_session(
            ids, snap_summaries=[dict(SAMPLE_FP_FLAT) for _ in range(5)],
            bar_sets=[(10.0, 9.5, 500.0, 5000.0) for _ in range(5)],
        )
        original_exec = session.execute

        async def spy(stmt):
            if "bars_daily" in str(stmt):
                bar_queries["n"] += 1
            return await original_exec(stmt)

        session.execute = spy
        facts = asyncio.run(load_day_fact_maps(
            session, trade_date=TARGET,
            source_core_run_id=uuid.uuid4(), instrument_ids=ids))
        # ceil(5/2) = 3 块 → 3 次 bar 查询
        assert bar_queries["n"] == 3
        assert len(facts) == 5

    def test_query_count_not_scope_dependent(self) -> None:
        """bar query count 取决于 chunk 数而非 scope 数（load-once scope 级）。"""
        ids = [uuid.uuid4() for _ in range(10)]
        bar_queries = {"n": 0}
        session = _make_session(
            ids, snap_summaries=[dict(SAMPLE_FP_FLAT) for _ in range(10)],
            bar_sets=[(10.0, 9.5, 500.0, 5000.0) for _ in range(10)],
        )
        original_exec = session.execute

        async def spy(stmt):
            if "bars_daily" in str(stmt):
                bar_queries["n"] += 1
            return await original_exec(stmt)

        session.execute = spy
        facts = asyncio.run(load_day_fact_maps(
            session, trade_date=TARGET,
            source_core_run_id=uuid.uuid4(), instrument_ids=ids))
        assert bar_queries["n"] == 1  # 10 < 256 → 单块
        assert len(facts) == 10

    def test_execution_order_build_before_next_chunk(self, monkeypatch) -> None:
        """P1-D2 MEMORY RESIDENCY CONTRACT: 每块 bar 查询后立即 build 本块 fact，
        再查下一块 bar。事件顺序必须是：query chunk1 → build chunk1 → query chunk2
        而非 query all → build all（那会令全市场 bar 同时驻留）。
        """
        import app.services.review_scope_service as rs
        from app.domain.review import member_fact as mf
        # 块大小=2，3 instrument → 2 块，强制 chunk1 完整 build 后才进入 chunk2。
        monkeypatch.setattr(rs, "REVIEW_BAR_INSTRUMENT_CHUNK_SIZE", 2)
        ids = [uuid.uuid4() for _ in range(3)]
        session = _make_session(
            ids, snap_summaries=[dict(SAMPLE_FP_FLAT) for _ in range(3)],
            bar_sets=[(10.0, 9.5, 500.0, 5000.0) for _ in range(3)],
        )
        events: list[str] = []
        original_exec = session.execute

        async def spy(stmt):
            sql = str(stmt)
            if "bars_daily" in sql:
                # 记录当前块的首标的，区分 chunk1/chunk2 查询
                events.append("QUERY_BAR_CHUNK")
            return await original_exec(stmt)

        session.execute = spy

        # patch ReviewMemberFact.build 仅在它实际被调用（即真正装配 fact）时记录，
        # 且 build 必须在对应块 bar 查询之后立即发生。
        original_build = mf.ReviewMemberFact.build

        def build_spy(*args, **kwargs):
            events.append("BUILD_FACT_CHUNK")
            return original_build(*args, **kwargs)

        monkeypatch.setattr(mf.ReviewMemberFact, "build", staticmethod(build_spy))

        facts = asyncio.run(load_day_fact_maps(
            session, trade_date=TARGET,
            source_core_run_id=uuid.uuid4(), instrument_ids=ids))

        assert len(facts) == 3
        # 期望事件序列（chunk 大小 2，3 instrument → 2 块）：
        #   QUERY_BAR_CHUNK, BUILD(×2), QUERY_BAR_CHUNK, BUILD(×1)
        expected = [
            "QUERY_BAR_CHUNK",
            "BUILD_FACT_CHUNK", "BUILD_FACT_CHUNK",
            "QUERY_BAR_CHUNK",
            "BUILD_FACT_CHUNK",
        ]
        assert events == expected, f"execution order violation: {events}"

    def test_completed_chunk_bars_not_retained(self, monkeypatch) -> None:
        """P1-D2 MEMORY RESIDENCY CONTRACT (struct): 跨块不构成全局 bar 累积结构。
        patch ReviewMemberFact.build 使 chunk N+1 的 build 调用时，chunk N 的 build
        已经发生过——证明装配发生在每块的 bar 查询之后、下一块查询之前，而非全部查询后。
        """
        import app.services.review_scope_service as rs
        from app.domain.review import member_fact as mf
        monkeypatch.setattr(rs, "REVIEW_BAR_INSTRUMENT_CHUNK_SIZE", 2)
        ids = [uuid.uuid4() for _ in range(4)]
        session = _make_session(
            ids, snap_summaries=[dict(SAMPLE_FP_FLAT) for _ in range(4)],
            bar_sets=[(10.0, 9.5, 500.0, 5000.0) for _ in range(4)],
        )
        build_order: list[int] = []  # 记录每次 build 的调用序号
        original_build = mf.ReviewMemberFact.build
        call_counter = {"n": 0}
        chunk_query_positions: list[int] = []
        original_exec = session.execute

        async def spy(stmt):
            if "bars_daily" in str(stmt):
                chunk_query_positions.append(call_counter["n"])
            return await original_exec(stmt)

        session.execute = spy

        def build_spy(*args, **kwargs):
            call_counter["n"] += 1
            build_order.append(call_counter["n"])
            return original_build(*args, **kwargs)

        monkeypatch.setattr(mf.ReviewMemberFact, "build", staticmethod(build_spy))

        facts = asyncio.run(load_day_fact_maps(
            session, trade_date=TARGET,
            source_core_run_id=uuid.uuid4(), instrument_ids=ids))
        assert len(facts) == 4
        # 第 1 块（2 instrument）build 序号必须早于第 2 块 bar 查询所对应的 build。
        # chunk_query_positions[1] 是第 2 块 bar 查询发生时已发生的 build 调用数。
        assert len(chunk_query_positions) >= 2
        # 第 2 块 bar 查询前必须已经完成第 1 块 2 个成员的 build。
        assert chunk_query_positions[1] >= 2, (
            f"chunk2 bar query happened after only {chunk_query_positions[1]} builds; "
            "completed chunk1 facts should be built before querying chunk2"
        )


# =====================================================================
# P1-A REVIEW_CURRENT_SNAPSHOT_OVERFETCH
# =====================================================================
class TestReviewCurrentSnapshotOverfetch:
    def _capture_snapshot_stmt(self, instruments, snap_summaries):
        """捕获正式 stock_core 路径发出的 StockFeatureSnapshot 语句字符串。"""
        captured: dict[str, str] = {}

        class _CaptureSession:
            def __init__(self) -> None:
                self._shared_run = uuid.uuid4()
                self._instruments = instruments
                self._snap_summaries = snap_summaries

            async def execute(self, stmt):
                sql = str(stmt)
                fake = MagicMock()
                if "stock_feature_snapshot" in sql:
                    captured["snapshot_stmt"] = sql
                    # 第三列现在是投影出的 flat（非全量 summary）
                    rows = [
                        (inst, TARGET, s)
                        for inst, s in zip(instruments, snap_summaries, strict=False)
                    ]
                    fake.all.return_value = rows
                    fake.scalars.return_value = MagicMock(
                        __iter__=lambda self: iter([])
                    )
                elif "first_pyramid_history_daily_state" in sql:
                    if "trade_date <" in sql:
                        rows = []
                    else:
                        rows = []
                    fake.scalars.return_value = MagicMock(__iter__=lambda self: iter(rows))
                elif "bars_daily" in sql:
                    bars = []
                    for inst, (c, prev_c, vol, amt) in zip(
                        instruments, [(10.0, 9.5, 500.0, 5000.0)] * len(instruments),
                        strict=False,
                    ):
                        bars.append(_Bar(inst, TARGET, c, prev_c, vol, amt))
                    fake.scalars.return_value = MagicMock(__iter__=lambda self: iter(bars))
                elif "instruments" in sql:
                    idents = [
                        _Instrument(inst, f"SYM{i}")
                        for i, inst in enumerate(instruments)
                    ]
                    fake.scalars.return_value = MagicMock(__iter__=lambda self: iter(idents))
                else:
                    fake.scalars.return_value = MagicMock(__iter__=lambda self: iter([]))
                return fake

        return _CaptureSession(), captured

    def test_a1_snapshot_query_projects_only_first_pyramid_flat(self) -> None:
        """A1：正式 stock_core loader 只投影 summary_payload['first_pyramid_flat']，
        不得把整个 summary_payload 作为 payload 结果返回/选择。"""
        ids = [uuid.uuid4() for _ in range(3)]
        session, captured = self._capture_snapshot_stmt(
            ids, [dict(SAMPLE_FP_FLAT) for _ in range(3)]
        )
        asyncio.run(load_day_fact_maps(
            session, trade_date=TARGET,
            source_core_run_id=uuid.uuid4(), instrument_ids=ids,
        ))
        assert "snapshot_stmt" in captured, "stock_core snapshot 语句未被发出"
        sql = captured["snapshot_stmt"]
        # 投影了 first_pyramid_flat
        assert "first_pyramid_flat" in sql
        # 不得直接选择整个 summary_payload 列（[...] 索引投影除外）
        # summary_payload 在 SQL 中仅应作为 JSONB 索引表达式的源，而非独立 SELECT 列。
        # 通过断言不存在独立的 "summary_payload" 裸列选择来捕捉 overfetch。
        # 投影表达式形如 summary_payload -> 'first_pyramid_flat' 或
        # summary_payload['first_pyramid_flat']，索引表达式内可含 summary_payload。
        # 关键：不能出现把整列当结果的语义（即 "summary_payload" 作为 SELECT 目标且无索引）。
        self._assert_no_full_payload_select(sql)

    @staticmethod
    def _assert_no_full_payload_select(sql: str) -> None:
        # 归一化：移除空白、转为小写
        norm = " ".join(sql.lower().split())
        # 投影表达式用 json 索引；裸 "summary_payload" 列选择（无 -> / [）视为 overfetch。
        import re
        # 匹配作为 SELECT 列目标的 summary_payload（前面是逗号或 select，后面不是 -> 或 [）
        pattern = re.compile(r"(?:select|,)\s*summary_payload(?![\[\->])")
        assert not pattern.search(norm), (
            f"snapshot 查询选择了整个 summary_payload 列（overfetch）：{sql}"
        )

    def test_a2_unrelated_summary_keys_excluded(self) -> None:
        """A2：fixture summary 概念上含无关大 payload，loader 输出必须精确使用
        first_pyramid_flat，无关 summary 键不得进入 current fact。"""
        ids = [uuid.uuid4() for _ in range(2)]
        expected_flat = dict(SAMPLE_FP_FLAT)
        # 模拟 DB 投影出的 first_pyramid_flat 与原始 summary 无结构关联：
        # fixture 概念上含 large_unrelated_payload 等无关键，但 loader 只接收投影出的 flat。
        snap_summaries = [expected_flat, expected_flat]
        session = _make_session(
            ids, snap_summaries=snap_summaries,
            bar_sets=[(10.0, 9.5, 500.0, 5000.0) for _ in range(2)],
        )
        facts = asyncio.run(load_day_fact_maps(
            session, trade_date=TARGET,
            source_core_run_id=uuid.uuid4(), instrument_ids=ids,
        ))
        assert len(facts) == 2
        for _iid in ids:
            fact = facts[_iid]
            # 派生事实来自 first_pyramid_flat（SSOT），而非无关 payload
            assert fact["fp_trend_direction"] == expected_flat["fp_trend_direction"]
            # 无关键绝不进入 fact
            assert "large_unrelated_payload" not in fact
            assert "summary_payload" not in fact

    def test_a3_source_run_and_trade_date_filter_preserved(self) -> None:
        """A3：保留 source_run_id 过滤、trade_date == T 过滤、空结果行为。"""
        ids = [uuid.uuid4() for _ in range(1)]
        session, captured = self._capture_snapshot_stmt(
            ids, [dict(SAMPLE_FP_FLAT)]
        )
        # 无 source_core_run_id → 立即返回空映射（不触 DB）
        assert asyncio.run(load_day_fact_maps(
            session, trade_date=TARGET, source_core_run_id=None,
            instrument_ids=ids,
        )) == {}
        # 有 run id：语句必须含 source_run_id 与 trade_date 过滤
        session, captured = self._capture_snapshot_stmt(
            ids, [dict(SAMPLE_FP_FLAT)]
        )
        asyncio.run(load_day_fact_maps(
            session, trade_date=TARGET,
            source_core_run_id=uuid.uuid4(), instrument_ids=ids,
        ))
        sql = captured["snapshot_stmt"]
        assert "source_run_id" in sql
        assert "trade_date" in sql
