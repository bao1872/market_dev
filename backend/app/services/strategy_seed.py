"""策略种子注册 - 从 examples YAML 加载并注册示例策略。

示例策略：
- DSA selector（dsa_selector.yaml）: 方向稳定性选股
- Volume Node monitor（volume_node_monitor.yaml）: 成交量节点簇监控

注册流程：
1. 加载 YAML 文件为 manifest 字典
2. 校验 manifest 符合 schema
3. 调用 strategy_service.create_strategy 创建草稿版本
4. 可选：调用 release_strategy_version 发布版本

幂等：重复运行不会报错，已存在的策略/版本会被跳过。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.manifest_validator import validate_manifest
from app.services.strategy_service import (
    create_strategy,
    list_strategies,
    release_strategy_version,
)

# 示例文件目录（相对 backend/）
_EXAMPLES_DIR = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "doc"
    / "trading_platform_development_docs_v1.1"
    / "examples"
)

# 内置示例策略文件名
SEED_STRATEGIES: list[str] = [
    "dsa_selector.yaml",
    "volume_node_monitor.yaml",
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
                # 版本已存在，跳过创建
                results.append((strategy_key, version_str, version_row.status))
                print(
                    f"  跳过已存在策略: {strategy_key} v{version_str} -> {version_row.status}"
                )
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
