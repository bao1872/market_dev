"""策略 API 路由 - 策略目录与版本管理。

端点：
- GET /strategies: 策略列表（支持 kind 过滤）
- GET /strategies/{key}: 策略详情
- GET /strategies/{key}/versions: 版本列表
- GET /strategies/{key}/versions/{version}/schema: 获取版本 schema
- POST /admin/strategies: 创建策略（admin）
- POST /admin/strategies/{key}/versions/{version}/release: 发布版本（admin）

说明：
- /strategies 为只读端点，所有用户可访问
- /admin/strategies 为管理端点，需 admin 角色（当前占位，R2 阶段接入 RBAC）
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.schemas.strategy import (
    CreateStrategyRequest,
    StrategyListResponse,
    StrategyResponse,
    StrategySchemaResponse,
    StrategyVersionListResponse,
    StrategyVersionResponse,
)
from app.services.manifest_validator import ManifestValidationError
from app.services.strategy_service import (
    InvalidStatusTransitionError,
    StrategyNotFoundError,
    StrategyServiceError,
    archive_strategy_version,
    create_strategy,
    get_strategy_by_key,
    get_version_schema,
    list_strategies,
    list_versions,
    release_strategy_version,
)

router = APIRouter(tags=["strategies"])


def _version_to_response(version) -> StrategyVersionResponse:
    """将 ORM 对象转为响应模型。"""
    return StrategyVersionResponse(
        id=version.id,
        strategy_definition_id=version.strategy_definition_id,
        version=version.version,
        status=version.status,
        build_hash=version.build_hash,
        released_at=version.released_at,
        manifest=version.manifest,
    )


@router.get("/strategies", response_model=StrategyListResponse)
async def get_strategies(
    kind: str | None = Query(None, description="按 kind 过滤：selector/monitor"),
    db: AsyncSession = Depends(get_db),
) -> StrategyListResponse:
    """获取策略列表。"""
    definitions = await list_strategies(db, kind=kind)
    items = [StrategyResponse.model_validate(d) for d in definitions]
    return StrategyListResponse(items=items, total=len(items))


@router.get("/strategies/{strategy_key}", response_model=StrategyResponse)
async def get_strategy(
    strategy_key: str,
    db: AsyncSession = Depends(get_db),
) -> StrategyResponse:
    """获取策略详情。"""
    try:
        definition = await get_strategy_by_key(db, strategy_key)
    except StrategyNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(e)
        ) from e
    return StrategyResponse.model_validate(definition)


@router.get(
    "/strategies/{strategy_key}/versions",
    response_model=StrategyVersionListResponse,
)
async def get_strategy_versions(
    strategy_key: str,
    db: AsyncSession = Depends(get_db),
) -> StrategyVersionListResponse:
    """获取策略的所有版本。"""
    try:
        versions = await list_versions(db, strategy_key)
    except StrategyNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(e)
        ) from e
    items = [_version_to_response(v) for v in versions]
    return StrategyVersionListResponse(items=items, total=len(items))


@router.get(
    "/strategies/{strategy_key}/versions/{version}/schema",
    response_model=StrategySchemaResponse,
)
async def get_strategy_version_schema(
    strategy_key: str,
    version: str,
    db: AsyncSession = Depends(get_db),
) -> StrategySchemaResponse:
    """获取策略版本的 schema（参数/输出/输入/能力）。"""
    try:
        schema = await get_version_schema(db, strategy_key, version)
    except StrategyNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(e)
        ) from e
    return StrategySchemaResponse(**schema)


@router.post(
    "/admin/strategies",
    response_model=StrategyVersionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_strategy_endpoint(
    request: CreateStrategyRequest,
    db: AsyncSession = Depends(get_db),
) -> StrategyVersionResponse:
    """创建策略（admin）- 提交 Manifest 创建策略定义 + 草稿版本。"""
    try:
        _, version = await create_strategy(
            db, request.manifest, request.strategy_schema
        )
    except ManifestValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)
        ) from e
    except StrategyServiceError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(e)
        ) from e
    await db.commit()
    return _version_to_response(version)


@router.post(
    "/admin/strategies/{strategy_key}/versions/{version}/release",
    response_model=StrategyVersionResponse,
)
async def release_strategy_version_endpoint(
    strategy_key: str,
    version: str,
    db: AsyncSession = Depends(get_db),
) -> StrategyVersionResponse:
    """发布策略版本（admin）- draft -> released，不可修改。

    需要先通过 strategy_key + version 查找版本 ID。
    """
    # 先查找版本 ID
    try:
        versions = await list_versions(db, strategy_key)
    except StrategyNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(e)
        ) from e

    target = next((v for v in versions if v.version == version), None)
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"策略版本不存在: strategy_key={strategy_key}, version={version}",
        )

    try:
        released = await release_strategy_version(db, target.id)
    except InvalidStatusTransitionError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(e)
        ) from e
    except StrategyNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(e)
        ) from e
    await db.commit()
    return _version_to_response(released)


@router.post(
    "/admin/strategies/{strategy_key}/versions/{version}/archive",
    response_model=StrategyVersionResponse,
)
async def archive_strategy_version_endpoint(
    strategy_key: str,
    version: str,
    db: AsyncSession = Depends(get_db),
) -> StrategyVersionResponse:
    """归档策略版本（admin）- released -> archived。"""
    try:
        versions = await list_versions(db, strategy_key)
    except StrategyNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(e)
        ) from e

    target = next((v for v in versions if v.version == version), None)
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"策略版本不存在: strategy_key={strategy_key}, version={version}",
        )

    try:
        archived = await archive_strategy_version(db, target.id)
    except InvalidStatusTransitionError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(e)
        ) from e
    except StrategyNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(e)
        ) from e
    await db.commit()
    return _version_to_response(archived)


if __name__ == "__main__":
    # 自测入口：验证路由注册
    paths = [r.path for r in router.routes]
    print(f"router.routes={paths}")
    assert "/strategies" in paths
    assert any("/admin/strategies" in p for p in paths)
    print("OK")
