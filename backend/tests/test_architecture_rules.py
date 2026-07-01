"""项目架构规则门禁测试。

根据 AGENTS.md 和 docs/architecture/ADR-001-postgresql-only.md，使用 AST +
字符串扫描检查以下规则：
- tests 目录不得创建 SQLite engine
- tests 目录不得 import aiosqlite
- tests 目录不得包含手写 CREATE TABLE 字符串
- tests 目录不得自定义 db_session fixture
- tests 目录不得注册 SQLite JSONB compiler
- backend/pyproject.toml 不得声明 aiosqlite
- 代码中不得出现 Role(name="user")
- 代码中不得出现 ADMIN_PLAN_CODE 常量引用
- docs/安全规范.md 不得包含 "Last verified commit: 待填写"
"""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent.parent
_BACKEND_DIR = _PROJECT_ROOT / "backend"
_TESTS_DIR = _BACKEND_DIR / "tests"
_DOCS_DIR = _PROJECT_ROOT / "docs"


def _iter_python_files(directory: Path) -> list[Path]:
    """返回目录下所有 Python 文件路径（排除 .venv、__pycache__）。"""
    return sorted(
        p for p in directory.rglob("*.py")
        if p.is_file()
        and ".venv" not in p.parts
        and "__pycache__" not in p.parts
    )


def _iter_test_python_files() -> list[Path]:
    """返回 tests 目录下除本文件外的所有 Python 文件路径。"""
    return sorted(
        p for p in _iter_python_files(_TESTS_DIR)
        if p.name != "test_architecture_rules.py"
    )


def _read_text(path: Path) -> str:
    """读取文件文本内容，并去除 BOM。"""
    return path.read_text(encoding="utf-8-sig")


# ---------------------------------------------------------------------------
# AST 辅助
# ---------------------------------------------------------------------------


class _CallVisitor(ast.NodeVisitor):
    """收集指定函数名的调用节点。"""

    def __init__(self, target_names: set[str]) -> None:
        self.target_names = target_names
        self.calls: list[ast.Call] = []

    def visit_Call(self, node: ast.Call) -> None:
        name = _get_call_name(node)
        if name in self.target_names:
            self.calls.append(node)
        self.generic_visit(node)


class _ImportVisitor(ast.NodeVisitor):
    """收集 import 名称。"""

    def __init__(self) -> None:
        self.imported_names: set[str] = set()

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.imported_names.add(alias.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            self.imported_names.add(node.module)
        for alias in node.names:
            self.imported_names.add(alias.name)


class _DecoratorVisitor(ast.NodeVisitor):
    """收集装饰器信息。"""

    def __init__(self) -> None:
        self.decorated_functions: list[tuple[ast.AsyncFunctionDef | ast.FunctionDef, list[ast.expr]]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self.decorated_functions.append((node, node.decorator_list))
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self.decorated_functions.append((node, node.decorator_list))
        self.generic_visit(node)


def _get_call_name(node: ast.Call) -> str | None:
    """从 Call 节点提取函数名（支持 Name 和 Attribute）。"""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _string_arg_value(node: ast.expr) -> str | None:
    """提取字符串参数值（Constant 或 JoinedStr 中的纯文本部分）。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _has_string_containing(node: ast.AST, substring: str) -> bool:
    """递归检查 AST 子树中是否包含包含指定子串的字符串常量。"""
    for child in ast.walk(node):
        value = _string_arg_value(child)
        if value is not None and substring in value:
            return True
    return False


def _is_pytest_asyncio_fixture_decorator(decorator: ast.expr) -> bool:
    """判断装饰器是否为 @pytest_asyncio.fixture。"""
    if isinstance(decorator, ast.Call):
        decorator = decorator.func
    if isinstance(decorator, ast.Attribute):
        return (
            isinstance(decorator.value, ast.Name)
            and decorator.value.id == "pytest_asyncio"
            and decorator.attr == "fixture"
        )
    if isinstance(decorator, ast.Name):
        return decorator.id == "fixture"
    return False


# ---------------------------------------------------------------------------
# 测试用例
# ---------------------------------------------------------------------------


def test_tests_directory_does_not_import_aiosqlite() -> None:
    """tests 目录不得 import aiosqlite。"""
    violations: list[str] = []
    for path in _iter_test_python_files():
        tree = ast.parse(_read_text(path))
        visitor = _ImportVisitor()
        visitor.visit(tree)
        if "aiosqlite" in visitor.imported_names:
            violations.append(f"{path.relative_to(_PROJECT_ROOT)}")
    assert not violations, f"tests 目录发现 aiosqlite import：{violations}"


def test_tests_directory_does_not_create_sqlite_engine() -> None:
    """tests 目录不得调用 create_async_engine 且 URL 含 sqlite。"""
    violations: list[str] = []
    for path in _iter_test_python_files():
        tree = ast.parse(_read_text(path))
        visitor = _CallVisitor({"create_async_engine"})
        visitor.visit(tree)
        for call in visitor.calls:
            if _has_string_containing(call, "sqlite"):
                violations.append(f"{path.relative_to(_PROJECT_ROOT)}")
                break
    assert not violations, f"tests 目录发现 SQLite engine 创建：{violations}"


def test_tests_directory_does_not_contain_create_table_strings() -> None:
    """tests 目录不得包含手写 CREATE TABLE 字符串（test_config_validation.py 除外）。"""
    marker = "CREATE" + " TABLE"  # 避免本文件自身命中简单文本扫描
    violations: list[str] = []
    for path in _iter_test_python_files():
        if path.name == "test_config_validation.py":
            continue
        text = _read_text(path)
        if marker.upper() in text.upper():
            violations.append(f"{path.relative_to(_PROJECT_ROOT)}")
    assert not violations, f"tests 目录发现手写 CREATE TABLE 字符串：{violations}"


def test_tests_directory_does_not_override_db_session_fixture() -> None:
    """tests 目录除 conftest.py 外不得定义名为 db_session 的 pytest_asyncio fixture。"""
    violations: list[str] = []
    for path in _iter_test_python_files():
        if path.name == "conftest.py":
            continue
        tree = ast.parse(_read_text(path))
        visitor = _DecoratorVisitor()
        visitor.visit(tree)
        for func, decorators in visitor.decorated_functions:
            if func.name != "db_session":
                continue
            if any(_is_pytest_asyncio_fixture_decorator(d) for d in decorators):
                violations.append(f"{path.relative_to(_PROJECT_ROOT)}:{func.lineno}")
                break
    assert not violations, f"tests 目录发现自定义 db_session fixture：{violations}"


def test_tests_directory_does_not_register_sqlite_jsonb_compiler() -> None:
    """tests 目录不得注册 SQLite JSONB compiler（@compiles(JSONB, "sqlite")）。"""
    violations: list[str] = []
    compile_pattern = re.compile(r"@compiles\s*\([^)]*JSONB[^)]*sqlite", re.IGNORECASE)
    for path in _iter_test_python_files():
        text = _read_text(path)
        if compile_pattern.search(text):
            violations.append(f"{path.relative_to(_PROJECT_ROOT)}")
    assert not violations, f"tests 目录发现 SQLite JSONB compiler 注册：{violations}"


def test_pyproject_does_not_declare_aiosqlite() -> None:
    """backend/pyproject.toml 不得声明 aiosqlite 依赖。"""
    pyproject_path = _BACKEND_DIR / "pyproject.toml"
    data = tomllib.loads(_read_text(pyproject_path))
    all_deps: list[str] = []
    all_deps.extend(data.get("project", {}).get("dependencies", []))
    all_deps.extend(data.get("project", {}).get("optional-dependencies", {}).get("dev", []))
    names = {dep.strip().split("[")[0].split("=")[0].split(">")[0].split("<")[0] for dep in all_deps}
    assert "aiosqlite" not in names, "backend/pyproject.toml 仍声明 aiosqlite 依赖"


def test_code_does_not_use_user_role() -> None:
    """代码中不得出现 Role(name="user") 或 _ensure_role(..., "user")（应为 member）。"""
    violations: list[str] = []
    for path in _iter_python_files(_BACKEND_DIR):
        text = _read_text(path)
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            pytest.fail(f"解析失败 {path}: {exc}")

        for node in ast.walk(tree):
            # Role(name="user", ...)
            if isinstance(node, ast.Call):
                name = _get_call_name(node)
                if name == "Role":
                    for kw in node.keywords:
                        if kw.arg == "name":
                            value = _string_arg_value(kw.value)
                            if value == "user":
                                violations.append(
                                    f"{path.relative_to(_PROJECT_ROOT)}:{node.lineno} Role(name='user')"
                                )
                # _ensure_role(..., "user")
                if name == "_ensure_role":
                    for arg in node.args:
                        value = _string_arg_value(arg)
                        if value == "user":
                            violations.append(
                                f"{path.relative_to(_PROJECT_ROOT)}:{node.lineno} _ensure_role(..., 'user')"
                            )
    assert not violations, f"发现 user 角色使用（应改为 member）：{violations}"


def test_code_does_not_reference_admin_plan_code() -> None:
    """代码中不得出现 ADMIN_PLAN_CODE 常量引用。"""
    violations: list[str] = []
    for path in _iter_python_files(_BACKEND_DIR):
        text = _read_text(path)
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            pytest.fail(f"解析失败 {path}: {exc}")

        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "ADMIN_PLAN_CODE":
                violations.append(f"{path.relative_to(_PROJECT_ROOT)}:{node.lineno}")
            if isinstance(node, ast.Attribute) and node.attr == "ADMIN_PLAN_CODE":
                violations.append(f"{path.relative_to(_PROJECT_ROOT)}:{node.lineno}")
    assert not violations, f"发现 ADMIN_PLAN_CODE 常量引用：{violations}"


def test_security_doc_does_not_have_placeholder_commit() -> None:
    """docs/安全规范.md 不得包含 "Last verified commit: 待填写"。"""
    doc_path = _DOCS_DIR / "安全规范.md"
    text = _read_text(doc_path)
    assert "待填写" not in text, "docs/安全规范.md 仍包含 '待填写' 占位符"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
