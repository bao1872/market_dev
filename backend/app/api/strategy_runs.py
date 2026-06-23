"""策略运行 API 路由 - 触发运行、查询历史、查询结果、发布批次。

端点：
- POST /admin/strategies/{key}/run: 触发策略运行（admin）— 仅创建 queued 运行，Worker 异步执行
- POST /admin/strategy-runs/{run_id}/publish: 发布运行结果（admin）
- GET /strategies/{key}/runs: 运行历史（admin）
- GET /strategies/{key}/published-runs: 已发布批次列表（普通用户可访问）
- GET /strategies/{key}/results: 查询策略结果（用户端，绑定 published run）
- GET /strategy-runs/{run_id}/results: 运行结果（分页+筛选+排序，需 published）
- GET /strategy-runs/{run_id}/results/{result_id}: 单个结果详情

说明：
- /admin/strategies 为管理端点，需 admin 角色
- /strategies 为只读端点，所有用户可访问
- 运行结果支持按指标筛选（metric_filters，支持 gt/gte/lt/lte/eq/between）和排序（sort_by）
- metric_key 必须在 manifest outputs.filterable 白名单中
- 普通用户只能查询 published 状态的 run 结果
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db, require_roles
from app.models.strategy import StrategyVersion
from app.models.strategy_run import StrategyRun
from app.repositories import strategy_result_repository
from app.repositories.strategy_result_repository import (
    MetricFilter,
    QueryResultPage,
    SortSpec,
    dict_filters_to_metric_filters,
)
from app.schemas.strategy_run import (
    StrategyResultListResponse,
    StrategyResultResponse,
    StrategyRunListResponse,
    StrategyRunResponse,
    TriggerRunRequest,
)
from app.services.strategy_batch_service import StrategyBatchService
from app.services.strategy_service import (
    StrategyNotFoundError,
    list_versions,
)

logger = logging.getLogger("api.strategy_runs")

router = APIRouter(tags=["strategy-runs"])

# 合法 operator 枚举
VALID_OPERATORS = {"gt", "gte", "lt", "lte", "eq", "between"}


async def _get_latest_version_id(
    db: AsyncSession, strategy_key: str
) -> tuple[uuid.UUID, StrategyVersion]:
    """获取策略的最新 released 版本。

    Args:
        db: 异步会话
        strategy_key: 策略 key

    Returns:
        (version_id, version) 元组

    Raises:
        HTTPException 404: 策略或版本不存在
    """
    try:
        versions = await list_versions(db, strategy_key)
    except StrategyNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(e)
        ) from e

    # 优先选择 released 版本，其次选择最新版本
    released = [v for v in versions if v.status == "released"]
    if released:
        version = released[-1]
    elif versions:
        version = versions[-1]
    else:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"策略无可用版本: strategy_key={strategy_key}",
        )

    return version.id, version


def _get_filterable_metric_keys(version: StrategyVersion) -> set[str]:
    """从 manifest outputs 中提取 filterable=true 的 metric_key 集合。

    Args:
        version: 策略版本 ORM 对象

    Returns:
        filterable metric_key 集合
    """
    manifest = version.manifest
    outputs = manifest.get("outputs", [])
    return {
        o["key"] for o in outputs
        if o.get("filterable") is True
    }


def _validate_metric_filters(
    filters: list[dict],
    version: StrategyVersion,
) -> None:
    """校验 metric_filters 中的 metric_key 和 operator。

    Args:
        filters: 指标筛选条件列表
        version: 策略版本 ORM 对象

    Raises:
        HTTPException 422: metric_key 不在白名单或 operator 非法
    """
    filterable_keys = _get_filterable_metric_keys(version)
    for f in filters:
        metric_key = f.get("metric_key")
        op = f.get("operator")

        if metric_key not in filterable_keys:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"非法 metric_key: {metric_key}（不在 filterable 白名单中）",
            )

        if op not in VALID_OPERATORS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"非法 operator: {op}（合法值: {VALID_OPERATORS}）",
            )

        # between 操作必须有 value1 和 value2
        if op == "between":
            if f.get("value1") is None or f.get("value2") is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"between 操作需要 value1 和 value2: metric_key={metric_key}",
                )
        else:
            # 非 between 操作必须有 value
            if f.get("value") is None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"操作 {op} 需要 value: metric_key={metric_key}",
                )


@router.post(
    "/admin/strategies/{strategy_key}/run",
    response_model=StrategyRunResponse,
    status_code=status.HTTP_201_CREATED,
)
async def trigger_strategy_run(
    strategy_key: str,
    request: TriggerRunRequest,
    db: AsyncSession = Depends(get_db),
    _user=Depends(require_roles("admin")),
) -> StrategyRunResponse:
    """触发策略运行（admin）— 仅创建 queued 运行，Worker 异步执行。

    流程：
    1. 调用 StrategyBatchService.create_batch_run 创建 queued 运行
    2. 数据就绪检查（非交易日/数据未就绪则拒绝）
    3. 预创建 strategy_run_items（status=pending）
    4. Worker 轮询 queued run 并执行

    Args:
        strategy_key: 策略 key
        request: 运行请求（trade_date/instrument_ids/run_type）
        db: 异步会话

    Returns:
        运行记录响应（status=queued）
    """
    trade_date = request.trade_date or date.today()
    service = StrategyBatchService()

    try:
        run = await service.create_batch_run(
            db,
            strategy_key=strategy_key,
            trade_date=trade_date,
            run_type=request.run_type,
            instrument_ids=request.instrument_ids,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e
    except RuntimeError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"创建批量计算失败: {e}",
        ) from e

    await db.commit()
    return StrategyRunResponse.model_validate(run)


@router.post(
    "/admin/strategy-runs/{run_id}/publish",
    response_model=StrategyRunResponse,
)
async def publish_strategy_run(
    run_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user=Depends(require_roles("admin")),
) -> StrategyRunResponse:
    """发布运行结果（admin）— completed/partial_failed → published。

    Args:
        run_id: 运行 ID
        db: 异步会话

    Returns:
        更新后的运行记录响应（status=published）
    """
    service = StrategyBatchService()

    try:
        run = await service.publish_run(db, run_id)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e
    except RuntimeError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"发布失败: {e}",
        ) from e

    await db.commit()
    return StrategyRunResponse.model_validate(run)


@router.get(
    "/strategies/{strategy_key}/runs",
    response_model=StrategyRunListResponse,
)
async def list_strategy_runs(
    strategy_key: str,
    status_filter: str | None = Query(None, alias="status", description="运行状态过滤"),
    limit: int = Query(50, ge=1, le=200, description="返回上限"),
    offset: int = Query(0, ge=0, description="偏移量"),
    db: AsyncSession = Depends(get_db),
    _user=Depends(require_roles("admin")),
) -> StrategyRunListResponse:
    """查询策略运行历史（admin）。

    Args:
        strategy_key: 策略 key
        status_filter: 运行状态过滤
        limit: 返回上限
        offset: 偏移量

    Returns:
        运行列表响应
    """
    # 查找策略所有版本
    try:
        versions = await list_versions(db, strategy_key)
    except StrategyNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(e)
        ) from e

    if not versions:
        return StrategyRunListResponse(items=[], total=0)

    # 查询所有版本的运行记录
    all_runs: list = []
    total = 0
    for version in versions:
        runs, count = await strategy_result_repository.list_runs(
            db,
            strategy_version_id=version.id,
            status=status_filter,
            limit=limit,
            offset=offset,
        )
        all_runs.extend(runs)
        total += count

    # 按开始时间降序排序
    from datetime import UTC, datetime
    all_runs.sort(
        key=lambda r: r.started_at or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    # 分页
    paginated = all_runs[:limit]

    items = [StrategyRunResponse.model_validate(r) for r in paginated]
    return StrategyRunListResponse(items=items, total=total)


@router.get(
    "/strategies/{strategy_key}/published-runs",
    response_model=StrategyRunListResponse,
)
async def list_published_runs(
    strategy_key: str,
    limit: int = Query(30, ge=1, le=100, description="返回上限"),
    offset: int = Query(0, ge=0, description="偏移量"),
    db: AsyncSession = Depends(get_db),
) -> StrategyRunListResponse:
    """查询已发布的运行批次（普通用户可访问）。

    只返回 status='published' 的 run，按 trade_date 降序。

    Args:
        strategy_key: 策略 key
        limit: 返回上限
        offset: 偏移量

    Returns:
        已发布运行列表响应
    """
    # 查找策略所有版本
    try:
        versions = await list_versions(db, strategy_key)
    except StrategyNotFoundError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(e)
        ) from e

    if not versions:
        return StrategyRunListResponse(items=[], total=0)

    # 查询所有版本的 published 运行记录
    all_runs: list = []
    total = 0
    for version in versions:
        runs, count = await strategy_result_repository.list_runs(
            db,
            strategy_version_id=version.id,
            status="published",
            limit=limit,
            offset=offset,
        )
        all_runs.extend(runs)
        total += count

    # 按 trade_date 降序排序
    all_runs.sort(
        key=lambda r: r.trade_date or date.min,
        reverse=True,
    )
    paginated = all_runs[:limit]

    items = [StrategyRunResponse.model_validate(r) for r in paginated]
    return StrategyRunListResponse(items=items, total=total)


@router.get(
    "/strategies/{strategy_key}/results",
    response_model=StrategyResultListResponse,
)
async def query_strategy_results(
    strategy_key: str,
    trade_date: date = Query(..., description="交易日"),
    metric_filters: str | None = Query(
        None,
        description=(
            '指标筛选 JSON，如 '
            '"[{"metric_key":"dsa_dir_bars","operator":"gte","value":50}]"'
        ),
    ),
    sort_by: str | None = Query(None, description="排序指标名"),
    sort_desc: bool = Query(False, description="是否降序"),
    limit: int = Query(100, ge=1, le=500, description="返回上限"),
    offset: int = Query(0, ge=0, description="偏移量"),
    db: AsyncSession = Depends(get_db),
) -> StrategyResultListResponse:
    """查询策略结果（用户端，绑定 published run）。

    流程：
    1. 查找 strategy_key 最新 released 版本
    2. 查找该版本在 trade_date 的 published run
    3. 校验 metric_filters（metric_key 白名单 + operator 校验）
    4. 调用 query_results(published_run_id, ...) 进行 SQL 端过滤

    Args:
        strategy_key: 策略 key
        trade_date: 交易日
        metric_filters: 指标筛选条件 JSON
        sort_by: 排序指标名
        sort_desc: 是否降序
        limit: 返回上限
        offset: 偏移量

    Returns:
        结果列表响应
    """
    # 1. 查找策略版本
    version_id, version = await _get_latest_version_id(db, strategy_key)

    # 2. 查找 published run
    run_stmt = (
        select(StrategyRun)
        .where(
            StrategyRun.strategy_version_id == version_id,
            StrategyRun.trade_date == trade_date,
            StrategyRun.status == "published",
        )
        .order_by(StrategyRun.published_at.desc())
        .limit(1)
    )
    run_result = await db.execute(run_stmt)
    run = run_result.scalar_one_or_none()
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"未找到已发布的运行: strategy_key={strategy_key}, trade_date={trade_date}",
        )

    # 3. 解析 metric_filters JSON
    filters: list[dict] | None = None
    if metric_filters:
        try:
            filters = json.loads(metric_filters)
        except json.JSONDecodeError as e:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"metric_filters JSON 解析失败: {e}",
            ) from e

    # 4. 校验 metric_filters
    if filters:
        _validate_metric_filters(filters, version)

    # 5. 查询结果（SQL 端过滤，绑定 published run）
    metric_filter_list = dict_filters_to_metric_filters(filters)
    sort_spec = SortSpec(field=sort_by, desc=sort_desc) if sort_by else None
    page = await strategy_result_repository.query_results(
        db,
        run_id=run.id,
        strategy_version_id=version_id,
        trade_date=trade_date,
        filters=metric_filter_list,
        sort=sort_spec,
        limit=limit,
        offset=offset,
    )

    result_items = [StrategyResultResponse.model_validate(r) for r in page.items]
    page_num = offset // limit + 1 if limit > 0 else 1
    return StrategyResultListResponse(
        items=result_items,
        total=page.total,
        page=page_num,
        page_size=limit,
    )


@router.get(
    "/strategy-runs/{run_id}/results",
    response_model=StrategyResultListResponse,
)
async def list_run_results(
    run_id: uuid.UUID,
    metric_filters: str | None = Query(
        None,
        description=(
            '指标筛选 JSON，如 '
            '"[{"metric_key":"dsa_dir_bars","operator":"gte","value":50}]"'
        ),
    ),
    sort_by: str | None = Query(None, description="排序指标名"),
    sort_desc: bool = Query(False, description="是否降序"),
    limit: int = Query(100, ge=1, le=500, description="返回上限"),
    offset: int = Query(0, ge=0, description="偏移量"),
    db: AsyncSession = Depends(get_db),
) -> StrategyResultListResponse:
    """查询运行结果（分页+筛选+排序，需 published）。

    变更：
    - 移除 matched_only 参数（matched 不再持久化）
    - 校验 run.status == 'published'（非 published 返回 403）
    - metric_filters 支持 operator: gt/gte/lt/lte/eq/between
    - metric_key 白名单校验（从 manifest outputs.filterable 读取）

    Args:
        run_id: 运行 ID
        metric_filters: 指标筛选条件 JSON
        sort_by: 排序指标名
        sort_desc: 是否降序
        limit: 返回上限
        offset: 偏移量

    Returns:
        结果列表响应
    """
    # 查询运行记录
    run_result = await db.execute(
        select(StrategyRun).where(StrategyRun.id == run_id)
    )
    run = run_result.scalar_one_or_none()
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"运行不存在: run_id={run_id}",
        )

    if run.trade_date is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"运行无 trade_date: run_id={run_id}",
        )

    # 校验 published 状态
    if run.status != "published":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"运行未发布（当前 {run.status}），仅 published 状态可查询结果",
        )

    # 加载策略版本（用于 metric_key 白名单校验）
    version_result = await db.execute(
        select(StrategyVersion).where(StrategyVersion.id == run.strategy_version_id)
    )
    version = version_result.scalar_one_or_none()
    if version is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"策略版本不存在: strategy_version_id={run.strategy_version_id}",
        )

    # 解析 metric_filters JSON
    filters: list[dict] | None = None
    if metric_filters:
        try:
            filters = json.loads(metric_filters)
        except json.JSONDecodeError as e:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"metric_filters JSON 解析失败: {e}",
            ) from e

    # 校验 metric_filters
    if filters:
        _validate_metric_filters(filters, version)

    # 查询结果（SQL 端过滤，绑定 published run）
    metric_filter_list = dict_filters_to_metric_filters(filters)
    sort_spec = SortSpec(field=sort_by, desc=sort_desc) if sort_by else None
    page = await strategy_result_repository.query_results(
        db,
        run_id=run.id,
        strategy_version_id=run.strategy_version_id,
        trade_date=run.trade_date,
        filters=metric_filter_list,
        sort=sort_spec,
        limit=limit,
        offset=offset,
    )

    result_items = [StrategyResultResponse.model_validate(r) for r in page.items]
    page_num = offset // limit + 1 if limit > 0 else 1
    return StrategyResultListResponse(
        items=result_items,
        total=page.total,
        page=page_num,
        page_size=limit,
    )


@router.get(
    "/strategy-runs/{run_id}/results/{result_id}",
    response_model=StrategyResultResponse,
)
async def get_run_result_detail(
    run_id: uuid.UUID,
    result_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> StrategyResultResponse:
    """获取单个结果详情。

    Args:
        run_id: 运行 ID（用于验证归属）
        result_id: 结果 ID

    Returns:
        结果详情响应
    """
    result = await strategy_result_repository.get_result(db, result_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"结果不存在: result_id={result_id}",
        )

    # 验证归属
    if result.run_id != run_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"结果不属于该运行: run_id={run_id}, result_id={result_id}",
        )

    return StrategyResultResponse.model_validate(result)


if __name__ == "__main__":
    # 自测入口：验证路由注册
    paths = [r.path for r in router.routes]
    print(f"router.routes={paths}")
    assert any("/admin/strategies" in p and "/run" in p for p in paths)
    assert any("/publish" in p for p in paths)
    assert any("/runs" in p for p in paths)
    assert any("/published-runs" in p for p in paths)
    assert any("/results" in p for p in paths)
    print("OK")
