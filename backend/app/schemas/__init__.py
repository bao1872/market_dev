"""Pydantic schemas 包。

V1.1 各阶段逐步添加 schema：
- R2: User（用户认证与响应）
- R3: Instrument（股票主数据）
- R4: Calendar（交易日历）
- R6: Config（配置注册表）
- R7: Strategy（策略目录与版本）
- R9: Notification（通知消息 DTO）
"""

from __future__ import annotations

from app.schemas.calendar import (
    CalendarListResponse,
    CalendarResponse,
    TradingDayResponse,
)
from app.schemas.config import (
    ConfigDefinitionResponse,
    ConfigDefinitionUpdate,
    ConfigListResponse,
)
from app.schemas.instrument import (
    InstrumentListResponse,
    InstrumentResponse,
)
from app.schemas.notification import (
    DeliveryResult,
    NotificationMessageDTO,
)
from app.schemas.strategy import (
    CreateStrategyRequest,
    StrategyListResponse,
    StrategyResponse,
    StrategySchemaResponse,
    StrategyVersionListResponse,
    StrategyVersionResponse,
)
from app.schemas.user import (
    TokenPayload,
    TokenResponse,
    UserCreate,
    UserLogin,
    UserResponse,
)
from app.schemas.watchlist import (
    WatchlistAddRequest,
    WatchlistItemResponse,
    WatchlistListResponse,
)

__all__ = [
    "CalendarListResponse",
    "CalendarResponse",
    "ConfigDefinitionResponse",
    "ConfigDefinitionUpdate",
    "ConfigListResponse",
    "CreateStrategyRequest",
    "DeliveryResult",
    "InstrumentListResponse",
    "InstrumentResponse",
    "NotificationMessageDTO",
    "StrategyListResponse",
    "StrategyResponse",
    "StrategySchemaResponse",
    "StrategyVersionListResponse",
    "StrategyVersionResponse",
    "TokenPayload",
    "TokenResponse",
    "TradingDayResponse",
    "UserCreate",
    "UserLogin",
    "UserResponse",
    "WatchlistAddRequest",
    "WatchlistItemResponse",
    "WatchlistListResponse",
]
