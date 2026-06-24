"""策略服务 - 策略定义与版本的创建、发布、归档流程。

核心流程：
1. create_strategy(manifest): 创建策略定义 + 草稿版本（status=draft）
2. release_strategy_version(version_id): 发布版本（draft -> released，不可修改）
3. archive_strategy_version(version_id): 归档旧版本（released -> archived）

幂等保证：
- 相同 manifest+schema+entrypoint 的 build_hash 相同
- create_strategy 时若已存在相同 strategy_key，则只创建新草稿版本
- release 时若已存在相同 build_hash 的 released 版本，则跳过（幂等）

版本不可变性：
- released 状态的版本不可修改 manifest/build_hash/status
- 仅允许 draft -> released、released -> archived 的状态转换
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.strategy import StrategyDefinition, StrategyVersion
from app.services.build_hash import compute_build_hash
from app.services.manifest_validator import validate_manifest


class StrategyServiceError(ValueError):
    """策略服务业务错误基类。"""


class StrategyNotFoundError(StrategyServiceError):
    """策略或版本不存在。"""


class VersionImmutableError(StrategyServiceError):
    """版本不可变错误：released 版本不可修改。"""


class InvalidStatusTransitionError(StrategyServiceError):
    """非法状态转换。"""


async def create_strategy(
    db: AsyncSession,
    manifest: dict[str, Any],
    schema: dict[str, Any] | None = None,
) -> tuple[StrategyDefinition, StrategyVersion]:
    """创建策略定义 + 草稿版本。

    幂等行为：
    - 若 strategy_key 已存在，则在该定义下创建新草稿版本（version 来自 manifest）
    - 若该定义下已存在相同 version 的草稿，则返回已有草稿

    Args:
        db: 异步会话
        manifest: 策略 Manifest 字典（必须符合 strategy_manifest.schema.json）
        schema: 策略参数/输出 schema（可选，参与 build_hash 计算）

    Returns:
        (StrategyDefinition, StrategyVersion) 元组

    Raises:
        ManifestValidationError: Manifest 校验失败
        StrategyServiceError: 其他业务错误
    """
    # 1. 校验 Manifest
    validate_manifest(manifest)

    strategy_key = manifest["strategy_id"]
    kind = manifest["kind"]
    display_name = manifest.get("display_name", strategy_key)
    version_str = manifest["version"]
    entrypoint = manifest.get("entrypoint", "")
    build_hash = compute_build_hash(manifest, schema, entrypoint)

    # 2. 查找或创建 StrategyDefinition（按 strategy_key 幂等）
    stmt = select(StrategyDefinition).where(
        StrategyDefinition.strategy_key == strategy_key
    )
    result = await db.execute(stmt)
    definition = result.scalar_one_or_none()

    if definition is None:
        definition = StrategyDefinition(
            strategy_key=strategy_key,
            kind=kind,
            display_name=display_name,
        )
        db.add(definition)
        try:
            await db.flush()
        except IntegrityError as e:
            await db.rollback()
            raise StrategyServiceError(
                f"创建策略定义失败（strategy_key={strategy_key} 可能已存在）: {e}"
            ) from e
    else:
        # 已存在定义时校验 kind 一致
        if definition.kind != kind:
            raise StrategyServiceError(
                f"策略 kind 不一致：现有={definition.kind}, manifest={kind}"
            )

    # 3. 查找是否已存在相同 version 的草稿（幂等）
    stmt_ver = select(StrategyVersion).where(
        StrategyVersion.strategy_definition_id == definition.id,
        StrategyVersion.version == version_str,
    )
    result_ver = await db.execute(stmt_ver)
    existing_version = result_ver.scalar_one_or_none()

    if existing_version is not None:
        # 已存在相同 version：草稿则返回，非草稿则报错
        if existing_version.status == "draft":
            return definition, existing_version
        raise StrategyServiceError(
            f"版本 {version_str} 已存在且状态为 {existing_version.status}，"
            f"无法重复创建（请升级 version 号）"
        )

    # 4. 创建草稿版本
    version = StrategyVersion(
        strategy_definition_id=definition.id,
        version=version_str,
        status="draft",
        manifest=manifest,
        build_hash=build_hash,
    )
    db.add(version)
    try:
        await db.flush()
    except IntegrityError as e:
        await db.rollback()
        raise StrategyServiceError(
            f"创建策略版本失败（version={version_str}）: {e}"
        ) from e

    return definition, version


async def release_strategy_version(
    db: AsyncSession,
    version_id: UUID,
) -> StrategyVersion:
    """发布策略版本（draft -> released，不可修改）。

    幂等行为：
    - 若版本已是 released 状态，直接返回（幂等）
    - 若已存在相同 build_hash 的 released 版本，则将当前版本归档并返回已发布版本

    Args:
        db: 异步会话
        version_id: 策略版本 ID

    Returns:
        发布后的 StrategyVersion

    Raises:
        StrategyNotFoundError: 版本不存在
        InvalidStatusTransitionError: archived 版本不可发布
    """
    stmt = select(StrategyVersion).where(StrategyVersion.id == version_id)
    result = await db.execute(stmt)
    version = result.scalar_one_or_none()
    if version is None:
        raise StrategyNotFoundError(f"策略版本不存在: version_id={version_id}")

    # 幂等：已 released 直接返回
    if version.status == "released":
        return version

    # archived 不可重新发布
    if version.status == "archived":
        raise InvalidStatusTransitionError(
            f"archived 版本不可重新发布: version_id={version_id}"
        )

    # 仅 draft 可发布
    if version.status != "draft":
        raise InvalidStatusTransitionError(
            f"仅 draft 版本可发布，当前状态={version.status}"
        )

    # 检查是否已存在相同 build_hash 的 released 版本（幂等发布）
    stmt_existing = (
        select(StrategyVersion)
        .where(
            StrategyVersion.strategy_definition_id == version.strategy_definition_id,
            StrategyVersion.build_hash == version.build_hash,
            StrategyVersion.status == "released",
            StrategyVersion.id != version.id,
        )
        .limit(1)
    )
    result_existing = await db.execute(stmt_existing)
    existing_released = result_existing.scalar_one_or_none()
    if existing_released is not None:
        # 已存在相同内容的 released 版本：将当前草稿归档，返回已发布版本
        version.status = "archived"
        await db.flush()
        return existing_released

    # 发布当前版本
    version.status = "released"
    version.released_at = datetime.now(UTC)
    await db.flush()
    return version


async def archive_strategy_version(
    db: AsyncSession,
    version_id: UUID,
) -> StrategyVersion:
    """归档策略版本（released -> archived）。

    幂等：已 archived 直接返回。

    Args:
        db: 异步会话
        version_id: 策略版本 ID

    Returns:
        归档后的 StrategyVersion

    Raises:
        StrategyNotFoundError: 版本不存在
        InvalidStatusTransitionError: draft 版本不可直接归档（应删除或发布后归档）
    """
    stmt = select(StrategyVersion).where(StrategyVersion.id == version_id)
    result = await db.execute(stmt)
    version = result.scalar_one_or_none()
    if version is None:
        raise StrategyNotFoundError(f"策略版本不存在: version_id={version_id}")

    if version.status == "archived":
        return version

    if version.status == "draft":
        raise InvalidStatusTransitionError(
            "draft 版本不可直接归档，请先发布或删除"
        )

    version.status = "archived"
    await db.flush()
    return version


async def list_strategies(
    db: AsyncSession,
    kind: str | None = None,
    user_visible_only: bool = False,
    admin_mode: bool = False,
) -> list[StrategyDefinition]:
    """列出策略定义。

    Args:
        db: 异步会话
        kind: 可选，按 kind 过滤（selector/monitor）
        user_visible_only: 仅返回 production 环境 + 对用户可见 + 有 released 版本的策略
        admin_mode: True 时跳过用户可见性过滤（仅用于 admin 端点）

    Returns:
        策略定义列表
    """
    stmt = select(StrategyDefinition).order_by(StrategyDefinition.strategy_key)
    if kind is not None:
        stmt = stmt.where(StrategyDefinition.kind == kind)
    # 非 admin 模式下始终过滤：仅返回 production + is_user_visible + 有 released 版本
    if not admin_mode:
        stmt = stmt.where(
            StrategyDefinition.environment == "production",
            StrategyDefinition.is_user_visible == True,  # noqa: E712
        )
    elif user_visible_only:
        stmt = stmt.where(
            StrategyDefinition.environment == "production",
            StrategyDefinition.is_user_visible == True,  # noqa: E712
        )
    result = await db.execute(stmt)
    definitions = list(result.scalars().all())

    # 非 admin 模式或 user_visible_only 时过滤掉无 released 版本的策略
    if not admin_mode or user_visible_only:
        filtered = []
        for d in definitions:
            ver_stmt = (
                select(StrategyVersion.id)
                .where(
                    StrategyVersion.strategy_definition_id == d.id,
                    StrategyVersion.status == "released",
                )
                .limit(1)
            )
            ver_result = await db.execute(ver_stmt)
            if ver_result.scalar_one_or_none() is not None:
                filtered.append(d)
        return filtered

    return definitions


async def get_strategy_by_key(
    db: AsyncSession,
    strategy_key: str,
) -> StrategyDefinition:
    """按 strategy_key 获取策略定义。

    Raises:
        StrategyNotFoundError: 策略不存在
    """
    stmt = select(StrategyDefinition).where(
        StrategyDefinition.strategy_key == strategy_key
    )
    result = await db.execute(stmt)
    definition = result.scalar_one_or_none()
    if definition is None:
        raise StrategyNotFoundError(f"策略不存在: strategy_key={strategy_key}")
    return definition


async def list_versions(
    db: AsyncSession,
    strategy_key: str,
) -> list[StrategyVersion]:
    """列出策略的所有版本。

    Raises:
        StrategyNotFoundError: 策略不存在
    """
    definition = await get_strategy_by_key(db, strategy_key)
    stmt = (
        select(StrategyVersion)
        .where(StrategyVersion.strategy_definition_id == definition.id)
        .order_by(StrategyVersion.version)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_version_schema(
    db: AsyncSession,
    strategy_key: str,
    version: str,
) -> dict[str, Any]:
    """获取策略版本的 schema（从 manifest 中提取 parameters/outputs 定义）。

    Manifest 本身已包含 parameters 和 outputs 的 schema 信息，
    此处返回 manifest 的子集用于前端表单渲染与校验。

    Raises:
        StrategyNotFoundError: 策略或版本不存在
    """
    definition = await get_strategy_by_key(db, strategy_key)
    stmt = select(StrategyVersion).where(
        StrategyVersion.strategy_definition_id == definition.id,
        StrategyVersion.version == version,
    )
    result = await db.execute(stmt)
    version_row = result.scalar_one_or_none()
    if version_row is None:
        raise StrategyNotFoundError(
            f"策略版本不存在: strategy_key={strategy_key}, version={version}"
        )
    manifest = version_row.manifest
    return {
        "strategy_id": manifest.get("strategy_id"),
        "version": manifest.get("version"),
        "kind": manifest.get("kind"),
        "parameters": manifest.get("parameters", []),
        "outputs": manifest.get("outputs", []),
        "input": manifest.get("input", {}),
        "capabilities": manifest.get("capabilities", {}),
    }


if __name__ == "__main__":
    # 自测入口：验证服务函数可导入（不连接数据库）
    print(f"create_strategy={create_strategy}")
    print(f"release_strategy_version={release_strategy_version}")
    print(f"archive_strategy_version={archive_strategy_version}")
    print(f"list_strategies={list_strategies}")
    print(f"get_strategy_by_key={get_strategy_by_key}")
    print(f"list_versions={list_versions}")
    print(f"get_version_schema={get_version_schema}")
    print("OK")
