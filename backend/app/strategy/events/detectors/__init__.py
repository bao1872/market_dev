"""V1.1 事件检测器包 - 从 ref/交易/event_lib/detectors/ 迁移并升级。

升级内容：
1. 每个检测器添加 state_ttl_seconds 和 allowed_roles 声明
2. structural_events.py 修复占位实现（向量化支撑/阻力突破）
3. 导入路径从 event_lib 改为 app.strategy.events

检测器列表（14 个文件）：
    - trend_events.py         趋势事件
    - breakout_events.py      突破事件
    - volume_events.py        量能事件
    - momentum_events.py      动量事件
    - structural_events.py    结构事件（已修复占位实现）
    - fundamental_events.py   基本面事件
    - composite_events.py     复合事件
    - stage_cost_zone_events.py  趋势位置事件
    - stage_wash_events.py    区间结构事件
    - stage_shake_events.py   破位收回事件
    - stage_repair_events.py  修复收回事件
    - stage_failure_events.py 风险破位事件
    - sr_support_events.py    SR支撑事件
    - sr_resistance_events.py SR压力事件

Usage:
    import app.strategy.events.detectors  # 自动注册所有事件
"""

# 自动导入并注册所有检测器（触发注册，导入即副作用）
from app.strategy.events.detectors import (  # noqa: F401
    breakout_events,
    composite_events,
    fundamental_events,
    momentum_events,
    sr_resistance_events,
    sr_support_events,
    stage_cost_zone_events,
    stage_failure_events,
    stage_repair_events,
    stage_shake_events,
    stage_wash_events,
    structural_events,
    trend_events,
    volume_events,
)
