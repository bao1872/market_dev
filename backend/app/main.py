"""FastAPI 应用入口。

V1.1 交易平台后端，提供：
- /health: 健康检查
- /auth: 认证 API（登录/注册/续期/刷新/当前用户）（R2 + V1.6）
- /me: 当前用户信息（R2）
- /me/membership: 当前用户会员状态（V1.6）
- /instruments: 股票主数据 API（R3）
- /calendar: 交易日历 API（R4）
- /strategies: 策略目录与版本（R7）
- /admin/strategies: 策略管理（R7）
- /admin/strategies/{key}/run: 策略运行（R12）
- /admin/config: 配置注册表管理（R6）
- /admin/invite-codes: 邀请码管理（V1.6）
- /admin/members: 会员账户管理（V1.6）
- /messages: 通知消息（R9）
- /notification-channels: 通知渠道（R9）
- /instruments/{id}/monitor-states: 监控状态查询（M3）
- /strategies/{key}/monitor-states: 监控状态查询（M3）
- /instruments/{id}/events: 策略事件查询（M4）
- /strategies/{key}/events: 策略事件查询（M4）
- /strategy-events/{id}: 事件详情（M4）
- /monitoring-plans: 监控组合方案管理（C5）
- /monitoring-plans/{id}/states: 方案状态查询（C6）
- /monitoring-plans/{id}/events: 组合事件查询（C8）
- /instruments/{id}/composite-state: 个股组合状态查询（C6）
- /composite-events/{id}: 组合事件详情（C8）
- /selection-plans: 选股组合方案管理（C1）
- /selection-plans/{id}/run: 选股方案执行（C4，幂等）
- /selection-plan-runs/{run_id}/results: 选股运行结果（C4）
- /metrics: Prometheus 指标端点（可观察性，无需认证）
- /api/v1: 业务 API（行情查询等）
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from app.api import metrics as metrics_api
from app.api.admin_config import router as admin_config_router
from app.api.admin_membership import router as admin_membership_router
from app.api.auth import router as auth_router
from app.api.bars import router as bars_router
from app.api.calendar import router as calendar_router
from app.api.health import router as health_router
from app.api.instruments import router as instruments_router
from app.api.metrics import http_request_duration_seconds, http_requests_total
from app.api.monitor_states import router as monitor_states_router
from app.api.monitoring_plans import router as monitoring_plans_router
from app.api.notifications import router as notifications_router
from app.api.selection_plans import router as selection_plans_router
from app.api.strategies import router as strategies_router
from app.api.strategy_events import router as strategy_events_router
from app.api.strategy_runs import router as strategy_runs_router
from app.api.watchlist import router as watchlist_router
from app.db import AsyncSessionLocal

logger = logging.getLogger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时初始化种子数据。

    幂等设计：seed_strategies 内部检查已存在的策略/版本并跳过，
    重复启动不会重复创建。
    """
    from app.services.strategy_seed import seed_strategies

    try:
        async with AsyncSessionLocal() as db:
            results = await seed_strategies(db, release=True)
            for strategy_key, version, status in results:
                logger.info(
                    "种子策略已注册: %s v%s -> %s",
                    strategy_key, version, status,
                )
    except Exception as e:
        logger.error("种子数据初始化失败（不影响启动）: %s", e)

    yield


app = FastAPI(
    title="Trading Platform V1.1",
    description="多用户选股与盘中监控平台后端",
    version="1.1.0",
    lifespan=lifespan,
)

# 健康检查路由
app.include_router(health_router)
# 认证路由（R2：登录/刷新/当前用户）
app.include_router(auth_router)
# 股票主数据路由（R3）
app.include_router(instruments_router)
# 交易日历路由（R4）
app.include_router(calendar_router)
# 行情查询路由
app.include_router(bars_router)
# 策略目录与版本路由（R7）
app.include_router(strategies_router)
# 策略运行与结果路由（R12）
app.include_router(strategy_runs_router)
# 监控状态查询路由（M3）
app.include_router(monitor_states_router)
# 策略事件查询路由（M4）
app.include_router(strategy_events_router)
# 通知消息与渠道路由（R9）
app.include_router(notifications_router)
# 配置注册表管理路由（R6，需 admin 角色）
app.include_router(admin_config_router)
# 会员与邀请码管理路由（V1.6，需 admin 角色）
app.include_router(admin_membership_router)
# 用户自选股路由（W1）
app.include_router(watchlist_router)
# 监控组合方案管理路由（C5/C6/C8）
app.include_router(monitoring_plans_router)
# 选股组合方案管理路由（C1/C4）
app.include_router(selection_plans_router)
# Prometheus 指标路由（无需认证，供 scraper 直接抓取）
app.include_router(metrics_api.router, tags=["metrics"])


def _record_http_metrics(request: Request, status: str, duration: float) -> None:
    """记录单次 HTTP 请求的计数与延迟指标到 Prometheus。

    使用路由模板路径（如 /instruments/{id}）作为 path label，避免按真实路径
    产生高基数；未匹配路由时回退到 request.url.path。
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None) or request.url.path
    method = request.method
    http_requests_total.labels(method=method, path=path, status=status).inc()
    http_request_duration_seconds.labels(method=method, path=path).observe(duration)


@app.middleware("http")
async def prometheus_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Prometheus 中间件：自动记录所有 HTTP 请求的计数与延迟。

    异常路径下仍记录指标（status 标记为 500）后重新抛出，确保不吞异常。
    """
    start = time.time()
    try:
        response = await call_next(request)
    except Exception:
        duration = time.time() - start
        _record_http_metrics(request, "500", duration)
        raise
    duration = time.time() - start
    _record_http_metrics(request, str(response.status_code), duration)
    return response


@app.get("/")
async def root() -> dict[str, str]:
    """根路径，返回应用信息。"""
    return {"app": "trading-platform", "version": "1.1.0"}


if __name__ == "__main__":
    # 自测入口：验证 app 创建
    print(f"app.title={app.title}")
    print(f"app.version={app.version}")
    # 跳过 _IncludedRouter 等无 path 属性的路由对象
    routes = [r.path for r in app.routes if hasattr(r, "path")]
    print(f"routes={routes}")
