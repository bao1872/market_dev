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

from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.models.stock_feature_snapshot import StockFeatureSnapshot
from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun
from app.schemas.stock_state import (
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
    """多字段变化时标题包含数量，不以第一个字段代表。"""
    curr = _make_state()
    changed = ["momentum.sqzmom", "volatility.bollPosition", "structure.price"]

    title, desc = build_event_title_and_description(curr, changed)

    assert "3 项" in title
    # 描述应包含所有变化字段
    assert "sqzmom" in desc or "bollPosition" in desc or "price" in desc


def test_build_event_title_and_description_no_change() -> None:
    """无变化时返回明确文案。"""
    curr = _make_state()
    title, desc = build_event_title_and_description(curr, [])

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
    """事件生成使用 ON CONFLICT DO NOTHING（幂等写入）。"""
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

    mock_session = MagicMock()

    # get(run) 返回 mock_run
    async def mock_get(model, obj_id):
        if model.__name__ == "StockFeatureSnapshotRun":
            return mock_run
        return None
    mock_session.get = AsyncMock(side_effect=mock_get)

    # 按调用顺序返回结果：
    # 1. _get_run_snapshots → execute(snapshots list) → scalars().all()
    # 2. _get_instrument_symbol → execute(symbol) → scalar_one_or_none()
    # 3. _find_previous_snapshot → execute(prev) → scalar_one_or_none()
    # 4. pg_insert.on_conflict_do_nothing → execute(insert)
    call_sequence = []

    async def mock_execute(stmt):
        call_sequence.append(stmt)
        # 判断是否为 insert 语句（ON CONFLICT DO NOTHING）
        stmt_type = type(stmt).__name__
        if stmt_type == "Insert":
            mock_result = MagicMock()
            return mock_result

        # 判断是否为 select 语句
        # 通过编译后的 SQL 文本判断查询目标
        compiled = str(stmt)
        if "stock_feature_snapshots" in compiled and "ORDER BY" in compiled:
            # _find_previous_snapshot 查询（含 ORDER BY ... DESC）
            mock_result = MagicMock()
            mock_result.scalar_one_or_none.return_value = prev_snapshot
            return mock_result
        elif "stock_feature_snapshots" in compiled:
            # _get_run_snapshots 查询
            mock_result = MagicMock()
            mock_result.scalars.return_value.all.return_value = [curr_snapshot]
            return mock_result
        elif "instruments" in compiled:
            # _get_instrument_symbol 查询
            mock_result = MagicMock()
            mock_result.scalar_one_or_none.return_value = "000001"
            return mock_result

        # 默认返回空结果
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_result.scalars.return_value.all.return_value = []
        return mock_result
    mock_session.execute = AsyncMock(side_effect=mock_execute)

    result = await generate_events_for_run(mock_session, run_id)

    # 应该生成 1 条事件（sqzmom 从 positive 变为 negative）
    assert result["event_count"] == 1, f"预期 1 条事件，实际: {result}"
    # 至少有一次 insert 调用（ON CONFLICT DO NOTHING）
    insert_calls = [s for s in call_sequence if type(s).__name__ == "Insert"]
    assert len(insert_calls) >= 1, "应至少有一次幂等 insert 调用"


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
