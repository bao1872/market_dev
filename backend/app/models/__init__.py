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
- Phase2: Plan（套餐定义表 plans，套餐契约唯一真源，替代 plan_contract.py 字典）
- Phase4.5: AccessAuditLog（访问审计日志表 access_audit_logs，记录 admin 关键操作）
"""

from __future__ import annotations

from app.models.access_audit_log import AccessAuditLog
from app.models.bar import BarDaily, BarMinute
from app.models.base import Base
from app.models.beta_application import BetaApplication
from app.models.calendar import TradingCalendar
from app.models.capture_job import CaptureJob
from app.models.config import ConfigDefinition
from app.models.event_recipient import StrategyEventRecipient
from app.models.instrument import Instrument
from app.models.invitation import InviteCode, InviteRedemption
from app.models.job import JobRun
from app.models.job_run_event import JobRunEvent
from app.models.monitor_evaluation import MonitorEvaluation
from app.models.monitor_state import MonitorState
from app.models.notification import (
    MessageDelivery,
    NotificationChannel,
    NotificationMessage,
    NotificationTemplate,
)
from app.models.outbox import Outbox
from app.models.plan import Plan
from app.models.scheduler_job_run import SchedulerJobRun
from app.models.stock_feature_snapshot import StockFeatureSnapshot
from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun
from app.models.subscription import Subscription
from app.models.stock_memo import StockMemo
from app.models.strategy import StrategyDefinition, StrategyVersion
from app.models.strategy_event import StrategyEvent
from app.models.strategy_run import (
    StrategyResult,
    StrategyResultMetric,
    StrategyRun,
    StrategyRunItem,
)
from app.models.user import Role, User, UserRole
from app.models.watchlist import UserWatchlistItem
from app.models.worker_heartbeat import WorkerHeartbeat

__all__ = [
    "AccessAuditLog",
    "BarDaily",
    "BarMinute",
    "Base",
    "BetaApplication",
    "CaptureJob",
    "ConfigDefinition",
    "Instrument",
    "InviteCode",
    "InviteRedemption",
    "JobRun",
    "JobRunEvent",
    "MessageDelivery",
    "MonitorEvaluation",
    "MonitorState",
    "NotificationChannel",
    "NotificationMessage",
    "NotificationTemplate",
    "Outbox",
    "Plan",
    "Role",
    "SchedulerJobRun",
    "StockFeatureSnapshot",
    "StockFeatureSnapshotRun",
    "StockMemo",
    "StrategyEventRecipient",
    "StrategyDefinition",
    "StrategyEvent",
    "StrategyResult",
    "StrategyResultMetric",
    "StrategyRun",
    "StrategyRunItem",
    "StrategyVersion",
    "Subscription",
    "TradingCalendar",
    "User",
    "UserRole",
    "UserWatchlistItem",
    "WorkerHeartbeat",
]
