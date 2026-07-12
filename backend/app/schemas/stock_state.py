"""StockState / StateEvent / StateValue schemas + build_stock_state 纯函数。

PRD V1.1 §7.3 核心契约：
- StateValue = { code, label, value, unit, timeframe, sourceField }
  - code: 稳定机器码，用于事件比较（禁止比较中文 label）
  - label: 用户可读文案
  - sourceField: 来源字段名（管理员可见，用户接口可省略）
- StockState: 统一 Bar 因子、时序特征、DSA 为版本化状态向量
- StateEvent: 比较相邻有效状态生成幂等客观事件

V1.1 硬性规定：
1. MACD 只能来自真实 MACD 数据；当前无 MACD 计算，code=null, label="暂不可用"
2. SQZMOM 单独命名，不与 MACD 混淆
3. value_area_zone 只能叫"成交密集区关系"，Phase 5 前不得叫"筹码共识"
4. source_run_id 和 algorithm_version 来自真实快照 run，禁止硬编码
5. build_stock_state 使用 snapshot.trade_date 作为 as_of（真实 point-in-time）

用法：
    from app.schemas.stock_state import build_stock_state
    state = build_stock_state(snapshot, run, symbol="000001")

模块自测：
    python -m app.schemas.stock_state
"""

# ruff: noqa: N815 - camelCase 字段为前端 JSON API 契约（asOf/sourceRunId 等）

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field

from app.models.stock_feature_snapshot import StockFeatureSnapshot
from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun

# =============================================================================
# StateValue: code + label 分离的核心类型
# =============================================================================


class StateValue(BaseModel):
    """状态值 - code 与 label 分离。

    code: 稳定机器码，用于事件比较（禁止比较中文 label）
    label: 用户可读文案
    value: 原始数值（可选）
    unit: 单位（可选）
    timeframe: 来源周期
    sourceField: 来源字段名（管理员可见，用户接口返回 None）
    """

    code: str | None = Field(..., description="稳定机器码，用于事件比较")
    label: str = Field(..., description="用户可读文案")
    value: float | None = Field(None, description="原始数值")
    unit: str | None = Field(None, description="单位")
    timeframe: str = Field(..., description="来源周期")
    sourceField: str | None = Field(
        None, description="来源字段名（管理员可见，用户接口返回 None）"
    )


class Evidence(BaseModel):
    """证据项 - 用户可展开查看的可读指标。"""

    fieldName: str = Field(..., description="可读指标名")
    code: str = Field(..., description="稳定字段 code")
    currentValue: str | None = Field(None, description="当前值")
    previousValue: str | None = Field(None, description="对比值（可选）")
    unit: str | None = Field(None, description="单位")
    timeframe: str = Field(..., description="周期")


class StockStructure(BaseModel):
    """结构状态 - 趋势结构。"""

    price: StateValue = Field(..., description="价格结构状态")


class StockMomentum(BaseModel):
    """动量状态 - MACD + SQZMOM + 时序特征。"""

    macd: StateValue = Field(..., description="MACD（无真实 MACD 时 code=null）")
    sqzmom: StateValue = Field(..., description="SQZMOM（独立于 MACD）")
    temporal: list[StateValue] = Field(
        default_factory=list, description="时序特征状态列表"
    )


class StockVolatility(BaseModel):
    """波动状态 -布林带位置。"""

    bollPosition: StateValue = Field(..., description="布林带位置")


class StockState(BaseModel):
    """统一状态向量 - 版本化、可追溯。

    所有字段携带 asOf/sourceRunId/version/computedAt，
    确保状态可追溯到具体快照和算法版本。
    """

    symbol: str = Field(..., description="股票代码")
    asOf: str = Field(..., description="状态截止时间（trade_date ISO）")
    sourceRunId: str = Field(..., description="来源快照 run ID（真实，非硬编码）")
    version: str = Field(..., description="算法版本（来自 schema_version）")
    computedAt: str = Field(..., description="计算时间（ISO 8601 带时区）")
    structure: StockStructure = Field(..., description="结构状态")
    momentum: StockMomentum = Field(..., description="动量状态")
    volatility: StockVolatility = Field(..., description="波动状态")
    evidence: list[Evidence] = Field(default_factory=list, description="证据列表")
    degradedReasons: list[str] = Field(
        default_factory=list, description="降级原因（数据质量）"
    )


class StateEventDTO(BaseModel):
    """状态变化事件 DTO - 用户可读。

    V1.1: idempotencyKey 仅数据库/管理员可见，用户接口返回 None。
    """

    id: str = Field(..., description="事件 ID")
    symbol: str = Field(..., description="股票代码")
    occurredAt: str = Field(..., description="事件发生时间（ISO 8601）")
    eventType: str = Field(..., description="稳定事件类型")
    title: str = Field(..., description="事件标题")
    description: str = Field(..., description="事件描述")
    evidence: list[Evidence] = Field(default_factory=list, description="证据列表")
    changedFields: list[str] = Field(
        default_factory=list, description="全部变化字段列表（稳定 code 路径）"
    )
    previousAsOf: str | None = Field(None, description="前一状态截止时间")
    currentAsOf: str = Field(..., description="当前状态截止时间")
    idempotencyKey: str | None = Field(
        None, description="稳定幂等键（仅数据库/管理员可见，用户接口返回 None）"
    )


class StockContextDataQuality(BaseModel):
    """数据质量信息 - 含 reasonCode 解释空态原因。

    reasonCode 值：
    - null: 状态正常返回，有数据
    - no_published_full_run: 没有 succeeded+published+full 的 run
    - snapshot_missing: run 存在但该 instrument 没有快照（既无精确匹配也无 legacy 匹配）
    - snapshot_run_not_linked: 快照存在（legacy 匹配）但 source_run_id 为 NULL，需修复归属
    - legacy_snapshot_ambiguous: legacy 匹配到多个快照或 source_run_id 指向其他 run
    """

    hasSucceededRun: bool = Field(..., description="是否有 succeeded+published+full run")
    hasSnapshot: bool = Field(..., description="是否找到该 instrument 的快照")
    reasonCode: str | None = Field(None, description="空态原因码（state 非空时为 null）")
    degradedReasons: list[str] = Field(default_factory=list, description="降级原因")
    runTradeDate: str | None = Field(None, description="run 的 trade_date（ISO）")
    runPublishedAt: str | None = Field(None, description="run 的 published_at（ISO）")
    instrumentStatus: str | None = Field(None, description="instrument 状态")


class StockContextResponse(BaseModel):
    """GET /stocks/{symbol}/context 响应 - 只读。"""

    state: StockState | None = Field(None, description="当前状态（无数据时为 null）")
    events: list[StateEventDTO] = Field(
        default_factory=list, description="最近事件列表"
    )
    dataQuality: StockContextDataQuality = Field(
        ..., description="数据质量信息（含 reasonCode）"
    )


# =============================================================================
# build_stock_state: 纯函数，从 StockFeatureSnapshot 构建 StockState
# =============================================================================


def _safe_get(d: dict[str, Any] | None, key: str, default: Any = None) -> Any:
    """安全取值，d 为 None 或 key 不存在返回 default。"""
    if d is None:
        return default
    return d.get(key, default)


def _make_state_value(
    *,
    code: str | None,
    label: str,
    value: float | None,
    unit: str | None,
    timeframe: str,
    source_field: str | None = None,
) -> StateValue:
    """构造 StateValue。

    source_field 默认 None（用户接口）；管理员路径可显式传入。
    """
    return StateValue(
        code=code,
        label=label,
        value=value,
        unit=unit,
        timeframe=timeframe,
        sourceField=source_field,
    )


def _build_price_structure(
    primary_factors: dict[str, Any], timeframe: str
) -> StateValue:
    """从 swing_position 构建 structure.price。

    使用 confirmed_swing_breakout_state 作为稳定 code。
    """
    swing = _safe_get(primary_factors, "swing_position", {})
    breakout_state = _safe_get(swing, "confirmed_swing_breakout_state")
    price_position = _safe_get(swing, "price_position_in_swing_0_1")

    label_map = {
        "above_confirmed_high": "突破确认高点",
        "below_confirmed_low": "跌破确认低点",
        "inside": "确认区间内",
    }
    label = label_map.get(breakout_state, "结构数据不足")

    return _make_state_value(
        code=breakout_state,
        label=label,
        value=price_position,
        unit=None,
        timeframe=timeframe,
        source_field="swing_position.confirmed_swing_breakout_state",
    )


def _build_macd(
    primary_factors: dict[str, Any], timeframe: str
) -> StateValue:
    """C6: 从 macd_state 构建真实 MACD 紧凑状态。

    V1.1: MACD 只能来自真实 MACD 数据。禁止用 SQZMOM 冒充 MACD。
    """
    macd_state = _safe_get(primary_factors, "macd_state", {})
    code = _safe_get(macd_state, "code")

    label_map = {
        "bullish_above": "MACD 多头增强",
        "bullish_below": "MACD 多头减弱",
        "bearish_below": "MACD 空头增强",
        "bearish_above": "MACD 空头减弱",
    }
    label = label_map.get(code, "MACD 数据不足")

    return _make_state_value(
        code=code,
        label=label,
        value=_safe_get(macd_state, "histogram"),
        unit=None,
        timeframe=timeframe,
        source_field="macd_state",
    )


def _build_sqzmom(
    primary_factors: dict[str, Any], timeframe: str
) -> StateValue:
    """从 volatility_momentum.sqzmom_val 构建 momentum.sqzmom。

    V1.1: SQZMOM 独立命名，不与 MACD 混淆。
    """
    vol_mom = _safe_get(primary_factors, "volatility_momentum", {})
    sqzmom_val = _safe_get(vol_mom, "sqzmom_val")

    code = "positive" if (sqzmom_val is not None and sqzmom_val > 0) else (
        "negative" if (sqzmom_val is not None and sqzmom_val < 0) else None
    )
    label = "动量为正" if code == "positive" else (
        "动量为负" if code == "negative" else "SQZMOM 数据不足"
    )

    return _make_state_value(
        code=code,
        label=label,
        value=sqzmom_val,
        unit=None,
        timeframe=timeframe,
        source_field="volatility_momentum.sqzmom_val",
    )


def _build_boll_position(
    primary_factors: dict[str, Any], timeframe: str
) -> StateValue:
    """从 volatility_momentum.bb_percent_b 构建 volatility.bollPosition。"""
    vol_mom = _safe_get(primary_factors, "volatility_momentum", {})
    bb_pct_b = _safe_get(vol_mom, "bb_percent_b")

    if bb_pct_b is None:
        code = None
        label = "布林带数据不足"
    elif bb_pct_b > 1.0:
        code = "above_upper"
        label = "突破上轨"
    elif bb_pct_b < 0.0:
        code = "below_lower"
        label = "跌破下轨"
    elif bb_pct_b > 0.8:
        code = "near_upper"
        label = "接近上轨"
    elif bb_pct_b < 0.2:
        code = "near_lower"
        label = "接近下轨"
    else:
        code = "middle"
        label = "中轨附近"

    return _make_state_value(
        code=code,
        label=label,
        value=bb_pct_b,
        unit=None,
        timeframe=timeframe,
        source_field="volatility_momentum.bb_percent_b",
    )


def _build_temporal_states(
    temporal_payload: dict[str, Any], timeframe: str
) -> list[StateValue]:
    """从 temporal_payload 构建 momentum.temporal 列表。"""
    daily = _safe_get(temporal_payload, "daily_context", {})
    derived = _safe_get(temporal_payload, "derived_relation", {})
    result: list[StateValue] = []

    # daily_dsa_dir
    dsa_dir = _safe_get(daily, "daily_dsa_dir")
    dir_code = str(dsa_dir) if dsa_dir is not None else None
    dir_label = "上升趋势段" if dsa_dir == 1 else (
        "下降趋势段" if dsa_dir == -1 else "DSA 方向数据不足"
    )
    result.append(_make_state_value(
        code=dir_code,
        label=dir_label,
        value=dsa_dir,
        unit=None,
        timeframe=timeframe,
        source_field="daily_context.daily_dsa_dir",
    ))

    # trend_alignment
    alignment = _safe_get(derived, "m15_response_direction_relative_to_daily")
    result.append(_make_state_value(
        code=alignment,
        label=alignment or "响应方向数据不足",
        value=None,
        unit=None,
        timeframe=timeframe,
        source_field="derived_relation.m15_response_direction_relative_to_daily",
    ))

    return result


def _build_evidence(
    primary_factors: dict[str, Any],
    temporal_payload: dict[str, Any],
    timeframe: str,
) -> list[Evidence]:
    """构建证据列表 - 用户可展开查看可读指标名、值、周期。"""
    evidence: list[Evidence] = []
    swing = _safe_get(primary_factors, "swing_position", {})
    cost = _safe_get(primary_factors, "cost_position", {})
    vol_mom = _safe_get(primary_factors, "volatility_momentum", {})
    participation = _safe_get(primary_factors, "participation", {})

    # 价格结构证据
    sh = _safe_get(swing, "confirmed_swing_high")
    sl = _safe_get(swing, "confirmed_swing_low")
    if sh is not None:
        evidence.append(Evidence(
            fieldName="确认高点",
            code="confirmed_swing_high",
            currentValue=f"{sh:.2f}" if isinstance(sh, (int, float)) else str(sh),
            unit="元",
            timeframe=timeframe,
        ))
    if sl is not None:
        evidence.append(Evidence(
            fieldName="确认低点",
            code="confirmed_swing_low",
            currentValue=f"{sl:.2f}" if isinstance(sl, (int, float)) else str(sl),
            unit="元",
            timeframe=timeframe,
        ))

    # 成本位置证据
    poc = _safe_get(cost, "poc_price")
    if poc is not None:
        evidence.append(Evidence(
            fieldName="成交密集点",
            code="poc_price",
            currentValue=f"{poc:.2f}" if isinstance(poc, (int, float)) else str(poc),
            unit="元",
            timeframe=timeframe,
        ))

    # SQZMOM 证据
    sqzmom_val = _safe_get(vol_mom, "sqzmom_val")
    if sqzmom_val is not None:
        evidence.append(Evidence(
            fieldName="SQZMOM",
            code="sqzmom_val",
            currentValue=f"{sqzmom_val:.4f}" if isinstance(sqzmom_val, (int, float)) else str(sqzmom_val),
            unit=None,
            timeframe=timeframe,
        ))

    # 布林带证据
    bb_pct_b = _safe_get(vol_mom, "bb_percent_b")
    if bb_pct_b is not None:
        evidence.append(Evidence(
            fieldName="布林带位置",
            code="bb_percent_b",
            currentValue=f"{bb_pct_b:.2f}" if isinstance(bb_pct_b, (int, float)) else str(bb_pct_b),
            unit=None,
            timeframe=timeframe,
        ))

    # 成交量证据
    vol_pct = _safe_get(participation, "volume_percentile_120")
    if vol_pct is not None:
        evidence.append(Evidence(
            fieldName="成交量百分位",
            code="volume_percentile_120",
            currentValue=f"{vol_pct:.1%}" if isinstance(vol_pct, (int, float)) else str(vol_pct),
            unit=None,
            timeframe=timeframe,
        ))

    return evidence


def build_stock_state(
    snapshot: StockFeatureSnapshot,
    run: StockFeatureSnapshotRun | None,
    symbol: str,
) -> StockState:
    """从 StockFeatureSnapshot 构建 StockState（纯函数，无副作用）。

    V1.1 核心实现：
    - as_of 来自 snapshot.trade_date（真实 point-in-time）
    - source_run_id 来自 run.id（真实快照 run，禁止硬编码）
    - algorithm_version 来自 run.schema_version（真实算法版本）
    - MACD code=null（无真实 MACD 计算）
    - SQZMOM 独立命名

    Args:
        snapshot: 特征快照 ORM 对象
        run: 特征快照 run ORM 对象（可为 None，此时 source_run_id="unknown"）
        symbol: 股票代码

    Returns:
        StockState DTO
    """
    primary_tf = snapshot.primary_timeframe
    structural = snapshot.structural_payload or {}
    temporal = snapshot.temporal_payload or {}
    primary_factors = _safe_get(structural, "primary", {}).get(primary_tf, {})
    degraded = snapshot.degraded_reasons or []

    # 构建各状态字段
    price = _build_price_structure(primary_factors, primary_tf)
    macd = _build_macd(primary_factors, primary_tf)
    sqzmom = _build_sqzmom(primary_factors, primary_tf)
    boll = _build_boll_position(primary_factors, primary_tf)
    temporal_states = _build_temporal_states(temporal, primary_tf)
    evidence = _build_evidence(primary_factors, temporal, primary_tf)

    # source_run_id 和 version 来自真实 run
    source_run_id = str(run.id) if run is not None else "unknown"
    algorithm_version = f"v{run.schema_version}" if run is not None else "unknown"

    # computed_at 来自快照的 source_primary_bar_time 或 updated_at
    computed_at = (
        snapshot.source_primary_bar_time.isoformat()
        if snapshot.source_primary_bar_time is not None
        else snapshot.updated_at.isoformat()
    )

    return StockState(
        symbol=symbol,
        asOf=snapshot.trade_date.isoformat(),
        sourceRunId=source_run_id,
        version=algorithm_version,
        computedAt=computed_at,
        structure=StockStructure(price=price),
        momentum=StockMomentum(
            macd=macd, sqzmom=sqzmom, temporal=temporal_states
        ),
        volatility=StockVolatility(bollPosition=boll),
        evidence=evidence,
        degradedReasons=degraded,
    )


def strip_internal_fields_for_user(
    state: StockState | None,
    events: list[StateEventDTO],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """剥离用户接口不应返回的内部字段（PRD V1.1 §7.3）。

    - StateValue.sourceField 完全排除（不是 null，是字段不存在）
    - StateEventDTO.idempotencyKey 完全排除（不是 null，是字段不存在）

    返回 dict 而非 Pydantic 模型，确保 JSON 序列化后字段完全消失。
    管理员路径保留完整 Pydantic 模型（含 sourceField/idempotencyKey）。
    """
    if state is not None:
        state_dict = state.model_dump()
        # 递归移除所有 sourceField 键
        _remove_field(state_dict, "sourceField")
    else:
        state_dict = None

    event_dicts: list[dict[str, Any]] = []
    for e in events:
        ed = e.model_dump()
        ed.pop("idempotencyKey", None)
        event_dicts.append(ed)

    return state_dict, event_dicts


def _remove_field(obj: Any, field: str) -> None:
    """递归移除 dict 中指定字段（原地修改）。"""
    if isinstance(obj, dict):
        obj.pop(field, None)
        for v in obj.values():
            _remove_field(v, field)
    elif isinstance(obj, list):
        for item in obj:
            _remove_field(item, field)


# =============================================================================
# 模块自测
# =============================================================================

if __name__ == "__main__":
    from datetime import UTC, date, datetime
    from uuid import uuid4

    print("stock_state schema 自测...")

    # 构造 mock snapshot
    mock_snapshot = StockFeatureSnapshot(
        instrument_id=uuid4(),
        trade_date=date(2026, 7, 10),
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
                        "confirmed_swing_breakout_state": "inside",
                        "price_position_in_swing_0_1": 0.5,
                        "confirmed_swing_high": 10.5,
                        "confirmed_swing_low": 9.5,
                    },
                    "cost_position": {
                        "value_area_zone": "inside_va",
                        "value_area_position_0_1": 0.5,
                        "poc_price": 10.0,
                    },
                    "volatility_momentum": {
                        "sqzmom_val": 0.001,
                        "bb_percent_b": 0.5,
                    },
                    "participation": {
                        "volume_percentile_120": 0.3,
                    },
                    # C6: 真实 MACD 紧凑状态
                    "macd_state": {
                        "code": "bullish_above",
                        "macd_val": 0.15,
                        "signal_val": 0.10,
                        "histogram": 0.05,
                    },
                }
            }
        },
        temporal_payload={
            "daily_context": {"daily_dsa_dir": 1},
            "derived_relation": {
                "m15_response_direction_relative_to_daily": "aligned"
            },
        },
        summary_payload={},
        degraded_reasons=[],
    )
    mock_run = StockFeatureSnapshotRun(
        id=uuid4(),
        trade_date=date(2026, 7, 10),
        schema_version=1,
        primary_timeframe="1d",
        secondary_timeframe="15m",
        adj="qfq",
        run_type="after_close",
        status="succeeded",
    )

    state = build_stock_state(mock_snapshot, mock_run, symbol="000001")

    # 验证 V1.1 核心契约
    assert state.symbol == "000001"
    assert state.asOf == "2026-07-10"
    assert state.sourceRunId == str(mock_run.id), "source_run_id 必须来自真实 run"
    assert state.version == "v1", "version 必须来自 schema_version"

    # C6: MACD 来自真实 macd_state（不再 code=null）
    assert state.momentum.macd.code == "bullish_above", "MACD code 必须来自真实 macd_state"
    assert "MACD" in state.momentum.macd.label

    # SQZMOM 独立命名
    assert state.momentum.sqzmom.code == "positive"
    assert state.momentum.sqzmom.sourceField == "volatility_momentum.sqzmom_val"

    # 证据列表
    assert len(state.evidence) > 0
    print(f"evidence count: {len(state.evidence)}")

    print("OK: build_stock_state 验证通过")
