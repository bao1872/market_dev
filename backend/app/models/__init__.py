"""SQLAlchemy ORM 模型包。

V1.1 各阶段模型统一继承 Base：
- R2: User/Role/UserRole（用户与权限基础表）
- R3: Instrument（股票主数据）
- R4: TradingCalendar（交易日历）
- R5: BarDaily/BarMinute（行情仓储）
- R6: ConfigDefinition（配置注册表）
- R7: StrategyDefinition/StrategyVersion（策略目录与版本）
- R8: JobRun（任务运行）、Outbox（事务性发件箱）
- R9: NotificationChannel/Template/Message/MessageDelivery（通知基础设施）
- M3: MonitorState（监控状态仓储，复合主键 strategy_version_id+instrument_id）
- M4: StrategyEvent（原始策略事件与快照，event_key 唯一）
- R12: StrategyRun/StrategyResult/StrategyResultMetric（策略运行与结果）

[LEGACY] 以下模型文件保留以兼容现有数据库表，但已从活跃 API 中移除：
- C1/C4: SelectionPlan/Revision/Member/Condition（选股组合方案）
- C5: MonitoringPlan/Revision/Member（监控组合方案）
- C6: MonitoringPlanState（监控组合状态）
- C8: CompositeMonitorEvent/Evidence（组合事件与证据）
如需直接操作这些表，请使用 from app.models.<module> import ... 显式导入。
"""

from __future__ import annotations

from app.models.bar import BarDaily, BarMinute
from app.models.base import Base
from app.models.calendar import TradingCalendar
from app.models.config import ConfigDefinition
from app.models.event_recipient import StrategyEventRecipient
from app.models.instrument import Instrument
from app.models.job import JobRun
from app.models.membership import InviteCode, InviteRedemption, Membership
from app.models.monitor_evaluation import MonitorEvaluation
from app.models.monitor_state import MonitorState
from app.models.notification import (
    MessageDelivery,
    NotificationChannel,
    NotificationMessage,
    NotificationTemplate,
)
from app.models.outbox import Outbox
# [LEGACY] combo models: 保留 DB 表兼容，不再导出。显式导入用 from app.models.selection_plan import ...
# from app.models.composite_event import CompositeEventEvidence, CompositeMonitorEvent
# from app.models.monitoring_plan import MonitoringPlan, MonitoringPlanMember, MonitoringPlanRevision
# from app.models.monitoring_plan_state import MonitoringPlanState
# from app.models.selection_plan import SelectionPlan, SelectionPlanRevision, SelectionPlanMember, SelectionMemberCondition
from app.models.selection_plan_run import (
    SelectionPlanResult,
    SelectionPlanRun,
    SelectionResultEvidence,
)
from app.models.strategy import StrategyDefinition, StrategyVersion
from app.models.strategy_event import StrategyEvent
from app.models.strategy_run import (
    StrategyResult,
    StrategyResultMetric,
    StrategyRun,
    StrategyRunItem,
)
from app.models.user import Role, User, UserRole
from app.models.stock_memo import StockMemo
from app.models.watchlist import UserWatchlistItem

__all__ = [
    "BarDaily",
    "BarMinute",
    "Base",
    "ConfigDefinition",
    "Instrument",
    "InviteCode",
    "InviteRedemption",
    "JobRun",
    "Membership",
    "MessageDelivery",
    "MonitorEvaluation",
    "MonitorState",
    "NotificationChannel",
    "NotificationMessage",
    "NotificationTemplate",
    "Outbox",
    "Role",
    "SelectionPlanResult",
    "SelectionPlanRun",
    "SelectionResultEvidence",
    "StrategyEventRecipient",
    "StrategyDefinition",
    "StrategyEvent",
    "StrategyResult",
    "StrategyResultMetric",
    "StrategyRun",
    "StrategyRunItem",
    "StrategyVersion",
    "TradingCalendar",
    "User",
    "UserRole",
    "StockMemo",
    "UserWatchlistItem",
]
