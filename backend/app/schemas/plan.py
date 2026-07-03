"""Plan schema.

提供套餐定义对外暴露的 Pydantic 模型。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class PlanResponse(BaseModel):
    """套餐定义响应 schema（plans 表公开视图）。"""

    model_config = ConfigDict(from_attributes=True)

    plan_code: str
    display_name: str
    monitor_limit: int
    notification_channel_limit: int
    message_retention_days: int
    features: list[str]
