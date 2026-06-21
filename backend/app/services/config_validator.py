"""配置值校验服务 - 根据 ConfigDefinition 的 value_type 与 validation 规则校验配置值。

校验规则（对齐 config_definition.schema.json 与 06_CONFIGURATION_CENTER.md）：
- string: 必须为字符串，可选 validation.min_length/max_length/pattern
- integer: 必须为整数，可选 validation.min/max
- number: 必须为数值（int/float），可选 validation.min/max
- boolean: 必须为布尔
- enum: 必须在 validation.options 列表中
- duration: 必须为字符串，格式如 "30s"/"5m"/"1h"（Go duration 风格）
- time: 必须为字符串，HH:MM 格式
- json: 必须为 dict/list（JSON 结构）
- secret: 必须为非空字符串（明文，服务端加密存储）
- url: 必须为字符串，以 http:// 或 https:// 开头

校验失败抛出 ConfigValidationError，含字段与原因，不吞没异常。
"""

from __future__ import annotations

import re
from typing import Any


class ConfigValidationError(ValueError):
    """配置校验失败异常，含字段名与原因。"""


# duration 格式正则：如 30s、5m、1h、2h30m、500ms
_DURATION_PATTERN = re.compile(
    r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?(?:(\d+)ms)?$",
    re.IGNORECASE,
)

# time 格式正则：HH:MM（24 小时制）
_TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def validate_config_value(
    value: Any,
    value_type: str,
    validation: dict[str, Any] | None = None,
) -> None:
    """校验配置值是否符合 value_type 与 validation 规则。

    Args:
        value: 待校验的配置值
        value_type: 值类型（string/integer/number/boolean/enum/duration/time/json/secret/url）
        validation: 校验规则字典（如 {"min": 0, "max": 100, "options": [...], "pattern": "..."}

    Raises:
        ConfigValidationError: 校验失败时抛出，含原因描述（不吞没）
    """
    validation = validation or {}

    if value_type == "string":
        _validate_string(value, validation)
    elif value_type == "integer":
        _validate_integer(value, validation)
    elif value_type == "number":
        _validate_number(value, validation)
    elif value_type == "boolean":
        _validate_boolean(value)
    elif value_type == "enum":
        _validate_enum(value, validation)
    elif value_type == "duration":
        _validate_duration(value)
    elif value_type == "time":
        _validate_time(value)
    elif value_type == "json":
        _validate_json(value)
    elif value_type == "secret":
        _validate_secret(value)
    elif value_type == "url":
        _validate_url(value, validation)
    else:
        raise ConfigValidationError(
            f"未知的 value_type={value_type!r}，无法校验"
        )


def _validate_string(value: Any, validation: dict[str, Any]) -> None:
    """校验字符串类型。"""
    if not isinstance(value, str):
        raise ConfigValidationError(
            f"string 类型要求值为字符串，实际类型={type(value).__name__}"
        )
    min_len = validation.get("min_length")
    if min_len is not None and len(value) < min_len:
        raise ConfigValidationError(
            f"字符串长度 {len(value)} 小于最小长度 {min_len}"
        )
    max_len = validation.get("max_length")
    if max_len is not None and len(value) > max_len:
        raise ConfigValidationError(
            f"字符串长度 {len(value)} 超过最大长度 {max_len}"
        )
    pattern = validation.get("pattern")
    if pattern is not None and not re.match(pattern, value):
        raise ConfigValidationError(
            f"字符串 {value!r} 不匹配 pattern={pattern!r}"
        )


def _validate_integer(value: Any, validation: dict[str, Any]) -> None:
    """校验整数类型（bool 是 int 的子类，需排除）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigValidationError(
            f"integer 类型要求值为整数，实际类型={type(value).__name__}"
        )
    _validate_numeric_range(value, validation, "integer")


def _validate_number(value: Any, validation: dict[str, Any]) -> None:
    """校验数值类型（int 或 float，排除 bool）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigValidationError(
            f"number 类型要求值为数值，实际类型={type(value).__name__}"
        )
    _validate_numeric_range(value, validation, "number")


def _validate_numeric_range(
    value: int | float, validation: dict[str, Any], type_name: str
) -> None:
    """校验数值范围（min/max）。"""
    min_val = validation.get("min")
    if min_val is not None and value < min_val:
        raise ConfigValidationError(
            f"{type_name} 值 {value} 小于最小值 {min_val}"
        )
    max_val = validation.get("max")
    if max_val is not None and value > max_val:
        raise ConfigValidationError(
            f"{type_name} 值 {value} 超过最大值 {max_val}"
        )


def _validate_boolean(value: Any) -> None:
    """校验布尔类型（严格 bool，排除 int）。"""
    if not isinstance(value, bool):
        raise ConfigValidationError(
            f"boolean 类型要求值为布尔，实际类型={type(value).__name__}"
        )


def _validate_enum(value: Any, validation: dict[str, Any]) -> None:
    """校验枚举类型（值必须在 validation.options 中）。"""
    options = validation.get("options")
    if not options or not isinstance(options, list):
        raise ConfigValidationError(
            "enum 类型要求 validation.options 为非空列表"
        )
    if value not in options:
        raise ConfigValidationError(
            f"enum 值 {value!r} 不在允许选项 {options!r} 中"
        )


def _validate_duration(value: Any) -> None:
    """校验 duration 类型（如 30s/5m/1h/2h30m）。"""
    if not isinstance(value, str):
        raise ConfigValidationError(
            f"duration 类型要求值为字符串，实际类型={type(value).__name__}"
        )
    if not _DURATION_PATTERN.match(value):
        raise ConfigValidationError(
            f"duration 值 {value!r} 格式非法，应为如 '30s'/'5m'/'1h'/'2h30m'"
        )


def _validate_time(value: Any) -> None:
    """校验 time 类型（HH:MM 24 小时制）。"""
    if not isinstance(value, str):
        raise ConfigValidationError(
            f"time 类型要求值为字符串，实际类型={type(value).__name__}"
        )
    if not _TIME_PATTERN.match(value):
        raise ConfigValidationError(
            f"time 值 {value!r} 格式非法，应为 HH:MM（24 小时制）"
        )


def _validate_json(value: Any) -> None:
    """校验 json 类型（必须为 dict 或 list）。"""
    if not isinstance(value, (dict, list)):
        raise ConfigValidationError(
            f"json 类型要求值为 dict 或 list，实际类型={type(value).__name__}"
        )


def _validate_secret(value: Any) -> None:
    """校验 secret 类型（必须为非空字符串明文）。"""
    if not isinstance(value, str):
        raise ConfigValidationError(
            f"secret 类型要求值为字符串明文，实际类型={type(value).__name__}"
        )
    if not value:
        raise ConfigValidationError("secret 值不能为空字符串")


def _validate_url(value: Any, validation: dict[str, Any]) -> None:
    """校验 url 类型（必须以 http:// 或 https:// 开头，可选域名白名单）。"""
    if not isinstance(value, str):
        raise ConfigValidationError(
            f"url 类型要求值为字符串，实际类型={type(value).__name__}"
        )
    if not (value.startswith("http://") or value.startswith("https://")):
        raise ConfigValidationError(
            f"url 值 {value!r} 必须以 http:// 或 https:// 开头"
        )
    # 可选域名白名单校验
    allowed_domains = validation.get("allowed_domains")
    if allowed_domains and isinstance(allowed_domains, list):
        matched = any(domain in value for domain in allowed_domains)
        if not matched:
            raise ConfigValidationError(
                f"url 值 {value!r} 的域名不在白名单 {allowed_domains!r} 中"
            )


if __name__ == "__main__":
    # 自测入口：验证各类配置值校验（无副作用）
    # 1. string 校验
    validate_config_value("hello", "string", {"min_length": 1, "max_length": 10})
    print("string OK")
    try:
        validate_config_value("", "string", {"min_length": 1})
        raise AssertionError("应抛出异常")
    except ConfigValidationError as e:
        print(f"string min_length 拦截: {e}")

    # 2. integer 校验
    validate_config_value(42, "integer", {"min": 0, "max": 100})
    print("integer OK")
    try:
        validate_config_value(150, "integer", {"max": 100})
        raise AssertionError("应抛出异常")
    except ConfigValidationError as e:
        print(f"integer max 拦截: {e}")

    # 3. boolean 校验
    validate_config_value(True, "boolean")
    print("boolean OK")
    try:
        validate_config_value(1, "boolean")
        raise AssertionError("应抛出异常")
    except ConfigValidationError as e:
        print(f"boolean 类型拦截: {e}")

    # 4. enum 校验
    validate_config_value("a", "enum", {"options": ["a", "b", "c"]})
    print("enum OK")
    try:
        validate_config_value("d", "enum", {"options": ["a", "b", "c"]})
        raise AssertionError("应抛出异常")
    except ConfigValidationError as e:
        print(f"enum 选项拦截: {e}")

    # 5. duration 校验
    validate_config_value("2h30m", "duration")
    print("duration OK")
    try:
        validate_config_value("abc", "duration")
        raise AssertionError("应抛出异常")
    except ConfigValidationError as e:
        print(f"duration 格式拦截: {e}")

    # 6. time 校验
    validate_config_value("09:30", "time")
    print("time OK")

    # 7. json 校验
    validate_config_value({"key": "value"}, "json")
    print("json OK")

    # 8. secret 校验
    validate_config_value("my-secret-key", "secret")
    print("secret OK")
    try:
        validate_config_value("", "secret")
        raise AssertionError("应抛出异常")
    except ConfigValidationError as e:
        print(f"secret 空值拦截: {e}")

    # 9. url 校验
    validate_config_value("https://open.feishu.cn/api", "url")
    print("url OK")
    try:
        validate_config_value("ftp://bad.example.com", "url")
        raise AssertionError("应抛出异常")
    except ConfigValidationError as e:
        print(f"url 协议拦截: {e}")

    print("All validation tests passed.")
