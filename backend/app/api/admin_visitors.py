"""[CHANGE-20260730-010] /admin/visitors API - 访问统计只读端点（admin only）。

[CHANGE-20260730-010] 从 GoAccess 迁移到 Umami：
- 数据来源改为 Umami 数据库（通过 umami_analytics_adapter 查询）
- 不再读取 /srv/goaccess/report.json
- data_source 改为 umami / empty / error
- 删除 GOACCESS_REPORT_PATH 和 GoAccess JSON 解析逻辑

安全设计：
- 仅 admin 角色可访问（require_roles("admin")）
- Umami 数据库使用独立只读用户（仅 SELECT 权限）
- 凭据从环境变量 UMAMI_DATABASE_URL 读取（生产由 market.env 注入）
- 前端不接触数据库密码或 Umami 管理员密码
- 敏感 query 参数由 _sanitize_path 脱敏
- 不缓存响应（每次请求查询最新数据）
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db, require_roles
from app.schemas.visitors import VisitorReport
from app.services.umami_analytics_adapter import fetch_umami_report

logger = logging.getLogger("admin_visitors")

router = APIRouter(prefix="/admin", tags=["admin-visitors"])


@router.get("/visitors", response_model=VisitorReport)
async def get_visitors_report(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> VisitorReport:
    """[CHANGE-20260730-010] 查询访客统计报告（admin only）。

    返回今日/7日/30日的 PV/UV、热门页面、来源、设备/浏览器、时段趋势。
    数据来源：Umami 数据库（通过独立只读连接查询）。
    本地开发环境未配置 Umami 时，返回 data_source="empty" + 空数据。

    Umami 替代 GoAccess：
    - 不再读取 /srv/goaccess/report.json
    - 不再依赖 GoAccess 容器
    - 保留 Nginx access.log 用于运维（由 logrotate 轮转）
    """
    _ = db  # 不使用主业务库，仅 Umami 库
    return await fetch_umami_report()
