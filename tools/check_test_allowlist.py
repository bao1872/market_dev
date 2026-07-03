#!/usr/bin/env python3
"""
校验 backend/tests/allowlist.json 与测试代码中的 @pytest.mark.xfail 是否一致。

用法：
    python tools/check_test_allowlist.py

校验项：
1. 代码中每个 @pytest.mark.xfail 都必须在 allowlist.json 中登记。
2. allowlist.json 中每条记录都必须对应代码中真实存在的 xfail 测试。
3. issue 必须是真实 GitHub issue URL 或纯数字编号，禁止占位符。
4. owner 必须为 bao1872。
5. expires 必须是未来的 ISO 日期。

未通过时以非零状态码退出。
"""
from __future__ import annotations

import ast
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
TESTS_DIR = BACKEND_DIR / "tests"
ALLOWLIST_PATH = TESTS_DIR / "allowlist.json"

GITHUB_ISSUE_URL_RE = re.compile(
    r"^https://github\.com/[\w.-]+/[\w.-]+/issues/\d+$",
    re.IGNORECASE,
)


def _is_xfail_decorator(decorator: ast.expr) -> bool:
    """判断 AST 节点是否为 pytest.mark.xfail 装饰器。"""
    if isinstance(decorator, ast.Call):
        func = decorator.func
    else:
        func = decorator

    if not isinstance(func, ast.Attribute):
        return False
    if func.attr != "xfail":
        return False

    # 支持 pytest.mark.xfail / mark.xfail / pytest.mark.xfail(...)
    value = func.value
    if isinstance(value, ast.Attribute) and value.attr == "mark":
        return True
    if isinstance(value, ast.Name) and value.id == "mark":
        return True
    return False


def _iter_test_functions(
    tree: ast.AST,
    parent_qualname: str = "",
) -> Any:
    """递归产出 (qualname, ast.FunctionDef/AsyncFunctionDef)。"""
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qualname = (
                f"{parent_qualname}::{node.name}" if parent_qualname else node.name
            )
            yield (qualname, node)
        elif isinstance(node, ast.ClassDef):
            class_qualname = (
                f"{parent_qualname}::{node.name}" if parent_qualname else node.name
            )
            yield from _iter_test_functions(node, class_qualname)


def collect_xfail_nodeids() -> set[str]:
    """AST 扫描 tests 目录，收集所有带 @pytest.mark.xfail 的测试 nodeid。"""
    nodeids: set[str] = set()

    for py_file in sorted(TESTS_DIR.rglob("test_*.py")):
        rel_path = py_file.relative_to(BACKEND_DIR).as_posix()
        source = py_file.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(py_file))
        except SyntaxError as exc:
            print(f"[ERROR] 语法错误无法解析 {rel_path}: {exc}")
            sys.exit(1)

        for qualname, func in _iter_test_functions(tree):
            if any(_is_xfail_decorator(dec) for dec in func.decorator_list):
                nodeids.add(f"{rel_path}::{qualname}")

    return nodeids


def load_allowlist() -> list[dict[str, Any]]:
    """读取并返回 allowlist.json 的 items。"""
    if not ALLOWLIST_PATH.exists():
        print(f"[ERROR] 未找到 allowlist 文件: {ALLOWLIST_PATH}")
        sys.exit(1)

    try:
        data = json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"[ERROR] allowlist.json JSON 解析失败: {exc}")
        sys.exit(1)

    if not isinstance(data, dict) or "items" not in data:
        print("[ERROR] allowlist.json 顶层必须是包含 'items' 的对象")
        sys.exit(1)

    items = data["items"]
    if not isinstance(items, list):
        print("[ERROR] allowlist.json 'items' 必须是数组")
        sys.exit(1)

    return items


def validate_issue(value: Any, entry_id: str) -> list[str]:
    """校验 issue 字段，返回错误信息列表。"""
    errors: list[str] = []
    if not isinstance(value, str):
        errors.append(
            f"[{entry_id}] issue 必须是字符串，当前类型={type(value).__name__}"
        )
        return errors

    stripped = value.strip()
    if not stripped:
        errors.append(f"[{entry_id}] issue 不能为空")
        return errors

    is_url = bool(GITHUB_ISSUE_URL_RE.match(stripped))
    is_number = stripped.isdigit()

    if not is_url and not is_number:
        errors.append(
            f"[{entry_id}] issue 必须是 GitHub issue URL 或纯数字编号，"
            f"禁止占位符: {stripped!r}"
        )

    return errors


def validate_owner(value: Any, entry_id: str) -> list[str]:
    """校验 owner 字段。"""
    errors: list[str] = []
    if value != "bao1872":
        errors.append(
            f"[{entry_id}] owner 必须为 'bao1872'，当前={value!r}"
        )
    return errors


def validate_expires(value: Any, entry_id: str) -> list[str]:
    """校验 expires 为未来的 ISO 日期。"""
    errors: list[str] = []
    if not isinstance(value, str):
        errors.append(
            f"[{entry_id}] expires 必须是字符串，当前类型={type(value).__name__}"
        )
        return errors

    try:
        expires_dt = datetime.fromisoformat(value)
    except ValueError:
        errors.append(
            f"[{entry_id}] expires 不是有效的 ISO 日期: {value!r}"
        )
        return errors

    if expires_dt.tzinfo is None:
        expires_dt = expires_dt.replace(tzinfo=UTC)

    if expires_dt <= datetime.now(UTC):
        errors.append(
            f"[{entry_id}] expires 必须是未来日期: {value}"
        )

    return errors


def main() -> int:
    """执行校验并打印报告。"""
    print("=" * 60)
    print("pytest xfail allowlist 一致性校验")
    print("=" * 60)

    xfail_nodeids = collect_xfail_nodeids()
    print(f"\n[1/4] 扫描到带 @pytest.mark.xfail 的测试: {len(xfail_nodeids)} 个")
    for nodeid in sorted(xfail_nodeids):
        print(f"  - {nodeid}")

    allowlist_items = load_allowlist()
    print(f"\n[2/4] allowlist.json 登记记录: {len(allowlist_items)} 条")

    allowlist_nodeids: set[str] = set()
    validation_errors: list[str] = []

    for idx, item in enumerate(allowlist_items, start=1):
        entry_id = item.get("test") or f"#{idx}"
        print(f"  - {entry_id}")

        required_fields = {"test", "reason", "issue", "owner", "expires"}
        missing = required_fields - set(item.keys())
        if missing:
            validation_errors.append(
                f"[{entry_id}] 缺少必填字段: {sorted(missing)}"
            )
            continue

        test_nodeid = item["test"]
        if not isinstance(test_nodeid, str):
            validation_errors.append(
                f"[{entry_id}] test 字段必须是字符串"
            )
            continue

        allowlist_nodeids.add(test_nodeid)

        validation_errors.extend(validate_issue(item["issue"], entry_id))
        validation_errors.extend(validate_owner(item["owner"], entry_id))
        validation_errors.extend(validate_expires(item["expires"], entry_id))

    # 3) xfail 未登记
    unregistered_xfails = xfail_nodeids - allowlist_nodeids
    if unregistered_xfails:
        validation_errors.append("以下 xfail 测试未在 allowlist.json 中登记:")
        for nodeid in sorted(unregistered_xfails):
            validation_errors.append(f"  - {nodeid}")

    # 4) allowlist 记录不存在
    orphan_allowlist = allowlist_nodeids - xfail_nodeids
    if orphan_allowlist:
        validation_errors.append("allowlist.json 中存在无对应 xfail 测试的记录:")
        for nodeid in sorted(orphan_allowlist):
            validation_errors.append(f"  - {nodeid}")

    print("\n[3/4] 字段校验")
    print(f"  校验问题: {len(validation_errors)} 项")

    print("\n[4/4] 一致性校验")
    print(f"  未登记 xfail: {len(unregistered_xfails)} 个")
    print(f"  无效 allowlist 记录: {len(orphan_allowlist)} 个")

    print("\n" + "=" * 60)
    if validation_errors:
        print("结果: FAIL")
        print("-" * 60)
        for err in validation_errors:
            print(f"  ❌ {err}")
        print("=" * 60)
        return 1

    print("结果: PASS")
    print("所有 @pytest.mark.xfail 均已登记，且 allowlist 字段合法。")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
