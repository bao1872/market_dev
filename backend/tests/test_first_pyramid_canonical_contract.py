"""第一金字塔 canonical 合同测试（PRD20 QM-62 / QM-63）。

对应"第一金字塔与 SMC 12 项合同测试"：
  1. 同 run calculatedAt 一致
  2. BOS 四组合（swing/internal × bullish/bearish）
  3. CHoCH 四组合
  4. OB 四组合
  5. 缺 direction 不默认 bearish
  6. 缺 level 不默认 swing
  7. 冲突 diagnostic
  8. 合法可空 availability
  9. chip 七态
 10. chip 非破坏 merge
 11. Swing/Internal OB 均可渲染
 12. producer / API / Review 跨入口一致

纯单元测试，不连数据库。
"""

from __future__ import annotations

import pytest

from app.schemas.first_pyramid import (
    CHIP_STATUS_STATES,
    FIELD_AVAILABILITY_REASONS,
    PYRAMID_DIRECTIONS,
    PYRAMID_STRUCTURE_LEVELS,
    ChipStatus,
    FieldAvailability,
    PyramidEvent,
    build_pyramid_event,
    normalize_direction,
    normalize_structure_level,
)
from app.services.first_pyramid_flatten import (
    FP_CHIP_KEYS,
    assemble_first_pyramid_read_model,
    flatten_first_pyramid,
)
from app.services.first_pyramid_semantic_adapter import adapt_legacy_pyramid_event

pytestmark = pytest.mark.pure_unit


# ---------------------------------------------------------------------------
# 合同 2/3/4/11：BOS / CHoCH / OB × swing/internal × bullish/bearish
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("event_type", ["BOS", "CHoCH", "OB_CREATED"])
@pytest.mark.parametrize("level", ["swing", "internal"])
@pytest.mark.parametrize(
    ("direction_raw", "expected_direction", "expected_bias"),
    [
        ("bullish", "bullish", 1),
        ("up", "bullish", 1),
        (1, "bullish", 1),
        (True, "bullish", 1),
        ("bearish", "bearish", -1),
        ("down", "bearish", -1),
        (-1, "bearish", -1),
        (False, "bearish", -1),
    ],
)
def test_event_all_combinations(
    event_type: str,
    level: str,
    direction_raw: object,
    expected_direction: str,
    expected_bias: int,
) -> None:
    """BOS/CHoCH/OB 的 level × direction 全组合都必须产出正式字段。"""
    evt = build_pyramid_event(
        event_type=event_type,
        direction_raw=direction_raw,
        structure_level_raw=level,
        occurred_at="2026-07-25",
        bar_index=100,
        price=10.5,
        freshness_bars=3,
    )
    assert evt.type == event_type
    assert evt.direction == expected_direction
    assert evt.structureLevel == level
    assert evt.bias == expected_bias
    # bias 与 direction 必须严格一致
    assert (evt.bias == 1) == (evt.direction == "bullish")
    assert (evt.bias == -1) == (evt.direction == "bearish")
    # 无冲突时不应产生诊断
    assert evt.diagnostics == []


def test_ob_swing_and_internal_both_produced() -> None:
    """[合同 11] Swing OB 与 Internal OB 都必须能生成（不得只保留 swing）。"""
    levels = {
        build_pyramid_event(
            event_type="OB_CREATED",
            direction_raw="bullish",
            structure_level_raw=lv,
            bar_index=1,
            freshness_bars=0,
        ).structureLevel
        for lv in ("swing", "internal")
    }
    assert levels == {"swing", "internal"}


# ---------------------------------------------------------------------------
# 合同 5/6：缺 direction 不默认 bearish；缺 level 不默认 swing
# ---------------------------------------------------------------------------


def test_missing_direction_is_none_not_bearish() -> None:
    """[合同 5] 缺方向必须是 None，绝不能默认 bearish。"""
    evt = build_pyramid_event(
        event_type="BOS",
        direction_raw=None,
        structure_level_raw="swing",
        bar_index=10,
        freshness_bars=1,
    )
    assert evt.direction is None, "缺方向不得默认 bearish"
    assert evt.bias is None, "缺方向时 bias 必须同为 None"


@pytest.mark.parametrize("junk", ["", "unknown", "  ", "sideways", object()])
def test_unrecognized_direction_is_none(junk: object) -> None:
    """无法识别的方向一律 None，不猜测。"""
    assert normalize_direction(junk) is None


def test_missing_level_is_none_not_swing() -> None:
    """[合同 6] 缺级别必须是 None，绝不能默认 swing。"""
    evt = build_pyramid_event(
        event_type="EQH",
        direction_raw="bullish",
        structure_level_raw=None,
        bar_index=10,
        freshness_bars=1,
    )
    assert evt.structureLevel is None, "缺级别不得默认 swing"


@pytest.mark.parametrize("junk", ["", "major", "minor", "unknown"])
def test_unrecognized_level_is_none(junk: str) -> None:
    assert normalize_structure_level(junk) is None


def test_direction_zero_is_none() -> None:
    """bias=0 表示未形成方向，不是 bullish 也不是 bearish。"""
    assert normalize_direction(0) is None


# ---------------------------------------------------------------------------
# 合同 7：direction 与 bias 冲突必须输出 diagnostic
# ---------------------------------------------------------------------------


def test_direction_bias_conflict_emits_diagnostic() -> None:
    """[合同 7] direction 与 bias 冲突必须记录 diagnostic，不得静默择一。"""
    evt = build_pyramid_event(
        event_type="BOS",
        direction_raw="bullish",
        bias_raw=-1,  # 冲突
        structure_level_raw="swing",
        bar_index=5,
        freshness_bars=0,
    )
    assert any("DIRECTION_BIAS_CONFLICT" in d for d in evt.diagnostics), (
        f"冲突必须输出 diagnostic，实际 {evt.diagnostics}"
    )
    # 以 direction 为准，且 bias 与之对齐（不得保留矛盾状态）
    assert evt.direction == "bullish"
    assert evt.bias == 1


def test_bias_only_derives_direction_without_conflict() -> None:
    """仅有 bias 时据此推导方向，不算冲突。"""
    evt = build_pyramid_event(
        event_type="BOS",
        direction_raw=None,
        bias_raw=-1,
        structure_level_raw="internal",
        bar_index=5,
        freshness_bars=0,
    )
    assert evt.direction == "bearish"
    assert evt.bias == -1
    assert evt.diagnostics == []


def test_dto_rejects_inconsistent_bias_direction() -> None:
    """绕过 producer 直接构造矛盾 DTO 必须被拒绝。"""
    with pytest.raises(ValueError, match="bias 与 direction 不一致"):
        PyramidEvent(
            type="BOS", direction="bullish", bias=-1,
            barIndex=1, freshnessBars=0,
        )


def test_dto_rejects_legacy_direction_value() -> None:
    """DTO 不再接受旧值 up/down，强制走 producer 归一。"""
    with pytest.raises(ValueError, match="direction 非法"):
        PyramidEvent(type="BOS", direction="up", bias=1, barIndex=1, freshnessBars=0)


def test_dto_rejects_unknown_structure_level() -> None:
    with pytest.raises(ValueError, match="structureLevel 非法"):
        PyramidEvent(
            type="BOS", direction=None, bias=None, structureLevel="major",
            barIndex=1, freshnessBars=0,
        )


# ---------------------------------------------------------------------------
# 兼容 adapter：唯一允许读取 extra.structure_level / extra.bias 的位置
# ---------------------------------------------------------------------------


def test_legacy_adapter_normalizes_up_down() -> None:
    """旧快照 up/down 必须归一为正式值。"""
    adapted = adapt_legacy_pyramid_event({
        "type": "BOS", "direction": "up", "extra": {"structure_level": "internal"},
    })
    assert adapted["direction"] == "bullish"
    assert adapted["structureLevel"] == "internal"
    assert adapted["bias"] == 1
    assert "STRUCTURE_LEVEL_FROM_LEGACY_EXTRA" in adapted["diagnostics"]


def test_legacy_adapter_conflict_diagnostic() -> None:
    adapted = adapt_legacy_pyramid_event({
        "type": "BOS", "direction": "down", "bias": 1,
    })
    assert "EVENT_DIRECTION_BIAS_CONFLICT" in adapted["diagnostics"]
    # direction 优先，bias 与之对齐
    assert adapted["direction"] == "bearish"
    assert adapted["bias"] == -1


def test_legacy_adapter_missing_direction_stays_none() -> None:
    adapted = adapt_legacy_pyramid_event({"type": "EQH"})
    assert adapted["direction"] is None
    assert adapted["bias"] is None
    assert adapted["structureLevel"] is None


def test_producer_reads_legacy_extra_level_as_fallback() -> None:
    """正式级别缺失时可从 extra 兼容读取，并留下诊断。"""
    evt = build_pyramid_event(
        event_type="BOS",
        direction_raw="bullish",
        structure_level_raw=None,
        extra={"structure_level": "internal"},
        bar_index=1,
        freshness_bars=0,
    )
    assert evt.structureLevel == "internal"
    assert any(
        d.startswith("STRUCTURE_LEVEL_FROM_LEGACY_EXTRA") for d in evt.diagnostics
    ), f"应记录兼容读取诊断，实际 {evt.diagnostics}"


# ---------------------------------------------------------------------------
# 合同 9：chip 七态
# ---------------------------------------------------------------------------


def test_chip_seven_states_defined() -> None:
    """[合同 9] chip 生命周期必须完整七态。"""
    assert CHIP_STATUS_STATES == {
        "pending", "ready", "unavailable", "failed",
        "interrupted", "stale", "partial",
    }, f"chip 七态不完整: {sorted(CHIP_STATUS_STATES)}"


@pytest.mark.parametrize("state", sorted(CHIP_STATUS_STATES))
def test_chip_status_accepts_each_state(state: str) -> None:
    """七态都必须能构造（非 ready 需 reasonCode）。"""
    status = ChipStatus(
        state=state,
        reasonCode=None if state == "ready" else "CHIP_JOB_PENDING",
        sourceRunId="run-1",
        jobId="job-1",
        freshness=0,
        coverage=1.0 if state == "ready" else 0.5,
    )
    assert status.state == state
    assert status.sourceRunId == "run-1"
    assert status.jobId == "job-1"


def test_chip_non_ready_requires_reason() -> None:
    """非 ready 状态必须给原因，禁止无原因的"暂不可用"。"""
    with pytest.raises(ValueError, match="必须提供 reasonCode"):
        ChipStatus(state="failed", reasonCode=None)


def test_chip_rejects_unknown_state() -> None:
    with pytest.raises(ValueError, match="state 非法"):
        ChipStatus(state="weird", reasonCode="CHIP_JOB_FAILED")


def test_chip_coverage_range_enforced() -> None:
    with pytest.raises(ValueError, match="coverage 必须在"):
        ChipStatus(state="partial", reasonCode="CHIP_PARTIAL_COVERAGE", coverage=1.5)


# ---------------------------------------------------------------------------
# 合同 10：chip 非破坏性 merge
# ---------------------------------------------------------------------------


def _core_flat() -> dict:
    """构造含核心维度值的 flat（模拟 stock_core 已发布）。"""
    flat = flatten_first_pyramid(None)
    flat["fp_summary"] = "趋势上行，结构共振"
    flat["fp_trend_direction"] = "上行"
    flat["fp_trend_bars"] = 60
    flat["fp_structure_event_type"] = "BOS"
    flat["fp_structure_event_direction"] = "bullish"
    flat["fp_momentum_event_type"] = "SQZ_OFF"
    return flat


def test_chip_failure_does_not_destroy_core_dimensions() -> None:
    """[合同 10] chip 失败不得覆盖 trend/structure/momentum/summary。"""
    flat = _core_flat()
    merged = assemble_first_pyramid_read_model(
        flat, snapshot_columns={}, chip_snapshot=None, max_bar_date=None,
    )
    assert merged is not None
    # 核心维度必须原样保留
    assert merged["fp_summary"] == "趋势上行，结构共振"
    assert merged["fp_trend_direction"] == "上行"
    assert merged["fp_trend_bars"] == 60
    assert merged["fp_structure_event_type"] == "BOS"
    assert merged["fp_structure_event_direction"] == "bullish"
    assert merged["fp_momentum_event_type"] == "SQZ_OFF"
    # chip 字段清空且 available=False（boolean，不是 None）
    assert merged["fp_chip_available"] is False
    for k in FP_CHIP_KEYS:
        assert merged[k] is None


def test_chip_available_is_always_boolean() -> None:
    """fp_chip_available 必须始终是 boolean，禁止 None。"""
    for chip_snapshot in (
        None,
        {},
        {"chip_available": False},
        {"chip_available": True, "chip_flat": {"fp_poc_price": 10.5}},
    ):
        merged = assemble_first_pyramid_read_model(
            _core_flat(), snapshot_columns={},
            chip_snapshot=chip_snapshot, max_bar_date=None,
        )
        assert merged is not None
        assert isinstance(merged["fp_chip_available"], bool), (
            f"fp_chip_available 必须是 bool，chip_snapshot={chip_snapshot}"
        )


def test_chip_merge_only_touches_chip_keys() -> None:
    """chip merge 只能改 chip 字段，不得越界。"""
    before = _core_flat()
    non_chip_before = {k: v for k, v in before.items() if k not in FP_CHIP_KEYS}
    merged = assemble_first_pyramid_read_model(
        dict(before), snapshot_columns={},
        chip_snapshot={"chip_available": True, "chip_flat": {"fp_poc_price": 9.9}},
        max_bar_date=None,
    )
    assert merged is not None
    non_chip_after = {
        k: v for k, v in merged.items()
        if k not in FP_CHIP_KEYS and k != "fp_chip_available"
    }
    assert non_chip_after == {
        k: v for k, v in non_chip_before.items() if k != "fp_chip_available"
    }


# ---------------------------------------------------------------------------
# 合同 12：跨入口字段一致性（producer → flatten → read model）
# ---------------------------------------------------------------------------


def test_flatten_preserves_canonical_direction_across_entries() -> None:
    """[合同 12] 同一事件经 flatten 后方向语义不得改变或丢失。"""
    snapshot = {
        "tradeDate": "2026-07-25",
        "statusText": "结构共振",
        "structure": {
            "available": True,
            "continuousFactors": {},
            "events": [
                {
                    "type": "BOS",
                    "direction": "bullish",
                    "bias": 1,
                    "structureLevel": "internal",
                    "freshnessBars": 3,
                    "occurredAt": "2026-07-22",
                    "price": 10.7,
                }
            ],
        },
    }
    flat = flatten_first_pyramid(snapshot)
    assert flat["fp_structure_event_direction"] == "bullish"
    assert flat["fp_structure_event_level"] == "internal"
    assert flat["fp_latest_bos_direction"] == "bullish"
    assert flat["fp_latest_bos_level"] == "internal"

    # 经 read model 组装后仍一致（不被重命名/重置）
    merged = assemble_first_pyramid_read_model(
        flat, snapshot_columns={}, chip_snapshot=None, max_bar_date=None,
    )
    assert merged is not None
    assert merged["fp_structure_event_direction"] == "bullish"
    assert merged["fp_structure_event_level"] == "internal"


def test_legacy_snapshot_normalized_at_flatten() -> None:
    """历史快照（up + extra.structure_level）经 flatten 归一为正式值。"""
    snapshot = {
        "structure": {
            "available": True,
            "continuousFactors": {},
            "events": [
                {
                    "type": "BOS",
                    "direction": "up",
                    "freshnessBars": 1,
                    "occurredAt": "2026-07-22",
                    "extra": {"structure_level": "swing"},
                }
            ],
        },
    }
    flat = flatten_first_pyramid(snapshot)
    assert flat["fp_structure_event_direction"] == "bullish"
    assert flat["fp_structure_event_level"] == "swing"


def test_legacy_ob_event_normalized_at_flatten() -> None:
    """历史 OB 事件必须统一经兼容 adapter 归一（up/down → bullish/bearish）。

    [报告 2026-08-04] 历史 OB 事件不得直接读原始 direction，必须经
    adapt_legacy_pyramid_event 归一，否则旧 up/down 值会污染正式字段。
    """
    snapshot = {
        "structure": {
            "available": True,
            "continuousFactors": {},
            "events": [
                {
                    "type": "OB_CREATED",
                    "direction": "down",
                    "freshnessBars": 2,
                    "occurredAt": "2026-07-20",
                    "extra": {"structure_level": "swing", "ob_high": 12.0, "ob_low": 11.0},
                }
            ],
        },
    }
    flat = flatten_first_pyramid(snapshot)
    assert flat["fp_latest_ob_direction"] == "bearish", (
        "历史 OB 的 down 必须归一为 bearish"
    )


def test_direction_and_level_value_sets_are_canonical() -> None:
    """正式取值集合本身即合同的一部分。"""
    assert PYRAMID_DIRECTIONS == {"bullish", "bearish"}
    assert PYRAMID_STRUCTURE_LEVELS == {"swing", "internal"}


# ---------------------------------------------------------------------------
# 合同 1：run 级来源（QM-62）—— 同 run calculatedAt / sourceRunId 一致
# ---------------------------------------------------------------------------


def test_run_calculated_at_is_generated_once_per_run() -> None:
    """[合同 1] 批任务入口必须只取一次时钟，供全 run 共享。

    通过源码守卫验证：三个批量入口都调用 _make_run_calculated_at()
    且把结果作为 run_calculated_at 传给单股计算。
    """
    import inspect

    from app.services import feature_snapshot_service as fss

    for fn in (
        fss.compute_review_core_batch_for_trade_date,
        fss.compute_review_core_with_run_items,
    ):
        src = inspect.getsource(fn)
        assert "_make_run_calculated_at()" in src, (
            f"{fn.__name__} 必须在入口取一次 run 级时间"
        )
        assert "run_calculated_at=run_calculated_at" in src, (
            f"{fn.__name__} 必须把 run 级时间传给单股计算"
        )
        # 单股函数内部不得各自取时钟
        assert src.count("_make_run_calculated_at()") == 1, (
            f"{fn.__name__} 只能取一次时钟"
        )


def test_single_stock_compute_does_not_take_its_own_clock() -> None:
    """单股计算函数不得自行取时钟（否则同 run 时间戳散落）。"""
    import inspect

    from app.services import feature_snapshot_service as fss

    src = inspect.getsource(fss.compute_review_core_for_trade_date)
    assert "_make_run_calculated_at()" not in src, (
        "单股计算不得自取时钟，必须由编排器注入 run_calculated_at"
    )
    assert 'first_pyramid_dict["sourceRunId"]' in src, (
        "单股计算必须注入 run 级 sourceRunId"
    )


@pytest.mark.asyncio
async def test_batch_entry_fails_fast_without_source_run_id() -> None:
    """[合同 1] 缺 sourceRunId 必须在批任务入口直接失败。

    否则会产出"一半有来源、一半没有来源"的快照。
    """
    import uuid as uuid_mod
    from datetime import date as date_mod
    from unittest.mock import AsyncMock

    from app.services.feature_snapshot_service import (
        compute_review_core_batch_for_trade_date,
    )

    with pytest.raises(ValueError, match=r"QM-62.*source_run_id"):
        await compute_review_core_batch_for_trade_date(
            AsyncMock(),
            date_mod(2026, 7, 31),
            [uuid_mod.uuid4(), uuid_mod.uuid4()],
            source_run_id=None,
        )


def test_snapshot_carries_source_run_id_field() -> None:
    """FirstPyramidSnapshot 必须承载 run 级来源字段。"""
    from app.schemas.first_pyramid import FirstPyramidSnapshot

    fields = FirstPyramidSnapshot.model_fields
    assert "sourceRunId" in fields, "快照必须含 sourceRunId"
    assert "calculatedAt" in fields, "快照必须含 calculatedAt"


# ---------------------------------------------------------------------------
# [字段级 availability 合同 2026-08-04] 条件性可空因子必须有字段级原因
# ---------------------------------------------------------------------------


def test_snapshot_carries_field_availability_field() -> None:
    """FirstPyramidSnapshot 必须承载 fieldAvailability（字段级原因元数据）。"""
    from app.schemas.first_pyramid import FirstPyramidSnapshot

    fields = FirstPyramidSnapshot.model_fields
    assert "fieldAvailability" in fields, "快照必须含 fieldAvailability"


def test_field_availability_reason_codes_canonical() -> None:
    """合法 reasonCode 必须覆盖 PRD 要求的 6 类。"""
    assert FIELD_AVAILABILITY_REASONS == {
        "not_applicable", "insufficient_history", "upstream_unavailable",
        "failed", "stale", "missing",
    }, f"reasonCode 集合不完整: {sorted(FIELD_AVAILABILITY_REASONS)}"


def test_field_availability_accepts_all_reason_codes() -> None:
    """六类 reasonCode 都必须能构造 FieldAvailability。"""
    for code in sorted(FIELD_AVAILABILITY_REASONS):
        fa = FieldAvailability(
            availability=code,
            reasonCode=code,
            reasonText="测试原因",
            observationCount=None,
            sourceRunId="run-1",
            calculatedAt="2026-08-04T15:00:00Z",
        )
        assert fa.reasonCode == code
        assert fa.sourceRunId == "run-1"


def test_field_availability_rejects_unknown_reason_code() -> None:
    """未知 reasonCode 必须被拒绝（禁止自造状态）。"""
    with pytest.raises(ValueError, match="reasonCode 非法"):
        FieldAvailability(
            availability="weird", reasonCode="weird", reasonText="x",
        )


def test_field_availability_builder_distinguishes_not_applicable() -> None:
    """无挤压状态 → squeeze_avg_volume/volume_relation 为 not_applicable。

    [字段级 availability 合同] 合法空值必须能区分"当前不适用"而非无原因缺失。
    """
    from app.schemas.first_pyramid import DimensionResult
    from app.services.first_pyramid_service import _build_field_availability

    momentum = DimensionResult(
        name="momentum",
        available=True,
        continuousFactors={"no_squeeze": True, "sqzmom_val": 1.2},
        events=[],
        statusText="无挤压",
    )
    avail = _build_field_availability(momentum)
    assert avail["momentum.squeeze_avg_volume"].reasonCode == "not_applicable"
    assert avail["momentum.volume_relation"].reasonCode == "not_applicable"
    # sqzmom_value 有值，不应标记
    assert "momentum.sqzmom_value" not in avail


def test_field_availability_builder_distinguishes_upstream_unavailable() -> None:
    """动量维度整体不可用 → 所有可空因子为 upstream_unavailable。"""
    from app.schemas.first_pyramid import DimensionResult
    from app.services.first_pyramid_service import _build_field_availability

    momentum = DimensionResult(
        name="momentum",
        available=True,
        continuousFactors={},  # 无上游连续因子
        events=[],
        statusText="无数据",
    )
    avail = _build_field_availability(momentum)
    assert avail["momentum.sqzmom_value"].reasonCode == "upstream_unavailable"


def test_field_availability_builder_distinguishes_missing() -> None:
    """挤压活跃但上游未提供 squeeze_period_volume_mean → missing。"""
    from app.schemas.first_pyramid import DimensionResult
    from app.services.first_pyramid_service import _build_field_availability

    momentum = DimensionResult(
        name="momentum",
        available=True,
        continuousFactors={
            "squeeze_on": True,
            "sqzmom_val": 1.2,
            "vol_divergence": "放量释放",
            # squeeze_period_volume_mean 缺失
        },
        events=[],
        statusText="挤压中",
    )
    avail = _build_field_availability(momentum)
    assert avail["momentum.squeeze_avg_volume"].reasonCode == "missing"
    assert "momentum.volume_relation" not in avail  # 有值，不应标记


def test_assemble_view_injects_field_availability() -> None:
    """assemble_first_pyramid_view 必须注入 fieldAvailability。"""
    from app.schemas.first_pyramid import DimensionResult, FirstPyramidCoreSnapshot
    from app.services.first_pyramid_service import assemble_first_pyramid_view

    trend = DimensionResult(name="trend", available=True, continuousFactors={"regime_value": 1}, events=[], statusText="上行")
    structure = DimensionResult(name="structure", available=True, continuousFactors={}, events=[], statusText="BOS")
    momentum = DimensionResult(name="momentum", available=True, continuousFactors={"no_squeeze": True}, events=[], statusText="无挤压")
    core = FirstPyramidCoreSnapshot(
        symbol="000001.SZ", tradeDate="2026-08-04",
        trend=trend, structure=structure, momentum=momentum,
        statusText="无挤压", inputHash="h", parameterHash="p", nBars=120, lastBarIndex=119,
    )
    snap = assemble_first_pyramid_view(core, None)
    assert snap.fieldAvailability, "快照必须携带 fieldAvailability"
    assert snap.fieldAvailability["momentum.squeeze_avg_volume"].reasonCode == "not_applicable"
