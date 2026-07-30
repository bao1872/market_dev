"""[CHANGE-20260730-010] Umami 访客分析 Adapter。

替代 GoAccess JSON 报告：通过独立只读连接查询 umami 数据库，
返回 today/seven_days/thirty_days 的 PV/UV/热门页面/来源/设备/浏览器/时段趋势。

安全设计：
- 使用独立 umami 只读用户（仅 SELECT 权限）
- 凭据从环境变量 UMAMI_DATABASE_URL 读取（生产由 market.env 注入）
- 前端不接触数据库凭据，只通过 /admin/visitors API 获取聚合结果
- IP 不再存储（Umami 默认不记录完整 IP，session 表无 IP 字段）
- 敏感 query 参数由 _sanitize_path 脱敏

Umami 数据库表结构（v3.2）：
- website_event: event_type=1 为 pageview，含 url_path/referrer_domain/created_at
- session: browser/os/device/country
- website: website_id 与 market.env 的 UMAMI_WEBSITE_ID 对应
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from app.schemas.visitors import VisitorMetricItem, VisitorReport, VisitorSummary

logger = logging.getLogger("umami_analytics_adapter")

# Umami 数据库连接串（asyncpg 形式）
# 生产从 /etc/market-dev/market.env 的 UMAMI_DATABASE_URL 读取
# 格式：postgresql+asyncpg://umami:***@trading-postgres:5432/umami
_DEFAULT_UMAMI_URL = "postgresql+asyncpg://umami:umami@trading-postgres:5432/umami"


def _resolve_umami_db_url() -> str | None:
    """从环境变量读取 Umami 数据库 URL，转 asyncpg 形式。

    umami.env 中是 postgresql://umami:***@...，需转为 postgresql+asyncpg://...
    """
    raw = os.environ.get("UMAMI_DATABASE_URL")
    if not raw:
        return None
    if raw.startswith("postgresql://"):
        return "postgresql+asyncpg://" + raw[len("postgresql://"):]
    if raw.startswith("postgresql+asyncpg://"):
        return raw
    return None


def _get_website_id() -> UUID | None:
    """从环境变量读取 Umami website_id。"""
    raw = os.environ.get("UMAMI_WEBSITE_ID")
    if not raw:
        return None
    try:
        return UUID(raw)
    except ValueError:
        logger.warning("[umami] UMAMI_WEBSITE_ID 非法 UUID: %s", raw)
        return None


# 敏感 query 参数黑名单（出现在 url_path 中时脱敏）
_SENSITIVE_QUERY_KEYS = {"token", "jwt", "password", "passwd", "key", "secret", "api_key", "access_token"}


def _sanitize_path(path: str) -> str:
    """脱敏路径中的敏感 query 参数（与原 GoAccess 实现一致）。"""
    if "?" not in path:
        return path
    base, query = path.split("?", 1)
    parts = []
    for kv in query.split("&"):
        if "=" in kv:
            k, _ = kv.split("=", 1)
            if k.lower() in _SENSITIVE_QUERY_KEYS:
                parts.append(f"{k}=***")
            else:
                parts.append(kv)
        else:
            parts.append(kv)
    return f"{base}?{'&'.join(parts)}"


@asynccontextmanager
async def _get_umami_session() -> AsyncIterator[AsyncSession]:
    """获取 Umami 数据库只读 session。

    每次请求创建独立 engine，避免与主业务库混淆。
    Umami 查询频率低（admin 页面 5 分钟轮询），性能可接受。
    """
    url = _resolve_umami_db_url()
    if not url:
        raise RuntimeError("UMAMI_DATABASE_URL 未配置")
    engine: AsyncEngine = create_async_engine(url, pool_pre_ping=True, pool_size=2)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session
    finally:
        await engine.dispose()


async def _query_summary(
    session: AsyncSession,
    website_id: UUID,
    start: datetime,
    end: datetime,
) -> VisitorSummary:
    """查询单个时间窗口的汇总数据。

    Args:
        session: Umami 数据库 session
        website_id: Umami website_id
        start: 窗口起始时间（UTC）
        end: 窗口结束时间（UTC）
    """
    # PV = pageview 数量
    pv_sql = text("""
        SELECT count(*)::int AS cnt
        FROM website_event
        WHERE website_id = :wid
          AND event_type = 1
          AND created_at >= :start
          AND created_at < :end
    """)
    pv_result = await session.execute(pv_sql, {"wid": website_id, "start": start, "end": end})
    pv = pv_result.scalar_one()

    # UV = distinct session_id
    uv_sql = text("""
        SELECT count(DISTINCT session_id)::int AS cnt
        FROM website_event
        WHERE website_id = :wid
          AND event_type = 1
          AND created_at >= :start
          AND created_at < :end
    """)
    uv_result = await session.execute(uv_sql, {"wid": website_id, "start": start, "end": end})
    uv = uv_result.scalar_one()

    # 热门页面 Top 10
    pages_sql = text("""
        SELECT url_path AS label, count(*)::int AS cnt,
               count(*) * 100.0 / NULLIF(:pv, 0) AS pct
        FROM website_event
        WHERE website_id = :wid
          AND event_type = 1
          AND created_at >= :start
          AND created_at < :end
        GROUP BY url_path
        ORDER BY cnt DESC
        LIMIT 10
    """)
    pages_result = await session.execute(pages_sql, {"wid": website_id, "start": start, "end": end, "pv": pv})
    top_pages = [
        VisitorMetricItem(
            label=_sanitize_path(str(row[0])),
            count=int(row[1]),
            percentage=float(row[2]) if row[2] is not None else None,
        )
        for row in pages_result
    ]

    # 来源 Top 10
    ref_sql = text("""
        SELECT COALESCE(referrer_domain, '直接访问') AS label,
               count(*)::int AS cnt,
               count(*) * 100.0 / NULLIF(:pv, 0) AS pct
        FROM website_event
        WHERE website_id = :wid
          AND event_type = 1
          AND created_at >= :start
          AND created_at < :end
        GROUP BY referrer_domain
        ORDER BY cnt DESC
        LIMIT 10
    """)
    ref_result = await session.execute(ref_sql, {"wid": website_id, "start": start, "end": end, "pv": pv})
    top_referrers = [
        VisitorMetricItem(
            label=str(row[0]),
            count=int(row[1]),
            percentage=float(row[2]) if row[2] is not None else None,
        )
        for row in ref_result
    ]

    # 设备/浏览器（join session 表）
    dev_sql = text("""
        SELECT s.device AS label, count(*)::int AS cnt,
               count(*) * 100.0 / NULLIF(:pv, 0) AS pct
        FROM website_event e
        JOIN session s ON s.session_id = e.session_id
        WHERE e.website_id = :wid
          AND e.event_type = 1
          AND e.created_at >= :start
          AND e.created_at < :end
          AND s.device IS NOT NULL AND s.device <> ''
        GROUP BY s.device
        ORDER BY cnt DESC
        LIMIT 10
    """)
    dev_result = await session.execute(dev_sql, {"wid": website_id, "start": start, "end": end, "pv": pv})
    devices = [
        VisitorMetricItem(
            label=str(row[0]),
            count=int(row[1]),
            percentage=float(row[2]) if row[2] is not None else None,
        )
        for row in dev_result
    ]

    br_sql = text("""
        SELECT s.browser AS label, count(*)::int AS cnt,
               count(*) * 100.0 / NULLIF(:pv, 0) AS pct
        FROM website_event e
        JOIN session s ON s.session_id = e.session_id
        WHERE e.website_id = :wid
          AND e.event_type = 1
          AND e.created_at >= :start
          AND e.created_at < :end
          AND s.browser IS NOT NULL AND s.browser <> ''
        GROUP BY s.browser
        ORDER BY cnt DESC
        LIMIT 10
    """)
    br_result = await session.execute(br_sql, {"wid": website_id, "start": start, "end": end, "pv": pv})
    browsers = [
        VisitorMetricItem(
            label=str(row[0]),
            count=int(row[1]),
            percentage=float(row[2]) if row[2] is not None else None,
        )
        for row in br_result
    ]

    # 24 小时时段趋势（按小时聚合）
    hourly_sql = text("""
        SELECT to_char(created_at AT TIME ZONE 'Asia/Shanghai', 'HH24:00') AS label,
               count(*)::int AS cnt
        FROM website_event
        WHERE website_id = :wid
          AND event_type = 1
          AND created_at >= :start
          AND created_at < :end
        GROUP BY label
        ORDER BY label
    """)
    hourly_result = await session.execute(hourly_sql, {"wid": website_id, "start": start, "end": end})
    hourly_trend = [
        VisitorMetricItem(label=str(row[0]), count=int(row[1]), percentage=None)
        for row in hourly_result
    ]

    return VisitorSummary(
        pv=pv,
        uv=uv,
        top_pages=top_pages,
        top_referrers=top_referrers,
        status_codes=[],  # Umami 不记录 HTTP 状态码
        devices=devices,
        browsers=browsers,
        hourly_trend=hourly_trend,
    )


async def fetch_umami_report() -> VisitorReport:
    """从 Umami 数据库查询访客统计报告。

    Returns:
        VisitorReport，data_source 为 'umami' / 'empty' / 'error'
    """
    website_id = _get_website_id()
    if website_id is None:
        return VisitorReport(
            data_source="empty",
            error_message="UMAMI_WEBSITE_ID 未配置，Umami 访客分析未启用",
        )

    db_url = _resolve_umami_db_url()
    if not db_url:
        return VisitorReport(
            data_source="empty",
            error_message="UMAMI_DATABASE_URL 未配置，Umami 访客分析未启用",
        )

    try:
        async with _get_umami_session() as session:
            now = datetime.now(UTC)
            today_start = now - timedelta(hours=now.hour, minutes=now.minute, seconds=now.second, microseconds=now.microsecond)
            seven_days_start = today_start - timedelta(days=6)
            thirty_days_start = today_start - timedelta(days=29)

            today_summary = await _query_summary(session, website_id, today_start, now)
            seven_days_summary = await _query_summary(session, website_id, seven_days_start, now)
            thirty_days_summary = await _query_summary(session, website_id, thirty_days_start, now)

            return VisitorReport(
                today=today_summary,
                seven_days=seven_days_summary,
                thirty_days=thirty_days_summary,
                generated_at=now,
                data_source="umami",
                error_message=None,
            )
    except RuntimeError as exc:
        logger.warning("[umami] 配置缺失: %s", exc)
        return VisitorReport(
            data_source="empty",
            error_message=str(exc),
        )
    except Exception as exc:
        logger.exception("[umami] 查询 Umami 数据库异常")
        return VisitorReport(
            data_source="error",
            error_message=f"Umami 数据库查询异常: {exc}",
        )
