"""策略种子注册 - 从 examples YAML 加载并注册示例策略。

示例策略：
- DSA selector（dsa_selector.yaml）: 方向稳定性选股
- Watchlist monitor（watchlist_monitor.yaml）: 自选股监控（布林带+成交量节点）

注册流程：
1. 加载 YAML 文件为 manifest 字典
2. 校验 manifest 符合 schema
3. 调用 strategy_service.create_strategy 创建草稿版本
4. 可选：调用 release_strategy_version 发布版本

幂等：重复运行不会报错（archived 版本除外，需升级版本号）。
清理：启动时自动将非种子策略标记为 test 环境、不可见、不调度，并归档其所有版本。
"""

from __future__ import annotations

import importlib.resources
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.strategy_keys import DSA_SELECTOR, WATCHLIST_MONITOR
from app.services.manifest_validator import validate_manifest
from app.services.strategy_service import (
    create_strategy,
    release_strategy_version,
)

# 示例文件目录（包内资源，Docker 兼容）
_EXAMPLES_DIR = Path(str(importlib.resources.files("app.strategy_assets.manifests")))

# 内置示例策略文件名
SEED_STRATEGIES: list[str] = [
    "dsa_selector.yaml",
    "watchlist_monitor.yaml",
]


def load_manifest_from_yaml(file_path: Path) -> dict[str, Any]:
    """从 YAML 文件加载 manifest。

    Args:
        file_path: YAML 文件路径

    Returns:
        manifest 字典

    Raises:
        FileNotFoundError: 文件不存在
        yaml.YAMLError: YAML 解析失败
        ManifestValidationError: manifest 校验失败
    """
    try:
        with file_path.open("r", encoding="utf-8") as f:
            manifest = yaml.safe_load(f)
    except FileNotFoundError as e:
        raise FileNotFoundError(f"策略示例文件不存在: {file_path}") from e
    except yaml.YAMLError as e:
        raise yaml.YAMLError(f"YAML 解析失败: {file_path}: {e}") from e

    # 校验 manifest
    validate_manifest(manifest)
    return manifest


async def seed_strategies(
    db: AsyncSession,
    release: bool = True,
) -> list[tuple[str, str, str]]:
    """注册所有内置示例策略。

    幂等：已存在的策略/版本会被跳过，不会重复创建。
    - 若策略定义不存在，创建定义 + 草稿版本（可选发布）
    - 若策略定义已存在但版本不存在，创建新版本（可选发布）
    - 若版本已存在且为 released，跳过（不报错）

    Args:
        db: 异步会话
        release: 是否同时发布版本（draft -> released）

    Returns:
        list of (strategy_key, version, status) 已注册的策略信息
    """
    from app.models.strategy import StrategyDefinition, StrategyVersion

    results: list[tuple[str, str, str]] = []
    for filename in SEED_STRATEGIES:
        file_path = _EXAMPLES_DIR / filename
        manifest = load_manifest_from_yaml(file_path)
        strategy_key = manifest["strategy_id"]
        version_str = manifest["version"]

        # 幂等检查：查找是否已存在相同 strategy_key + version 的已发布版本
        existing_def = await db.execute(
            select(StrategyDefinition).where(
                StrategyDefinition.strategy_key == strategy_key
            )
        )
        definition = existing_def.scalar_one_or_none()

        if definition is not None:
            existing_ver = await db.execute(
                select(StrategyVersion).where(
                    StrategyVersion.strategy_definition_id == definition.id,
                    StrategyVersion.version == version_str,
                )
            )
            version_row = existing_ver.scalar_one_or_none()
            if version_row is not None:
                if version_row.status == "archived":
                    raise ValueError(
                        f"策略 {strategy_key} v{version_str} 已归档，"
                        f"请升级版本号后再创建新版本"
                    )
                if release and version_row.status == "draft":
                    # [策略种子] - 发布 draft 版本
                    from datetime import UTC
                    from datetime import datetime as _dt

                    version_row.status = "released"
                    version_row.released_at = _dt.now(UTC)
                    db.add(version_row)
                    results.append((strategy_key, version_str, "released"))
                    print(f"  发布已存在 draft 策略: {strategy_key} v{version_str} -> released")
                    continue
                # 已是 released 或其他状态，跳过
                results.append((strategy_key, version_str, version_row.status))
                print(f"  跳过已存在策略: {strategy_key} v{version_str} -> {version_row.status}")
                continue

        # 创建策略定义 + 版本
        _, version_row = await create_strategy(db, manifest)

        if release and version_row.status == "draft":
            version_row = await release_strategy_version(db, version_row.id)

        results.append((strategy_key, version_str, version_row.status))
        print(
            f"  注册策略: {strategy_key} v{version_str} -> {version_row.status}"
        )

    await db.commit()

    # [策略种子] - 启动断言：两个必需策略都必须有 released 版本
    # [策略种子] - 描述: 版本升级后同一策略可能存在多个 released 版本（旧+新），
    # 必须使用 scalars().first() 而非 scalar_one_or_none()，否则 MultipleResultsFound
    for required_key in [DSA_SELECTOR, WATCHLIST_MONITOR]:
        req_def = await db.execute(
            select(StrategyDefinition).where(
                StrategyDefinition.strategy_key == required_key
            )
        )
        req_definition = req_def.scalar_one_or_none()
        if req_definition is not None:
            req_released = await db.execute(
                select(StrategyVersion.id).where(
                    StrategyVersion.strategy_definition_id == req_definition.id,
                    StrategyVersion.status == "released",
                )
            )
            if req_released.scalars().first() is None:
                raise RuntimeError(
                    f"必需策略 {required_key} 没有 released 版本，"
                    "相关功能不可用。请检查 seed_strategies 执行结果。"
                )

    # [策略种子] - 清理非种子策略：标记为测试环境、不可见、不调度，版本归档
    seed_keys = {Path(s).stem for s in SEED_STRATEGIES}  # {"dsa_selector", "watchlist_monitor"}

    all_defs = await db.execute(select(StrategyDefinition))
    for sd in all_defs.scalars():
        if sd.strategy_key not in seed_keys:
            changed = False
            if sd.environment != "test":
                sd.environment = "test"
                changed = True
            if sd.is_user_visible:
                sd.is_user_visible = False
                changed = True
            if sd.is_scheduled:
                sd.is_scheduled = False
                changed = True
            # 归档该策略下所有非 archived 版本
            old_vers = await db.execute(
                select(StrategyVersion).where(
                    StrategyVersion.strategy_definition_id == sd.id,
                    StrategyVersion.status != "archived",
                )
            )
            for ver in old_vers.scalars():
                ver.status = "archived"
                changed = True
                print(f"  归档非种子版本: {sd.strategy_key} v{ver.version}")
            if changed:
                print(f"  清理非种子策略: {sd.strategy_key} → test/archived")

    await db.commit()
    return results


if __name__ == "__main__":
    # 自测入口：验证 YAML 加载与 manifest 校验（不连接数据库）
    print("加载并校验示例策略 manifest:")
    for filename in SEED_STRATEGIES:
        file_path = _EXAMPLES_DIR / filename
        manifest = load_manifest_from_yaml(file_path)
        print(
            f"  - {filename}: strategy_id={manifest['strategy_id']}, "
            f"kind={manifest['kind']}, version={manifest['version']}"
        )
    print("OK")
