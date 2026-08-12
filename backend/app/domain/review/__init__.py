"""复盘模块 domain 层 - 纯计算与状态机（PRD §4、§7-§10）。

子模块：
- metric_registry: P/Q/U/C/V component 字段映射（PRD §7）
- metric_engine: P/Q/U/C/V 计算引擎
- scope_observation: Canonical Scope Observation Core（PRD §7，family-agnostic）
- filter_definitions: A/B/C 三类筛选器 Pydantic schema（PRD §8）
- filter_engine: 筛选器执行引擎
- attribution_engine: 子范围与个股归因（PRD §9）
- tracking_state_machine: 信号生命周期状态机（PRD §10）

domain 层不直接访问数据库，所有数据由 service 层传入。
"""
