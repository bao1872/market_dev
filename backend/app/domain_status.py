"""V2.1 领域集中状态枚举。

[PRD V2.1 §4.3 ORCH-03 / next.md EPIC-01 E01-T04]
- 集中定义 run status / readiness / auction mode / production closure，
  避免后端多处字符串、前端自建枚举、DB 与 API 命名不一致。
- 所有枚举以 frozenset 提供，供校验与转换使用。
"""

from __future__ import annotations

# ===== run status（领域 run 通用）=====
RUN_STATUS_QUEUED = "queued"
RUN_STATUS_RUNNING = "running"
RUN_STATUS_SUCCEEDED = "succeeded"
RUN_STATUS_PARTIAL = "partial"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_SKIPPED = "skipped"
RUN_STATUS_INTERRUPTED = "interrupted"
RUN_STATUS_CANCELLED = "cancelled"

ALL_RUN_STATUS = frozenset({
    RUN_STATUS_QUEUED,
    RUN_STATUS_RUNNING,
    RUN_STATUS_SUCCEEDED,
    RUN_STATUS_PARTIAL,
    RUN_STATUS_FAILED,
    RUN_STATUS_SKIPPED,
    RUN_STATUS_INTERRUPTED,
    RUN_STATUS_CANCELLED,
})

TERMINAL_RUN_STATUS = frozenset({
    RUN_STATUS_SUCCEEDED,
    RUN_STATUS_PARTIAL,
    RUN_STATUS_FAILED,
    RUN_STATUS_SKIPPED,
    RUN_STATUS_CANCELLED,
})

# ===== readiness（产品可安全消费等级）=====
READINESS_PENDING = "pending"
READINESS_READY = "ready"
READINESS_READY_REUSED = "ready_reused"
READINESS_DEGRADED = "degraded"
READINESS_UNAVAILABLE = "unavailable"
READINESS_BLOCKED = "blocked"

ALL_READINESS = frozenset({
    READINESS_PENDING,
    READINESS_READY,
    READINESS_READY_REUSED,
    READINESS_DEGRADED,
    READINESS_UNAVAILABLE,
    READINESS_BLOCKED,
})

READY_READINESS = frozenset({READINESS_READY, READINESS_READY_REUSED})

# ===== DSA projection requirement 兼容阶段（EPIC-04 E04-T03）=====
# 初始阶段为 required_compatibility：消费者必须读取由 CoreComputationArtifact
# 派生的 precomputed DSA projection，禁止回退旧 DSA-only canonical 路径。
DSA_PROJECTION_REQUIREMENT_REQUIRED = "required_compatibility"
DSA_PROJECTION_REQUIREMENT_OPTIONAL = "optional_compatibility"
DSA_PROJECTION_REQUIREMENT_RETIRED = "retired"

ALL_DSA_PROJECTION_REQUIREMENTS = frozenset({
    DSA_PROJECTION_REQUIREMENT_REQUIRED,
    DSA_PROJECTION_REQUIREMENT_OPTIONAL,
    DSA_PROJECTION_REQUIREMENT_RETIRED,
})

# ===== auction mode =====
AUCTION_MODE_STRUCTURE_ONLY = "structure_only"
AUCTION_MODE_HYBRID = "hybrid"
AUCTION_MODE_COMPOSITE = "composite"

ALL_AUCTION_MODES = frozenset({
    AUCTION_MODE_STRUCTURE_ONLY,
    AUCTION_MODE_HYBRID,
    AUCTION_MODE_COMPOSITE,
})

# 每股锚点 mode
ANCHOR_MODE_STRUCTURE = "structure"
ANCHOR_MODE_CHIP = "chip"
ANCHOR_MODE_COMPOSITE = "composite"

ALL_ANCHOR_MODES = frozenset({
    ANCHOR_MODE_STRUCTURE,
    ANCHOR_MODE_CHIP,
    ANCHOR_MODE_COMPOSITE,
})

# ===== production closure（product readiness 聚合）=====
# [CHANGE-20260806-005 / Phase 4 / 六态] 在 core_ready 与 degraded_ready 之间新增
# mandatory_ready_enhancing：mandatory 产品已就绪，但 enhancement 产品未全部就绪。
# 固定判定顺序：blocked → pending → core_ready → mandatory_ready_enhancing →
# degraded_ready → fully_ready。
CLOSURE_PENDING = "pending"
CLOSURE_BLOCKED = "blocked"
CLOSURE_CORE_READY = "core_ready"
CLOSURE_MANDATORY_READY_ENHANCING = "mandatory_ready_enhancing"
CLOSURE_DEGRADED_READY = "degraded_ready"
CLOSURE_FULLY_READY = "fully_ready"

ALL_CLOSURE = frozenset({
    CLOSURE_PENDING,
    CLOSURE_BLOCKED,
    CLOSURE_CORE_READY,
    CLOSURE_MANDATORY_READY_ENHANCING,
    CLOSURE_DEGRADED_READY,
    CLOSURE_FULLY_READY,
})

# ===== board facts run 专有状态（EPIC-02 E02-T13）=====
BOARD_FACTS_STATUS_FETCHING = "fetching"
BOARD_FACTS_STATUS_NORMALIZING = "normalizing"
BOARD_FACTS_STATUS_VALIDATING = "validating"
BOARD_FACTS_STATUS_PERSISTING = "persisting"
BOARD_FACTS_STATUS_PUBLISHED = "published"
BOARD_FACTS_STATUS_REUSED_PREVIOUS = "reused_previous"

ALL_BOARD_FACTS_STATUS = frozenset(ALL_RUN_STATUS | {
    BOARD_FACTS_STATUS_FETCHING,
    BOARD_FACTS_STATUS_NORMALIZING,
    BOARD_FACTS_STATUS_VALIDATING,
    BOARD_FACTS_STATUS_PERSISTING,
    BOARD_FACTS_STATUS_PUBLISHED,
    BOARD_FACTS_STATUS_REUSED_PREVIOUS,
})

# ===== 错误码（稳定错误合同）=====
ERR_BOARD_HISTORICAL_SNAPSHOT_MISSING = "BOARD_HISTORICAL_SNAPSHOT_MISSING"
ERR_BOARD_REUSED_PREVIOUS_SNAPSHOT = "BOARD_REUSED_PREVIOUS_SNAPSHOT"
ERR_BOARD_PROVIDER_UNAVAILABLE = "BOARD_PROVIDER_UNAVAILABLE"
ERR_BOARD_QUALITY_GATE_FAILED = "BOARD_QUALITY_GATE_FAILED"
ERR_CHIP_UNAVAILABLE = "CHIP_UNAVAILABLE"
ERR_AUCTION_PARTIAL = "AUCTION_PARTIAL"

# 板块失败复用最大陈旧交易日数（默认，交易日历计算）
BOARD_MAX_REUSE_TRADING_DAYS_DEFAULT = 5


def is_terminal_run_status(status: str) -> bool:
    """判断 run status 是否为终态。"""
    return status in TERMINAL_RUN_STATUS


def is_ready_readiness(readiness: str | None) -> bool:
    """readiness 是否可安全消费（ready 或 ready_reused）。"""
    return readiness in READY_READINESS
