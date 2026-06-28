"""Manifest 校验器 - 校验策略 Manifest 符合 strategy_manifest.schema.json。

使用 jsonschema 库进行 Draft 2020-12 校验，校验失败抛出包含字段路径的详细错误。

Schema 来源：app/strategy_assets/schemas/strategy_manifest.schema.json
- 该文件被 git 跟踪，随 Docker 镜像分发（COPY app ./app）
- 不再依赖 doc/ 目录的 volume 挂载（doc/ 被 .gitignore）
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

# Schema 文件路径（app/strategy_assets/schemas/，与 manifest 同目录，git 跟踪）
_SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent  # app/services -> app
    / "strategy_assets"
    / "schemas"
    / "strategy_manifest.schema.json"
)


def _load_schema() -> dict[str, Any]:
    """加载 strategy_manifest.schema.json。

    Raises:
        FileNotFoundError: schema 文件不存在
        json.JSONDecodeError: schema 文件 JSON 解析失败
    """
    try:
        with _SCHEMA_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError as e:
        raise FileNotFoundError(
            f"策略 Manifest schema 文件不存在: {_SCHEMA_PATH}"
        ) from e
    except json.JSONDecodeError as e:
        raise json.JSONDecodeError(
            f"策略 Manifest schema JSON 解析失败: {e.msg} (line={e.lineno}, col={e.colno})",
            e.doc,
            e.pos,
        ) from e


# 模块级单例 Validator，避免重复加载 schema
_VALIDATOR: Draft202012Validator | None = None


def _get_validator() -> Draft202012Validator:
    """获取 schema Validator 单例。"""
    global _VALIDATOR
    if _VALIDATOR is None:
        schema = _load_schema()
        _VALIDATOR = Draft202012Validator(schema)
    return _VALIDATOR


class ManifestValidationError(ValueError):
    """Manifest 校验失败异常，包含详细字段路径信息。"""

    def __init__(self, errors: list[dict[str, Any]]) -> None:
        self.errors = errors
        # 拼接所有错误信息，含字段路径
        parts = []
        for err in errors:
            path = "/".join(str(p) for p in err["path"]) or "<root>"
            parts.append(f"[{path}] {err['message']}")
        super().__init__("Manifest 校验失败:\n  - " + "\n  - ".join(parts))


def validate_manifest(manifest_dict: dict[str, Any]) -> None:
    """校验 Manifest 符合 strategy_manifest.schema.json。

    Args:
        manifest_dict: 待校验的 Manifest 字典

    Raises:
        ManifestValidationError: 校验失败，包含所有错误及字段路径
        FileNotFoundError: schema 文件不存在
        json.JSONDecodeError: schema 文件解析失败
    """
    validator = _get_validator()
    errors = sorted(validator.iter_errors(manifest_dict), key=lambda e: list(e.path))
    if errors:
        error_list = [
            {"path": list(err.path), "message": err.message, "schema_path": list(err.absolute_schema_path)}
            for err in errors
        ]
        raise ManifestValidationError(error_list)


if __name__ == "__main__":
    # 自测入口：使用 dsa_selector.yaml 示例验证校验器
    import yaml

    example_path = (
        Path(__file__).resolve().parent.parent  # app/services -> app
        / "strategy_assets"
        / "manifests"
        / "dsa_selector.yaml"
    )
    with example_path.open("r", encoding="utf-8") as f:
        manifest = yaml.safe_load(f)
    print(f"加载示例 manifest: strategy_id={manifest.get('strategy_id')}")

    # 正例：应通过校验
    validate_manifest(manifest)
    print("正例校验通过: PASS")

    # 反例：缺少必填字段，应抛出 ManifestValidationError
    bad_manifest = {"strategy_id": "test"}
    try:
        validate_manifest(bad_manifest)
    except ManifestValidationError as e:
        print(f"反例校验失败（预期）: {len(e.errors)} 个错误")
        for err in e.errors:
            path = "/".join(str(p) for p in err["path"]) or "<root>"
            print(f"  - [{path}] {err['message']}")
        print("反例校验: PASS")
    print("OK")
