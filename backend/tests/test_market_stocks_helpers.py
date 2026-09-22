"""market_stocks_service 纯单元测试（不连接数据库）。

测试内容（CHANGE-20260729-009 + CHANGE-20260730-010 + PANJI-INTRADAY-DIRECT-SOURCE）：
1. _compute_factor_ready：各种输入场景的返回值
   - flat_fp=None + daily_bar_count 不同值 → INSUFFICIENT_DAILY_BARS / COMPUTE_FAILED / no_snapshot
   - flat_fp 存在但维度缺失 → trend_missing / structure_missing / momentum_missing
   - 全部维度就绪 → (True, None, None, None)
2. chip 状态：
   - **[PANJI-INTRADAY-DIRECT-SOURCE]** 生产入口 resolve_chip_status 恒 retired /
     CHIP_PIPELINE_RETIRED（不再读 legacy chip 表）；
   - 退役前的「chip row → ChipStatus」映射（succeeded/skipped/failed 各态）保留在
     chip_status_resolver._build_chip_status_from_row，仅审计路径使用，继续覆盖；
   - `market_stocks_service._build_chip_status_struct` 已随列表读链退役删除。
3. chip LATERAL 永久移除：_needs_chip_lateral 恒 False。
4. _MIN_DAILY_BARS_FOR_FACTOR / _CHIP_MIN_15M_BARS 常量值正确

运行方式：
    PURE_UNIT_TEST=1 PYTHONDONTWRITEBYTECODE=1 pytest -p no:cacheprovider tests/test_market_stocks_helpers.py -v
"""

from __future__ import annotations

import uuid
import warnings
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import SAWarning

from app.schemas.first_pyramid import ChipStatus
from app.services.chip_status_resolver import (
    _build_chip_status_from_row,
    build_retired_chip_status,
    resolve_chip_status,
)
from app.services.first_pyramid_flatten import FpFilterSpec, FpSortSpec
from app.services.market_stocks_service import (
    _CHIP_MIN_15M_BARS,
    _FP_MOMENTUM_KEYS,
    _FP_STRUCTURE_KEYS,
    _FP_TREND_KEYS,
    _MIN_DAILY_BARS_FOR_FACTOR,
    _build_fp_filter_conditions,
    _build_fp_value_expr,
    _compute_factor_ready,
    _needs_chip_lateral,
)

# ===== _compute_factor_ready 测试 =====


class TestComputeFactorReady_NoneFlatFp:
    """flat_fp=None 时的各种场景。"""

    def test_none_no_bar_count_returns_no_snapshot(self):
        """flat_fp=None + 无日线计数 → no_snapshot。"""
        ready, error, actual, required = _compute_factor_ready(None)
        assert ready is False
        assert error == "no_snapshot"
        assert actual is None
        assert required is None

    def test_none_zero_bars_returns_insufficient(self):
        """flat_fp=None + 0 日线 → INSUFFICIENT_DAILY_BARS。"""
        ready, error, actual, required = _compute_factor_ready(None, daily_bar_count=0)
        assert ready is False
        assert error == "INSUFFICIENT_DAILY_BARS"
        assert actual == 0
        assert required == _MIN_DAILY_BARS_FOR_FACTOR

    def test_none_below_threshold_returns_insufficient(self):
        """flat_fp=None + 日线 < 60 → INSUFFICIENT_DAILY_BARS。"""
        ready, error, actual, required = _compute_factor_ready(None, daily_bar_count=45)
        assert ready is False
        assert error == "INSUFFICIENT_DAILY_BARS"
        assert actual == 45
        assert required == 60

    def test_none_just_below_threshold_returns_insufficient(self):
        """flat_fp=None + 日线 = 59 → INSUFFICIENT_DAILY_BARS。"""
        ready, error, actual, required = _compute_factor_ready(None, daily_bar_count=59)
        assert ready is False
        assert error == "INSUFFICIENT_DAILY_BARS"
        assert actual == 59
        assert required == 60

    def test_none_at_threshold_returns_compute_failed(self):
        """flat_fp=None + 日线 = 60 → COMPUTE_FAILED（不是 INSUFFICIENT_DAILY_BARS）。"""
        ready, error, actual, required = _compute_factor_ready(None, daily_bar_count=60)
        assert ready is False
        assert error == "COMPUTE_FAILED"
        assert actual == 60
        assert required == 60

    def test_none_above_threshold_returns_compute_failed(self):
        """flat_fp=None + 日线 > 60 → COMPUTE_FAILED（有数据但计算仍失败）。"""
        ready, error, actual, required = _compute_factor_ready(None, daily_bar_count=200)
        assert ready is False
        assert error == "COMPUTE_FAILED"
        assert actual == 200
        assert required == 60


class TestComputeFactorReady_DimensionsMissing:
    """flat_fp 存在但维度缺失。"""

    def test_all_dimensions_present_returns_ready(self):
        """三维度均有权威字段 → factor_ready=True。"""
        flat = {
            "fp_trend_direction": "up",
            "fp_swing_direction": "bull",
            "fp_sqzmom_value": 0.5,
        }
        ready, error, actual, required = _compute_factor_ready(flat)
        assert ready is True
        assert error is None
        assert actual is None
        assert required is None

    def test_trend_missing(self):
        """趋势维度全 None → trend_missing。"""
        flat = {
            "fp_trend_direction": None,
            "fp_trend_bars": None,
            "fp_swing_direction": "bull",
            "fp_sqzmom_value": 0.5,
        }
        ready, error, actual, required = _compute_factor_ready(flat)
        assert ready is False
        assert error == "trend_missing"

    def test_structure_missing(self):
        """结构维度全 None → structure_missing。"""
        flat = {
            "fp_trend_direction": "up",
            "fp_swing_direction": None,
            "fp_structure_alignment": None,
            "fp_sqzmom_value": 0.5,
        }
        ready, error, actual, required = _compute_factor_ready(flat)
        assert ready is False
        assert error == "structure_missing"

    def test_momentum_missing(self):
        """动量维度全 None → momentum_missing。"""
        flat = {
            "fp_trend_direction": "up",
            "fp_swing_direction": "bull",
            "fp_sqzmom_value": None,
            "fp_momentum_direction": None,
            "fp_squeeze_state": None,
        }
        ready, error, actual, required = _compute_factor_ready(flat)
        assert ready is False
        assert error == "momentum_missing"

    def test_trend_partial_presence_counts_as_ready(self):
        """趋势维度有一个字段非空即视为就绪。"""
        flat = {
            "fp_trend_direction": None,
            "fp_trend_bars": 55,
            "fp_swing_direction": "bull",
            "fp_sqzmom_value": 0.5,
        }
        ready, error, _, _ = _compute_factor_ready(flat)
        assert ready is True


class TestComputeFactorReady_Constants:
    """验证常量值。"""

    def test_min_daily_bars_is_60(self):
        assert _MIN_DAILY_BARS_FOR_FACTOR == 60

    def test_trend_keys(self):
        assert _FP_TREND_KEYS == ("fp_trend_direction", "fp_trend_bars")

    def test_structure_keys(self):
        assert _FP_STRUCTURE_KEYS == ("fp_swing_direction", "fp_structure_alignment")

    def test_momentum_keys(self):
        assert _FP_MOMENTUM_KEYS == ("fp_sqzmom_value", "fp_momentum_direction", "fp_squeeze_state")


# ===== 生产 chip 读链退役（PANJI-INTRADAY-DIRECT-SOURCE） =====


class TestProductionChipStatusIsRetired:
    """生产入口恒定 retired：不再读 stock_chip_consensus_snapshots。"""

    def test_build_retired_chip_status_contract(self):
        st = build_retired_chip_status()
        assert st.state == "retired"
        assert st.reasonCode == "CHIP_PIPELINE_RETIRED"
        assert st.computedAt is None
        # retired ≠ pending：pending 会被读成「以后还会算」
        assert st.state != "pending"
        assert st.reasonCode != "CHIP_JOB_PENDING"

    @pytest.mark.asyncio
    async def test_resolve_chip_status_returns_retired_without_db_read(self):
        """resolve_chip_status 不得触碰 session（传 None 也必须工作）。"""
        st = await resolve_chip_status(
            None,  # type: ignore[arg-type]  # 退役实现不应使用 session
            uuid.uuid4(),
            date(2026, 9, 22),
            uuid.uuid4(),
        )
        assert st.state == "retired"
        assert st.reasonCode == "CHIP_PIPELINE_RETIRED"

    def test_chip_status_schema_accepts_retired(self):
        from app.schemas.first_pyramid import (
            CHIP_STATUS_NOT_READY_STATES,
            CHIP_STATUS_REASON_CODES,
            CHIP_STATUS_STATES,
        )

        assert "retired" in CHIP_STATUS_STATES
        assert "retired" in CHIP_STATUS_NOT_READY_STATES
        assert "CHIP_PIPELINE_RETIRED" in CHIP_STATUS_REASON_CODES


class TestChipLateralPermanentlyRemoved:
    """_needs_chip_lateral 恒 False：无论 filter/sort 是否命中 chip 字段。"""

    def test_no_specs(self):
        assert _needs_chip_lateral([], None) is False

    def test_chip_source_field_does_not_trigger_lateral(self):
        spec = FpFilterSpec(fp_key="fp_poc_price", operator="gte", value="10", value2=None)
        assert _needs_chip_lateral([spec], None) is False
        assert _needs_chip_lateral([], FpSortSpec(fp_key="fp_poc_price", direction="desc")) is False

    def test_chip_available_computed_does_not_trigger_lateral(self):
        spec = FpFilterSpec(fp_key="fp_chip_available", operator="eq", value="true", value2=None)
        assert _needs_chip_lateral([spec], None) is False


class TestChipSourceFieldsRenderNullPlaceholder:
    """chip 源字段在 SQL 表达式层恒定 NULL / False（§十五 字段 schema 保留但无真实取值路径）。

    这是「旧 chip 行不可能出现在列表响应」的**最直接纯单元证据**：
    chip 源字段没有 chip LATERAL 可引用（chip_subq 恒 None），因此只能渲染 NULL 占位；
    fp_chip_available 是 computed，恒定渲染 false 常量。
    """

    # 10 个 chip 源键的 data_type 只有 text/number/percent/enum 四种
    _CHIP_KEYS = (
        ("fp_chip_state", "text"),
        ("fp_poc_price", "number"),
        ("fp_poc_distance_pct", "percent"),
        ("fp_peak_node_count", "number"),
        ("fp_vah_price", "number"),
        ("fp_val_price", "number"),
        ("fp_node_event_type", "enum"),
        ("fp_node_event_direction", "enum"),
        ("fp_node_event_freshness", "number"),
        ("fp_node_event_price", "number"),
    )

    def test_all_chip_source_fields_render_null(self):
        """chip 源字段（chip_subq=None）必须渲染为 SQL NULL，且不得抛错。"""
        for fp_key, data_type in self._CHIP_KEYS:
            expr = _build_fp_value_expr(fp_key, snap_subq=None, chip_subq=None)
            assert expr is not None, fp_key
            compiled = str(expr.compile(compile_kwargs={"literal_binds": True}))
            assert compiled.upper() == "NULL", (
                f"{fp_key} (data_type={data_type}) 应渲染 NULL 占位，实际 {compiled!r}"
            )

    def test_chip_source_field_spec_is_still_chip(self):
        """字段 schema 不变：这 10 个键仍是 source=chip（只停读，不改字段合同）。"""
        from app.services.first_pyramid_flatten import FP_QUERY_FIELD_SPECS

        for fp_key, data_type in self._CHIP_KEYS:
            spec = FP_QUERY_FIELD_SPECS[fp_key]
            assert spec["source"] == "chip", fp_key
            assert spec["data_type"] == data_type, fp_key

    def test_chip_available_renders_false_constant(self):
        """fp_chip_available（computed/chip_available）恒定渲染 false。"""
        expr = _build_fp_value_expr("fp_chip_available", snap_subq=None, chip_subq=None)
        compiled = str(expr.compile(compile_kwargs={"literal_binds": True})).lower()
        assert "true" not in compiled, f"fp_chip_available 不得为 true: {compiled!r}"
        assert "false" in compiled or compiled == "0", (
            f"fp_chip_available 应为 false 常量，实际 {compiled!r}"
        )

    def test_chip_filter_conditions_still_sql_valid(self):
        """chip 字段参与 filter 时表达式仍须是合法 WHERE 条件（不抛错、不命中）。"""
        specs = [
            FpFilterSpec(fp_key="fp_poc_price", operator="gte", value="10", value2=None),
            FpFilterSpec(fp_key="fp_chip_available", operator="eq", value="true", value2=None),
        ]
        conds = _build_fp_filter_conditions(
            specs, snap_subq=None, chip_subq=None, max_trade_date_subq=None,
        )
        assert len(conds) == len(specs)
        # chip 值是 NULL 占位 ⇒ 条件里必然出现 NULL 比较，SQLAlchemy 会为此发
        # SAWarning（comparisons to NULL should use IS）。这是**预期**行为：NULL 比较
        # 结果为 NULL ⇒ 不命中，正是「无 chip 数据」的正确筛选语义。此处局部静音。
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SAWarning)
            for c in conds:
                assert str(c.compile(compile_kwargs={"literal_binds": True}))


# ===== chip row → ChipStatus 映射（退役前实现，仅审计路径） =====


def _make_chip_row(
    status: str,
    chip_payload: dict | None = None,
    error_message: str | None = None,
    created_at: datetime | None = None,
) -> SimpleNamespace:
    """构造模拟 chip row（NamedTuple 替代）。"""
    return SimpleNamespace(
        status=status,
        chip_payload=chip_payload,
        error_message=error_message,
        created_at=created_at,
    )


class TestLegacyChipStatusRowMapping:
    """chip_status_resolver._build_chip_status_from_row（原 _build_chip_status_struct 的唯一实现）。

    ⚠️ [PANJI-INTRADAY-DIRECT-SOURCE] 本映射已不在生产读链上：生产入口
    resolve_chip_status 恒返回 retired（见 TestProductionChipStatusIsRetired）。
    这里继续覆盖，是因为它仍是 ``resolve_legacy_chip_status`` 的实现，
    审计/历史工具会用到；合同不变（camelCase，与详情 API 同口径的历史语义）。
    """

    def test_none_would_be_pending_in_legacy_path(self):
        """legacy 路径无 chip 记录的历史语义是 pending（保留记录，避免与 retired 混淆）。"""
        legacy_pending = ChipStatus(
            state="pending",
            reasonCode="CHIP_JOB_PENDING",
            reasonText="筹码任务尚未执行",
            computedAt=None,
        )
        assert legacy_pending.state == "pending"
        # 生产口径必须是 retired，不能是 pending
        assert resolve_chip_status is not None
        assert build_retired_chip_status().state == "retired"

    def test_succeeded_with_available_chip(self):
        """succeeded + chip.available=True → state=ready。"""
        row = _make_chip_row(
            "succeeded",
            chip_payload={"chip": {"available": True, "poc_price": 10.5}},
        )
        result = _build_chip_status_from_row(row)
        assert result.state == "ready"
        assert result.reasonCode is None
        assert result.reasonText == "已计算"
        assert result.requiredBars is None

    def test_succeeded_with_unavailable_chip(self):
        """succeeded + chip.available=False → state=unavailable, NO_VALID_PEAK。"""
        row = _make_chip_row(
            "succeeded",
            chip_payload={"chip": {"available": False}},
        )
        result = _build_chip_status_from_row(row)
        assert result.state == "unavailable"
        assert result.reasonCode == "NO_VALID_PEAK"

    def test_skipped_m15_insufficient_from_payload(self):
        """skipped + payload.reason=M15_BARS_INSUFFICIENT → 结构化状态。"""
        row = _make_chip_row(
            "skipped",
            chip_payload={"reason": "M15_BARS_INSUFFICIENT", "actual_bars": 354},
            error_message="15m bars insufficient: 354 < 500",
        )
        result = _build_chip_status_from_row(row)
        assert result.state == "unavailable"
        assert result.reasonCode == "M15_BARS_INSUFFICIENT"
        assert result.actualBars == 354
        assert result.requiredBars == _CHIP_MIN_15M_BARS
        assert result.fullQualityBars == 4000
        assert "15分钟数据不足" in result.reasonText
        assert "354" in result.reasonText
        assert "4000" in result.reasonText

    def test_skipped_m15_insufficient_from_error_message(self):
        """skipped + 无 payload.reason 但 error_message 含 '15m' → M15_BARS_INSUFFICIENT。"""
        row = _make_chip_row(
            "skipped",
            chip_payload=None,
            error_message="15m bars insufficient: 200 < 500",
        )
        result = _build_chip_status_from_row(row)
        assert result.reasonCode == "M15_BARS_INSUFFICIENT"
        assert result.requiredBars == _CHIP_MIN_15M_BARS

    def test_skipped_other_reason(self):
        """skipped + 非 15m 原因 → 通用 unavailable + 原始 reasonCode。"""
        row = _make_chip_row(
            "skipped",
            chip_payload={"reason": "OTHER_REASON"},
            error_message="some other reason",
        )
        result = _build_chip_status_from_row(row)
        assert result.state == "unavailable"
        assert result.reasonCode == "OTHER_REASON"
        assert result.reasonText == "some other reason"

    def test_failed(self):
        """failed 状态 → CHIP_JOB_FAILED。"""
        row = _make_chip_row(
            "failed",
            chip_payload=None,
            error_message="compute error: division by zero",
        )
        result = _build_chip_status_from_row(row)
        assert result.state == "failed"
        assert result.reasonCode == "CHIP_JOB_FAILED"
        assert result.reasonText == "compute error: division by zero"

    def test_failed_no_error_message(self):
        """failed + 无 error_message → 通用失败文本。"""
        row = _make_chip_row("failed", chip_payload=None, error_message=None)
        result = _build_chip_status_from_row(row)
        assert result.state == "failed"
        assert result.reasonText == "计算失败"

    def test_created_at_conversion(self):
        """created_at 正确转换为 ISO 字符串（computedAt）。"""
        dt = datetime(2026, 7, 29, 15, 30, 0, tzinfo=UTC)
        row = _make_chip_row("succeeded", chip_payload={"chip": {"available": True}}, error_message=None, created_at=dt)
        result = _build_chip_status_from_row(row)
        assert result.computedAt is not None
        assert "2026-07-29" in result.computedAt

    def test_no_created_at(self):
        """created_at=None → computedAt=None。"""
        row = _make_chip_row("succeeded", chip_payload={"chip": {"available": True}}, error_message=None, created_at=None)
        result = _build_chip_status_from_row(row)
        assert result.computedAt is None


class TestChipMinBarsConstant:
    """验证 chip 门槛常量。"""

    def test_chip_min_15m_bars_is_500(self):
        assert _CHIP_MIN_15M_BARS == 500
