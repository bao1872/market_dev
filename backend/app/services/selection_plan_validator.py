"""选股组合方案校验器 - 校验方案符合 selection_plan.schema.json。

使用 jsonschema 库进行 Draft 2020-12 校验，校验失败抛出包含字段路径的详细错误。

Schema 来源：doc/trading_platform_development_docs_v1.1/schemas/selection_plan.schema.json

校验内容（由 schema 文件定义）：
- 必填字段：name, operator, members
- name: 字符串，1-80 字符
- description: 字符串，最长 500 字符
- operator: 枚举 ALL/ANY
- missing_member_policy: 枚举 FAIL_CLOSED/IGNORE_MEMBER（默认 FAIL_CLOSED）
- members: 数组，至少 1 个成员
  - 每个成员必填：strategy_key, version_policy, conditions
  - version_policy: 枚举 PINNED/STABLE_TRACK
  - conditions: 数组，每项必填 metric_key/operator/value
    - operator: 枚举 gt/gte/lt/lte/eq/between
- additionalProperties: false（禁止未知字段）

附加语义校验（schema 之外）：
- PINNED 版本策略必须提供 strategy_version
- between 操作必须提供 value2
- 成员 position 唯一（若提供）
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

# Schema 文件路径（相对工程根 backend/）
_SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent
    / "doc"
    / "trading_platform_development_docs_v1.1"
    / "schemas"
    / "selection_plan.schema.json"
)


def _load_schema() -> dict[str, Any]:
    """加载 selection_plan.schema.json。

    Raises:
        FileNotFoundError: schema 文件不存在
        json.JSONDecodeError: schema 文件 JSON 解析失败
    """
    try:
        with _SCHEMA_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError as e:
        raise FileNotFoundError(
            f"选股方案 schema 文件不存在: {_SCHEMA_PATH}"
        ) from e
    except json.JSONDecodeError as e:
        raise json.JSONDecodeError(
            f"选股方案 schema JSON 解析失败: {e.msg} (line={e.lineno}, col={e.colno})",
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


class SelectionPlanValidationError(ValueError):
    """选股方案校验失败异常，包含详细字段路径信息。"""

    def __init__(self, errors: list[dict[str, Any]]) -> None:
        self.errors = errors
        # 拼接所有错误信息，含字段路径
        parts = []
        for err in errors:
            path = "/".join(str(p) for p in err["path"]) or "<root>"
            parts.append(f"[{path}] {err['message']}")
        super().__init__("选股方案校验失败:\n  - " + "\n  - ".join(parts))


def _check_semantic(plan_dict: dict[str, Any]) -> list[dict[str, Any]]:
    """附加语义校验（schema 之外的业务规则）。

    校验规则：
    - PINNED 版本策略必须提供 strategy_version
    - between 操作必须提供 value2
    - 成员 position 唯一（若提供）

    Args:
        plan_dict: 待校验的方案字典

    Returns:
        错误列表（空列表表示无错误）
    """
    errors: list[dict[str, Any]] = []
    members = plan_dict.get("members", [])

    # 收集 position 检查唯一性
    positions: list[int] = []
    for i, member in enumerate(members):
        # PINNED 必须提供 strategy_version
        if member.get("version_policy") == "PINNED" and not member.get("strategy_version"):
            errors.append({
                "path": [f"members[{i}]", "strategy_version"],
                "message": "version_policy=PINNED 时必须提供 strategy_version",
            })

        # position 唯一性
        pos = member.get("position")
        if pos is not None:
            if pos in positions:
                errors.append({
                    "path": [f"members[{i}]", "position"],
                    "message": f"成员 position 重复: {pos}",
                })
            positions.append(pos)

        # between 操作必须提供 value2
        conditions = member.get("conditions", [])
        for j, cond in enumerate(conditions):
            if cond.get("operator") == "between" and "value2" not in cond:
                errors.append({
                    "path": [f"members[{i}]", f"conditions[{j}]", "value2"],
                    "message": "operator=between 时必须提供 value2",
                })

    return errors


def validate_plan(plan_dict: dict[str, Any]) -> None:
    """校验选股方案符合 selection_plan.schema.json + 附加语义规则。

    Args:
        plan_dict: 待校验的方案字典

    Raises:
        SelectionPlanValidationError: 校验失败，包含所有错误及字段路径
        FileNotFoundError: schema 文件不存在
        json.JSONDecodeError: schema 文件解析失败
    """
    # 1. jsonschema 结构校验（含枚举值校验）
    validator = _get_validator()
    schema_errors = sorted(validator.iter_errors(plan_dict), key=lambda e: list(e.path))
    all_errors: list[dict[str, Any]] = []
    if schema_errors:
        all_errors.extend([
            {
                "path": list(err.path),
                "message": err.message,
                "schema_path": list(err.absolute_schema_path),
            }
            for err in schema_errors
        ])

    # 2. 附加语义校验（仅在结构校验通过后执行，避免误报）
    if not all_errors:
        all_errors.extend(_check_semantic(plan_dict))

    if all_errors:
        raise SelectionPlanValidationError(all_errors)


if __name__ == "__main__":
    # 自测入口：使用 selection_plan_multi_strategy.json 示例验证校验器
    example_path = (
        Path(__file__).resolve().parent.parent.parent.parent
        / "doc"
        / "trading_platform_development_docs_v1.1"
        / "examples"
        / "selection_plan_multi_strategy.json"
    )
    with example_path.open("r", encoding="utf-8") as f:
        plan = json.load(f)
    print(f"加载示例方案: name={plan.get('name')}, operator={plan.get('operator')}")

    # 正例：应通过校验
    validate_plan(plan)
    print("正例校验通过: PASS")

    # 反例 1：缺少必填字段 operator
    bad_plan_1 = {"name": "test", "members": [{"strategy_key": "x", "version_policy": "PINNED", "strategy_version": "1.0.0", "conditions": []}]}
    try:
        validate_plan(bad_plan_1)
    except SelectionPlanValidationError as e:
        print(f"反例 1 校验失败（预期）: {len(e.errors)} 个错误")
        for err in e.errors:
            path = "/".join(str(p) for p in err["path"]) or "<root>"
            print(f"  - [{path}] {err['message']}")
        print("反例 1 校验: PASS")

    # 反例 2：非法枚举值 operator=XXX
    bad_plan_2 = {
        "name": "test",
        "operator": "XXX",
        "members": [{"strategy_key": "x", "version_policy": "PINNED", "strategy_version": "1.0.0", "conditions": []}],
    }
    try:
        validate_plan(bad_plan_2)
    except SelectionPlanValidationError as e:
        print(f"反例 2 校验失败（预期）: {len(e.errors)} 个错误")
        print("反例 2 校验: PASS")

    # 反例 3：PINNED 缺少 strategy_version（语义校验）
    bad_plan_3 = {
        "name": "test",
        "operator": "ALL",
        "members": [{"strategy_key": "x", "version_policy": "PINNED", "conditions": []}],
    }
    try:
        validate_plan(bad_plan_3)
    except SelectionPlanValidationError as e:
        print(f"反例 3 校验失败（预期）: {len(e.errors)} 个错误")
        for err in e.errors:
            path = "/".join(str(p) for p in err["path"]) or "<root>"
            print(f"  - [{path}] {err['message']}")
        print("反例 3 校验: PASS")

    print("OK")
