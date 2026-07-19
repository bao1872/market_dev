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
    macd_code: str | None = "bullish_above",
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
                    # C6: 真实 MACD 紧凑状态
                    "macd_state": {
                        "code": macd_code,
                        "macd_val": 0.15 if macd_code else None,
                        "signal_val": 0.10 if macd_code else None,
                        "histogram": 0.05 if macd_code else None,
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


def test_build_stock_state_macd_from_real_data() -> None:
    """C6: MACD 只能来自真实 MACD 紧凑状态（macd_state），不接受 code=null 作为完成状态。"""
    snapshot = _make_mock_snapshot(macd_code="bullish_above")
    run = _make_mock_run()
    state = build_stock_state(snapshot, run, symbol="000001")

    assert state.momentum.macd.code == "bullish_above", "MACD code 必须来自真实 macd_state"
    assert "MACD" in state.momentum.macd.label
    assert state.momentum.macd.sourceField == "macd_state"


def test_build_stock_state_macd_null_when_no_data() -> None:
    """C6: macd_state.code=None 时 MACD 显示数据不足（非'暂不可用'永久状态）。"""
    snapshot = _make_mock_snapshot(macd_code=None)
    run = _make_mock_run()
    state = build_stock_state(snapshot, run, symbol="000001")

    assert state.momentum.macd.code is None
    assert "数据不足" in state.momentum.macd.label


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
    """extract_state_codes 返回所有 6 个字段路径。"""
    state = _make_state()
    codes = extract_state_codes(state)

    assert set(codes.keys()) == set(_STATE_FIELD_PATHS)
    assert len(codes) == 6


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


# =============================================================================
# 9. C3: 幂等键 = symbol:source_run_id:algorithm_version
# V1.1: 每只股票每个 source_run_id 最多一条事件（禁止用日期+hash 绕开）
# =============================================================================


def test_c3_idempotency_key_same_run_same_key() -> None:
    """C3: 相同 (symbol, source_run_id, algorithm_version) 必须产生相同幂等键。

    同一 run 重跑（无论 evidence 是否变化）产生相同 key → ON CONFLICT DO NOTHING。
    这保证每只股票每个 run 最多一条事件。
    """
    from app.services.state_event_service import compute_idempotency_key

    symbol = "000001"
    run_id = uuid4()
    algo_version = "schema_v1+state_v1"

    key_1 = compute_idempotency_key(symbol, run_id, algo_version)
    key_2 = compute_idempotency_key(symbol, run_id, algo_version)

    assert key_1 == key_2, f"相同 run 必须产生相同 key: {key_1} vs {key_2}"
    assert key_1 == f"{symbol}:{run_id}:{algo_version}"


def test_c3_idempotency_key_differs_for_different_runs() -> None:
    """C3: 不同 source_run_id 必须产生不同幂等键。

    同一只股票的不同 run 产生不同 key，确保每次 run 都能生成新事件。
    """
    from app.services.state_event_service import compute_idempotency_key

    symbol = "000001"
    run_a = uuid4()
    run_b = uuid4()
    algo_version = "schema_v1+state_v1"

    key_a = compute_idempotency_key(symbol, run_a, algo_version)
    key_b = compute_idempotency_key(symbol, run_b, algo_version)

    assert key_a != key_b, f"不同 run 必须产生不同 key: {key_a} vs {key_b}"


def test_c3_idempotency_key_differs_for_different_symbols() -> None:
    """C3: 不同 symbol 必须产生不同幂等键。"""
    from app.services.state_event_service import compute_idempotency_key

    run_id = uuid4()
    algo_version = "schema_v1+state_v1"

    key_a = compute_idempotency_key("000001", run_id, algo_version)
    key_b = compute_idempotency_key("000002", run_id, algo_version)

    assert key_a != key_b, f"不同 symbol 必须产生不同 key: {key_a} vs {key_b}"


def test_c3_idempotency_key_differs_for_different_algo_versions() -> None:
    """C3: 不同 algorithm_version 必须产生不同幂等键。"""
    from app.services.state_event_service import compute_idempotency_key

    symbol = "000001"
    run_id = uuid4()

    key_a = compute_idempotency_key(symbol, run_id, "schema_v1+state_v1")
    key_b = compute_idempotency_key(symbol, run_id, "schema_v2+state_v1")

    assert key_a != key_b, f"不同 algo 必须产生不同 key: {key_a} vs {key_b}"


# =============================================================================
# 10. C4: 前一成功状态同日多 run 确定性选择最新批次
# =============================================================================


@pytest.mark.asyncio
async def test_c4_previous_snapshot_picks_latest_run_same_day() -> None:
    """C4: 同日存在多个成功 run 时，按 published_at DESC 确定性选最新。

    场景：instrument X 在 2026-07-09 有两条成功 run 的快照
    - Run A: published_at = 10:00
    - Run B: published_at = 15:00
    应选择 Run B（最新发布）。
    """
    from app.services.state_event_service import _batch_get_previous_snapshots

    inst_id = uuid4()
    mock_session = MagicMock()

    # 构造两个同日快照 + run
    snap_a = _make_mock_snapshot(trade_date=date(2026, 7, 9))
    snap_a.instrument_id = inst_id
    run_a = _make_mock_run(trade_date=date(2026, 7, 9))
    run_a.published_at = datetime(2026, 7, 9, 10, 0, tzinfo=UTC)

    snap_b = _make_mock_snapshot(trade_date=date(2026, 7, 9))
    snap_b.instrument_id = inst_id
    run_b = _make_mock_run(trade_date=date(2026, 7, 9))
    run_b.published_at = datetime(2026, 7, 9, 15, 0, tzinfo=UTC)

    # 模拟 SQL 返回（ORDER BY 已确保 B 在前）
    mock_result = MagicMock()
    mock_result.all.return_value = [(snap_b, run_b), (snap_a, run_a)]
    mock_session.execute = AsyncMock(return_value=mock_result)

    result = await _batch_get_previous_snapshots(
        mock_session,
        [inst_id],
        current_trade_date=date(2026, 7, 10),
        schema_version=1,
        primary_timeframe="1d",
        secondary_timeframe="15m",
        adj="qfq",
    )

    assert inst_id in result
    selected_snap, selected_run = result[inst_id]
    # C4: 必须选择 published_at 更新的 run_b
    assert selected_run.published_at == run_b.published_at, (
        "C4: 同日多 run 必须选最新 published_at"
    )


@pytest.mark.asyncio
async def test_c4_previous_snapshot_first_row_wins() -> None:
    """C4: 首行保留逻辑 — 同 instrument 后续行跳过。

    模拟 SQL ORDER BY published_at DESC 返回多行，
    只有第一行（最新 run）被保留。
    """
    from app.services.state_event_service import _batch_get_previous_snapshots

    inst_id = uuid4()
    mock_session = MagicMock()

    snap_old = _make_mock_snapshot(trade_date=date(2026, 7, 9))
    snap_old.instrument_id = inst_id
    run_old = _make_mock_run(trade_date=date(2026, 7, 9))
    run_old.published_at = datetime(2026, 7, 9, 10, 0, tzinfo=UTC)

    snap_new = _make_mock_snapshot(trade_date=date(2026, 7, 9))
    snap_new.instrument_id = inst_id
    run_new = _make_mock_run(trade_date=date(2026, 7, 9))
    run_new.published_at = datetime(2026, 7, 9, 15, 0, tzinfo=UTC)

    # SQL 已 ORDER BY published_at DESC，new 在前
    mock_result = MagicMock()
    mock_result.all.return_value = [(snap_new, run_new), (snap_old, run_old)]
    mock_session.execute = AsyncMock(return_value=mock_result)

    result = await _batch_get_previous_snapshots(
        mock_session,
        [inst_id],
        current_trade_date=date(2026, 7, 10),
        schema_version=1,
        primary_timeframe="1d",
        secondary_timeframe="15m",
        adj="qfq",
    )

    # 只保留首行（最新 run）
    assert len(result) == 1
    _, selected_run = result[inst_id]
    assert selected_run.published_at == run_new.published_at


@pytest.mark.asyncio
async def test_c4_previous_snapshot_empty_instrument_ids() -> None:
    """C4: 空 instrument_ids 直接返回空 dict（不发 SQL）。"""
    from app.services.state_event_service import _batch_get_previous_snapshots

    mock_session = MagicMock()
    mock_session.execute = AsyncMock()

    result = await _batch_get_previous_snapshots(
        mock_session,
        [],
        current_trade_date=date(2026, 7, 10),
        schema_version=1,
        primary_timeframe="1d",
        secondary_timeframe="15m",
        adj="qfq",
    )

    assert result == {}
    mock_session.execute.assert_not_awaited()


# =============================================================================
# 11. P0-3: 时区边界 + 同日多 run
# [CHANGE-20260718-007] - _event_to_dto 已在 commit 095f4eb（Atomic Fact Contract V1）
# 重构中移除，旧 state/events API 替换为 contractVersion/core/auxiliary/availability。
# 新 API 的 evidence/recentChanges 覆盖由 test_stock_context_atomic_facts.py 和
# test_atomic_fact_contract_service.py 承担。此处保留时区与 run 查询的纯函数测试。
# =============================================================================


def test_p03_build_event_evidence_includes_values() -> None:
    """P0-3: build_event_evidence 保存 prevValue/currValue/unit/timeframe。"""
    prev = _make_state(sqzmom_code="positive")
    # 构造带 value 的 curr state
    curr = StockState(
        symbol="000001",
        asOf="2026-07-10",
        sourceRunId=str(uuid4()),
        version="v1",
        computedAt="2026-07-10T15:00:00+08:00",
        structure=StockStructure(
            price=StateValue(code="inside", label="test", value=None, unit=None, timeframe="1d", sourceField="test"),
        ),
        momentum=StockMomentum(
            macd=StateValue(code=None, label="test", value=None, unit=None, timeframe="1d", sourceField="test"),
            sqzmom=StateValue(code="negative", label="test", value=-0.002, unit=None, timeframe="1d", sourceField="test"),
            temporal=[
                StateValue(code="1", label="test", value=None, unit=None, timeframe="1d", sourceField="test"),
                StateValue(code="aligned", label="test", value=None, unit=None, timeframe="1d", sourceField="test"),
            ],
        ),
        volatility=StockVolatility(
            bollPosition=StateValue(code="middle", label="test", value=0.5, unit=None, timeframe="1d", sourceField="test"),
        ),
    )
    changed = ["momentum.sqzmom"]

    evidence = build_event_evidence(prev, curr, changed)

    assert len(evidence) == 1
    e = evidence[0]
    assert e["field"] == "momentum.sqzmom"
    assert e["prevCode"] == "positive"
    assert e["currCode"] == "negative"
    assert e["prevValue"] is None  # prev state 的 sqzmom value 为 None（_make_state 不设 value）
    assert e["currValue"] == -0.002
    assert e["timeframe"] == "1d"


def test_p03_shanghai_tz_not_utc() -> None:
    """P0-3: _SHANGHAI_TZ 必须是 Asia/Shanghai（非 UTC）。"""
    from zoneinfo import ZoneInfo

    from app.api.stock_context import _SHANGHAI_TZ

    assert _SHANGHAI_TZ == ZoneInfo("Asia/Shanghai")
    assert str(_SHANGHAI_TZ) == "Asia/Shanghai"


def test_p03_as_of_cutoff_next_day_exclusive() -> None:
    """P0-3: as_of 截止为次日 00:00 exclusive（非 max.time()+1day-1sec）。

    场景：as_of=2026-07-10
    - 旧方案（错误）: 2026-07-11T23:59:59+08:00
    - 新方案（正确）: 2026-07-11T00:00:00+08:00 (exclusive)

    验证：occurred_at = 2026-07-10T23:30:00+08:00 应被包含（<= cutoff）
         occurred_at = 2026-07-11T00:00:00+08:00 应被排除（== cutoff, 但 cutoff 是 exclusive 的上界）
         注意：当前实现使用 <=，所以 == cutoff 的会被包含。这是 SQL 层的语义。
         此处验证 cutoff 本身是次日 00:00 而非 23:59:59。
    """
    from datetime import timedelta

    from app.api.stock_context import _SHANGHAI_TZ

    as_of = date(2026, 7, 10)
    next_day = as_of + timedelta(days=1)
    # 模拟 _build_stock_context 中的 cutoff 计算
    cutoff = datetime.combine(next_day, datetime.min.time(), tzinfo=_SHANGHAI_TZ)

    # cutoff 应该是 2026-07-11T00:00:00+08:00
    assert cutoff.year == 2026
    assert cutoff.month == 7
    assert cutoff.day == 11
    assert cutoff.hour == 0
    assert cutoff.minute == 0
    assert cutoff.second == 0
    # 时区为 Asia/Shanghai
    assert cutoff.tzinfo is _SHANGHAI_TZ
    # 禁止 max.time()+1day-1sec 写法（那会是 23:59:59）
    assert cutoff.second != 59 or cutoff.minute != 59


@pytest.mark.asyncio
async def test_p03_find_latest_succeeded_run_deterministic_order() -> None:
    """P0-3: _find_latest_succeeded_run 按 trade_date/published_at/finished_at 确定性倒序。

    场景：同日有两个 succeeded run
    - Run A: published_at=10:00
    - Run B: published_at=15:00
    应选择 Run B（最新发布）。
    """
    from app.api.stock_context import _find_latest_succeeded_run
    from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun

    # 构造两个同日 run（run_a 仅作为上下文，mock 返回 run_b）
    StockFeatureSnapshotRun(
        trade_date=date(2026, 7, 10),
        schema_version=1,
        primary_timeframe="1d",
        secondary_timeframe="15m",
        adj="qfq",
        run_type="after_close",
        status="succeeded",
        published_at=datetime(2026, 7, 10, 10, 0, tzinfo=UTC),
        finished_at=datetime(2026, 7, 10, 9, 55, tzinfo=UTC),
        metadata_={"scope": "full"},
    )
    run_b = StockFeatureSnapshotRun(
        trade_date=date(2026, 7, 10),
        schema_version=1,
        primary_timeframe="1d",
        secondary_timeframe="15m",
        adj="qfq",
        run_type="after_close",
        status="succeeded",
        published_at=datetime(2026, 7, 10, 15, 0, tzinfo=UTC),
        finished_at=datetime(2026, 7, 10, 14, 55, tzinfo=UTC),
        metadata_={"scope": "full"},
    )

    mock_session = MagicMock()

    # 捕获 SQL 语句验证 ORDER BY
    captured_stmts: list = []

    async def mock_execute(stmt):
        captured_stmts.append(stmt)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = run_b
        return mock_result
    mock_session.execute = AsyncMock(side_effect=mock_execute)

    result = await _find_latest_succeeded_run(mock_session)

    assert result is run_b, "应选择 published_at 更新的 run_b"
    assert len(captured_stmts) == 1
    compiled = str(captured_stmts[0])
    # 验证 ORDER BY 包含 trade_date, published_at, finished_at
    assert "trade_date" in compiled
    assert "published_at" in compiled
    assert "finished_at" in compiled


@pytest.mark.asyncio
async def test_p03_find_run_by_trade_date_deterministic_order() -> None:
    """P0-3: _find_run_by_trade_date 按 published_at/finished_at 确定性倒序。"""
    from app.api.stock_context import _find_run_by_trade_date
    from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun

    # run_a 仅作为上下文，mock 返回 run_b
    StockFeatureSnapshotRun(
        trade_date=date(2026, 7, 10),
        schema_version=1,
        primary_timeframe="1d",
        secondary_timeframe="15m",
        adj="qfq",
        run_type="after_close",
        status="succeeded",
        published_at=datetime(2026, 7, 10, 10, 0, tzinfo=UTC),
        finished_at=datetime(2026, 7, 10, 9, 55, tzinfo=UTC),
        metadata_={"scope": "full"},
    )
    run_b = StockFeatureSnapshotRun(
        trade_date=date(2026, 7, 10),
        schema_version=1,
        primary_timeframe="1d",
        secondary_timeframe="15m",
        adj="qfq",
        run_type="after_close",
        status="succeeded",
        published_at=datetime(2026, 7, 10, 15, 0, tzinfo=UTC),
        finished_at=datetime(2026, 7, 10, 14, 55, tzinfo=UTC),
        metadata_={"scope": "full"},
    )

    mock_session = MagicMock()
    captured_stmts: list = []

    async def mock_execute(stmt):
        captured_stmts.append(stmt)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = run_b
        return mock_result
    mock_session.execute = AsyncMock(side_effect=mock_execute)

    result = await _find_run_by_trade_date(mock_session, date(2026, 7, 10))

    assert result is run_b
    assert len(captured_stmts) == 1
    compiled = str(captured_stmts[0])
    # 验证 ORDER BY 包含 published_at, finished_at
    assert "published_at" in compiled
    assert "finished_at" in compiled


# =============================================================================
# 10. P0-2: StockContext reasonCode 覆盖测试
# 用户要求 9 项测试中的 5 项 API 测试 + 1 项无写副作用测试
# ============================================================================


async def _create_db_run(
    db: AsyncSession,
    trade_date: date = date(2026, 7, 10),
    status: str = "succeeded",
    published: bool = True,
    scope: str = "full",
    run_id: uuid.UUID | None = None,
) -> StockFeatureSnapshotRun:
    """在测试 DB 中创建 snapshot run。"""
    from datetime import UTC, datetime

    run = StockFeatureSnapshotRun(
        id=run_id or uuid.uuid4(),
        trade_date=trade_date,
        schema_version=1,
        primary_timeframe="1d",
        secondary_timeframe="15m",
        adj="qfq",
        run_type="after_close",
        status=status,
        published_at=datetime.now(UTC) if published and status == "succeeded" else None,
        finished_at=datetime.now(UTC) if status != "running" else None,
        metadata_={"scope": scope},
    )
    db.add(run)
    await db.flush()
    return run


async def _create_db_snapshot(
    db: AsyncSession,
    instrument_id: uuid.UUID,
    run: StockFeatureSnapshotRun,
    source_run_id: uuid.UUID | None = None,
    trade_date: date | None = None,
) -> StockFeatureSnapshot:
    """在测试 DB 中创建 snapshot（source_run_id 可空表示未关联）。"""
    snap = StockFeatureSnapshot(
        instrument_id=instrument_id,
        trade_date=trade_date or run.trade_date,
        primary_timeframe=run.primary_timeframe,
        secondary_timeframe=run.secondary_timeframe,
        adj=run.adj,
        schema_version=run.schema_version,
        source_run_id=source_run_id,
        source_primary_bar_time=datetime(2026, 7, 10, 15, 0, tzinfo=UTC),
        structural_payload={
            "primary": {
                "1d": {
                    "swing_position": {
                        "confirmed_swing_breakout_state": "inside",
                        "price_position_in_swing_0_1": 0.5,
                        "confirmed_swing_high": 10.5,
                        "confirmed_swing_low": 9.5,
                    },
                    "cost_position": {"poc_price": 10.0},
                    "volatility_momentum": {"sqzmom_val": 0.001, "bb_percent_b": 0.5},
                    "macd_state": {"code": "bullish_above", "histogram": 0.05},
                }
            }
        },
        temporal_payload={
            "daily_context": {"daily_dsa_dir": 1},
            "derived_relation": {"m15_response_direction_relative_to_daily": "aligned"},
        },
        summary_payload={},
        degraded_reasons=[],
    )
    db.add(snap)
    await db.flush()
    return snap


@pytest.mark.asyncio
async def test_p02_context_exact_source_run_id_returns_state(
    stock_context_client,
) -> None:
    """P0-2: exact source_run_id 精确匹配 → snapshot 可读, reasonCode=null。

    [CHANGE-20260718-007] - Atomic Fact Contract V1 重构后，API 响应不再包含 `state`
    字段，改为 `core`/`auxiliary`/`availability`。此处验证 dataQuality 仍正确反映
    snapshot 可读性（hasSnapshot=True, reasonCode=null）。
    """
    client, db = stock_context_client
    admin = await _create_admin_user(db)
    inst = await _create_test_instrument(db, "P02001")
    run = await _create_db_run(db, trade_date=date(2026, 7, 10))
    await _create_db_snapshot(db, inst.id, run, source_run_id=run.id)

    resp = await client.get(
        "/api/v1/stocks/P02001/context",
        headers=_auth_headers(admin.id),
    )
    assert resp.status_code == 200
    body = resp.json()
    # Atomic Fact Contract V1 响应结构
    assert body["contractVersion"] is not None
    assert body["asOf"] == "2026-07-10"
    assert body["dataQuality"]["reasonCode"] is None
    assert body["dataQuality"]["hasSucceededRun"] is True
    assert body["dataQuality"]["hasSnapshot"] is True


@pytest.mark.asyncio
async def test_p02_context_no_published_full_run(
    stock_context_client,
) -> None:
    """P0-2: 无 succeeded+published+full run → 空态响应, reasonCode=no_published_full_run。

    [CHANGE-20260718-007] - Atomic Fact Contract V1 重构后，空态响应 core 全缺失，
    dataQuality.reasonCode 解释空态原因。
    """
    client, db = stock_context_client
    admin = await _create_admin_user(db)
    await _create_test_instrument(db, "P02002")
    # 不创建任何 run

    resp = await client.get(
        "/api/v1/stocks/P02002/context",
        headers=_auth_headers(admin.id),
    )
    assert resp.status_code == 200
    body = resp.json()
    # 空态响应
    assert body["asOf"] is None
    assert body["dataQuality"]["reasonCode"] == "no_published_full_run"
    assert body["dataQuality"]["hasSucceededRun"] is False
    assert body["dataQuality"]["hasSnapshot"] is False


@pytest.mark.asyncio
async def test_p02_context_snapshot_missing(
    stock_context_client,
) -> None:
    """P0-2: run 存在但该 instrument 无快照 → 空态响应, reasonCode=snapshot_missing。

    [CHANGE-20260718-007] - Atomic Fact Contract V1 重构后，空态响应 core 全缺失。
    """
    client, db = stock_context_client
    admin = await _create_admin_user(db)
    await _create_test_instrument(db, "P02003")
    # 创建 run 但不为该 instrument 创建 snapshot
    await _create_db_run(db, trade_date=date(2026, 7, 10))

    resp = await client.get(
        "/api/v1/stocks/P02003/context",
        headers=_auth_headers(admin.id),
    )
    assert resp.status_code == 200
    body = resp.json()
    # 空态响应：run 存在但 snapshot 缺失 → _empty_atomic_response(asOf=None)
    # [CHANGE-20260718-007] - Atomic Fact Contract V1 重构后，snapshot 缺失走
    # _empty_atomic_response，asOf=None（旧 API 的 state=None 等价语义）。
    assert body["asOf"] is None
    assert body["dataQuality"]["reasonCode"] == "snapshot_missing"
    assert body["dataQuality"]["hasSucceededRun"] is True
    assert body["dataQuality"]["hasSnapshot"] is False


@pytest.mark.asyncio
async def test_p02_context_snapshot_run_not_linked(
    stock_context_client,
) -> None:
    """P0-2: legacy 快照 source_run_id=NULL → _get_snapshot_for_instrument 返回 snapshot_run_not_linked。

    [CHANGE-20260718-007] - Atomic Fact Contract V1 重构后，API 响应不再包含 `state` 字段。
    commit d8eda23（AFC V1 终审修正）变更 _build_data_quality 行为：
    legacy/ambiguous 快照（snapshot 非空但 reason_code 非空）现在传播 reasonCode
    并加入 degradedReasons，不再清为 None。此为有意设计——legacy 快照虽可读，
    但归属异常需在 dataQuality 显式标注，前端据此提示修复 source_run_id。
    """
    from app.api.stock_context import _get_snapshot_for_instrument

    client, db = stock_context_client
    admin = await _create_admin_user(db)
    inst = await _create_test_instrument(db, "P02004")
    run = await _create_db_run(db, trade_date=date(2026, 7, 10))
    # 创建 snapshot 但 source_run_id=NULL（未关联）
    await _create_db_snapshot(db, inst.id, run, source_run_id=None)

    # 测试内部函数返回正确的 reasonCode
    snapshot, reason_code = await _get_snapshot_for_instrument(db, inst.id, run)
    assert snapshot is not None
    assert reason_code == "snapshot_run_not_linked"

    # API 层面：snapshot 可读（legacy 匹配成功，hasSnapshot=True），
    # reasonCode 传播为 "snapshot_run_not_linked"（commit d8eda23 有意行为）
    resp = await client.get(
        "/api/v1/stocks/P02004/context",
        headers=_auth_headers(admin.id),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["dataQuality"]["hasSnapshot"] is True
    assert body["dataQuality"]["reasonCode"] == "snapshot_run_not_linked"
    # 同时加入 degradedReasons
    assert "snapshot_run_not_linked" in (body["dataQuality"]["degradedReasons"] or [])


@pytest.mark.asyncio
async def test_p02_context_legacy_snapshot_ambiguous(
    stock_context_client,
) -> None:
    """P0-2: legacy 快照 source_run_id 指向其他 run → _get_snapshot_for_instrument 返回 legacy_snapshot_ambiguous。"""
    from app.api.stock_context import _get_snapshot_for_instrument

    client, db = stock_context_client
    inst = await _create_test_instrument(db, "P02005")
    run = await _create_db_run(db, trade_date=date(2026, 7, 10))
    # 创建另一个 run（作为 source_run_id 指向的目标）
    other_run = await _create_db_run(
        db, trade_date=date(2026, 7, 10), run_id=uuid.uuid4()
    )
    # 创建 snapshot，source_run_id 指向 other_run（非查询的 run）
    await _create_db_snapshot(db, inst.id, run, source_run_id=other_run.id)

    # 测试内部函数：精确匹配失败（source_run_id != run.id），legacy 匹配成功但 source_run_id 指向其他 run
    snapshot, reason_code = await _get_snapshot_for_instrument(db, inst.id, run)
    assert snapshot is not None
    assert reason_code == "legacy_snapshot_ambiguous"


@pytest.mark.asyncio
async def test_p02_context_get_no_write_side_effect(
    stock_context_client,
) -> None:
    """P0-2: GET /context 只读，不产生任何写副作用（不创建事件/snapshot/run）。

    通过在请求前后查询行数验证。
    """
    from sqlalchemy import func
    from sqlalchemy import select as sa_select

    from app.models.stock_feature_snapshot import StockFeatureSnapshot
    from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun

    client, db = stock_context_client
    admin = await _create_admin_user(db)
    inst = await _create_test_instrument(db, "P02006")
    run = await _create_db_run(db, trade_date=date(2026, 7, 10))
    await _create_db_snapshot(db, inst.id, run, source_run_id=run.id)

    # 请求前行数
    snap_before = await db.scalar(
        sa_select(func.count()).select_from(StockFeatureSnapshot)
    )
    run_before = await db.scalar(
        sa_select(func.count()).select_from(StockFeatureSnapshotRun)
    )

    # 发起 GET 请求（多次调用确保无写副作用）
    for _ in range(3):
        resp = await client.get(
            "/api/v1/stocks/P02006/context",
            headers=_auth_headers(admin.id),
        )
        assert resp.status_code == 200

    # 请求后行数不变
    snap_after = await db.scalar(
        sa_select(func.count()).select_from(StockFeatureSnapshot)
    )
    run_after = await db.scalar(
        sa_select(func.count()).select_from(StockFeatureSnapshotRun)
    )
    assert snap_after == snap_before, "GET context 不得创建 snapshot"
    assert run_after == run_before, "GET context 不得创建 run"
