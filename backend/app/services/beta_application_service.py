"""内测申请服务层 - 公开端点业务逻辑（spec 第三节）。

提供：
- create_application: 创建申请（重复检测、IP 限流、日志脱敏、先 DB 后 Outbox）
- get_admin_stats: 管理后台统计聚合（累计/今日/7天/30天/状态分布/理由占比/股票区间）
- update_status: 管理员更新申请状态
- retry_feishu: 重新入队 Outbox 事件

业务规则（spec 第三节）：
- 重复检测：同 phone/wechat 24h 内返回原申请（不产生新数据）
- 频率限制：同 ip_hash 1h 内 ≤5 次，超限 raise HTTPException(429)
- 日志脱敏：手机号/微信号只显示后 4 位
- 先 DB 后 Outbox：先 db.commit 成功，再写 Outbox（失败仅 logger.error）
- 异常处理：禁止 except: pass，try/except 仅补上下文后 re-raise 或抛更清晰异常
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.beta_application import (
    BETA_APPLICATION_STATUSES_DEFAULT,
    is_valid_status,
)
from app.models.beta_application import BetaApplication
from app.models.outbox import Outbox
from app.schemas.beta_application import BetaApplicationCreate
from app.services.beta_application_notifier import send_admin_notification
from app.services.outbox_relay import write_outbox

logger = logging.getLogger("beta_application_service")

# IP 频率限制：同 ip_hash 1h 内最多 5 次
_IP_RATE_LIMIT_WINDOW_HOURS = 1
_IP_RATE_LIMIT_MAX = 5

# 重复检测窗口：同 phone/wechat 24h 内视为重复
_DUPLICATE_WINDOW_HOURS = 24

# Outbox 事件类型（与 beta_application_notifier.BETA_APPLICATION_ADMIN_EVENT 保持一致）
_BETA_APPLICATION_ADMIN_EVENT = "beta_application.admin_notification.created"


def _mask_contact(value: str | None) -> str:
    """脱敏联系方式（只显示后 4 位）。

    Args:
        value: 手机号或微信号

    Returns:
        脱敏后的字符串（如 ***1234），None 返回 'None'
    """
    if value is None:
        return "None"
    if len(value) <= 4:
        return f"***{value}"
    return f"***{value[-4:]}"


async def _find_duplicate(
    db: AsyncSession, payload: BetaApplicationCreate
) -> BetaApplication | None:
    """查找 24h 内同 phone/wechat 的已有申请。

    Args:
        db: 异步数据库会话
        payload: 创建请求

    Returns:
        已有申请对象或 None
    """
    conditions = []
    if payload.phone:
        conditions.append(BetaApplication.phone == payload.phone)
    if payload.wechat:
        conditions.append(BetaApplication.wechat == payload.wechat)

    if not conditions:
        return None

    threshold = datetime.now(UTC) - timedelta(hours=_DUPLICATE_WINDOW_HOURS)
    stmt = (
        select(BetaApplication)
        .where(or_(*conditions))
        .where(BetaApplication.submitted_at >= threshold)
        .order_by(BetaApplication.submitted_at.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def _check_ip_rate_limit(db: AsyncSession, ip_hash: str) -> None:
    """检查同 IP 1h 内提交次数，超限抛 HTTPException(429)。

    Args:
        db: 异步数据库会话
        ip_hash: 客户端 IP 的 SHA256 哈希

    Raises:
        HTTPException: 同 IP 1h 内已提交 ≥ _IP_RATE_LIMIT_MAX 次
    """
    threshold = datetime.now(UTC) - timedelta(hours=_IP_RATE_LIMIT_WINDOW_HOURS)
    stmt = (
        select(func.count(BetaApplication.id))
        .where(BetaApplication.ip_hash == ip_hash)
        .where(BetaApplication.submitted_at >= threshold)
    )
    result = await db.execute(stmt)
    count = int(result.scalar() or 0)

    if count >= _IP_RATE_LIMIT_MAX:
        logger.warning(
            "[BetaApplication] IP 限流触发: ip_hash=%s... count=%d window=%dh",
            ip_hash[:12], count, _IP_RATE_LIMIT_WINDOW_HOURS,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"提交过于频繁，请 {_IP_RATE_LIMIT_WINDOW_HOURS} 小时后再试",
        )


async def create_application(
    db: AsyncSession,
    payload: BetaApplicationCreate,
    ip_hash: str,
    source: str | None = None,
) -> tuple[BetaApplication, bool]:
    """创建内测申请 - 公开端点核心业务逻辑。

    流程：
    1. 重复检测：同 phone/wechat 24h 内返回原申请（is_new=False）
    2. IP 限流：同 ip_hash 1h 内 ≤5 次，超限 raise HTTPException(429)
    3. 创建申请 + db.commit（先持久化申请）
    4. 写 Outbox 事件（失败仅 logger.error，不影响已提交申请）

    日志脱敏：手机号/微信号只显示后 4 位（***1234）

    Args:
        db: 异步数据库会话
        payload: 创建请求（已通过 schema 校验）
        ip_hash: 客户端 IP 的 SHA256 哈希
        source: 提交来源（如 landing_page，优先使用 payload.source）

    Returns:
        (BetaApplication, is_new) 元组，is_new=False 表示重复提交

    Raises:
        HTTPException 429: 同 IP 1h 内提交超过 5 次
    """
    effective_source = payload.source or source

    # 1. 重复检测（先于限流：重复不产生新数据，不消耗限流配额）
    existing = await _find_duplicate(db, payload)
    if existing is not None:
        logger.info(
            "[BetaApplication] 重复提交返回原申请: app_id=%s "
            "phone=%s wechat=%s reason=%s",
            existing.id,
            _mask_contact(existing.phone),
            _mask_contact(existing.wechat),
            existing.reason_code,
        )
        return existing, False

    # 2. IP 限流
    await _check_ip_rate_limit(db, ip_hash)

    # 3. 创建申请
    app = BetaApplication(
        wechat=payload.wechat,
        phone=payload.phone,
        watch_stock_count=payload.watch_stock_count,
        reason_code=payload.reason_code,
        reason_other=payload.reason_other,
        status=BETA_APPLICATION_STATUSES_DEFAULT,
        source=effective_source,
        ip_hash=ip_hash,
    )
    db.add(app)
    await db.flush()  # 校验约束并获取 id
    await db.commit()  # 先持久化申请（spec: 先 DB）
    await db.refresh(app)  # 重新加载 server_default 字段

    logger.info(
        "[BetaApplication] 新申请已提交: app_id=%s "
        "phone=%s wechat=%s watch=%d reason=%s source=%s",
        app.id,
        _mask_contact(app.phone),
        _mask_contact(app.wechat),
        app.watch_stock_count,
        app.reason_code,
        app.source,
    )

    # 4. 写 Outbox 事件（best-effort，失败不影响已提交申请）
    # notifier.send_admin_notification 内部使用 savepoint 隔离 Outbox 写入，
    # 设置 feishu_delivery_status='pending'，失败时标记 failed
    await send_admin_notification(db, app)

    return app, True


async def get_admin_stats(db: AsyncSession) -> dict[str, Any]:
    """管理后台统计聚合 - 统计卡数据。

    返回：
    - total: 累计申请数
    - today: 今日新增
    - last_7_days: 近 7 天新增
    - last_30_days: 近 30 天新增
    - by_status: 各状态计数（new/contacted/approved/rejected/converted）
    - avg_watch_stock_count: 平均盯盘数
    - by_reason: 理由占比
    - by_watch_range: 股票数量区间分布（1-10/11-20/21-50/50+）

    Args:
        db: 异步数据库会话

    Returns:
        统计字典
    """
    now = datetime.now(UTC)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # 累计
    total_stmt = select(func.count(BetaApplication.id))
    total = int((await db.execute(total_stmt)).scalar() or 0)

    # 今日
    today_stmt = select(func.count(BetaApplication.id)).where(
        BetaApplication.submitted_at >= today_start
    )
    today_count = int((await db.execute(today_stmt)).scalar() or 0)

    # 近 7 天
    seven_days_ago = now - timedelta(days=7)
    last_7_stmt = select(func.count(BetaApplication.id)).where(
        BetaApplication.submitted_at >= seven_days_ago
    )
    last_7 = int((await db.execute(last_7_stmt)).scalar() or 0)

    # 近 30 天
    thirty_days_ago = now - timedelta(days=30)
    last_30_stmt = select(func.count(BetaApplication.id)).where(
        BetaApplication.submitted_at >= thirty_days_ago
    )
    last_30 = int((await db.execute(last_30_stmt)).scalar() or 0)

    # 各状态计数
    status_stmt = (
        select(BetaApplication.status, func.count(BetaApplication.id))
        .group_by(BetaApplication.status)
    )
    status_result = await db.execute(status_stmt)
    by_status = {row[0]: int(row[1]) for row in status_result.all()}

    # 平均盯盘数
    avg_stmt = select(func.avg(BetaApplication.watch_stock_count))
    avg_watch = (await db.execute(avg_stmt)).scalar()
    avg_watch_count = float(avg_watch) if avg_watch is not None else 0.0

    # 理由占比
    reason_stmt = (
        select(BetaApplication.reason_code, func.count(BetaApplication.id))
        .group_by(BetaApplication.reason_code)
    )
    reason_result = await db.execute(reason_stmt)
    by_reason = {row[0]: int(row[1]) for row in reason_result.all()}

    # 股票数量区间分布
    range_stmt = (
        select(
            case(
                (BetaApplication.watch_stock_count <= 10, "1-10"),
                (BetaApplication.watch_stock_count <= 20, "11-20"),
                (BetaApplication.watch_stock_count <= 50, "21-50"),
                else_="50+",
            ).label("range"),
            func.count(BetaApplication.id),
        )
        .group_by("range")
    )
    range_result = await db.execute(range_stmt)
    by_watch_range = {row[0]: int(row[1]) for row in range_result.all()}

    return {
        "total": total,
        "today": today_count,
        "last_7_days": last_7,
        "last_30_days": last_30,
        "by_status": by_status,
        "avg_watch_stock_count": round(avg_watch_count, 2),
        "by_reason": by_reason,
        "by_watch_range": by_watch_range,
    }


async def update_status(
    db: AsyncSession,
    app_id: UUID,
    new_status: str,
    admin_id: UUID,
    note: str | None = None,
) -> BetaApplication:
    """管理员更新申请状态。

    Args:
        db: 异步数据库会话
        app_id: 申请 ID
        new_status: 新状态（new/contacted/approved/rejected/converted）
        admin_id: 处理人 user_id
        note: 管理员备注（可选）

    Returns:
        更新后的 BetaApplication

    Raises:
        HTTPException 404: 申请不存在
        HTTPException 400: 状态非法
    """
    if not is_valid_status(new_status):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"非法状态: {new_status}",
        )

    stmt = select(BetaApplication).where(BetaApplication.id == app_id)
    result = await db.execute(stmt)
    app = result.scalar_one_or_none()
    if app is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"申请不存在: {app_id}",
        )

    app.status = new_status
    app.handled_by = admin_id
    app.handled_at = datetime.now(UTC)
    if note is not None:
        app.admin_note = note
    await db.commit()
    await db.refresh(app)

    logger.info(
        "[BetaApplication] 状态已更新: app_id=%s status=%s admin=%s",
        app_id, new_status, admin_id,
    )
    return app


async def retry_feishu(db: AsyncSession, app_id: UUID) -> Outbox:
    """重新入队 Outbox 事件（管理员手动重发飞书）。

    Args:
        db: 异步数据库会话
        app_id: 申请 ID

    Returns:
        新创建的 Outbox 记录

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

    payload: dict[str, Any] = {
        "application_id": str(app.id),
        "wechat": app.wechat,
        "phone": app.phone,
        "watch_stock_count": app.watch_stock_count,
        "reason_code": app.reason_code,
        "reason_other": app.reason_other,
        "submitted_at": app.submitted_at.isoformat() if app.submitted_at else None,
        "source": app.source,
        "retry": True,
    }
    outbox = await write_outbox(
        db=db,
        event_type=_BETA_APPLICATION_ADMIN_EVENT,
        payload=payload,
        aggregate_type="beta_application",
        aggregate_id=app.id,
    )
    # 重置飞书投递状态为 pending（等待 Outbox relay 处理）
    app.feishu_delivery_status = "pending"
    app.feishu_last_error = None
    await db.commit()

    logger.info(
        "[BetaApplication] 飞书重发已入队: app_id=%s outbox_id=%s",
        app_id, outbox.id,
    )
    return outbox


if __name__ == "__main__":
    # 自测入口：验证函数可导入（不连接 DB）
    print(f"create_application={create_application}")
    print(f"get_admin_stats={get_admin_stats}")
    print(f"update_status={update_status}")
    print(f"retry_feishu={retry_feishu}")

    # 验证脱敏函数
    assert _mask_contact(None) == "None"
    assert _mask_contact("1234") == "***1234"
    assert _mask_contact("13800138000") == "***8000"
    assert _mask_contact("my_secret_wechat_id") == "***t_id"
    print(f"_mask_contact('13800138000')={_mask_contact('13800138000')}")
    print(f"_mask_contact('my_secret_wechat_id')={_mask_contact('my_secret_wechat_id')}")

    # 验证限流常量
    assert _IP_RATE_LIMIT_MAX == 5
    assert _IP_RATE_LIMIT_WINDOW_HOURS == 1
    assert _DUPLICATE_WINDOW_HOURS == 24
    assert _BETA_APPLICATION_ADMIN_EVENT == "beta_application.admin_notification.created"
    print("OK")
