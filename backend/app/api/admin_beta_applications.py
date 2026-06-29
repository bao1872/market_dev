"""管理员 API 路由 - 内测申请后台管理（Task 4，SubTask 4.1）。

端点：
- GET /admin/beta-applications: 列表（分页+筛选 status/reason_code/watch_stock_range/
  date_from/date_to + 搜索 keyword 匹配 wechat/phone）
- GET /admin/beta-applications/stats: 统计卡数据（累计/今日/7天/30天/状态/理由/区间）
- GET /admin/beta-applications/export: CSV 导出（支持与列表相同的筛选条件）
- GET /admin/beta-applications/{id}: 详情（含完整字段 + 飞书投递信息）
- PATCH /admin/beta-applications/{id}: 修改 status/admin_note
- POST /admin/beta-applications/{id}/retry-feishu: 重发飞书（重新入队 Outbox）

权限：
- 所有端点需要 admin 角色（RBAC require_roles("admin")）
- 普通用户访问返回 403，未认证返回 401

设计说明：
- 路由声明顺序：/stats、/export 必须在 /{id} 之前，避免被动态路由匹配
- watch_stock_range 处理 URL 中 "+" 解码为空格的情况（normalize_range）
- 复用 beta_application_service.get_admin_stats / update_status / retry_feishu
- CSV 导出使用 csv.writer 生成 UTF-8 BOM 文本，便于 Excel 正确显示中文
- 异常处理：service 层抛出 HTTPException 直接传播，不吞没
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import UTC, datetime, time, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db, require_roles
from app.models.beta_application import BetaApplication
from app.schemas.beta_application import (
    BetaApplicationAdminResponse,
    BetaApplicationListItem,
    BetaApplicationListResponse,
    BetaApplicationPatchRequest,
    BetaApplicationStatsResponse,
    RetryFeishuResponse,
)
from app.services.beta_application_service import (
    get_admin_stats,
    retry_feishu,
    update_status,
)

logger = logging.getLogger("admin_beta_applications")

router = APIRouter(
    prefix="/admin",
    tags=["admin-beta-applications"],
)

# 盯盘数量区间映射：(min, max) 闭区间，"50+" 为开区间 (>50)
_WATCH_RANGES: dict[str, tuple[int, int | None]] = {
    "1-10": (1, 10),
    "11-20": (11, 20),
    "21-50": (21, 50),
    "50+": (51, None),
}

# CSV 导出表头（中文，便于管理员阅读）
_CSV_HEADERS = [
    "申请编号", "微信号", "手机号", "盯盘数", "理由代码", "补充说明",
    "状态", "来源", "管理员备注", "处理人ID", "处理时间", "提交时间",
    "飞书投递状态", "飞书投递时间", "飞书最近错误",
]


def _normalize_range(value: str) -> str:
    """归一化 watch_stock_range 参数值。

    URL 查询字符串中 "+" 会被 parse_qsl 解码为空格，故 "50+" 可能以 "50 " 传入。
    去除首尾空格后，若结果为 "50" 则视为 "50+"（"50" 本身非合法区间值）。

    Args:
        value: 原始查询参数值

    Returns:
        归一化后的区间标识符（"1-10"/"11-20"/"21-50"/"50+"）
    """
    stripped = value.strip()
    if stripped == "50":
        return "50+"
    return stripped


def _build_list_filters(
    base_stmt,
    *,
    app_status: str | None = None,
    reason_code: str | None = None,
    watch_stock_range: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    keyword: str | None = None,
):
    """为列表查询叠加筛选条件。

    所有参数均可选，None 表示不筛选。返回叠加条件后的 select 语句。

    Args:
        base_stmt: 基础 select 语句
        app_status: 状态筛选
        reason_code: 理由代码筛选
        watch_stock_range: 盯盘数量区间（1-10/11-20/21-50/50+）
        date_from: 起始日期 YYYY-MM-DD（含）
        date_to: 截止日期 YYYY-MM-DD（含，内部转为次日 00:00:00 以包含当日）
        keyword: 搜索关键词（模糊匹配 wechat/phone）
    """
    if app_status:
        base_stmt = base_stmt.where(BetaApplication.status == app_status)
    if reason_code:
        base_stmt = base_stmt.where(BetaApplication.reason_code == reason_code)
    if watch_stock_range:
        normalized = _normalize_range(watch_stock_range)
        range_bounds = _WATCH_RANGES.get(normalized)
        if range_bounds is not None:
            lo, hi = range_bounds
            if hi is None:
                base_stmt = base_stmt.where(BetaApplication.watch_stock_count >= lo)
            else:
                base_stmt = base_stmt.where(
                    BetaApplication.watch_stock_count >= lo,
                    BetaApplication.watch_stock_count <= hi,
                )
    if date_from:
        try:
            start_dt = datetime.combine(
                datetime.strptime(date_from, "%Y-%m-%d").date(), time.min
            )
            base_stmt = base_stmt.where(BetaApplication.submitted_at >= start_dt)
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"date_from 格式非法（需 YYYY-MM-DD）: {date_from!r}",
            ) from e
    if date_to:
        try:
            end_date = datetime.strptime(date_to, "%Y-%m-%d").date()
            # 截止日期包含当日，转为次日 00:00:00
            end_dt = datetime.combine(end_date + timedelta(days=1), time.min)
            base_stmt = base_stmt.where(BetaApplication.submitted_at < end_dt)
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"date_to 格式非法（需 YYYY-MM-DD）: {date_to!r}",
            ) from e
    if keyword:
        pattern = f"%{keyword}%"
        base_stmt = base_stmt.where(
            or_(
                BetaApplication.wechat.ilike(pattern),
                BetaApplication.phone.ilike(pattern),
            )
        )
    return base_stmt


@router.get(
    "/beta-applications",
    response_model=BetaApplicationListResponse,
)
async def list_beta_applications(
    app_status: str | None = Query(default=None, alias="status", description="状态筛选"),
    reason_code: str | None = Query(default=None, description="理由代码筛选"),
    watch_stock_range: str | None = Query(
        default=None, description="盯盘数量区间：1-10/11-20/21-50/50+"
    ),
    date_from: str | None = Query(default=None, description="起始日期 YYYY-MM-DD（含）"),
    date_to: str | None = Query(default=None, description="截止日期 YYYY-MM-DD（含）"),
    keyword: str | None = Query(default=None, description="搜索关键词（匹配 wechat/phone）"),
    limit: int = Query(default=20, ge=1, le=200, description="分页大小"),
    offset: int = Query(default=0, ge=0, description="分页偏移"),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> BetaApplicationListResponse:
    """查询内测申请列表（分页+筛选+搜索）。

    支持的筛选：
    - status: 状态精确匹配
    - reason_code: 理由代码精确匹配
    - watch_stock_range: 区间筛选（1-10/11-20/21-50/50+）
    - date_from/date_to: 提交日期范围（含两端）
    - keyword: 模糊匹配微信号或手机号

    Args:
        db: 异步数据库会话
        current_user: 当前管理员（由 require_roles 注入）

    Returns:
        BetaApplicationListResponse: {items, total, limit, offset}
    """
    count_stmt = _build_list_filters(
        select(func.count(BetaApplication.id)),
        app_status=app_status,
        reason_code=reason_code,
        watch_stock_range=watch_stock_range,
        date_from=date_from,
        date_to=date_to,
        keyword=keyword,
    )
    total = int((await db.execute(count_stmt)).scalar() or 0)

    list_stmt = _build_list_filters(
        select(BetaApplication),
        app_status=app_status,
        reason_code=reason_code,
        watch_stock_range=watch_stock_range,
        date_from=date_from,
        date_to=date_to,
        keyword=keyword,
    )
    list_stmt = (
        list_stmt.order_by(BetaApplication.submitted_at.desc())
        .offset(offset)
        .limit(limit)
    )
    result = await db.execute(list_stmt)
    rows = list(result.scalars().all())

    return BetaApplicationListResponse(
        items=[BetaApplicationListItem.model_validate(r) for r in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/beta-applications/stats",
    response_model=BetaApplicationStatsResponse,
)
async def get_beta_application_stats(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> BetaApplicationStatsResponse:
    """获取内测申请统计数据（统计卡数据）。

    复用 beta_application_service.get_admin_stats 聚合逻辑：
    - 累计/今日/近7天/近30天 计数
    - 各状态分布（new/contacted/approved/rejected/converted）
    - 平均盯盘数
    - 理由占比
    - 股票数量区间分布（1-10/11-20/21-50/50+）

    Args:
        db: 异步数据库会话
        current_user: 当前管理员

    Returns:
        BetaApplicationStatsResponse
    """
    stats = await get_admin_stats(db)
    return BetaApplicationStatsResponse(**stats)


@router.get(
    "/beta-applications/export",
)
async def export_beta_applications(
    app_status: str | None = Query(default=None, alias="status", description="状态筛选"),
    reason_code: str | None = Query(default=None, description="理由代码筛选"),
    watch_stock_range: str | None = Query(
        default=None, description="盯盘数量区间：1-10/11-20/21-50/50+"
    ),
    date_from: str | None = Query(default=None, description="起始日期 YYYY-MM-DD（含）"),
    date_to: str | None = Query(default=None, description="截止日期 YYYY-MM-DD（含）"),
    keyword: str | None = Query(default=None, description="搜索关键词"),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
):
    """导出内测申请为 CSV（支持与列表相同的筛选条件）。

    返回 text/csv 响应，Content-Disposition 包含 filename（含日期）。
    CSV 包含 UTF-8 BOM 头，确保 Excel 正确显示中文。

    Args:
        db: 异步数据库会话
        current_user: 当前管理员

    Returns:
        Response: text/csv
    """
    list_stmt = _build_list_filters(
        select(BetaApplication).order_by(BetaApplication.submitted_at.desc()),
        app_status=app_status,
        reason_code=reason_code,
        watch_stock_range=watch_stock_range,
        date_from=date_from,
        date_to=date_to,
        keyword=keyword,
    )
    result = await db.execute(list_stmt)
    rows = list(result.scalars().all())

    # 生成 CSV（带 UTF-8 BOM，便于 Excel 识别中文）
    buffer = io.StringIO()
    buffer.write("\ufeff")  # UTF-8 BOM
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(_CSV_HEADERS)
    for r in rows:
        writer.writerow([
            str(r.id),
            r.wechat or "",
            r.phone or "",
            r.watch_stock_count,
            r.reason_code,
            r.reason_other or "",
            r.status,
            r.source or "",
            r.admin_note or "",
            str(r.handled_by) if r.handled_by else "",
            r.handled_at.isoformat() if r.handled_at else "",
            r.submitted_at.isoformat() if r.submitted_at else "",
            r.feishu_delivery_status or "",
            r.feishu_delivered_at.isoformat() if r.feishu_delivered_at else "",
            r.feishu_last_error or "",
        ])

    csv_text = buffer.getvalue()
    today_str = datetime.now(UTC).strftime("%Y%m%d")
    filename = f"beta_applications_{today_str}.csv"

    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@router.get(
    "/beta-applications/{app_id}",
    response_model=BetaApplicationAdminResponse,
)
async def get_beta_application_detail(
    app_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> BetaApplicationAdminResponse:
    """获取内测申请详情（含完整字段 + 飞书投递信息）。

    Args:
        app_id: 申请 ID
        db: 异步数据库会话
        current_user: 当前管理员

    Returns:
        BetaApplicationAdminResponse

    Raises:
        HTTPException 404: 申请不存在
    """
    stmt = select(BetaApplication).where(BetaApplication.id == app_id)
    result = await db.execute(stmt)
    app = result.scalar_one_or_none()
    if app is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"申请不存在: {app_id}",
        )
    return BetaApplicationAdminResponse.model_validate(app)


@router.patch(
    "/beta-applications/{app_id}",
    response_model=BetaApplicationAdminResponse,
)
async def update_beta_application(
    app_id: UUID,
    payload: BetaApplicationPatchRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> BetaApplicationAdminResponse:
    """修改内测申请状态（status + admin_note）。

    复用 beta_application_service.update_status：
    - 校验 status 合法性（非法返回 400）
    - 设置 handled_by / handled_at
    - 更新 admin_note（若提供）

    Args:
        app_id: 申请 ID
        payload: 请求体（status 必填，admin_note 可选）
        db: 异步数据库会话
        current_user: 当前管理员

    Returns:
        BetaApplicationAdminResponse: 更新后的申请详情

    Raises:
        HTTPException 400: status 非法
        HTTPException 404: 申请不存在
    """
    app = await update_status(
        db=db,
        app_id=app_id,
        new_status=payload.status,
        admin_id=current_user.id,
        note=payload.admin_note,
    )
    return BetaApplicationAdminResponse.model_validate(app)


@router.post(
    "/beta-applications/{app_id}/retry-feishu",
    response_model=RetryFeishuResponse,
)
async def retry_beta_application_feishu(
    app_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> RetryFeishuResponse:
    """重发内测申请飞书通知（重新入队 Outbox）。

    复用 beta_application_service.retry_feishu：
    - 创建新的 Outbox 事件（event_type=beta_application_admin）
    - 重置 feishu_delivery_status 为 pending
    - 清空 feishu_last_error

    Args:
        app_id: 申请 ID
        db: 异步数据库会话
        current_user: 当前管理员

    Returns:
        RetryFeishuResponse: {id, outbox_id, message}

    Raises:
        HTTPException 404: 申请不存在
    """
    outbox = await retry_feishu(db=db, app_id=app_id)
    return RetryFeishuResponse(
        id=app_id,
        outbox_id=outbox.id,
        message="飞书重发已入队",
    )


if __name__ == "__main__":
    # 自测入口：验证路由注册
    paths = [r.path for r in router.routes]
    print(f"router.routes={paths}")
    assert "/admin/beta-applications" in paths
    assert "/admin/beta-applications/stats" in paths
    assert "/admin/beta-applications/export" in paths
    assert "/admin/beta-applications/{app_id}" in paths
    # 验证区间映射
    assert _WATCH_RANGES["1-10"] == (1, 10)
    assert _WATCH_RANGES["50+"] == (51, None)
    # 验证 normalize_range
    assert _normalize_range("1-10") == "1-10"
    assert _normalize_range("50+") == "50+"
    assert _normalize_range("50 ") == "50+"
    assert _normalize_range("50") == "50+"
    print("OK")
