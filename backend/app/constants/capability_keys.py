"""权限能力键常量 - V2.1 三能力唯一定义。

PRD §4 统一能力键：
- watchlist_management: 自选管理（含盘中监控）
- market_screening: 行情选股（含详情/K线/指标）
- review_management: 复盘管理（本期只保存授权，不虚构业务页面）

设计要点：
- 禁止在 API、Worker、前端散落不同字符串
- 能力键用于 invite_code_capabilities 和 user_capability_grants 表
- CheckConstraint 在 migration 和 ORM 两层保证
"""

from __future__ import annotations

WATCHLIST_MANAGEMENT = "watchlist_management"
MARKET_SCREENING = "market_screening"
REVIEW_MANAGEMENT = "review_management"

#: 所有合法能力键（用于 CheckConstraint 和校验）
ALL_CAPABILITY_KEYS: frozenset[str] = frozenset({
    WATCHLIST_MANAGEMENT,
    MARKET_SCREENING,
    REVIEW_MANAGEMENT,
})

#: 技术安全上限（PRD §6.2）
MAX_WATCHLIST_STOCK_LIMIT = 100000
MAX_DURATION_MONTHS = 120

#: source_type 枚举
SOURCE_INVITE_CODE = "invite_code"
SOURCE_LEGACY_SUBSCRIPTION = "legacy_subscription"
SOURCE_LEGACY_INVITE = "legacy_invite"
