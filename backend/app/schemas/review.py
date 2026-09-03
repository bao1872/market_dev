"""复盘模块 API Schema - 复盘工作台响应契约（PRD §12）。

对应 ORM 模型 `app.models.market_review`：
- MarketReviewRun / MarketReviewRunItem / MarketReviewScopeSnapshot
- MarketReviewMetricObservation（first_pyramid facts）
- MarketReviewScopeObservationFact / ReviewScopeCompositionSnapshot（canonical facts）
- 历史 ORM 表（MarketReviewSignal / MarketReviewTracking 等）保留不 DROP，但本 schema 不再暴露其 API 契约。

字段命名约定：
- 与前端 JSON API 契约一致，使用 camelCase（reviewRunId / tradeDate / signalId 等）
  复盘 API 响应字段直接以 PRD §12 / §7.1 的字段命名为准
- UUID 字段统一字符串化
- 日期字段：ISO 字符串（YYYY-MM-DD 或带时区 ISO datetime）
- payload 为 JSONB 透传字段，前端按结构自行解析

PRD §7.1 P/Q/U/C/V payload 通用结构：
    {
        "value": 63.4,
        "rawValue": 0.572,
        "delta1d": -4.1,
        "delta5d": 6.7,
        "historyPercentile120d": 78.2,
        "crossSectionPercentile": 84.0,
        "historyObservationCount": 120,
        "components": [...],
        "coverage": 0.982,
        "status": "ready"
    }

模块自测：
    python -m app.schemas.review
"""

from __future__ import annotations

# ruff: noqa: N815 - camelCase 字段为前端 JSON API 契约
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

# =============================================================================
# 基础 DTO
# =============================================================================


class ReviewMetricComponentDTO(BaseModel):
    """P/Q/U/C/V 单个 component（PRD §7.1 components 元素）。

    每个 component 必须保留：
    - 原始值（rawValue）
    - 方向（direction: positive/negative/neutral，正向或反向贡献）
    - 分母（denominator）
    - 字段来源（fieldSource：权威扁平字段名）
    - 权重（weight：在加权平均中的权重）
    """

    name: str = Field(..., description="component 名称（如 advance_ratio）")
    rawValue: float | None = Field(None, description="原始值（0-1 比率或数值）")
    normalizedValue: float | None = Field(
        None, description="归一化值（0-100，按历史分位归一化后）",
    )
    direction: str = Field(
        "neutral", description="方向：positive（正向）/negative（反向）/neutral",
    )
    denominator: int | None = Field(None, description="分母（如 ready_count）")
    fieldSource: str = Field(
        ..., description="字段来源（权威扁平字段名，如 fp_trend_direction）",
    )
    weight: float = Field(1.0, description="权重（默认 1.0，等权）")
    coverage: float | None = Field(None, description="该 component 的覆盖率")
    status: str = Field(
        "ready", description="状态：ready/insufficient_history/partial/unavailable",
    )
    extra: dict[str, Any] | None = Field(
        None, description="附加字段（如 price_source / weight_mode）",
    )
    weightMode: str | None = Field(None, description="权重模式")
    readiness: dict[str, Any] = Field(
        default_factory=dict,
        description="原始值/归一化值就绪状态与具体原因",
    )


class ReviewMetricPayloadDTO(BaseModel):
    """P/Q/U/C/V 单个聚合变量 payload（PRD §7.1 通用结构）。"""

    value: float | None = Field(None, description="归一化值（0-100）")
    rawValue: float | None = Field(None, description="原始值（加权前）")
    delta1d: float | None = Field(None, description="1 日变化（归一化值差）")
    delta5d: float | None = Field(None, description="5 日变化（归一化值差）")
    historyPercentile120d: float | None = Field(
        None, description="120 日历史分位（0-100），不足 60 日为 None",
    )
    crossSectionPercentile: float | None = Field(
        None, description="当日横截面分位（0-100）",
    )
    historyObservationCount: int | None = Field(
        None, description="历史观测样本数（<=baseline_window）",
    )
    components: list[ReviewMetricComponentDTO] = Field(
        default_factory=list, description="components 列表",
    )
    coverage: float | None = Field(None, description="该变量覆盖率（0-1）")
    status: str = Field(
        "ready",
        description="状态：ready/insufficient_history/partial/unavailable",
    )
    readiness: dict[str, Any] = Field(
        default_factory=dict,
        description="指标级 raw/normalized readiness、历史门槛与缺失原因",
    )


# =============================================================================
# 日期与总览
# =============================================================================


class ReviewDatesResponse(BaseModel):
    """GET /api/v1/review/dates 响应。"""

    trade_dates: list[str] = Field(
        default_factory=list,
        description="已发布复盘交易日列表（ISO YYYY-MM-DD，按日期降序）",
    )
    latest_trade_date: str | None = Field(
        None, description="最新已发布复盘交易日（无则为 None）",
    )


class ReviewLatestResponse(BaseModel):
    """GET /api/v1/review/latest 响应（重定向到最新发布的复盘）。"""

    review_run_id: str = Field(..., description="复盘 run ID（UUID）")
    trade_date: str = Field(..., description="业务交易日（ISO YYYY-MM-DD）")
    status: str = Field(..., description="run 状态")
    algorithm_version: str = Field(..., description="算法版本")
    filter_version: str = Field(..., description="筛选器版本")


class ReviewOverviewCoverageDTO(BaseModel):
    """overview.coverage 子结构（PRD §12.1）。"""

    market: float | None = Field(None, description="全市场覆盖率")
    indices: float | None = Field(None, description="主要指数覆盖率")
    styles: float | None = Field(None, description="风格覆盖率")
    industryL1: float | None = Field(None, description="一级行业覆盖率")


class ReviewChipCoverageDTO(BaseModel):
    """[P0 2026-08-04] chip 真实覆盖率明细（以 stock_core expected_count 为分母）。

    替代旧的"chip 表已有行占位比例"，前端据此显示真实覆盖率而非误称为 Chip Run 的 core run id。
    """

    expectedCount: int | None = Field(
        None, description="stock_core run 期望股票数（覆盖率分母）",
    )
    succeededCount: int = Field(0, description="chip succeeded 股票数")
    failedCount: int = Field(0, description="chip failed 股票数")
    skippedCount: int = Field(0, description="chip skipped 股票数")
    missingCount: int = Field(0, description="缺失股票数（expected - 已有行）")
    coverage: float | None = Field(
        None, description="真实覆盖率 succeeded/expected（0-1）",
    )


class ReviewOverviewResponse(BaseModel):
    """GET /api/v1/review/{trade_date}/overview 响应（PRD §12.1）。"""

    reviewRunId: str = Field(..., description="复盘 run ID（UUID）")
    tradeDate: str = Field(..., description="业务交易日（ISO YYYY-MM-DD）")
    status: str = Field(..., description="run 状态")
    sourceCoreRunId: str = Field(..., description="输入 stock_core run ID")
    sourceBoardRunId: str | None = Field(
        None, description="[Slice 3] legacy Board Analysis run ID（新 run 为 null）",
    )
    sourceChipRunId: str | None = Field(
        None,
        description=(
            "[P0 2026-08-04] chip 无独立可追溯 snapshot run ID，本字段恒为 null；"
            "即使 chip 覆盖 100% 也为 null（chip 只通过 core_run_id 挂靠 stock_core）。"
            "chip 真实质量读取 chipCoverage 与 degradedReasons。"
        ),
    )
    degradedReasons: list[str] = Field(
        default_factory=list,
        description=(
            "[QM-63] 降级原因列表（CHIP_UNAVAILABLE / CHIP_PARTIAL 等）；"
            "空数组表示无降级"
        ),
    )
    chipCoverage: ReviewChipCoverageDTO | None = Field(
        None,
        description=(
            "[P0 2026-08-04] chip 真实覆盖率明细；sourceChipRunId 恒为 null 时，"
            "以此展示真实覆盖情况而非误称为 Chip Run 的 core run id"
        ),
    )
    algorithmVersion: str = Field(..., description="算法版本")
    filterVersion: str = Field(..., description="筛选器版本")
    baselineWindow: int = Field(120, description="历史基线窗口（默认 120，最低 60）")
    coverage: ReviewOverviewCoverageDTO = Field(
        default_factory=ReviewOverviewCoverageDTO,
        description="整体覆盖率明细",
    )
    coverageRatio: float | None = Field(None, description="整体 coverage_ratio")
    expectedScopeCount: int = Field(0, description="期望扫描范围总数")
    succeededScopeCount: int = Field(0, description="扫描成功范围数")
    failedScopeCount: int = Field(0, description="扫描失败范围数")
    signalCount: int = Field(0, description="命中信号总数")
    startedAt: str | None = Field(None, description="计算开始时间 ISO")
    completedAt: str | None = Field(None, description="计算完成时间 ISO")
    publishedAt: str | None = Field(None, description="发布时间 ISO")


# =============================================================================
# 市场扫描
# =============================================================================


class ReviewScopeSummaryDTO(BaseModel):
    """Thin Scope-list analysis projection（Slice B）。

    Pure projection of persisted canonical Composition analysis fields.  Every
    analysis field is ``Optional``: a missing/unavailable value is ``None`` and
    MUST NOT be coerced to ``0`` (PRD unavailable≠zero).  The backend canonical
    owners (Observation Fact + Composition) remain the single source; this DTO
    never triggers recomputation.  ``summary=None`` (not an all-zero object) is
    the legal state when a Fact exists but its Composition is missing.
    """

    model_config = ConfigDict(populate_by_name=True)

    dynamicsStatus: str | None = None
    phase: str | None = None

    position: float | None = None
    velocity: float | None = None
    acceleration: float | None = None
    upperOccupancy: float | None = None
    lowerOccupancy: float | None = None

    equalWeightReturn: float | None = None
    amountWeightedReturn: float | None = None
    capitalTilt: float | None = None

    advanceRatio: float | None = None
    declineRatio: float | None = None
    unchangedRatio: float | None = None
    returnDispersion: float | None = None

    priceNormalizedHhi: float | None = None
    amountNormalizedHhi: float | None = None

    leadershipStatus: str | None = None
    jaccardStability: float | None = None
    migration: float | None = None


class ReviewScopeObservationSummaryDTO(BaseModel):
    """Thin Scope-list Observation projection (R2B).

    Pure projection of a SMALL set of already-persisted canonical Observation
    Facts (ReviewScopeObservationFact.observation_payload).  This is a SEPARATE
    owner from ReviewScopeSummaryDTO (Composition thin projection): it is
    sourced ONLY from the Fact and MUST NOT be gated on composition_present.
    Every field is Optional — a missing/unavailable value is None and MUST NOT
    be coerced to 0 (e.g. freshnessTodayCount=0 is a valid zero, not None).
    No derived metric (no top5 ratio) is computed here.
    """

    model_config = ConfigDict(populate_by_name=True)

    freshnessTodayCount: int | None = None
    freshnessDecayWeightedDensity: float | None = None

    technicalHhi: float | None = None

    technicalTop5Numerator: float | None = None
    technicalTop5Denominator: float | None = None

    technicalLeaderMedianGap: float | None = None
    technicalLeaderSymbol: str | None = None
    technicalMemberCount: int | None = None


class ReviewCanonicalScopeResponse(BaseModel):
    """GET /api/v1/review/{trade_date}/scopes 单条记录（canonical）。

    [REVIEW-CANONICAL-RUNTIME-REPLACEMENT] 该端点不再返回 legacy P/Q/U/C/V
    （MarketReviewScopeSnapshot 已退役），改读 canonical ReviewScopeObservationFact
    + run.metadata_json["canonical_composition_readiness"/"canonical_coverage"]。

    readiness 为 canonical composition readiness（唯一发布判断依据）；status 为
    fact 级 PIT 状态；summary 为 persisted Composition 的薄投影（Slice B），
    只展示、不重算。完整 Observation 由 scope detail endpoint 提供，列表不再
    复制完整 Observation payload（避免 100×~130KiB over-fetch）。
    """

    model_config = ConfigDict(populate_by_name=True, from_attributes=True)

    scopeType: str = Field(..., description="范围类型（industry_l1/l2/l3/concept）")
    scopeKey: str = Field(..., description="范围标识")
    scopeName: str | None = Field(None, description="范围名称")
    readiness: str = Field(..., description="canonical composition readiness")
    status: str = Field(..., description="fact 级 PIT 状态")
    eligibleCount: int = Field(0, description="PIT(T) 成员数（分母）")
    providedCount: int = Field(0, description="实际提供成员观察数")
    coverageRatio: float | None = Field(None, description="provided/eligible 覆盖率")
    summary: ReviewScopeSummaryDTO | None = Field(
        None, description="persisted Composition 薄投影；Composition 缺失时为 None",
    )
    observationSummary: ReviewScopeObservationSummaryDTO | None = Field(
        None,
        description=(
            "persisted Observation Fact 薄投影（R2B）；仅含 freshness / technical "
            "标量，独立于 summary。Fact 存在即填充，不依赖 Composition"
        ),
    )


class ReviewScopeListResponse(BaseModel):
    """GET /api/v1/review/{trade_date}/scopes 分页响应（canonical facts）。"""

    items: list[ReviewCanonicalScopeResponse] = Field(default_factory=list)
    total: int = Field(0, description="总数")
    page: int = Field(1, description="当前页码")
    page_size: int = Field(20, description="每页大小")
    has_more: bool = Field(False, description="是否有下一页")


class ReviewScopeHistoryFieldDTO(BaseModel):
    """One canonical field's 20D rolling diagnostics (aligned to ``history.dates``).

    ``series`` / ``mean20`` / ``std20`` / ``zscore20`` / ``percentile20`` /
    ``baselineCount`` are all aligned to the same display-window axis (ascending).
    Missing canonical values are ``None`` (never 0). ``zscore20`` is ``None`` when
    the lagged baseline std == 0 or has < 2 finite samples.
    """

    model_config = ConfigDict(populate_by_name=True, from_attributes=True)

    key: str = Field(..., description="curated history field key")
    label: str = Field(..., description="展示标签")
    unit: str | None = Field(None, description="pct / ratio / None")
    series: list[float | None] = Field(default_factory=list, description="display-window 原始值")
    mean20: list[float | None] = Field(default_factory=list, description="lagged baseline mean(T-20..T-1)")
    std20: list[float | None] = Field(default_factory=list, description="lagged baseline population std")
    zscore20: list[float | None] = Field(default_factory=list, description="(v-mean20)/std20，std==0→None")
    percentile20: list[float | None] = Field(default_factory=list, description="自身在 trailing 窗口的经验分位 [0,100]")
    baselineCount: list[int | None] = Field(default_factory=list, description="每个点 lagged baseline 的有限样本数")


class ReviewScopeSmcHistoryDTO(BaseModel):
    """[R3 History / SMC] 窄 SMC 历史投影（published-run 安全日序列 query-time 构建）。

    与 ReviewScopeHistoryDTO 共享同一正式 published 日期轴。swing_state / internal_state
    每个日期槽为结构状态分布（up/neutral/down ratio + denominator），缺失该日 fact 为 null
    （保留 date slot，显示 gap）。event_tape 每个日期槽为 canonical structure.events
    （status / cells），缺失为 null。不重新查询 / 不重算 canonical SMC。
    """

    model_config = ConfigDict(populate_by_name=True, from_attributes=True)

    dates: list[str] = Field(default_factory=list, description="display-window 交易日（升序，共享 history 日期轴）")
    swing_state: list[dict[str, Any] | None] = Field(
        default_factory=list, description="每个日期槽 Swing 状态分布（up/neutral/down ratio + denominator）；缺失为 null"
    )
    internal_state: list[dict[str, Any] | None] = Field(
        default_factory=list, description="每个日期槽 Internal 状态分布；缺失为 null"
    )
    event_tape: list[dict[str, Any] | None] = Field(
        default_factory=list, description="每个日期槽 canonical structure.events；缺失为 null"
    )


class ReviewScopeHistoryDTO(BaseModel):
    """[R3 History] 20D 历史诊断 DTO（由 published-run 安全日序列 query-time 构建）。"""

    model_config = ConfigDict(populate_by_name=True, from_attributes=True)

    dates: list[str] = Field(default_factory=list, description="display-window 交易日（升序）")
    displayWindow: int = Field(20, description="展示窗口（默认 20 交易日）")
    availability: dict[str, Any] = Field(
        default_factory=dict,
        description="状态：ready / empty / not_activated；含 totalSnapshots / fromDate / toDate",
    )
    fields: dict[str, ReviewScopeHistoryFieldDTO] = Field(
        default_factory=dict, description="curated 历史字段 -> 滚动诊断"
    )
    smc: ReviewScopeSmcHistoryDTO | None = Field(
        None,
        description=(
            "[R3 History / SMC] 窄 SMC 历史投影（structure swing/internal state + event tape），"
            "复用同一 published-run 安全日序列；非 activated scope_type 为 None"
        ),
    )


class ReviewScopeCompositionDetailResponse(BaseModel):
    """GET /api/v1/review/{trade_date}/scopes/{scope_type}/{scope_key} 完整响应。

    [R3A Canonical Observation Detail Contract] Scope Detail 以 Observation
    Fact 为第一归属：``observation`` 与 ``observationGroups`` 在每个成功响应中
    均 NON-NULL；``composition`` 为可选 enrichment（缺失 = null，仍 200）。
    响应身份优先取自 canonical Fact（scopeType/scopeKey/scopeName/tradeDate）。
    """

    model_config = ConfigDict(populate_by_name=True, from_attributes=True)

    reviewRunId: str = Field(..., description="所属 canonical published ReviewRun id")
    tradeDate: str = Field(..., description="交易日 YYYY-MM-DD（来自 canonical Fact）")
    scopeType: str = Field(..., description="范围类型（来自 canonical Fact）")
    scopeKey: str = Field(..., description="范围标识（来自 canonical Fact）")
    scopeName: str | None = Field(None, description="范围名称（来自 canonical Fact）")
    algorithmVersion: str = Field(
        ...,
        description=(
            "算法版本元数据回退（仅 metadata 选择，不业务计算）："
            "snapshot > fact.algorithm_version > run.algorithm_version；"
            "不发明 unknown/legacy/default"
        ),
    )
    observation: dict[str, Any] = Field(
        ...,
        description="Canonical Observation Fact L1 客观事实 payload（detail 最小存在 owner，成功响应中 NON-NULL）",
    )
    observationGroups: dict[str, Any] = Field(
        ...,
        description=(
            "Canonical L2 Observation Groups（由 fact.observation_payload 经 "
            "build_l2_observation_groups 直接投影的 8 个固定组："
            "price_capital / trend_state / trend_progress / trend_volume_confirmation / "
            "structure_break_turn / structure_evolution_position / "
            "momentum_squeeze_release / volume_anomaly），成功响应中 NON-NULL"
        ),
    )
    composition: dict[str, Any] | None = Field(
        None,
        description=(
            "可选 enrichment：完整 Canonical Composition（固定 9 个 top-level keys："
            "scope / trade_date / capability / scope_observation / "
            "historical_dynamics / internal_structure_facts / leadership / "
            "member_attribution / composition_readiness）；缺失时为 null（Fact-only detail）"
        ),
    )
    memberDirectory: dict[str, dict[str, str]] = Field(
        default_factory=dict,
        description=(
            "[DSA closure] 成员身份目录（ADDITIVE display metadata，不写回、不改写 persisted "
            "Observation/Composition）：``{instrument_uuid: {symbol, name}}``。"
            "ref IDs = Composition leadership/attribution 引用 UNION "
            "observation.trend.transition.changed_members UNION "
            "observation.structure.swing.transition.changed_members UNION "
            "observation.structure.internal.transition.changed_members；ONE bulk Instrument query "
            "（去重后一次性查）。Composition=null 不意味着目录一定为空：只要 Observation 任一 transition "
            "含 changed-member UUID，目录也能生成。前端唯一展示 owner ``displayMember(id)`` 用其解析 名称+代码。"
        ),
    )
    history: ReviewScopeHistoryDTO | None = Field(
        None,
        description=(
            "[R3 History] 由 published-run 安全日序列 query-time 构建的 20D 滚动诊断 "
            "(regime_strength / vwap_dev / returns / breadth / dispersion / hhi / "
            "bb_position / bb_width / volume ratio+zscore / trend composition)。"
            "非 activated scope_type 为 None；不足历史为 availability.empty。"
        ),
    )
    crossSection: dict[str, Any] | None = Field(
        None,
        description=(
            "[R3 Cross-sectional P0] published-run lineage 安全的横截面位置证据 "
            "(C1 empirical percentile)。market scope 无 peer -> None。"
        ),
    )


# =============================================================================
# 信号
# =============================================================================


class ReviewSignalResponse(BaseModel):
    """GET /api/v1/review/{trade_date}/signals 单条记录（PRD §12.3、§14.4）。

    一张 SignalCard 必须显示：
    - 范围；信号类型；生命周期状态；首次出现日期和持续日数；
    - 触发变量；历史分位；coverage；结构化解释。
    """

    model_config = ConfigDict(populate_by_name=True, from_attributes=True)

    id: str = Field(..., description="信号 ID（UUID）")
    reviewRunId: str = Field(..., description="复盘 run ID")
    tradeDate: str = Field(..., description="业务交易日")
    filterFamily: str = Field(..., description="筛选器族：A/B/C/D")
    signalType: str = Field(..., description="信号类型")
    scopeType: str = Field(..., description="范围类型")
    scopeKey: str = Field(..., description="范围标识")
    scopeName: str = Field(..., description="范围名称")
    status: str = Field(
        ..., description="生命周期状态：new/continuing/confirmed/weakened/invalidated/transformed",
    )
    firstSeenDate: str = Field(..., description="信号首次出现日期")
    previousSignalId: str | None = Field(None, description="前一交易日信号 ID")
    transformedToSignalId: str | None = Field(
        None, description="转化后的新信号 ID（status=transformed 时填充）",
    )
    triggerPayload: dict[str, Any] = Field(
        default_factory=dict, description="触发条件 payload",
    )
    baselinePayload: dict[str, Any] = Field(
        default_factory=dict, description="基线 payload",
    )
    evidencePayload: dict[str, Any] = Field(
        default_factory=dict, description="证据 payload（结构化解释）",
    )
    confirmationRule: dict[str, Any] = Field(
        default_factory=dict, description="确认规则 JSON",
    )
    invalidationRule: dict[str, Any] = Field(
        default_factory=dict, description="失效规则 JSON",
    )
    coverageRatio: float | None = Field(None, description="覆盖率")
    rankKey: dict[str, Any] = Field(
        default_factory=dict, description="排序键 JSON（PRD §8.4）",
    )
    durationDays: int = Field(
        0, description="持续日数（trade_date - first_seen_date，API 层注入）",
    )
    createdAt: str | None = Field(None, description="创建时间 ISO")


class ReviewSignalListResponse(BaseModel):
    """GET /api/v1/review/{trade_date}/signals 分页响应。"""

    items: list[ReviewSignalResponse] = Field(default_factory=list)
    total: int = Field(0)
    page: int = Field(1)
    page_size: int = Field(20)
    has_more: bool = Field(False)


# =============================================================================
# 归因与个股
# =============================================================================


class ReviewAttributionResponse(BaseModel):
    """GET /api/v1/review/signals/{signal_id}/attributions 单条记录（PRD §12.4、§9.1）。"""

    model_config = ConfigDict(populate_by_name=True, from_attributes=True)

    id: str = Field(..., description="归因 ID（UUID）")
    signalId: str = Field(..., description="信号 ID")
    childScopeType: str = Field(..., description="子范围类型")
    childScopeKey: str = Field(..., description="子范围标识")
    childScopeName: str = Field(..., description="子范围名称")
    relationType: str | None = Field(None, description="与父范围关系类型")
    contributionValue: float | None = Field(None, description="贡献值（可正可负）")
    contributionRank: int | None = Field(None, description="贡献排名（按绝对贡献排序）")
    metricsPayload: dict[str, Any] = Field(
        default_factory=dict, description="子范围指标 payload",
    )
    evidencePayload: dict[str, Any] = Field(
        default_factory=dict, description="证据 payload",
    )
    coverageRatio: float | None = Field(None, description="覆盖率")
    sourceBoardSnapshotId: str | None = Field(None, description="Board snapshot ID")
    taxonomyVersion: str | None = Field(None, description="taxonomy version")
    taxonomyCompatibilityKey: str | None = Field(None, description="taxonomy compatibility key")
    membershipVersion: str | None = Field(None, description="PIT membership version")
    eligibleCount: int | None = Field(None, description="PIT eligible count")
    readyCount: int | None = Field(None, description="ready count")
    dataQuality: dict[str, Any] = Field(default_factory=dict)
    createdAt: str | None = Field(None, description="创建时间 ISO")


class ReviewAttributionListResponse(BaseModel):
    """GET /api/v1/review/signals/{signal_id}/attributions 分页响应。"""

    items: list[ReviewAttributionResponse] = Field(default_factory=list)
    total: int = Field(0)
    page: int = Field(1)
    page_size: int = Field(20)
    has_more: bool = Field(False)


# =============================================================================
# 管理端
# =============================================================================


class ReviewRunCreateRequest(BaseModel):
    """POST /api/v1/admin/review/runs 请求体。"""

    trade_date: str = Field(..., description="业务交易日（ISO YYYY-MM-DD）")
    source_core_run_id: str | None = Field(
        None, description="输入 stock_core run ID（None 时从最新发布 pointer 读取）",
    )
    source_board_run_id: str | None = Field(
        None,
        description=(
            "[DEPRECATED Slice 3] Board Analysis 不再是 Review 输入。本字段必须为空；"
            "传入非 NULL 值将被拒绝。"
        ),
    )
    algorithm_version: str | None = Field(None, description="算法版本（默认 REVIEW_ALGORITHM_VERSION）")
    filter_version: str | None = Field(None, description="筛选器版本（默认 REVIEW_FILTER_VERSION）")
    baseline_window: int = Field(120, ge=60, description="历史基线窗口（默认 120，最低 60）")
    canary: bool = Field(False, description="是否 canary 模式（限定范围数）")
    symbols: list[str] | None = Field(
        None, description="限定股票列表（canary/debug 用，None=全市场）",
    )
    dry_run: bool = Field(
        False, description="dry-run 模式：只校验输入，不写 run/snapshot",
    )
    idempotency_key: str = Field(..., description="幂等键")

    @model_validator(mode="after")
    def _reject_board_input(self) -> ReviewRunCreateRequest:
        # [Slice 3] Board Analysis 不再是 Review 输入；历史 lineage 仍保留在既有 run，
        # 但新请求不得指定 source_board_run_id（避免重建已废弃依赖）。
        if self.source_board_run_id is not None:
            raise ValueError(
                "source_board_run_id is no longer a supported Review input "
                "(Board Analysis is not a Review prerequisite since Slice 3); "
                "omit this field or pass null"
            )
        return self


class ReviewRunResponse(BaseModel):
    """复盘 run 响应（管理端 status/publish/resume 共用）。"""

    model_config = ConfigDict(populate_by_name=True, from_attributes=True)

    id: str = Field(..., description="run ID（UUID）")
    trade_date: str = Field(..., description="业务交易日")
    source_core_run_id: str = Field(..., description="输入 stock_core run ID")
    source_board_run_id: str | None = Field(
        None, description="[Slice 3] legacy board_analysis run ID（新 run 为 null）",
    )
    source_chip_run_id: str | None = Field(
        None,
        description=(
            "[P0 2026-08-04] chip 无独立可追溯 snapshot run ID，本字段恒为 null；"
            "chip 真实质量读取 chip_coverage 与 degraded_reasons。"
        ),
    )
    degraded_reasons: list[str] = Field(
        default_factory=list,
        description="[QM-63] 降级原因列表；空数组表示无降级",
    )
    chip_coverage: ReviewChipCoverageDTO | None = Field(
        None,
        description="[P0 2026-08-04] chip 真实覆盖率明细（以 expected_count 为分母）",
    )
    algorithm_version: str = Field(..., description="算法版本")
    filter_version: str = Field(..., description="筛选器版本")
    baseline_window: int = Field(..., description="历史基线窗口")
    status: str = Field(..., description="run 状态")
    expected_scope_count: int = Field(0)
    succeeded_scope_count: int = Field(0)
    failed_scope_count: int = Field(0)
    signal_count: int = Field(0)
    coverage_ratio: float | None = Field(None)
    started_at: str | None = Field(None)
    completed_at: str | None = Field(None)
    published_at: str | None = Field(None)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(..., description="创建时间 ISO")
    updated_at: str = Field(..., description="更新时间 ISO")


class ReviewRunResumeRequest(BaseModel):
    """POST /api/v1/admin/review/runs/{id}/resume 请求体。"""

    idempotency_key: str = Field(..., description="幂等键")
    only_pending: bool = Field(
        True,
        description="只处理 pending/可重试 failed/过期 running（默认 True）",
    )


class ReviewRunPublishRequest(BaseModel):
    """POST /api/v1/admin/review/runs/{id}/publish 请求体。"""

    idempotency_key: str = Field(..., description="幂等键")
    force: bool = Field(
        False,
        description=(
            "[P0 安全收口 2026-08-01] True 时只生成 provisional 标记"
            "（不写正式 pointer、run 不进入 published），仅 admin 调试用"
        ),
    )


class ReviewRunStatusItemDTO(BaseModel):
    """run status 响应中的 item 状态明细（PRD §11）。"""

    scope_type: str = Field(...)
    scope_key: str = Field(...)
    phase: str = Field(...)
    status: str = Field(...)
    attempt_count: int = Field(0)
    last_error: str | None = Field(None)
    started_at: str | None = Field(None)
    completed_at: str | None = Field(None)


class ReviewRunStatusResponse(BaseModel):
    """GET /api/v1/admin/review/runs/{id}/status 响应。"""

    run: ReviewRunResponse
    items: list[ReviewRunStatusItemDTO] = Field(default_factory=list)
    publishable: bool = Field(
        False, description="是否满足发布门禁（PRD §11.1）",
    )
    publish_blockers: list[str] = Field(
        default_factory=list, description="发布门禁失败原因列表",
    )


# =============================================================================
# Review 历史 bootstrap（正式 admin 入口）
# =============================================================================


if __name__ == "__main__":
    # 自测：构造最小合法 payload
    comp = ReviewMetricComponentDTO(
        name="advance_ratio",
        rawValue=0.62,
        normalizedValue=62.0,
        direction="positive",
        denominator=100,
        fieldSource="fp_trend_direction",
        weight=1.0,
        coverage=0.98,
    )
    payload = ReviewMetricPayloadDTO(
        value=62.0,
        rawValue=0.62,
        delta1d=2.5,
        delta5d=4.1,
        historyPercentile120d=78.2,
        crossSectionPercentile=84.0,
        historyObservationCount=120,
        components=[comp],
        coverage=0.98,
        status="ready",
    )
    assert payload.value == 62.0
    assert payload.components[0].fieldSource == "fp_trend_direction"
    print(f"OK: ReviewMetricPayloadDTO value={payload.value} components={len(payload.components)}")

    overview = ReviewOverviewResponse(
        reviewRunId="00000000-0000-0000-0000-000000000001",
        tradeDate="2026-07-29",
        status="published",
        sourceCoreRunId="00000000-0000-0000-0000-000000000002",
        sourceBoardRunId="00000000-0000-0000-0000-000000000003",
        algorithmVersion="review-1.0.0",
        filterVersion="filters-1.0.0",
        baselineWindow=120,
    )
    assert overview.coverage.market is None
    print(f"OK: ReviewOverviewResponse status={overview.status}")
    print("OK: review schemas verified")
