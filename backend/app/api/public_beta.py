"""公开 API - 内测申请端点（无需登录）。

提供：
- POST /public/beta-applications: 提交内测申请（公开端点，spec 第三节）

设计说明：
- 无需 JWT 认证（不依赖 get_current_user）
- IP 提取：优先 X-Forwarded-For 首段（反向代理场景），回退 Request.client.host
- IP 哈希：SHA256（不可逆，保护隐私，不存储原始 IP）
- 限流：同 ip_hash 1h 内 ≤5 次（service 层实现，超限 raise HTTPException 429）
- 重复检测：同 phone/wechat 24h 内返回原申请（service 层实现）
- 响应：仅返回 id/status/submitted_at，不返回完整联系方式（隐私保护）
- 状态码：新申请 201，重复提交 200，限流 429，校验失败 422
"""

from __future__ import annotations

import hashlib
import logging

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.route_utils import get_route_paths
from app.db import get_db
from app.schemas.beta_application import (
    BetaApplicationCreate,
    BetaApplicationResponse,
)
from app.services.beta_application_service import create_application

logger = logging.getLogger("public_beta")

router = APIRouter(prefix="/public", tags=["public"])


def _extract_client_ip(request: Request) -> str:
    """从请求中提取客户端 IP。

    优先使用 X-Forwarded-For 首段（反向代理场景，如 Nginx），
    回退到 Request.client.host（直连场景）。

    Args:
        request: FastAPI 请求对象

    Returns:
        客户端 IP 字符串（无法确定时返回 "unknown"）
    """
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        # X-Forwarded-For: client, proxy1, proxy2 -> 取首个（最原始客户端）
        return forwarded_for.split(",")[0].strip()
    client = request.client
    if client and client.host:
        return client.host
    return "unknown"


def _hash_ip(ip: str) -> str:
    """计算 IP 的 SHA256 哈希（不可逆，保护隐私）。

    Args:
        ip: 客户端 IP 字符串

    Returns:
        SHA256 哈希的十六进制字符串
    """
    return hashlib.sha256(ip.encode("utf-8")).hexdigest()


@router.post(
    "/beta-applications",
    response_model=BetaApplicationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_beta_application(
    payload: BetaApplicationCreate,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> BetaApplicationResponse:
    """提交内测申请（公开端点，无需登录）。

    流程：
    1. IP 提取 + SHA256 哈希
    2. 调用 create_application（重复检测 → IP 限流 → DB 写入 → Outbox）
    3. 新申请返回 201，重复提交返回 200

    异常：
    - 422: 请求体校验失败（FastAPI Pydantic 自动处理）
    - 429: IP 频率限制（service 层 raise HTTPException）

    Args:
        payload: 请求体（已通过 schema 校验）
        request: FastAPI 请求对象（用于提取 IP）
        response: FastAPI 响应对象（用于覆盖重复提交的状态码）
        db: 异步数据库会话

    Returns:
        BetaApplicationResponse: 申请编号、状态、提交时间
    """
    client_ip = _extract_client_ip(request)
    ip_hash = _hash_ip(client_ip)

    app, is_new = await create_application(
        db=db, payload=payload, ip_hash=ip_hash
    )

    if not is_new:
        # 重复提交：覆盖默认 201 为 200
        response.status_code = status.HTTP_200_OK

    return BetaApplicationResponse.model_validate(app)


if __name__ == "__main__":
    # 自测入口：验证路由注册
    print(f"router.routes={get_route_paths(router.routes)}")
    # 验证 IP 哈希函数
    assert _hash_ip("192.168.1.1") == _hash_ip("192.168.1.1")
    assert _hash_ip("192.168.1.1") != _hash_ip("192.168.1.2")
    assert len(_hash_ip("test")) == 64  # SHA256 十六进制长度
    print(f"_hash_ip('192.168.1.1')={_hash_ip('192.168.1.1')[:16]}...")
    print("OK")
