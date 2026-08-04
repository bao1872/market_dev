"""复盘模块 API Schema - 复盘工作台响应契约（PRD §12）。

对应 ORM 模型 `app.models.market_review` 8 张表：
- MarketReviewRun / MarketReviewRunItem / MarketReviewScopeSnapshot
- MarketReviewSignal / MarketReviewSignalAttribution / MarketReviewSignalInstrument
- MarketReviewTracking / MarketReviewTrackingEvaluation

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

from pydantic import BaseModel, ConfigDict, Field

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


class ReviewOverviewSignalSummaryDTO(BaseModel):
    """overview.signalSummary 子结构（PRD §12.1）。"""

    new: int = Field(0, description="new 状态信号数")
    continuing: int = Field(0, description="continuing 状态信号数")
    confirmed: int = Field(0, description="confirmed 状态信号数")
    weakened: int = Field(0, description="weakened 状态信号数")
    invalidated: int = Field(0, description="invalidated 状态信号数")
    transformed: int = Field(0, description="transformed 状态信号数")


class ReviewOverviewResponse(BaseModel):
    """GET /api/v1/review/{trade_date}/overview 响应（PRD §12.1）。"""

    reviewRunId: str = Field(..., description="复盘 run ID（UUID）")
    tradeDate: str = Field(..., description="业务交易日（ISO YYYY-MM-DD）")
    status: str = Field(..., description="run 状态")
    sourceCoreRunId: str = Field(..., description="输入 stock_core run ID")
    sourceBoardRunId: str = Field(..., description="输入 board_analysis run ID")
    sourceChipRunId: str | None = Field(
        None,
        description=(
            "[QM-63] 输入 chip 共识 run ID；null 表示 chip 不可用，"
            "本次 run 降级为 core-only（不得理解为未记录）"
        ),
    )
    degradedReasons: list[str] = Field(
        default_factory=list,
        description=(
            "[QM-63] 降级原因列表（CHIP_UNAVAILABLE / CHIP_PARTIAL 等）；"
            "空数组表示无降级"
        ),
    )
    algorithmVersion: str = Field(..., description="算法版本")
    filterVersion: str = Field(..., description="筛选器版本")
    baselineWindow: int = Field(120, description="历史基线窗口（默认 120，最低 60）")
    coverage: ReviewOverviewCoverageDTO = Field(
        default_factory=ReviewOverviewCoverageDTO,
        description="整体覆盖率明细",
    )
    signalSummary: ReviewOverviewSignalSummaryDTO = Field(
        default_factory=ReviewOverviewSignalSummaryDTO,
        description="信号状态汇总",
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


class ReviewScopeMetricsResponse(BaseModel):
    """GET /api/v1/review/{trade_date}/scopes 单条记录。

    返回每个范围的 P/Q/U/C/V、变化、历史分位和命中数量（PRD §14.3）。
    """

    model_config = ConfigDict(populate_by_name=True, from_attributes=True)

    id: str = Field(..., description="快照 ID（UUID）")
    reviewRunId: str = Field(..., description="复盘 run ID")
    tradeDate: str = Field(..., description="业务交易日")
    scopeType: str = Field(..., description="范围类型（market/major_index/...）")
    scopeKey: str = Field(..., description="范围标识")
    scopeName: str = Field(..., description="范围名称")
    parentScopeType: str | None = Field(None, description="父范围类型")
    parentScopeKey: str | None = Field(None, description="父范围标识")
    eligibleCount: int = Field(..., description="范围成员总数")
    readyCount: int = Field(..., description="有效成员数")
    coverageRatio: float = Field(..., description="覆盖率")
    status: str = Field(..., description="快照状态")
    p: ReviewMetricPayloadDTO | None = Field(None, description="P 价格表现强度")
    q: ReviewMetricPayloadDTO | None = Field(None, description="Q 内部结构质量")
    u: ReviewMetricPayloadDTO | None = Field(None, description="U 参与范围")
    c: ReviewMetricPayloadDTO | None = Field(None, description="C 集中程度")
    v: ReviewMetricPayloadDTO | None = Field(None, description="V 成交活跃与效率")
    dataQuality: dict[str, Any] | None = Field(None, description="数据质量明细")
    signalCount: int = Field(0, description="该范围命中信号数（API 层注入）")


class ReviewScopeListResponse(BaseModel):
    """GET /api/v1/review/{trade_date}/scopes 分页响应。"""

    items: list[ReviewScopeMetricsResponse] = Field(default_factory=list)
    total: int = Field(0, description="总数")
    page: int = Field(1, description="当前页码")
    page_size: int = Field(20, description="每页大小")
    has_more: bool = Field(False, description="是否有下一页")


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


class ReviewInstrumentResponse(BaseModel):
    """GET /api/v1/review/signals/{signal_id}/instruments 单条记录（PRD §12.4、§9.2、§14.6）。"""

    model_config = ConfigDict(populate_by_name=True, from_attributes=True)

    id: str = Field(..., description="instrument 记录 ID（UUID）")
    signalId: str = Field(..., description="信号 ID")
    instrumentId: str = Field(..., description="instruments.id")
    symbol: str = Field(..., description="股票代码")
    name: str = Field(..., description="股票名称")
    boardRole: str | None = Field(
        None,
        description="板块角色：core/second_line/elasticity/follower/laggard/unclassified",
    )
    relationToScope: str | None = Field(
        None,
        description=(
            "与板块关系：synchronized_strengthening/synchronized_weakening/"
            "instrument_leads_scope/scope_strong_instrument_lags/"
            "instrument_strong_scope_unsupported/unconfirmed"
        ),
    )
    contributionValue: float | None = Field(None, description="贡献值")
    contributionRank: int | None = Field(None, description="贡献排名")
    firstPyramidPayload: dict[str, Any] = Field(
        default_factory=dict, description="第一金字塔 payload（趋势/结构/动量/筹码）",
    )
    freshEventsPayload: dict[str, Any] = Field(
        default_factory=dict, description="新鲜事件 payload",
    )
    contributionPayload: dict[str, Any] = Field(
        default_factory=dict, description="P/Q/U/C/V 分项贡献与分母",
    )
    roleEvidence: dict[str, Any] = Field(
        default_factory=dict, description="角色判定结构化证据",
    )
    sourceSnapshotId: str | None = Field(
        None, description="来源单股快照 ID（stock_feature_snapshots.id）",
    )
    createdAt: str | None = Field(None, description="创建时间 ISO")


class ReviewInstrumentListResponse(BaseModel):
    """GET /api/v1/review/signals/{signal_id}/instruments 分页响应。"""

    items: list[ReviewInstrumentResponse] = Field(default_factory=list)
    total: int = Field(0)
    page: int = Field(1)
    page_size: int = Field(20)
    has_more: bool = Field(False)


# =============================================================================
# 追踪
# =============================================================================


class ReviewTrackingResponse(BaseModel):
    """GET /api/v1/review/trackings 单条记录（PRD §12.5、§10.2）。"""

    model_config = ConfigDict(populate_by_name=True, from_attributes=True)

    id: str = Field(..., description="追踪 ID（UUID）")
    userId: str = Field(..., description="用户 ID")
    sourceSignalId: str | None = Field(None, description="关联信号 ID")
    trackingType: str = Field(..., description="追踪类型：signal/scope/instrument")
    scopeType: str | None = Field(None, description="范围类型（追踪 scope 时填充）")
    scopeKey: str | None = Field(None, description="范围标识（追踪 scope 时填充）")
    instrumentId: str | None = Field(None, description="instruments.id（追踪 instrument 时填充）")
    status: str = Field(..., description="状态：active/confirmed/invalidated/closed")
    confirmationConditions: dict[str, Any] = Field(
        default_factory=dict, description="确认条件 JSON",
    )
    invalidationConditions: dict[str, Any] = Field(
        default_factory=dict, description="失效条件 JSON",
    )
    note: str | None = Field(None, description="用户备注")
    createdAt: str = Field(..., description="创建时间 ISO")
    closedAt: str | None = Field(None, description="关闭时间 ISO")


class ReviewTrackingListResponse(BaseModel):
    """GET /api/v1/review/trackings 分页响应。"""

    items: list[ReviewTrackingResponse] = Field(default_factory=list)
    total: int = Field(0)
    page: int = Field(1)
    page_size: int = Field(20)
    has_more: bool = Field(False)


class ReviewTrackingCreateRequest(BaseModel):
    """POST /api/v1/review/trackings 请求体。"""

    tracking_type: str = Field(..., description="追踪类型：signal/scope/instrument")
    source_signal_id: str | None = Field(None, description="信号 ID（追踪 signal 时必填）")
    scope_type: str | None = Field(None, description="范围类型（追踪 scope 时必填）")
    scope_key: str | None = Field(None, description="范围标识（追踪 scope 时必填）")
    instrument_id: str | None = Field(None, description="instruments.id（追踪 instrument 时必填）")
    confirmation_conditions: dict[str, Any] = Field(
        default_factory=dict, description="确认条件 JSON",
    )
    invalidation_conditions: dict[str, Any] = Field(
        default_factory=dict, description="失效条件 JSON",
    )
    note: str | None = Field(None, description="用户备注")
    idempotency_key: str = Field(
        ..., description="幂等键（PRD §12.6 所有写操作要求幂等键）",
    )


class ReviewTrackingPatchRequest(BaseModel):
    """PATCH /api/v1/review/trackings/{id} 请求体（部分字段更新）。"""

    status: str | None = Field(
        None, description="状态：active/confirmed/invalidated/closed",
    )
    confirmation_conditions: dict[str, Any] | None = Field(None)
    invalidation_conditions: dict[str, Any] | None = Field(None)
    note: str | None = Field(None)
    idempotency_key: str = Field(..., description="幂等键")


class ReviewTrackingEvaluationResponse(BaseModel):
    """GET /api/v1/review/trackings/{id}/evaluations 单条记录（PRD §5.8、§10.2）。"""

    model_config = ConfigDict(populate_by_name=True, from_attributes=True)

    id: str = Field(..., description="评估记录 ID（UUID）")
    trackingId: str = Field(..., description="追踪 ID")
    reviewRunId: str = Field(..., description="复盘 run ID")
    tradeDate: str = Field(..., description="业务交易日")
    previousState: str | None = Field(None, description="前一交易日状态")
    currentState: str = Field(..., description="当日状态")
    evaluationPayload: dict[str, Any] = Field(
        default_factory=dict, description="评估 payload",
    )
    createdAt: str = Field(..., description="创建时间 ISO")


class ReviewTrackingEvaluationListResponse(BaseModel):
    """GET /api/v1/review/trackings/{id}/evaluations 分页响应。"""

    items: list[ReviewTrackingEvaluationResponse] = Field(default_factory=list)
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
        None, description="输入 board_analysis run ID（None 时从最新发布 pointer 读取）",
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


class ReviewRunResponse(BaseModel):
    """复盘 run 响应（管理端 status/publish/resume 共用）。"""

    model_config = ConfigDict(populate_by_name=True, from_attributes=True)

    id: str = Field(..., description="run ID（UUID）")
    trade_date: str = Field(..., description="业务交易日")
    source_core_run_id: str = Field(..., description="输入 stock_core run ID")
    source_board_run_id: str = Field(..., description="输入 board_analysis run ID")
    source_chip_run_id: str | None = Field(
        None,
        description=(
            "[QM-63] 输入 chip 共识 run ID；null 表示 chip 不可用，"
            "本次 run 降级为 core-only"
        ),
    )
    degraded_reasons: list[str] = Field(
        default_factory=list,
        description="[QM-63] 降级原因列表；空数组表示无降级",
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


class ReviewBootstrapRequest(BaseModel):
    """POST /api/v1/admin/review/bootstrap 请求体。

    dry_run 默认 True：默认不产生任何业务写入，必须显式传 False 才会回填。
    """

    end_date: str | None = Field(
        None,
        description="截止交易日（ISO YYYY-MM-DD）；为空时解析为最近一个完整 A 股交易日",
    )
    days_back: int = Field(
        120, ge=60, description="回溯交易日数（默认 120，最低 60）",
    )
    algorithm_version: str | None = Field(
        None, description="显式算法版本（默认当前 REVIEW_ALGORITHM_VERSION）",
    )
    operator: str = Field(
        ..., min_length=1, description="执行人标识（必填，审计用）",
    )
    reason: str = Field(
        ..., min_length=1, description="执行原因（必填，审计用）",
    )
    dry_run: bool = Field(
        True, description="True=只计算不写业务数据（默认）",
    )


class ReviewBootstrapSubmitResponse(BaseModel):
    """POST /api/v1/admin/review/bootstrap 响应（202 Accepted）。

    历史回填是长任务，API 只提交 queued 任务并立即返回 job_run_id，
    真正计算由 Worker 领取执行；进度经 status 接口查询。
    """

    job_run_id: str = Field(..., description="bootstrap 任务 ID（用于查询状态）")
    status: str = Field(..., description="任务状态（queued）")
    is_new: bool = Field(
        ..., description="False 表示复用已有活跃任务（幂等）",
    )
    dry_run: bool = Field(..., description="是否 dry-run")
    end_date: str = Field(..., description="解析后的截止交易日")
    days_back: int = Field(..., description="回溯交易日数")
    algorithm_version: str = Field(..., description="解析后的算法版本")
    operator: str = Field(..., description="执行人标识")
    reason: str = Field(..., description="执行原因")
    input_hash: str = Field(..., description="输入指纹（同范围多次执行一致）")
    message: str = Field(..., description="提示信息")


class ReviewBootstrapScopeCounts(BaseModel):
    """按 scope 的四类计数。"""

    succeeded: int = Field(0, description="成功产出观测的 scope 数")
    skipped: int = Field(0, description="幂等跳过的 scope 数")
    unavailable: int = Field(
        0, description="PIT 成员或历史事实缺失，不可用的 scope 数",
    )
    failed: int = Field(0, description="执行失败的 scope 数")


class ReviewBootstrapSummary(BaseModel):
    """bootstrap 全局执行摘要。"""

    eligible_dates: int = Field(0, description="可回填的交易日数")
    processed: int = Field(0, description="已处理交易日数")
    skipped: int = Field(0, description="跳过交易日数")
    written: int = Field(0, description="实际写入交易日数（dry-run 恒为 0）")
    scope_counts: ReviewBootstrapScopeCounts = Field(
        default_factory=ReviewBootstrapScopeCounts,
    )
    reason_codes: dict[str, int] = Field(
        default_factory=dict, description="不可用/失败原因码计数",
    )


class ReviewBootstrapScopeResult(BaseModel):
    """单条 (trade_date, scope_type, scope_key) 执行明细。"""

    trade_date: str | None = Field(None)
    scope_type: str | None = Field(None)
    scope_key: str | None = Field(None)
    status: str | None = Field(None)
    reason: str | None = Field(None, description="不可用/失败原因码")
    eligible_count: int | None = Field(None)
    ready_count: int | None = Field(None)
    coverage: float | None = Field(None)


class ReviewBootstrapStatusResponse(BaseModel):
    """GET /api/v1/admin/review/bootstrap/{job_run_id} 响应。

    全局 summary 常驻返回；scope 明细分页（120 日 × 全 scope 可达上万行）。
    """

    job_run_id: str = Field(...)
    job_name: str = Field(...)
    status: str = Field(..., description="SchedulerJobRun 状态")
    bootstrap_status: str = Field(..., description="bootstrap 业务状态")
    dry_run: bool = Field(...)
    operator: str | None = Field(None)
    reason: str | None = Field(None)
    input_hash: str | None = Field(None)
    end_date: str | None = Field(None)
    days_back: int | None = Field(None)
    algorithm_version: str | None = Field(None)
    summary: ReviewBootstrapSummary = Field(default_factory=ReviewBootstrapSummary)
    scope_results: list[ReviewBootstrapScopeResult] = Field(default_factory=list)
    scope_results_total: int = Field(0, description="明细总行数（用于分页）")
    offset: int = Field(0)
    limit: int = Field(0)
    started_at: str | None = Field(None)
    finished_at: str | None = Field(None)
    heartbeat_at: str | None = Field(None)
    error_code: str | None = Field(None)
    error_message: str | None = Field(None)


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
    assert overview.signalSummary.new == 0
    print(f"OK: ReviewOverviewResponse status={overview.status}")

    # bootstrap 请求：dry_run 默认 True，operator/reason 必填
    boot_req = ReviewBootstrapRequest(operator="owner", reason="review-2.0.0 回填")
    assert boot_req.dry_run is True, "dry_run 必须默认 True"
    assert boot_req.days_back == 120
    assert boot_req.end_date is None, "end_date 默认 None（由服务端解析交易日）"
    try:
        ReviewBootstrapRequest(operator="", reason="x")
    except Exception:
        pass
    else:  # pragma: no cover - 防御性断言
        raise AssertionError("operator 为空应校验失败")
    print(f"OK: ReviewBootstrapRequest dry_run={boot_req.dry_run}")

    boot_status = ReviewBootstrapStatusResponse(
        job_run_id="00000000-0000-0000-0000-000000000009",
        job_name="review_bootstrap",
        status="succeeded",
        bootstrap_status="ok",
        dry_run=True,
    )
    assert boot_status.summary.scope_counts.unavailable == 0
    assert boot_status.scope_results_total == 0
    print(f"OK: ReviewBootstrapStatusResponse status={boot_status.status}")
    print("OK: review schemas verified")
