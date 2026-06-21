"""管理员配置注册表 API 路由 - 配置查询与更新。

端点：
- GET /admin/config: 查询配置列表（支持 scope/sensitivity/value_type 筛选 + 分页）
- GET /admin/config/{config_key}: 查询单个配置（Secret 脱敏）
- PUT /admin/config/{config_key}: 更新配置值（Secret 加密存储）

权限：
- 所有端点需要 admin 角色（RBAC，通过 require_roles("admin") 依赖注入）

Secret 处理（V1.1 06_CONFIGURATION_CENTER.md §4）：
- value_type=secret 时，更新时接收明文，服务端用 Fernet 加密后存入 current_value
- 查询时 current_value 脱敏为 "***"，不返回明文/密文
- 管理员不可读取用户 Secret 明文
"""

from __future__ import annotations

import math
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db, require_roles
from app.core.security import encrypt_secret
from app.models.config import ConfigDefinition
from app.schemas.config import (
    SECRET_MASK,
    ConfigDefinitionResponse,
    ConfigDefinitionUpdate,
    ConfigListResponse,
)
from app.services.config_validator import ConfigValidationError, validate_config_value

router = APIRouter(
    prefix="/admin/config",
    tags=["admin-config"],
    # 所有端点需要 admin 角色
    dependencies=[Depends(require_roles("admin"))],
)


def _is_secret_config(config: ConfigDefinition) -> bool:
    """判断配置项是否为 Secret 类型（需要脱敏）。

    sensitivity=secret 或 value_type=secret 均视为 Secret。
    """
    return config.sensitivity == "secret" or config.value_type == "secret"


def _to_response(config: ConfigDefinition) -> ConfigDefinitionResponse:
    """将 ConfigDefinition ORM 对象转换为响应模型。

    Secret 配置的 current_value 脱敏为 "***"。
    """
    current_value: Any = config.current_value
    if _is_secret_config(config) and current_value is not None:
        current_value = SECRET_MASK
    return ConfigDefinitionResponse(
        id=config.id,
        config_key=config.config_key,
        display_name=config.display_name,
        description=config.description,
        value_type=config.value_type,
        allowed_scopes=config.allowed_scopes or [],
        default_value=config.default_value,
        current_value=current_value,
        is_required=config.is_required,
        validation=config.validation,
        sensitivity=config.sensitivity,
        restart_policy=config.restart_policy,
        ui=config.ui or {},
        test_action=config.test_action,
        audit=config.audit,
        status=config.status,
        created_at=config.created_at,
        updated_at=config.updated_at,
    )


@router.get("", response_model=ConfigListResponse)
async def list_configs(
    scope: str | None = Query(None, description="按 allowed_scopes 筛选"),
    sensitivity: str | None = Query(
        None, description="按敏感级别筛选：public/internal/secret"
    ),
    value_type: str | None = Query(None, description="按值类型筛选"),
    page: int = Query(1, ge=1, description="页码（从 1 开始）"),
    page_size: int = Query(20, ge=1, le=100, description="每页大小（最大 100）"),
    db: AsyncSession = Depends(get_db),
) -> ConfigListResponse:
    """查询配置列表，支持 scope/sensitivity/value_type 筛选与分页。

    Secret 配置的 current_value 脱敏为 "***"。
    """
    # 构建查询条件
    stmt = select(ConfigDefinition)
    if sensitivity:
        stmt = stmt.where(ConfigDefinition.sensitivity == sensitivity)
    if value_type:
        stmt = stmt.where(ConfigDefinition.value_type == value_type)
    # scope 筛选：JSONB 数组包含查询（PostgreSQL 特有）
    if scope:
        stmt = stmt.where(ConfigDefinition.allowed_scopes.contains([scope]))

    # 计数查询
    count_stmt = select(func.count()).select_from(stmt.subquery())
    count_result = await db.execute(count_stmt)
    total = count_result.scalar_one()

    # 分页数据查询
    data_stmt = (
        stmt.order_by(ConfigDefinition.config_key)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    data_result = await db.execute(data_stmt)
    configs = data_result.scalars().all()

    pages = math.ceil(total / page_size) if total > 0 else 0

    return ConfigListResponse(
        items=[_to_response(c) for c in configs],
        total=total,
        page=page,
        page_size=page_size,
        pages=pages,
    )


@router.get("/{config_key}", response_model=ConfigDefinitionResponse)
async def get_config(
    config_key: str,
    db: AsyncSession = Depends(get_db),
) -> ConfigDefinitionResponse:
    """查询单个配置（按 config_key）。Secret 配置脱敏。"""
    stmt = select(ConfigDefinition).where(ConfigDefinition.config_key == config_key)
    result = await db.execute(stmt)
    config = result.scalar_one_or_none()
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"配置项不存在: config_key={config_key}",
        )
    return _to_response(config)


@router.put("/{config_key}", response_model=ConfigDefinitionResponse)
async def update_config(
    config_key: str,
    payload: ConfigDefinitionUpdate,
    db: AsyncSession = Depends(get_db),
) -> ConfigDefinitionResponse:
    """更新配置值。

    流程：
    1. 按 config_key 查询配置定义
    2. 校验新值符合 value_type 与 validation 规则
    3. 若为 Secret 类型，加密后存入 current_value；否则直接存入
    4. 提交事务并返回更新后的配置（Secret 脱敏）

    Args:
        config_key: 配置项唯一标识
        payload: 更新请求（current_value 为新值，Secret 类型传明文）
        db: 异步数据库会话

    Returns:
        更新后的配置定义响应（Secret 脱敏）

    Raises:
        HTTPException 404: 配置项不存在
        HTTPException 422: 配置值校验失败
    """
    # 查询配置定义
    stmt = select(ConfigDefinition).where(ConfigDefinition.config_key == config_key)
    result = await db.execute(stmt)
    config = result.scalar_one_or_none()
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"配置项不存在: config_key={config_key}",
        )

    # 校验新值
    try:
        validate_config_value(
            payload.current_value,
            config.value_type,
            config.validation,
        )
    except ConfigValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"配置值校验失败: {e}",
        ) from e

    # Secret 类型：加密后存储；其他类型：直接存储
    if _is_secret_config(config):
        try:
            encrypted = encrypt_secret(str(payload.current_value))
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Secret 加密失败: {e}",
            ) from e
        config.current_value = encrypted
    else:
        config.current_value = payload.current_value

    await db.commit()
    # 重新查询以获取更新后的时间戳
    await db.refresh(config)
    return _to_response(config)


if __name__ == "__main__":
    # 自测入口：验证路由注册
    paths = [r.path for r in router.routes]
    print(f"router.routes={paths}")
    assert "/admin/config" in paths
    assert "/admin/config/{config_key}" in paths
    print(f"SECRET_MASK={SECRET_MASK}")
    print("OK")
