"""FastAPI 依赖注入 - JWT 认证 + RBAC 权限控制 + UserContext 注入。

提供：
- get_db: 异步数据库会话（从 app.db 重新导出）
- get_current_user: 从 Authorization header 解析 JWT，查询数据库返回 User 对象
- get_current_active_user: 检查用户状态为 active
- require_roles(*roles): 角色检查依赖工厂（RBAC）
- get_capture_token_payload: Capture Token 解析依赖（仅用于 /api/v1/capture/* 端点）

关键安全约束（V1.1 15_SECURITY_TENANCY.md + advice.md 第十节）：
- 私有资源的 user_id 由认证上下文注入，不接受客户端传入
- token 类型必须为 access（refresh/capture token 不可用于 API 认证）
- capture token 为短期截图模式令牌，仅通过 URL query parameter 或 Authorization
  header 传给 /api/v1/capture/* 端点，不能访问普通 API
- Capture Token 必须校验 type=capture + scope=stock_detail_capture + exp 未过期
- 用户状态非 active 时拒绝访问
- 角色检查失败返回 403 Forbidden
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

from fastapi import Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_token
from app.db import get_db
from app.models.user import Role, User, UserRole

__all__ = [
    "get_db",
    "get_current_user",
    "get_current_active_user",
    "require_roles",
    "get_capture_token_payload",
]

# Bearer token 提取器（自动从 Authorization: Bearer <token> 解析）
_bearer_scheme = HTTPBearer(
    auto_error=True,
    description="JWT access token（Authorization: Bearer <token>）",
)


async def _fetch_user_with_roles(db: AsyncSession, user_id: UUID) -> User | None:
    """查询用户及其角色名列表。

    通过 user_roles 关联表 JOIN roles 表，获取用户的所有角色名。
    使用单次查询避免 N+1 问题。

    Args:
        db: 异步数据库会话
        user_id: 用户 ID

    Returns:
        User 对象（roles 属性已填充角色名列表），或 None
    """
    # 查询用户
    user_stmt = select(User).where(User.id == user_id)
    user_result = await db.execute(user_stmt)
    user = user_result.scalar_one_or_none()
    if user is None:
        return None

    # 查询用户角色（JOIN user_roles + roles，单次查询）
    role_stmt = (
        select(Role.name)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user_id)
    )
    role_result = await db.execute(role_stmt)
    role_names = [row[0] for row in role_result.all()]

    # 动态挂载 roles 属性（避免 ORM relationship 引入额外查询）
    # 使用 object.__setattr__ 绕过 SQLAlchemy 属性管理
    object.__setattr__(user, "_roles", role_names)
    return user


def _get_user_roles(user: User) -> list[str]:
    """获取用户的角色名列表（从 _fetch_user_with_roles 挂载的 _roles 属性）。"""
    return getattr(user, "_roles", []) or []


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """从 JWT token 解析当前用户。

    流程：
    1. 从 Authorization header 提取 Bearer token
    2. 解码 JWT，验证签名与过期时间
    3. 校验 token 类型为 access（refresh token 不可用于 API 认证）
    4. 从 sub 声明提取 user_id，查询数据库返回 User 对象

    Args:
        credentials: Bearer token 凭证
        db: 异步数据库会话

    Returns:
        当前用户 User 对象（含角色列表）

    Raises:
        HTTPException 401: token 无效/过期/类型错误/用户不存在
    """
    token = credentials.credentials
    try:
        payload = decode_token(token)
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"无效或过期的 token: {e}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e

    # 校验 token 类型：仅 access token 可用于 API 认证
    # capture token 仅通过 URL query parameter 用于截图端点，不经过此依赖
    token_type = payload.get("type")
    if token_type != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="token 类型错误，需要 access token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 从 sub 提取 user_id
    sub = payload.get("sub")
    if not sub:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="token 缺少 sub 声明",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        user_id = UUID(sub)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"token sub 声明不是有效的 UUID: {e}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e

    # 查询用户
    user = await _fetch_user_with_roles(db, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="token 对应的用户不存在",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return user


async def get_current_active_user(
    current_user: User = Depends(get_current_user),
) -> User:
    """检查当前用户状态为 active。

    disabled/pending 状态的用户拒绝访问。

    Args:
        current_user: 当前用户（由 get_current_user 注入）

    Returns:
        active 状态的 User 对象

    Raises:
        HTTPException 403: 用户状态非 active
    """
    if current_user.status != "active":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"用户状态非 active（当前: {current_user.status}），禁止访问",
        )
    return current_user


def require_roles(*required_roles: str) -> Callable[..., object]:
    """角色检查依赖工厂（RBAC）。

    返回一个 FastAPI 依赖函数，检查当前用户是否拥有任一所需角色。
    通常配合 get_current_active_user 使用。

    用法：
        @router.post("/admin/config", dependencies=[Depends(require_roles("admin"))])
        async def create_config(...): ...

    Args:
        *required_roles: 所需角色名（任一匹配即通过）

    Returns:
        FastAPI 依赖函数，校验通过返回当前用户，否则 403
    """
    if not required_roles:
        raise ValueError("require_roles 至少需要一个角色名")

    async def _check_roles(
        current_user: User = Depends(get_current_active_user),
    ) -> User:
        """检查当前用户是否拥有所需角色之一。"""
        user_roles = _get_user_roles(current_user)
        if not any(role in user_roles for role in required_roles):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"权限不足：需要角色 {list(required_roles)} 之一，"
                    f"当前角色 {user_roles}"
                ),
            )
        return current_user

    return _check_roles


# [Capture] - 描述: stock_detail_capture 作用域常量（advice.md 第六节）
CAPTURE_SCOPE_STOCK_DETAIL = "stock_detail_capture"


async def get_capture_token_payload(
    credentials: HTTPAuthorizationCredentials | None = Depends(
        HTTPBearer(auto_error=False)
    ),
    token_query: str | None = Query(
        None, alias="token", description="Capture Token（query 参数，与 Authorization header 二选一）"
    ),
) -> dict[str, Any]:
    """解析并校验 Capture Token，返回 payload。

    [Capture] - 描述: 仅用于 /api/v1/capture/* 端点（advice.md 第六节 + 第十节硬规则）

    支持两种传入方式（任一即可）：
    1. Authorization: Bearer <token>
    2. query 参数 token=<token>（前端 /capture/stock/:symbol?...&token=... 场景）

    校验规则（任一失败返回 401）：
    - token 可解码（签名 + exp 有效）
    - payload.type == "capture"
    - payload.scope == "stock_detail_capture"
    - payload.user_id 存在
    - payload.instrument_id 存在
    - payload.event_id 存在

    普通访问 token（type=access）会被拒绝（type 不匹配），保证 Capture Token 隔离。

    Args:
        credentials: Bearer token 凭证（可选，从 Authorization header 解析）
        token_query: query 参数 token（可选）

    Returns:
        Capture Token payload dict（含 user_id/instrument_id/event_id/scope/type/exp）

    Raises:
        HTTPException 401: token 缺失/无效/类型错误/scope 不匹配/缺少必需声明
    """
    # 1. 提取 token 字符串（Authorization header 优先，回退 query 参数）
    token_str: str | None = None
    if credentials is not None and credentials.credentials:
        token_str = credentials.credentials
    elif token_query:
        token_str = token_query

    if not token_str:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少 Capture Token（Authorization header 或 query 参数 token）",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 2. 解码 + 验证签名/过期
    try:
        payload = decode_token(token_str)
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"无效或过期的 Capture Token: {e}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e

    # 3. 校验 type=capture（拒绝 access/refresh token）
    if payload.get("type") != "capture":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="token 类型错误，需要 capture token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 4. 校验 scope=stock_detail_capture（advice.md 第六节硬规则）
    if payload.get("scope") != CAPTURE_SCOPE_STOCK_DETAIL:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Capture Token scope 错误，需要 {CAPTURE_SCOPE_STOCK_DETAIL}",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 5. 校验必需声明（user_id/instrument_id/event_id）
    missing = [
        k for k in ("user_id", "instrument_id", "event_id") if not payload.get(k)
    ]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Capture Token 缺少必需声明: {missing}",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return payload


if __name__ == "__main__":
    # 自测入口：验证依赖函数可调用
    print(f"get_db={get_db}")
    print(f"get_current_user={get_current_user}")
    print(f"get_current_active_user={get_current_active_user}")
    checker = require_roles("admin")
    print(f"require_roles('admin') -> {checker}")

    # [Capture] - 验证 get_capture_token_payload 与 scope 常量
    assert CAPTURE_SCOPE_STOCK_DETAIL == "stock_detail_capture"
    assert callable(get_capture_token_payload), "get_capture_token_payload 应可调用"
    print(f"CAPTURE_SCOPE_STOCK_DETAIL={CAPTURE_SCOPE_STOCK_DETAIL}")
    print(f"get_capture_token_payload={get_capture_token_payload}")
    print("OK")
