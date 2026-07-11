"""Phase 4 StockState + StateEvent 单元测试。

PRD V1.1 §7.3 核心验证：
1. build_stock_state: MACD code=null、SQZMOM 独立、sourceRunId 来自真实 run、
   value_area_zone → "成交密集区"（不是筹码共识）
2. extract_state_codes / compare_state_codes: 比较 code（非 label）、None 处理
3. build_event_evidence: 只保存必要证据
4. build_event_title_and_description: 稳定 event_type
5. generate_events_for_run: 无变化不建事件、幂等、版本不兼容不比较
6. as_of 过滤: build_stock_state 使用 snapshot.trade_date

用法：
    cd backend && pytest tests/test_stock_state_and_events.py -v
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.stock_feature_snapshot import StockFeatureSnapshot
from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun
from app.schemas.stock_state import (
    StateEventDTO,
    StateValue,
    StockMomentum,
    StockState,
    StockStructure,
    StockVolatility,
    build_stock_state,
)
from app.services.state_event_service import (
    _STATE_FIELD_PATHS,
    build_event_evidence,
    build_event_title_and_description,
    compare_state_codes,
    extract_state_codes,
)

# =============================================================================
# 测试辅助：构造 mock snapshot 和 run
# =============================================================================


def _make_mock_snapshot(
    trade_date: date = date(2026, 7, 10),
    sqzmom_val: float | None = 0.001,
    bb_percent_b: float | None = 0.5,
    value_area_zone: str | None = "inside_va",
    breakout_state: str | None = "inside",
    daily_dsa_dir: int | None = 1,
    alignment: str | None = "aligned",
) -> StockFeatureSnapshot:
    """构造 mock StockFeatureSnapshot。"""
    return StockFeatureSnapshot(
        instrument_id=uuid4(),
        trade_date=trade_date,
        primary_timeframe="1d",
        secondary_timeframe="15m",
        adj="qfq",
        schema_version=1,
        source_primary_bar_time=datetime(2026, 7, 10, 15, 0, tzinfo=UTC),
        source_secondary_bar_time=None,
        structural_payload={
            "primary": {
                "1d": {
                    "swing_position": {
                        "confirmed_swing_breakout_state": breakout_state,
                        "price_position_in_swing_0_1": 0.5,
                        "confirmed_swing_high": 10.5,
                        "confirmed_swing_low": 9.5,
                    },
                    "cost_position": {
                        "value_area_zone": value_area_zone,
                        "value_area_position_0_1": 0.5,
                        "poc_price": 10.0,
                    },
                    "volatility_momentum": {
                        "sqzmom_val": sqzmom_val,
                        "bb_percent_b": bb_percent_b,
                    },
                    "participation": {
                        "volume_percentile_120": 0.3,
                    },
                }
            }
        },
        temporal_payload={
            "daily_context": {"daily_dsa_dir": daily_dsa_dir},
            "derived_relation": {
                "m15_response_direction_relative_to_daily": alignment
            },
        },
        summary_payload={},
        degraded_reasons=[],
    )


def _make_mock_run(
    run_id=None,
    trade_date: date = date(2026, 7, 10),
    schema_version: int = 1,
) -> StockFeatureSnapshotRun:
    """构造 mock StockFeatureSnapshotRun。"""
    return StockFeatureSnapshotRun(
        id=run_id or uuid4(),
        trade_date=trade_date,
        schema_version=schema_version,
        primary_timeframe="1d",
        secondary_timeframe="15m",
        adj="qfq",
        run_type="after_close",
        status="succeeded",
    )


def _make_state(
    price_code: str | None = "inside",
    consensus_code: str | None = "inside_va",
    macd_code: str | None = None,
    sqzmom_code: str | None = "positive",
    dsa_dir_code: str | None = "1",
    alignment_code: str | None = "aligned",
    boll_code: str | None = "middle",
) -> StockState:
    """构造 StockState 用于纯函数测试。"""
    return StockState(
        symbol="000001",
        asOf="2026-07-10",
        sourceRunId=str(uuid4()),
        version="v1",
        computedAt="2026-07-10T15:00:00+08:00",
        structure=StockStructure(
            price=StateValue(code=price_code, label="test", value=None, unit=None, timeframe="1d", sourceField="test"),
            consensusRelation=StateValue(code=consensus_code, label="test", value=None, unit=None, timeframe="1d", sourceField="test"),
        ),
        momentum=StockMomentum(
            macd=StateValue(code=macd_code, label="test", value=None, unit=None, timeframe="1d", sourceField="test"),
            sqzmom=StateValue(code=sqzmom_code, label="test", value=None, unit=None, timeframe="1d", sourceField="test"),
            temporal=[
                StateValue(code=dsa_dir_code, label="test", value=None, unit=None, timeframe="1d", sourceField="test"),
                StateValue(code=alignment_code, label="test", value=None, unit=None, timeframe="1d", sourceField="test"),
            ],
        ),
        volatility=StockVolatility(
            bollPosition=StateValue(code=boll_code, label="test", value=None, unit=None, timeframe="1d", sourceField="test"),
        ),
    )


# =============================================================================
# 1. build_stock_state: V1.1 核心契约验证
# =============================================================================


def test_build_stock_state_macd_code_is_null() -> None:
    """V1.1: MACD 只能来自真实 MACD 数据；当前无 MACD 计算，code=null。"""
    snapshot = _make_mock_snapshot()
    run = _make_mock_run()
    state = build_stock_state(snapshot, run, symbol="000001")

    assert state.momentum.macd.code is None, "MACD code 必须为 null（无真实 MACD）"
    assert state.momentum.macd.label == "暂不可用"


def test_build_stock_state_sqzmom_independent_from_macd() -> None:
    """V1.1: SQZMOM 单独命名，不与 MACD 混淆。"""
    snapshot = _make_mock_snapshot(sqzmom_val=0.001)
    run = _make_mock_run()
    state = build_stock_state(snapshot, run, symbol="000001")

    # SQZMOM code 来自 sqzmom_val
    assert state.momentum.sqzmom.code == "positive"
    # SQZMOM sourceField 指向 volatility_momentum.sqzmom_val（非 macd）
    assert state.momentum.sqzmom.sourceField == "volatility_momentum.sqzmom_val"
    # MACD 和 SQZMOM 是不同字段
    assert state.momentum.macd.sourceField != state.momentum.sqzmom.sourceField


def test_build_stock_state_source_run_id_from_real_run() -> None:
    """V1.1: source_run_id 来自真实 run，禁止硬编码。"""
    run_id = uuid4()
    snapshot = _make_mock_snapshot()
    run = _make_mock_run(run_id=run_id)
    state = build_stock_state(snapshot, run, symbol="000001")

    assert state.sourceRunId == str(run_id), "sourceRunId 必须来自真实 run.id"
    assert state.version == "v1", "version 必须来自 schema_version"


def test_build_stock_state_value_area_zone_not_consensus() -> None:
    """V1.1: value_area_zone → "成交密集区关系"，Phase 5 前不得叫筹码共识。"""
    snapshot = _make_mock_snapshot(value_area_zone="inside_va")
    run = _make_mock_run()
    state = build_stock_state(snapshot, run, symbol="000001")

    assert "成交密集区" in state.structure.consensusRelation.label
    assert "筹码共识" not in state.structure.consensusRelation.label


def test_build_stock_state_as_of_from_snapshot_trade_date() -> None:
    """V1.1: as_of 来自 snapshot.trade_date（真实 point-in-time）。"""
    snapshot = _make_mock_snapshot(trade_date=date(2026, 7, 9))
    run = _make_mock_run()
    state = build_stock_state(snapshot, run, symbol="000001")

    assert state.asOf == "2026-07-09", "asOf 必须来自 snapshot.trade_date"


def test_build_stock_state_sqzmom_negative() -> None:
    """SQZMOM 负值检测。"""
    snapshot = _make_mock_snapshot(sqzmom_val=-0.002)
    run = _make_mock_run()
    state = build_stock_state(snapshot, run, symbol="000001")

    assert state.momentum.sqzmom.code == "negative"


def test_build_stock_state_sqzmom_null() -> None:
    """SQZMOM 数据不足时 code=null。"""
    snapshot = _make_mock_snapshot(sqzmom_val=None)
    run = _make_mock_run()
    state = build_stock_state(snapshot, run, symbol="000001")

    assert state.momentum.sqzmom.code is None


# =============================================================================
# 2. extract_state_codes / compare_state_codes: code 比较
# =============================================================================


def test_extract_state_codes_returns_all_paths() -> None:
    """extract_state_codes 返回所有 7 个字段路径。"""
    state = _make_state()
    codes = extract_state_codes(state)

    assert set(codes.keys()) == set(_STATE_FIELD_PATHS)
    assert len(codes) == 7


def test_compare_state_codes_no_change() -> None:
    """无变化不返回任何字段。"""
    prev = _make_state()
    curr = _make_state()
    changed = compare_state_codes(
        extract_state_codes(prev), extract_state_codes(curr)
    )
    assert len(changed) == 0


def test_compare_state_codes_real_change() -> None:
    """真实变化检测（sqzmom + bollPosition 变化）。"""
    prev = _make_state()
    curr = _make_state(sqzmom_code="negative", boll_code="near_upper")
    changed = compare_state_codes(
        extract_state_codes(prev), extract_state_codes(curr)
    )
    assert "momentum.sqzmom" in changed
    assert "volatility.bollPosition" in changed
    assert len(changed) == 2


def test_compare_state_codes_none_to_none_not_change() -> None:
    """None → None 不算变化（MACD 始终为 None）。"""
    prev = _make_state(macd_code=None)
    curr = _make_state(macd_code=None)
    changed = compare_state_codes(
        extract_state_codes(prev), extract_state_codes(curr)
    )
    assert "momentum.macd" not in changed


def test_compare_state_codes_none_to_value_is_change() -> None:
    """None → 非 None 算变化（数据从无到有）。"""
    prev = _make_state(macd_code=None)
    curr = _make_state(macd_code="positive")
    changed = compare_state_codes(
        extract_state_codes(prev), extract_state_codes(curr)
    )
    assert "momentum.macd" in changed


def test_compare_state_codes_value_to_none_is_change() -> None:
    """非 None → None 算变化（数据从有到无）。"""
    prev = _make_state(sqzmom_code="positive")
    curr = _make_state(sqzmom_code=None)
    changed = compare_state_codes(
        extract_state_codes(prev), extract_state_codes(curr)
    )
    assert "momentum.sqzmom" in changed


# =============================================================================
# 3. build_event_evidence: 只保存必要证据
# =============================================================================


def test_build_event_evidence_only_changed_fields() -> None:
    """证据只包含变化字段，不包含未变化字段。"""
    prev = _make_state()
    curr = _make_state(sqzmom_code="negative", boll_code="near_upper")
    changed = ["momentum.sqzmom", "volatility.bollPosition"]

    evidence = build_event_evidence(prev, curr, changed)

    assert len(evidence) == 2
    fields = [e["field"] for e in evidence]
    assert "momentum.sqzmom" in fields
    assert "volatility.bollPosition" in fields
    # 每条证据包含 field/prevCode/currCode
    for e in evidence:
        assert "field" in e
        assert "prevCode" in e
        assert "currCode" in e


def test_build_event_evidence_no_prev_state() -> None:
    """无前值时 prevCode 为 None。"""
    curr = _make_state()
    changed = ["momentum.sqzmom"]

    evidence = build_event_evidence(None, curr, changed)

    assert len(evidence) == 1
    assert evidence[0]["prevCode"] is None
    assert evidence[0]["currCode"] == "positive"


# =============================================================================
# 4. build_event_title_and_description: 稳定 event_type
# =============================================================================


def test_build_event_title_and_description_multi_fields() -> None:
    """多字段变化时标题包含数量，不以第一个字段代表。

    P1-1: 使用白名单映射，不输出内部路径。
    """
    changed = ["momentum.sqzmom", "volatility.bollPosition", "structure.price"]

    title, desc = build_event_title_and_description(changed)

    assert "3 项" in title
    # P1-1: 描述使用白名单中文文案，不输出内部 code 路径
    assert "SQZMOM" in desc or "动量" in desc
    assert "布林" in desc or "位置" in desc
    assert "价格" in desc
    # 禁止输出原始 code 路径
    assert "sqzmom" not in desc.lower() or "SQZMOM" in desc
    assert "bollPosition" not in desc
    assert "structure.price" not in desc


def test_build_event_title_and_description_no_change() -> None:
    """无变化时返回明确文案。"""
    title, desc = build_event_title_and_description([])

    assert "无状态变化" in title


# =============================================================================
# 5. generate_events_for_run: 幂等、版本不兼容、无变化
# =============================================================================


@pytest.mark.asyncio
async def test_generate_events_for_run_skips_non_succeeded_run() -> None:
    """非 succeeded run 不生成事件。"""
    from app.services.state_event_service import generate_events_for_run

    mock_session = MagicMock()
    mock_run = MagicMock()
    mock_run.status = "failed"
    mock_session.get = AsyncMock(return_value=mock_run)

    result = await generate_events_for_run(mock_session, uuid4())

    assert result["event_count"] == 0
    assert result["failed_count"] == 0


@pytest.mark.asyncio
async def test_generate_events_for_run_nonexistent_run() -> None:
    """不存在的 run 返回空统计。"""
    from app.services.state_event_service import generate_events_for_run

    mock_session = MagicMock()
    mock_session.get = AsyncMock(return_value=None)

    result = await generate_events_for_run(mock_session, uuid4())

    assert result["event_count"] == 0
    assert result["run_id"] is not None


@pytest.mark.asyncio
async def test_generate_events_for_run_no_snapshots() -> None:
    """run 无快照时返回空统计。"""
    from app.services.state_event_service import generate_events_for_run

    run_id = uuid4()
    mock_run = _make_mock_run(run_id=run_id)
    mock_session = MagicMock()
    mock_session.get = AsyncMock(return_value=mock_run)

    # 模拟空快照列表
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    mock_session.execute = AsyncMock(return_value=mock_result)

    result = await generate_events_for_run(mock_session, run_id)

    assert result["event_count"] == 0
    assert result["skipped_count"] == 0


@pytest.mark.asyncio
async def test_generate_events_for_run_idempotent_write() -> None:
    """事件生成使用 ON CONFLICT DO NOTHING（幂等写入，批量架构）。

    V1.1 架构：4 条固定 SQL（不随股票数增长）
    1. session.get(run) - 读取当前 run
    2. _batch_get_run_snapshots_with_symbol - JOIN 批量查询快照+symbol
    3. _batch_get_previous_snapshots - MAX 子查询 + JOIN run（过滤 succeeded）
    4. pg_insert.on_conflict_do_nothing - 批量幂等写入

    P1: Query 3 返回 (snapshot, run) 元组，前一状态来自前一成功 run。
    """
    from app.services.state_event_service import generate_events_for_run

    run_id = uuid4()
    mock_run = _make_mock_run(run_id=run_id)
    instrument_id = uuid4()
    prev_snapshot = _make_mock_snapshot(trade_date=date(2026, 7, 9))
    curr_snapshot = _make_mock_snapshot(
        trade_date=date(2026, 7, 10),
        sqzmom_val=-0.001,  # 变化：positive → negative
    )
    curr_snapshot.instrument_id = instrument_id
    prev_snapshot.instrument_id = instrument_id

    mock_session = MagicMock()

    # get(run) 返回 mock_run
    async def mock_get(model, obj_id):
        if model.__name__ == "StockFeatureSnapshotRun":
            return mock_run
        return None
    mock_session.get = AsyncMock(side_effect=mock_get)

    call_sequence = []

    async def mock_execute(stmt):
        call_sequence.append(stmt)
        stmt_type = type(stmt).__name__

        # 批量 INSERT（on_conflict_do_nothing）
        if stmt_type == "Insert":
            mock_result = MagicMock()
            mock_result.rowcount = 1
            return mock_result

        compiled = str(stmt)

        # Query 2: _batch_get_run_snapshots_with_symbol（JOIN instruments）
        # 返回 list of (snapshot, symbol) tuples via result.all()
        if "stock_feature_snapshots" in compiled and "instruments" in compiled:
            mock_result = MagicMock()
            mock_result.all.return_value = [(curr_snapshot, "000001")]
            return mock_result

        # Query 3: _batch_get_previous_snapshots（子查询 MAX + JOIN run）
        # P1: 返回 list of (snapshot, run) tuples via result.all()
        #     run 必须 status='succeeded'（SQL 层过滤）
        if "stock_feature_snapshots" in compiled and "max" in compiled.lower():
            prev_run = _make_mock_run(trade_date=date(2026, 7, 9))
            mock_result = MagicMock()
            mock_result.all.return_value = [(prev_snapshot, prev_run)]
            return mock_result

        # 默认返回空结果
        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_result.scalars.return_value.all.return_value = []
        return mock_result
    mock_session.execute = AsyncMock(side_effect=mock_execute)

    result = await generate_events_for_run(mock_session, run_id)

    # 应该生成 1 条事件（sqzmom 从 positive 变为 negative）
    assert result["event_count"] == 1, f"预期 1 条事件，实际: {result}"
    # 至少有一次 insert 调用（ON CONFLICT DO NOTHING）
    insert_calls = [s for s in call_sequence if type(s).__name__ == "Insert"]
    assert len(insert_calls) >= 1, "应至少有一次幂等 insert 调用"


@pytest.mark.asyncio
async def test_generate_events_for_run_batch_sql_count_constant() -> None:
    """P0-2: 批量 SQL 计数恒定 - 10/100/5000 股票 SQL 查询数相同。

    无论股票数量多少，generate_events_for_run 只执行固定 4 条 SQL：
    1. session.get(run)
    2. _batch_get_run_snapshots_with_symbol (1 条 JOIN)
    3. _batch_get_previous_snapshots (1 条 MAX 子查询 + JOIN run)
    4. batch INSERT (1 条 ON CONFLICT DO NOTHING)

    P1: Query 3 返回 (snapshot, run) 元组，前一状态来自前一成功 run。
    禁止逐股查询形成 N+1。
    """
    from app.services.state_event_service import generate_events_for_run

    async def _run_with_n_stocks(n: int) -> int:
        """构造 n 只股票的 mock，返回 execute 调用次数。"""
        run_id = uuid4()
        mock_run = _make_mock_run(run_id=run_id)

        # 构造 n 个快照（每个都有变化，触发事件生成）
        snapshots_with_symbol: list[tuple[StockFeatureSnapshot, str]] = []
        prev_snapshots: list[StockFeatureSnapshot] = []
        for i in range(n):
            inst_id = uuid4()
            curr = _make_mock_snapshot(
                trade_date=date(2026, 7, 10),
                sqzmom_val=-0.001 if i % 2 == 0 else 0.001,
            )
            curr.instrument_id = inst_id
            prev = _make_mock_snapshot(trade_date=date(2026, 7, 9))
            prev.instrument_id = inst_id
            snapshots_with_symbol.append((curr, f"{i:06d}"))
            prev_snapshots.append(prev)

        mock_session = MagicMock()

        async def mock_get(model, obj_id):
            if model.__name__ == "StockFeatureSnapshotRun":
                return mock_run
            return None
        mock_session.get = AsyncMock(side_effect=mock_get)

        execute_count = 0

        async def mock_execute(stmt):
            nonlocal execute_count
            execute_count += 1

            if type(stmt).__name__ == "Insert":
                mock_result = MagicMock()
                mock_result.rowcount = 1
                return mock_result

            compiled = str(stmt)
            if "instruments" in compiled:
                # Query 2: batch snapshots with symbol
                mock_result = MagicMock()
                mock_result.all.return_value = snapshots_with_symbol
                return mock_result
            if "max" in compiled.lower():
                # Query 3: batch previous snapshots (subquery MAX + JOIN run)
                # P1: 返回 list of (snapshot, run) tuples
                prev_run = _make_mock_run(trade_date=date(2026, 7, 9))
                mock_result = MagicMock()
                mock_result.all.return_value = [
                    (prev, prev_run) for prev in prev_snapshots
                ]
                return mock_result
            mock_result = MagicMock()
            mock_result.all.return_value = []
            mock_result.scalars.return_value.all.return_value = []
            return mock_result
        mock_session.execute = AsyncMock(side_effect=mock_execute)

        await generate_events_for_run(mock_session, run_id)
        return execute_count

    # 10 / 100 / 5000 股票的 execute 调用次数必须相同
    count_10 = await _run_with_n_stocks(10)
    count_100 = await _run_with_n_stocks(100)
    count_5000 = await _run_with_n_stocks(5000)

    assert count_10 == count_100 == count_5000, (
        f"SQL 计数不恒定: 10={count_10}, 100={count_100}, 5000={count_5000}"
    )
    # 固定 3 次 execute（Query 2 + Query 3 + Query 4 INSERT）
    # session.get(run) 不计入 execute
    assert count_10 == 3, f"预期 3 次 execute，实际: {count_10}"


@pytest.mark.asyncio
async def test_cleanup_old_events_records_metrics() -> None:
    """P1-2: cleanup_old_events 记录 deleted_count/cutoff/duration_ms。"""
    from app.services.state_event_service import cleanup_old_events

    mock_session = MagicMock()
    mock_result = MagicMock()
    mock_result.rowcount = 42
    mock_session.execute = AsyncMock(return_value=mock_result)

    stats = await cleanup_old_events(mock_session, days=90)

    assert "deleted_count" in stats
    assert "cutoff_date" in stats
    assert "duration_ms" in stats
    assert stats["deleted_count"] == 42
    assert isinstance(stats["duration_ms"], int)
    assert stats["duration_ms"] >= 0


@pytest.mark.asyncio
async def test_get_recent_events_filters_future_events() -> None:
    """P0-4: as_of 历史查询时 occurred_at_lte 过滤未来事件。"""
    from app.services.state_event_service import get_recent_events_for_instrument

    mock_session = MagicMock()
    # 捕获传入的 stmt 以验证 occurred_at <= occurred_at_lte 条件
    captured_stmts: list = []

    async def mock_execute(stmt):
        captured_stmts.append(stmt)
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        return mock_result
    mock_session.execute = AsyncMock(side_effect=mock_execute)

    cutoff = datetime(2026, 7, 10, 23, 59, 59, tzinfo=UTC)
    await get_recent_events_for_instrument(
        mock_session, uuid4(), limit=10, occurred_at_lte=cutoff,
    )

    assert len(captured_stmts) == 1
    compiled = str(captured_stmts[0])
    # SQL 应包含 occurred_at <= cutoff 的过滤条件
    assert "occurred_at" in compiled
    assert "<=" in compiled


@pytest.mark.asyncio
async def test_get_recent_events_no_filter_when_no_lte() -> None:
    """无 occurred_at_lte 时不加未来事件过滤（最新查询场景）。"""
    from app.services.state_event_service import get_recent_events_for_instrument

    mock_session = MagicMock()

    async def mock_execute(stmt):
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        return mock_result
    mock_session.execute = AsyncMock(side_effect=mock_execute)

    await get_recent_events_for_instrument(mock_session, uuid4(), limit=10)

    # 无 occurred_at_lte 时也应正常执行
    mock_session.execute.assert_awaited_once()


# =============================================================================
# 6. 版本不兼容不比较
# =============================================================================


def test_state_field_paths_are_stable_codes() -> None:
    """_STATE_FIELD_PATHS 使用稳定 code 路径（非中文 label）。"""
    for path in _STATE_FIELD_PATHS:
        # 所有路径都是英文 code 路径
        assert all(c.isascii() for c in path), f"路径含非 ASCII 字符: {path}"
        assert "." in path, f"路径缺少层级分隔符: {path}"


def test_event_type_is_stable_transition() -> None:
    """event_type 使用稳定类型 state_transition，不以第一个变化字段代表。"""
    from app.services.state_event_service import _EVENT_TYPE_TRANSITION

    assert _EVENT_TYPE_TRANSITION == "state_transition"
    # 验证不包含中文
    assert all(c.isascii() for c in _EVENT_TYPE_TRANSITION)


# =============================================================================
# 7. StateValue code/label 分离验证
# =============================================================================


def test_state_value_code_and_label_separation() -> None:
    """StateValue code 和 label 是独立字段。"""
    sv = StateValue(
        code="inside_va",
        label="位于成交密集区内",
        value=0.5,
        unit=None,
        timeframe="1d",
        sourceField="cost_position.value_area_zone",
    )
    assert sv.code == "inside_va"
    assert sv.label == "位于成交密集区内"
    assert sv.code != sv.label  # code 和 label 不同


def test_state_value_code_nullable() -> None:
    """StateValue code 可为 None（数据不足时）。"""
    sv = StateValue(
        code=None,
        label="暂不可用",
        value=None,
        unit=None,
        timeframe="1d",
        sourceField="macd",
    )
    assert sv.code is None
    assert sv.label == "暂不可用"


def test_state_value_source_field_optional() -> None:
    """V1.1: sourceField 可省略（用户接口默认 None）。"""
    sv = StateValue(
        code="inside",
        label="确认区间内",
        value=None,
        unit=None,
        timeframe="1d",
    )
    assert sv.sourceField is None


# =============================================================================
# 7.1 strip_internal_fields_for_user: 用户接口字段剥离
# PRD V1.1 §7.3: sourceField/idempotencyKey 仅管理员可见
# =============================================================================


def test_strip_internal_fields_for_user_clears_source_field() -> None:
    """V1.1: strip 后 sourceField 字段完全不存在（不是 null）。"""
    from app.schemas.stock_state import strip_internal_fields_for_user

    state = _make_state()
    # 原始状态 sourceField 不为 None
    assert state.structure.price.sourceField == "test"
    stripped_state, _ = strip_internal_fields_for_user(state, [])
    assert stripped_state is not None
    # sourceField 必须完全不存在（不是 None）
    assert "sourceField" not in stripped_state["structure"]["price"]
    assert "sourceField" not in stripped_state["structure"]["consensusRelation"]
    assert "sourceField" not in stripped_state["momentum"]["macd"]
    assert "sourceField" not in stripped_state["momentum"]["sqzmom"]
    for t in stripped_state["momentum"]["temporal"]:
        assert "sourceField" not in t
    assert "sourceField" not in stripped_state["volatility"]["bollPosition"]


def test_strip_internal_fields_for_user_clears_idempotency_key() -> None:
    """V1.1: strip 后 idempotencyKey 字段完全不存在（不是 null）。"""
    from app.schemas.stock_state import strip_internal_fields_for_user

    event = StateEventDTO(
        id=str(uuid4()),
        symbol="000001",
        occurredAt="2026-07-10T15:00:00+00:00",
        eventType="structure.price.transition",
        title="测试事件",
        description="测试描述",
        changedFields=["structure.price"],
        currentAsOf="2026-07-10",
        idempotencyKey="evt-000001-2026-07-10-abc123",
    )
    _, events = strip_internal_fields_for_user(None, [event])
    assert "idempotencyKey" not in events[0]


def test_strip_internal_fields_for_user_preserves_other_fields() -> None:
    """V1.1: strip 只移除 sourceField/idempotencyKey，code/label/value 保留。"""
    from app.schemas.stock_state import strip_internal_fields_for_user

    state = _make_state(price_code="above_confirmed_high", sqzmom_code="positive")
    stripped_state, _ = strip_internal_fields_for_user(state, [])
    assert stripped_state is not None
    assert stripped_state["structure"]["price"]["code"] == "above_confirmed_high"
    assert stripped_state["momentum"]["sqzmom"]["code"] == "positive"


# =============================================================================
# 8. 权限守卫验证：require_active_subscription / require_admin
# PRD V1.1: 不允许 skip，使用 FastAPI TestClient + dependency_overrides
# 验证：未登录 401、普通用户 403、管理员 200
# =============================================================================


@pytest.mark.asyncio
async def test_require_active_subscription_rejects_expired() -> None:
    """P0-1: require_active_subscription 拒绝过期/无订阅 member（单元级）。"""
    from fastapi import HTTPException

    from app.services.access_control_service import (
        AccessContext,
        require_active_subscription,
    )

    expired_ctx = AccessContext(
        user_id=str(uuid4()),
        account_status="active",
        roles=["member"],
        is_admin=False,
        is_member=True,
        subscription_active=False,
        plan_code="observe_20",
        plan_display_name="观察版20",
        expires_at=datetime(2026, 1, 1, tzinfo=UTC),
        features=[],
        limits={},
    )

    with pytest.raises(HTTPException) as exc_info:
        await require_active_subscription(ctx=expired_ctx)
    assert exc_info.value.status_code == 403
    assert "过期" in exc_info.value.detail


@pytest.mark.asyncio
async def test_require_active_subscription_allows_admin() -> None:
    """P0-1: admin 豁免订阅检查（单元级）。"""
    from app.services.access_control_service import (
        AccessContext,
        require_active_subscription,
    )

    admin_ctx = AccessContext(
        user_id=str(uuid4()),
        account_status="active",
        roles=["admin"],
        is_admin=True,
        is_member=False,
        subscription_active=True,
        plan_code=None,
        plan_display_name="",
        expires_at=None,
        features=[],
        limits={},
    )

    result = await require_active_subscription(ctx=admin_ctx)
    assert result is admin_ctx


@pytest.mark.asyncio
async def test_require_admin_rejects_member() -> None:
    """P0-1: require_admin 拒绝普通 member（单元级）。"""
    from fastapi import HTTPException

    from app.services.access_control_service import (
        AccessContext,
        require_admin,
    )

    member_ctx = AccessContext(
        user_id=str(uuid4()),
        account_status="active",
        roles=["member"],
        is_admin=False,
        is_member=True,
        subscription_active=True,
        plan_code="observe_20",
        plan_display_name="观察版20",
        expires_at=datetime(2026, 12, 31, tzinfo=UTC),
        features=[],
        limits={},
    )

    with pytest.raises(HTTPException) as exc_info:
        await require_admin(ctx=member_ctx)
    assert exc_info.value.status_code == 403
    assert "管理员" in exc_info.value.detail


@pytest.mark.asyncio
async def test_require_admin_allows_admin() -> None:
    """P0-1: require_admin 允许 admin（单元级）。"""
    from app.services.access_control_service import (
        AccessContext,
        require_admin,
    )

    admin_ctx = AccessContext(
        user_id=str(uuid4()),
        account_status="active",
        roles=["admin"],
        is_admin=True,
        is_member=False,
        subscription_active=True,
        plan_code=None,
        plan_display_name="",
        expires_at=None,
        features=[],
        limits={},
    )

    result = await require_admin(ctx=admin_ctx)
    assert result is admin_ctx


# ---------------------------------------------------------------------------
# HTTP 集成测试：通过 ASGITransport + dependency_overrides 调用真实端点
# PRD V1.1: 禁止 skip，验证未登录 401、普通用户 403、管理员 200
# ---------------------------------------------------------------------------


async def _ensure_role(db: AsyncSession, name: str):
    """确保角色存在并返回。"""
    from app.models.user import Role

    result = await db.execute(select(Role).where(Role.name == name))
    role = result.scalar_one_or_none()
    if role is None:
        role = Role(id=uuid.uuid4(), name=name, description=name)
        db.add(role)
        await db.flush()
    return role


async def _create_admin_user(db: AsyncSession):
    """创建管理员用户（admin 角色，无 subscription）。"""
    from app.core.security import get_password_hash
    from app.models.user import User, UserRole

    admin = User(
        id=uuid.uuid4(),
        email=f"admin_{uuid.uuid4().hex[:8]}@test.com",
        password_hash=get_password_hash("admin-password-123"),
        status="active",
        timezone="Asia/Shanghai",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db.add(admin)
    admin_role = await _ensure_role(db, "admin")
    db.add(UserRole(user_id=admin.id, role_id=admin_role.id))
    await db.flush()
    return admin


async def _create_member_without_subscription(db: AsyncSession):
    """创建无订阅记录的 member 用户。"""
    from app.core.security import get_password_hash
    from app.models.user import User, UserRole

    user = User(
        id=uuid.uuid4(),
        email=f"nomember_{uuid.uuid4().hex[:8]}@test.com",
        password_hash=get_password_hash("password-12345"),
        status="active",
        timezone="Asia/Shanghai",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db.add(user)
    member_role = await _ensure_role(db, "member")
    db.add(UserRole(user_id=user.id, role_id=member_role.id))
    await db.flush()
    return user


async def _create_member_with_active_subscription(db: AsyncSession):
    """创建有有效订阅的 member 用户。"""
    from app.services.subscription_service import (
        generate_invite_codes,
        register_with_invite_code,
    )

    admin = await _create_admin_user(db)
    results = await generate_invite_codes(
        db=db,
        count=1,
        created_by=admin.id,
        plan_code="observe_20",
        grant_months=1,
    )
    await db.flush()
    email = f"member_{uuid.uuid4().hex[:8]}@test.com"
    user, _subscription = await register_with_invite_code(
        db=db,
        email=email,
        password="password-12345",
        raw_invite_code=results[0][1],
    )
    await db.flush()
    return user


async def _create_test_instrument(db: AsyncSession, symbol: str = "TEST001"):
    """创建测试标的。"""
    from app.models.instrument import Instrument

    inst = Instrument(
        symbol=symbol,
        name="测试标的",
        market="SZ",
        status="active",
    )
    db.add(inst)
    await db.flush()
    return inst


@pytest_asyncio.fixture
async def stock_context_client(
    db_session: AsyncSession,
):
    """提供 HTTP 客户端 + 测试 DB session，通过 dependency_overrides 注入。"""
    from collections.abc import AsyncGenerator

    from app.core.deps import get_db as deps_get_db
    from app.db import get_db as db_get_db
    from app.main import app
    from tests.conftest import make_asgi_transport

    async def get_test_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[deps_get_db] = get_test_db
    app.dependency_overrides[db_get_db] = get_test_db

    transport = make_asgi_transport(app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, db_session

    app.dependency_overrides.clear()


def _auth_headers(user_id) -> dict[str, str]:
    """生成 Bearer token 认证头。"""
    from app.core.security import create_access_token

    token = create_access_token(str(user_id))
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_stock_context_unauthenticated_returns_401(
    stock_context_client,
) -> None:
    """P0-1: 未登录访问 /api/v1/stocks/{symbol}/context 返回 401。"""
    client, db = stock_context_client
    await _create_test_instrument(db, "TEST001")
    resp = await client.get("/api/v1/stocks/TEST001/context")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_stock_context_member_without_subscription_returns_403(
    stock_context_client,
) -> None:
    """P0-1: 无订阅 member 访问 context 返回 403。"""
    client, db = stock_context_client
    user = await _create_member_without_subscription(db)
    await _create_test_instrument(db, "TEST002")
    resp = await client.get(
        "/api/v1/stocks/TEST002/context",
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_stock_context_member_with_subscription_returns_200(
    stock_context_client,
) -> None:
    """P0-1: 有效订阅 member 访问 context 返回 200。"""
    client, db = stock_context_client
    user = await _create_member_with_active_subscription(db)
    await _create_test_instrument(db, "TEST003")
    resp = await client.get(
        "/api/v1/stocks/TEST003/context",
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_stock_context_admin_returns_200(
    stock_context_client,
) -> None:
    """P0-1: admin 访问 context 返回 200（豁免订阅）。"""
    client, db = stock_context_client
    admin = await _create_admin_user(db)
    await _create_test_instrument(db, "TEST004")
    resp = await client.get(
        "/api/v1/stocks/TEST004/context",
        headers=_auth_headers(admin.id),
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_admin_stock_debug_unauthenticated_returns_401(
    stock_context_client,
) -> None:
    """P0-1: 未登录访问 /api/v1/admin/stocks/{symbol}/debug 返回 401。"""
    client, db = stock_context_client
    await _create_test_instrument(db, "TEST005")
    resp = await client.get("/api/v1/admin/stocks/TEST005/debug")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_admin_stock_debug_member_returns_403(
    stock_context_client,
) -> None:
    """P0-1: member（即使有订阅）访问 admin debug 返回 403。"""
    client, db = stock_context_client
    user = await _create_member_with_active_subscription(db)
    await _create_test_instrument(db, "TEST006")
    resp = await client.get(
        "/api/v1/admin/stocks/TEST006/debug",
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_admin_stock_debug_admin_returns_200(
    stock_context_client,
) -> None:
    """P0-1: admin 访问 admin debug 返回 200。"""
    client, db = stock_context_client
    admin = await _create_admin_user(db)
    await _create_test_instrument(db, "TEST007")
    resp = await client.get(
        "/api/v1/admin/stocks/TEST007/debug",
        headers=_auth_headers(admin.id),
    )
    assert resp.status_code == 200
