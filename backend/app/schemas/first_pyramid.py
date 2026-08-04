"""第一金字塔统一快照 Schema（Phase 5B-1 + Gate1 收口）。

PRD20 QM-01 要求个股状态按固定顺序组织：
    趋势 > 结构 > 动量 > 筹码共识

本模块定义唯一权威 DTO `FirstPyramidSnapshot`，单股详情、批量计算、行情列表
和盘后计算层必须复用此结构，不得各自重新拼字段。

约束：
- `ordered_dimensions` 固定为 `["trend", "structure", "momentum", "chip_consensus"]`
- 前三维（trend/structure/momentum）必选；任一缺失必须抛 `ValueError`，不得静默伪造
- `chip_consensus` 允许 `None`（PRD20 QM-40 可选定位）
- 每个事件必须含 `type`、`freshness_bars`；其余字段视事件类型可空
- Gate1：统一 VolumeContext 被趋势/结构/动量复用，禁止各维度重复计算

用法：
    from app.schemas.first_pyramid import FirstPyramidSnapshot
    from app.services.first_pyramid_service import compute_first_pyramid_snapshot

    snapshot = compute_first_pyramid_snapshot(bars, symbol="000001.SZ")
    payload = snapshot.to_dict()

模块自测：
    python -m app.schemas.first_pyramid
"""

from __future__ import annotations

# ruff: noqa: N815 - camelCase 字段为前端 JSON API 契约（tradeDate/inputHash 等）
from typing import Any

from pydantic import BaseModel, Field, model_validator

# 固定维度顺序（PRD20 QM-01；禁止页面或前端动态组合）
ORDERED_DIMENSIONS: tuple[str, ...] = ("trend", "structure", "momentum", "chip_consensus")

# 必选维度（前三维）
REQUIRED_DIMENSIONS: tuple[str, ...] = ("trend", "structure", "momentum")

# 可选维度
OPTIONAL_DIMENSIONS: tuple[str, ...] = ("chip_consensus",)

# 算法版本（每次算法或契约变更时递增）
# [P0-5 修复 2026-07-29] 升级版本：DSA mean/mean、SMC OB 三事件、SQZ_RELEASE 方向、
# regime_strength 修正、history 逐 bar readiness、core/chip 拆分
# 旧版本 "1.1.0-gate1-volume-context" 快照不可混用
FIRST_PYRAMID_ALGORITHM_VERSION = "2.0.0-20260729-history-ssot"


class VolumeContextSchema(BaseModel):
    """统一量能上下文（Gate1：计算一次，趋势/结构/动量复用）。

    窗口样本不足时各指标为 None，readiness=False。
    """

    volume: float | None = Field(None, description="当日成交量")
    amount: float | None = Field(None, description="当日成交额")
    turnoverRate: float | None = Field(None, description="当日换手率")
    volumeMa20: float | None = Field(None, description="20日平均成交量")
    volumeMa200: float | None = Field(None, description="200日平均成交量")
    volumeRatio20: float | None = Field(None, description="当前量 / 20日均量")
    volumeRatio200: float | None = Field(None, description="当前量 / 200日均量")
    volumePercentile20: float | None = Field(None, description="20日百分位（0-100）")
    volumePercentile200: float | None = Field(None, description="200日百分位（0-100）")
    volumeZscore20: float | None = Field(None, description="20日 z-score")
    volumeZscore200: float | None = Field(None, description="200日 z-score")
    readiness: bool = Field(False, description="True=数据充分；False=窗口不足")
    badge: str | None = Field(None, description="量能徽标：放量/缩量/正常/未知")


# =============================================================================
# [QM-63 canonical 2026-08-04] 事件方向 / 结构级别正式合同
# =============================================================================
# 唯一 producer 约束：direction / bias / structureLevel 必须由
# `build_pyramid_event()` 统一生成，禁止各调用点自行推断或补默认值。
#
# 关键规则（PRD20 QM-63）：
# - bias=1  必须对应 direction="bullish"
# - bias=-1 必须对应 direction="bearish"
# - 二者冲突必须输出 diagnostic，不得静默选一个
# - 缺方向不得默认 bearish；缺级别不得默认 swing（一律 None）
# - extra.structure_level 仅由兼容 adapter 读取，新 producer 不再写入为唯一来源
# =============================================================================

DIRECTION_BULLISH = "bullish"
DIRECTION_BEARISH = "bearish"

# 合法正式方向值（None 表示"方向未知"，属合法状态）
PYRAMID_DIRECTIONS: frozenset[str] = frozenset({DIRECTION_BULLISH, DIRECTION_BEARISH})

STRUCTURE_LEVEL_SWING = "swing"
STRUCTURE_LEVEL_INTERNAL = "internal"

# 合法结构级别（None 表示"级别不适用/未知"，如 EQH/EQL）
PYRAMID_STRUCTURE_LEVELS: frozenset[str] = frozenset({
    STRUCTURE_LEVEL_SWING, STRUCTURE_LEVEL_INTERNAL,
})

# 旧值 → 正式方向（兼容 adapter；up/down 为 2.0.x 遗留表达）
_LEGACY_DIRECTION_MAP: dict[Any, str] = {
    "up": DIRECTION_BULLISH,
    "bullish": DIRECTION_BULLISH,
    "long": DIRECTION_BULLISH,
    1: DIRECTION_BULLISH,
    "down": DIRECTION_BEARISH,
    "bearish": DIRECTION_BEARISH,
    "short": DIRECTION_BEARISH,
    -1: DIRECTION_BEARISH,
}
# 注：bool 不放入本表（Python 中 True == 1 会与整数键冲突），
# 由 normalize_direction 显式分支处理。

_DIRECTION_TO_BIAS: dict[str, int] = {
    DIRECTION_BULLISH: 1,
    DIRECTION_BEARISH: -1,
}


def normalize_direction(raw: Any) -> str | None:
    """把任意历史方向表达归一为正式 direction（bullish/bearish/None）。

    [QM-63] 无法识别一律返回 None —— 缺方向不得默认 bearish。
    注意：bool 必须先于 int 判断（Python 中 True == 1）。
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        return _LEGACY_DIRECTION_MAP.get(raw.strip().lower())
    if isinstance(raw, bool):
        return DIRECTION_BULLISH if raw else DIRECTION_BEARISH
    if isinstance(raw, int):
        if raw > 0:
            return DIRECTION_BULLISH
        if raw < 0:
            return DIRECTION_BEARISH
        return None  # 0 = 未形成，非方向
    return None


def normalize_structure_level(raw: Any) -> str | None:
    """归一结构级别为 swing / internal / None。

    [QM-63] 缺级别不得默认 swing。
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        # internal 布尔标记：True=internal, False=swing
        return STRUCTURE_LEVEL_INTERNAL if raw else STRUCTURE_LEVEL_SWING
    if isinstance(raw, str):
        value = raw.strip().lower()
        if value in PYRAMID_STRUCTURE_LEVELS:
            return value
    return None


class PyramidEvent(BaseModel):
    """金字塔事件（离散、时序、含新鲜度）。

    PRD20 QM-60：连续因子与事件分离；事件必须含发生时间和新鲜度。
    PRD20 QM-63：正式事件必须携带方向、级别、时间、价格与诊断。
    Gate1：事件可携带事件发生 bar 的 VolumeContext。
    """

    type: str = Field(..., description="事件类型，如 BOS / CHoCH / OB_ENTRY / SQZ_OFF / NODE_CROSS_UP")
    direction: str | None = Field(
        None,
        description="正式方向：'bullish' / 'bearish' / None（方向未知，禁止默认 bearish）",
    )
    structureLevel: str | None = Field(
        None, description="正式结构层级：swing / internal；非结构事件为 None（禁止默认 swing）"
    )
    bias: int | None = Field(
        None, description="正式数值方向：1(bullish) / -1(bearish) / None；必须与 direction 一致"
    )
    diagnostics: list[str] = Field(
        default_factory=list, description="字段矛盾或旧合同适配诊断"
    )
    occurredAt: str | None = Field(
        None, description="事件发生日期（ISO YYYY-MM-DD）；intrabar 事件可空"
    )
    barIndex: int | None = Field(
        None, description="事件在计算序列中的位置索引（0-based）；与 occurredAt 至少一个非空"
    )
    price: float | None = Field(None, description="事件触发价格（可空）")
    freshnessBars: int = Field(
        ..., ge=0, description="事件新鲜度：从事件发生到当前计算 bar 的 bar 数（>=0）"
    )
    volumeContext: VolumeContextSchema | None = Field(
        None, description="事件发生 bar 的量能上下文（Gate1）"
    )
    volumeBadge: str | None = Field(
        None, description="事件量能徽标：放量/缩量/正常/未知（Gate1）"
    )
    extra: dict[str, Any] | None = Field(
        None, description="事件附加字段（如 relativeVolume / boundary 等）"
    )

    @model_validator(mode="after")
    def _check_time_or_index(self) -> PyramidEvent:
        if self.occurredAt is None and self.barIndex is None:
            raise ValueError(
                f"PyramidEvent(type={self.type}) 必须至少包含 occurredAt 或 barIndex"
            )
        return self

    @model_validator(mode="after")
    def _check_direction_contract(self) -> PyramidEvent:
        """[QM-63] 校验正式方向/级别合同。

        - direction 必须是 bullish/bearish/None，不接受 up/down 等旧值；
        - structureLevel 必须是 swing/internal/None；
        - bias 与 direction 必须一一对应（1↔bullish, -1↔bearish）。

        冲突在 build_pyramid_event 阶段就应被记录 diagnostic 并对齐；
        走到这里仍不一致说明有绕过 producer 的调用点，直接拒绝。
        """
        if self.direction is not None and self.direction not in PYRAMID_DIRECTIONS:
            raise ValueError(
                f"PyramidEvent(type={self.type}) direction 非法: {self.direction!r}，"
                f"只接受 {sorted(PYRAMID_DIRECTIONS)} 或 None"
            )
        if (
            self.structureLevel is not None
            and self.structureLevel not in PYRAMID_STRUCTURE_LEVELS
        ):
            raise ValueError(
                f"PyramidEvent(type={self.type}) structureLevel 非法: "
                f"{self.structureLevel!r}，只接受 {sorted(PYRAMID_STRUCTURE_LEVELS)} 或 None"
            )
        expected_bias = (
            _DIRECTION_TO_BIAS[self.direction] if self.direction is not None else None
        )
        if self.bias != expected_bias:
            raise ValueError(
                f"PyramidEvent(type={self.type}) bias 与 direction 不一致: "
                f"direction={self.direction!r}, bias={self.bias!r}，"
                f"期望 bias={expected_bias!r}"
            )
        return self


def build_pyramid_event(
    *,
    event_type: str,
    freshness_bars: int,
    direction_raw: Any = None,
    structure_level_raw: Any = None,
    bias_raw: Any = None,
    occurred_at: str | None = None,
    bar_index: int | None = None,
    price: float | None = None,
    volume_context: VolumeContextSchema | None = None,
    volume_badge: str | None = None,
    extra: dict[str, Any] | None = None,
) -> PyramidEvent:
    """[QM-63] 唯一 PyramidEvent producer。

    所有第一金字塔事件必须经此工厂生成，保证：
    1. direction 归一为 bullish/bearish/None（缺方向不默认 bearish）；
    2. structureLevel 归一为 swing/internal/None（缺级别不默认 swing）；
    3. bias 由 direction 派生，永远与之一致；
    4. direction_raw 与 bias_raw 冲突时输出 diagnostic 并以 direction_raw 为准；
    5. extra.structure_level 仅作为兼容回退来源读取，不作为唯一事实来源。

    Args:
        direction_raw: 任意历史方向表达（up/down/bullish/bearish/1/-1/bool）。
        bias_raw: 独立的数值方向来源；仅在 direction_raw 缺失时用于推导，
                  存在冲突时记录 diagnostic。
        structure_level_raw: swing/internal 字符串，或 internal 布尔标记。
    """
    diagnostics: list[str] = []

    direction = normalize_direction(direction_raw)
    bias_direction = normalize_direction(bias_raw)

    if direction is None and bias_direction is not None:
        # 只有 bias 可用：以 bias 推导方向（不算冲突）
        direction = bias_direction
    elif (
        direction is not None
        and bias_direction is not None
        and direction != bias_direction
    ):
        # [QM-63] 冲突必须输出 diagnostic，不得静默择一
        diagnostics.append(
            f"DIRECTION_BIAS_CONFLICT: direction_raw={direction_raw!r} → {direction}, "
            f"bias_raw={bias_raw!r} → {bias_direction}; 以 direction 为准"
        )

    structure_level = normalize_structure_level(structure_level_raw)
    if structure_level is None and structure_level_raw is not None:
        diagnostics.append(
            f"STRUCTURE_LEVEL_UNRECOGNIZED: {structure_level_raw!r} 无法识别，置为 None"
        )

    # 兼容 adapter：正式级别缺失时，回退读取 extra.structure_level
    if structure_level is None and extra:
        legacy_level = normalize_structure_level(extra.get("structure_level"))
        if legacy_level is not None:
            structure_level = legacy_level
            diagnostics.append(
                "STRUCTURE_LEVEL_FROM_LEGACY_EXTRA: 由 extra.structure_level 兼容读取"
            )

    bias = _DIRECTION_TO_BIAS[direction] if direction is not None else None

    return PyramidEvent(
        type=event_type,
        direction=direction,
        structureLevel=structure_level,
        bias=bias,
        diagnostics=diagnostics,
        occurredAt=occurred_at,
        barIndex=bar_index,
        price=price,
        freshnessBars=freshness_bars,
        volumeContext=volume_context,
        volumeBadge=volume_badge,
        extra=extra,
    )


class DimensionResult(BaseModel):
    """单维度结果（必选维度 available 必须为 True）。

    PRD20 QM-60：每维包含连续因子、离散事件、状态文本和证据。
    Gate1：每维携带当前 bar 的 VolumeContext（共享计算结果）。
    """

    name: str = Field(..., description="维度名：trend / structure / momentum / chip_consensus")
    available: bool = Field(..., description="该维度是否有数据；必选维度必须为 True")
    continuousFactors: dict[str, Any] = Field(
        default_factory=dict, description="连续因子与特征（标量指标）"
    )
    events: list[PyramidEvent] = Field(
        default_factory=list, description="离散事件列表（按时间升序）"
    )
    statusText: str = Field(..., description="中文状态描述（由结构化结果生成）")
    evidence: dict[str, Any] = Field(
        default_factory=dict, description="证据（如段起止、锚点、参数引用等）"
    )
    volumeContext: VolumeContextSchema | None = Field(
        None, description="当前 bar 的量能上下文（Gate1，共享计算结果）"
    )
    availability: str = Field(
        default="available", description="显式可用性：available / unavailable"
    )
    unavailableReason: str | None = Field(
        None, description="不可用时稳定原因；可用时为空"
    )


# 字段级 availability 合法 reasonCode（[字段级 availability 合同 2026-08-04]）
FIELD_AVAILABILITY_REASONS: frozenset[str] = frozenset({
    "not_applicable",       # 语义不适用（如无挤压时挤压期均量）
    "insufficient_history", # 历史样本不足
    "upstream_unavailable", # 上游数据缺失
    "failed",               # 计算异常
    "stale",                # 结果过期
    "missing",              # producer 未写该字段
})


class FieldAvailability(BaseModel):
    """条件性可空因子的字段级 availability 合同（[QM 2026-08-04]）。

    解决"合法空值没有字段级原因"：具体因子为 None 时必须能区分
    not_applicable / insufficient_history / upstream_unavailable / failed /
    stale / missing，并返回完整的溯源元数据。
    """

    availability: str = Field(
        ...,
        description=(
            "稳定状态码：not_applicable / insufficient_history / "
            "upstream_unavailable / failed / stale / missing"
        ),
    )
    reasonCode: str = Field(
        ...,
        description="机器可读原因码（与 availability 相同，保持稳定）",
    )
    reasonText: str = Field(
        ..., description="人类可读原因（含实际数据数量与边界，便于诊断）"
    )
    observationCount: int | None = Field(
        None, description="可用观测数（如历史样本数）；不适用时 None"
    )
    sourceRunId: str | None = Field(
        None, description="产生该字段的 run id；单股各自计算时为 None"
    )
    calculatedAt: str | None = Field(
        None, description="产生该字段的计算时间 ISO"
    )

    @model_validator(mode="after")
    def _check_reason_code(self) -> FieldAvailability:
        if self.reasonCode not in FIELD_AVAILABILITY_REASONS:
            raise ValueError(
                f"reasonCode 非法: {self.reasonCode!r}，"
                f"只接受 {sorted(FIELD_AVAILABILITY_REASONS)}"
            )
        return self


class FirstPyramidSnapshot(BaseModel):
    """第一金字塔统一快照（PRD20 QM-01~QM-43、QM-60~QM-62）。

    单股详情、批量计算、行情列表 payload 和盘后 compute 调用必须复用此结构。
    """

    symbol: str = Field(..., description="股票代码")
    tradeDate: str = Field(..., description="交易日（ISO YYYY-MM-DD）")
    orderedDimensions: list[str] = Field(
        default_factory=lambda: list(ORDERED_DIMENSIONS),
        description="固定维度顺序，禁止页面动态组合",
    )
    trend: DimensionResult = Field(..., description="趋势维度（必选）")
    structure: DimensionResult = Field(..., description="结构维度（必选）")
    momentum: DimensionResult = Field(..., description="动量维度（必选）")
    chipConsensus: DimensionResult | None = Field(
        None, description="筹码共识维度（可选；无有效峰时为 None）"
    )
    chipStatus: ChipStatus | None = Field(
        None,
        description=(
            "[CHANGE-20260729-004 P0-2] 筹码共识结构化状态"
            "（ready/pending/failed/unavailable/stale + reasonCode/reasonText/computedAt）"
        ),
    )
    statusText: str = Field(..., description="金字塔聚合中文状态描述")
    volumeContext: VolumeContextSchema | None = Field(
        None, description="共享量能上下文（Gate1，前端量能水位条数据源）"
    )
    inputHash: str = Field(..., description="OHLCV 输入 hash（用于跨入口一致性校验）")
    parameterHash: str = Field(..., description="参数 hash（含算法版本与固定参数）")
    algorithmVersion: str = Field(
        default=FIRST_PYRAMID_ALGORITHM_VERSION, description="算法版本"
    )
    calculatedAt: str | None = Field(
        None, description="run级唯一计算时间；由编排器注入，禁止单股各自取时钟"
    )
    # [QM-63 run 级来源合同 2026-08-04] 与 calculatedAt 同批注入。
    # 同一 run 的所有股票必须共享完全相同的 sourceRunId 与 calculatedAt；
    # 批任务入口缺 sourceRunId 应直接失败，不得产出"一半有来源"的快照。
    sourceRunId: str | None = Field(
        None, description="run级唯一来源 id；由编排器注入，单股各自计算时为 None"
    )
    # [字段级 availability 合同 2026-08-04] 条件性可空因子的字段级原因。
    # key 为因子路径（如 "momentum.squeeze_avg_volume"），value 为 FieldAvailability。
    # 具体因子为 None 时必须在 fieldAvailability 中给出原因，禁止无原因的缺失。
    fieldAvailability: dict[str, FieldAvailability] = Field(
        default_factory=dict,
        description=(
            "条件性可空因子的字段级 availability 元数据"
            "（key=字段路径，value=原因+溯源）"
        ),
    )

    @model_validator(mode="after")
    def _check_required_dimensions(self) -> FirstPyramidSnapshot:
        """前三维必选，任一 available=False 必须明确错误（不得静默伪造）。

        PRD20 QM-02：趋势、结构、动量为必选维度。
        """
        for name, dim in (
            ("trend", self.trend),
            ("structure", self.structure),
            ("momentum", self.momentum),
        ):
            if not dim.available:
                raise ValueError(
                    f"必选维度 {name} 不可缺失（available=False）；"
                    f"调用方必须传入足够数据，不得静默伪造"
                )
        if list(self.orderedDimensions) != list(ORDERED_DIMENSIONS):
            raise ValueError(
                f"orderedDimensions 必须为 {list(ORDERED_DIMENSIONS)}，"
                f"实际：{self.orderedDimensions}"
            )
        return self

    def to_dict(self) -> dict[str, Any]:
        """序列化为前端 JSON 友好字典（camelCase 字段）。"""
        return self.model_dump(by_alias=False)


# =============================================================================
# [CHANGE-20260729-003 核心与筹码解耦] 拆分中间 DTO
# =============================================================================

# Core 算法版本（不含 Node Cluster 参数，仅含 trend/structure/momentum）
FIRST_PYRAMID_CORE_ALGORITHM_VERSION = "1.0.0-core-split"

# Chip 算法版本（独立版本，关联 Node Cluster engine 版本）
CHIP_CONSENSUS_ALGORITHM_VERSION = "1.0.0-chip-split"


class FirstPyramidCoreSnapshot(BaseModel):
    """第一金字塔核心快照（趋势/结构/动量；不含筹码共识）。

    [CHANGE-20260729-003] 盘后 review core 关键路径使用本结构，禁止 Node Cluster
    和 15m Node 输入。core 的 inputHash/parameterHash 排除 Node 参数。

    - trend/structure/momentum 必选维度，任一 available=False 必须抛 ValueError
    - chip_consensus 由独立路径 compute_chip_consensus_snapshot 计算
    - assemble_first_pyramid_view(core, chip) 组合为完整 FirstPyramidSnapshot
    """

    symbol: str = Field(..., description="股票代码")
    tradeDate: str = Field(..., description="交易日（ISO YYYY-MM-DD）")
    trend: DimensionResult = Field(..., description="趋势维度（必选）")
    structure: DimensionResult = Field(..., description="结构维度（必选）")
    momentum: DimensionResult = Field(..., description="动量维度（必选）")
    statusText: str = Field(
        ...,
        description="核心三维共享 summary builder 生成的聚合中文状态描述",
    )
    volumeContext: VolumeContextSchema | None = Field(
        None, description="共享量能上下文（Gate1）"
    )
    inputHash: str = Field(..., description="OHLCV 输入 hash（不含 Node 参数）")
    parameterHash: str = Field(
        ..., description="核心参数 hash（排除 Node Cluster 参数）"
    )
    algorithmVersion: str = Field(
        default=FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
        description="核心算法版本（不含 chip）",
    )
    calculatedAt: str | None = Field(
        None, description="run级唯一计算时间；由编排器注入"
    )
    nBars: int = Field(..., description="输入 bar 数")
    lastBarIndex: int = Field(..., description="最后一根 bar 的索引（0-based）")

    @model_validator(mode="after")
    def _check_required_dimensions(self) -> FirstPyramidCoreSnapshot:
        for name, dim in (
            ("trend", self.trend),
            ("structure", self.structure),
            ("momentum", self.momentum),
        ):
            if not dim.available:
                raise ValueError(
                    f"必选维度 {name} 不可缺失（available=False）；"
                    f"调用方必须传入足够数据，不得静默伪造"
                )
        return self


class ChipConsensusResult(BaseModel):
    """筹码共识计算结果（独立于 core）。

    [CHANGE-20260729-003] chip 使用独立 version/hash/run 关联。
    可独立失败/重试，绝不反改主 run 或重算 core。
    """

    chip: DimensionResult | None = Field(
        None, description="筹码共识维度；无有效峰或数据不足时为 None"
    )
    chipHash: str = Field(..., description="chip 输入 hash（daily+15m bars）")
    algorithmVersion: str = Field(
        default=CHIP_CONSENSUS_ALGORITHM_VERSION,
        description="chip 算法版本（关联 Node Cluster engine 版本）",
    )
    dailyBarsCount: int = Field(0, description="输入日线 bar 数")
    bars15mCount: int = Field(0, description="输入 15m bar 数")
    error: str | None = Field(
        None, description="计算失败原因（chip 失败不阻塞 core）"
    )


# =============================================================================
# [CHANGE-20260729-004 P0-2] 结构化 chipStatus DTO
# =============================================================================
# 替代页面统一显示"暂不可用"：前端读取 chipStatus.reasonCode/reasonText 展示真实原因
# 至少区分：CHIP_JOB_PENDING / CHIP_JOB_FAILED / DAILY_BARS_INSUFFICIENT /
#           M15_BARS_INSUFFICIENT / NO_VALID_PEAK / CORE_RUN_MISMATCH / STALE_RESULT
# =============================================================================


# [QM-63 chip 生命周期 2026-08-04] chipStatus.state 完整七态
# pending      : chip job 已入队/运行中，尚未产出
# ready        : chip 结果完整可用
# unavailable  : 上游数据不足，本交易日合法不可算（非错误）
# failed       : chip 计算异常（错误，需排查）
# interrupted  : chip job 被取消/Worker 接管而未完成
# stale        : chip 结果存在但落后于 core run（旧残留）
# partial      : chip 部分维度可用（如有 POC 无峰簇），coverage < 1
CHIP_STATUS_STATES: frozenset[str] = frozenset({
    "pending", "ready", "unavailable", "failed",
    "interrupted", "stale", "partial",
})

# 表示"chip 不可直接使用"的状态（fp_chip_available=False）
CHIP_STATUS_NOT_READY_STATES: frozenset[str] = frozenset({
    "pending", "unavailable", "failed", "interrupted", "stale",
})

CHIP_SEMANTIC_STATES: frozenset[str] = frozenset({
    "strong_support", "weak_support", "neutral", "weak_pressure",
    "strong_pressure", "unavailable", "not_applicable",
})
CHIP_SEMANTIC_META: dict[str, dict[str, str | int]] = {
    "strong_support": {"label": "强支撑", "tone": "positive", "order": 1},
    "weak_support": {"label": "弱支撑", "tone": "positive", "order": 2},
    "neutral": {"label": "中性", "tone": "neutral", "order": 3},
    "weak_pressure": {"label": "弱压力", "tone": "negative", "order": 4},
    "strong_pressure": {"label": "强压力", "tone": "negative", "order": 5},
    "unavailable": {"label": "不可用", "tone": "muted", "order": 6},
    "not_applicable": {"label": "不适用", "tone": "muted", "order": 7},
}

# chipStatus.reasonCode 合法值（与 NODE_* 区分：chip_* 用于第一金字塔 DTO）
CHIP_STATUS_REASON_CODES: frozenset[str] = frozenset({
    "CHIP_JOB_PENDING",         # chip job 异步未完成
    "CHIP_JOB_FAILED",          # chip job 执行异常
    "DAILY_BARS_INSUFFICIENT",  # 日线 bars 不足（<10）
    "M15_BARS_INSUFFICIENT",     # 15m bars 不足（<4000 或 INPUT_CONTRACT_VIOLATION）
    "NO_VALID_PEAK",            # Node Cluster 运行完成但无有效峰（PROFILE_EMPTY）
    "CORE_RUN_MISMATCH",        # chip 与 core 的 run_id 不匹配（旧 chip 残留）
    "STALE_RESULT",             # chip 结果 trade_date 早于 core trade_date
    # [QM-63 chip 生命周期 2026-08-04] 新增两态对应原因码
    "CHIP_JOB_INTERRUPTED",     # chip job 被取消或 Worker 接管而未完成
    "CHIP_PARTIAL_COVERAGE",    # chip 部分维度可用（coverage < 1）
})


class ChipStatus(BaseModel):
    """[CHANGE-20260729-004 P0-2 + CHANGE-20260730-010] 筹码共识结构化状态。

    [CHANGE-20260730-010] 抽取为 /market/stocks 与 /first-pyramid 共享 schema：
    - 列表 API 与详情 API 返回完全相同的 chipStatus 结构（camelCase）
    - 新增 actualBars / requiredBars / fullQualityBars 诊断字段
    - 000021 深科技场景：state=unavailable, reasonCode=M15_BARS_INSUFFICIENT,
      actualBars=354, requiredBars=500, fullQualityBars=4000

    前端读取 reasonCode/reasonText 展示真实原因，state 用于决定渲染样式：
    - ready: 显示完整 chipConsensus（POC/峰数/距离）
    - pending: 显示"筹码计算中"
    - failed: 显示"筹码计算失败" + reasonText
    - unavailable: 显示"筹码数据不足" + reasonText（含缺失周期和数量）
    - stale: 显示"筹码结果已过期，等待重新计算"
    """

    state: str = Field(
        ...,
        description=(
            "筹码任务七态：pending/ready/unavailable/failed/"
            "interrupted/stale/partial"
        ),
    )
    semanticState: str = Field(
        default="unavailable", description="筹码共识七态语义"
    )
    label: str = Field(default="不可用", description="七态稳定展示文案")
    tone: str = Field(default="muted", description="七态稳定视觉语气")
    order: int = Field(default=6, description="七态稳定排序")
    reasonCode: str | None = Field(
        None,
        description=(
            "稳定状态码（机器可读）：CHIP_JOB_PENDING / CHIP_JOB_FAILED / "
            "DAILY_BARS_INSUFFICIENT / M15_BARS_INSUFFICIENT / NO_VALID_PEAK / "
            "CORE_RUN_MISMATCH / STALE_RESULT"
        ),
    )
    reasonText: str | None = Field(
        None,
        description="人类可读原因（含实际数据数量与边界，便于诊断）",
    )
    computedAt: str | None = Field(
        None,
        description="chip 计算时间 ISO（来自 chip job 完成时间）",
    )
    # [CHANGE-20260730-010] 诊断字段：M15_BARS_INSUFFICIENT 时填充，便于前端精确展示
    actualBars: int | None = Field(
        None,
        description="实际 15m bar 数（仅 M15_BARS_INSUFFICIENT 时填充）",
    )
    requiredBars: int | None = Field(
        None,
        description="最低要求 15m bar 数（=500，仅 M15_BARS_INSUFFICIENT 时填充）",
    )
    fullQualityBars: int | None = Field(
        None,
        description="完整质量门槛 15m bar 数（=4000，仅 M15_BARS_INSUFFICIENT 时填充）",
    )
    # [QM-63 chip 生命周期 2026-08-04] 来源与覆盖度合同
    sourceRunId: str | None = Field(
        None, description="产出该 chip 结果的 run id（用于与 core run 比对）"
    )
    jobId: str | None = Field(
        None, description="chip 异步任务 id（用于定位失败/中断的具体 job）"
    )
    freshness: int | None = Field(
        None, description="chip 结果落后 core 的交易日数；0 表示同日"
    )
    coverage: float | None = Field(
        None, description="chip 维度覆盖度 0~1；partial 状态必须 <1"
    )

    @model_validator(mode="after")
    def _check_chip_lifecycle(self) -> ChipStatus:
        """[QM-63] 校验 chip 七态合同。

        - state 必须属于七态之一（不得出现自造状态）；
        - 非 ready 状态必须给出 reasonCode（否则前端只能显示"暂不可用"）；
        - coverage 若给出必须在 [0, 1]。
        """
        if self.state not in CHIP_STATUS_STATES:
            raise ValueError(
                f"chipStatus.state 非法: {self.state!r}，"
                f"只接受 {sorted(CHIP_STATUS_STATES)}"
            )
        if self.state != "ready" and not self.reasonCode:
            raise ValueError(
                f"chipStatus.state={self.state} 必须提供 reasonCode，"
                "禁止无原因的不可用"
            )
        if self.coverage is not None and not (0.0 <= self.coverage <= 1.0):
            raise ValueError(
                f"chipStatus.coverage 必须在 [0,1]，实际 {self.coverage}"
            )
        return self


# 模块自测：构造最小合法 snapshot
if __name__ == "__main__":
    evt = build_pyramid_event(
        event_type="BOS",
        direction_raw="up",  # 旧值经 producer 归一为 bullish
        structure_level_raw="swing",
        occurred_at="2026-07-25",
        bar_index=100,
        price=10.5,
        freshness_bars=2,
    )
    assert evt.direction == "bullish" and evt.bias == 1
    trend = DimensionResult(
        name="trend",
        available=True,
        continuousFactors={"regime_value": 1, "dsa_dir_bars": 60},
        events=[],
        statusText="DSA 趋势上行，连续 60 根",
        evidence={"anchor": "2026-04-01"},
    )
    structure = DimensionResult(
        name="structure",
        available=True,
        continuousFactors={"swing_bias": 1},
        events=[evt],
        statusText="BOS 上行突破，新鲜度 2 根",
    )
    momentum = DimensionResult(
        name="momentum",
        available=True,
        continuousFactors={"squeeze_on": False, "bb_width": 0.12},
        events=[],
        statusText="动量扩张，BB 带宽 0.12",
    )
    snap = FirstPyramidSnapshot(
        symbol="000001.SZ",
        tradeDate="2026-07-25",
        trend=trend,
        structure=structure,
        momentum=momentum,
        chipConsensus=None,
        statusText="趋势上行 + 结构 BOS + 动量扩张",
        inputHash="sha256:dummy",
        parameterHash="sha256:dummy",
    )
    print(f"OK: {snap.symbol} {snap.tradeDate} algo={snap.algorithmVersion}")
    print(f"Ordered: {snap.orderedDimensions}")
    print(f"Status: {snap.statusText}")
