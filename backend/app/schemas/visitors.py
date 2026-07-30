"""[CHANGE-20260730-010] 访问统计 Schema - Umami 访客分析聚合 DTO。

[CHANGE-20260730-010] 从 GoAccess 迁移到 Umami：
- 数据来源改为 Umami 数据库（umami_analytics_adapter 查询）
- 不再读取 /srv/goaccess/report.json
- data_source 改为 umami / empty / error

设计说明：
- Umami 默认不存储完整 IP（session 表无 IP 字段），无需匿名化
- 敏感 query 参数（token/jwt/password/key）由 _sanitize_path 脱敏
- 本 Schema 仅做数据透传与字段命名规范化，不重新解析日志
- 缺数据时返回 null（不静默省略），前端展示空态
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class VisitorMetricItem(BaseModel):
    """单个指标项（如某页面、某来源、某状态码）。"""

    model_config = ConfigDict(populate_by_name=True)

    label: str = Field(..., description="展示标签（如路径 /market、来源 google.com、状态码 200）")
    count: int = Field(..., description="访问次数")
    percentage: float | None = Field(None, description="占比（0-100），缺数据时为 None")


class VisitorSummary(BaseModel):
    """访问汇总（今日/7日/30日各一组）。"""

    pv: int = Field(0, description="页面浏览量 Page View")
    uv: int = Field(0, description="独立访客数 Unique Visitor（按匿名 IP 聚合）")
    top_pages: list[VisitorMetricItem] = Field(default_factory=list, description="热门页面 Top N")
    top_referrers: list[VisitorMetricItem] = Field(default_factory=list, description="来源 Top N")
    status_codes: list[VisitorMetricItem] = Field(default_factory=list, description="状态码分布")
    devices: list[VisitorMetricItem] = Field(default_factory=list, description="设备类型分布")
    browsers: list[VisitorMetricItem] = Field(default_factory=list, description="浏览器分布")
    hourly_trend: list[VisitorMetricItem] = Field(
        default_factory=list, description="24 小时时段趋势（label=HH:00）"
    )


class VisitorReport(BaseModel):
    """[Gate5] /admin/visitors 响应体。"""

    # 三个时间窗口汇总
    today: VisitorSummary = Field(default_factory=VisitorSummary, description="今日汇总")
    seven_days: VisitorSummary = Field(default_factory=VisitorSummary, description="最近 7 日汇总")
    thirty_days: VisitorSummary = Field(default_factory=VisitorSummary, description="最近 30 日汇总")

    # 元信息
    generated_at: datetime | None = Field(
        None, description="报告生成时间（查询 Umami 数据库时间）；None 表示无可用报告"
    )
    data_source: str = Field(
        "umami",
        description="数据来源：umami / empty / error",
    )
    error_message: str | None = Field(
        None, description="data_source=error 时的错误说明；正常时为 None"
    )


if __name__ == "__main__":
    # 自测入口
    report = VisitorReport(
        today=VisitorSummary(
            pv=100, uv=50,
            top_pages=[VisitorMetricItem(label="/market", count=80, percentage=80.0)],
            top_referrers=[VisitorMetricItem(label="直接访问", count=60, percentage=60.0)],
            status_codes=[VisitorMetricItem(label="200", count=95, percentage=95.0)],
            devices=[VisitorMetricItem(label="Desktop", count=70, percentage=70.0)],
            browsers=[VisitorMetricItem(label="Chrome", count=80, percentage=80.0)],
            hourly_trend=[VisitorMetricItem(label="10:00", count=20)],
        ),
        data_source="empty",
    )
    dumped = report.model_dump()
    assert "today" in dumped
    assert "seven_days" in dumped
    assert "thirty_days" in dumped
    assert dumped["today"]["pv"] == 100
    print("OK")
