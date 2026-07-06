"""监控自动通知 source_type 集合。

由 outbox_relay 与 delivery_worker 共享，确保 monitor 通知口径一致。
"""

MONITOR_SOURCE_TYPES = frozenset({"monitor_event", "strategy_event", "monitor_chart"})
