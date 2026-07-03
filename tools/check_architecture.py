#!/usr/bin/env python3
"""项目架构规则静态检查器（Standalone）。

用法：
    python tools/check_architecture.py

说明：
- 不导入 backend/tests/conftest.py，不连接数据库。
- 扫描 backend/、frontend/src/、docs/、tools/ 及根目录 markdown 文件。
- 排除 .venv、node_modules、__pycache__、.git。
- 对历史 Alembic/ADR 文件保留豁免（如 strategy_author）。
- 退出码：0 表示无违规，1 表示有违规。
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).parent.parent.resolve()
CHECKER_PATH = Path(__file__).resolve()

# ---------------------------------------------------------------------------
# 路径与扫描范围
# ---------------------------------------------------------------------------

SCAN_DIRS = [
    ROOT / "backend",
    ROOT / "frontend" / "src",
    ROOT / "docs",
    ROOT / "tools",
]
ROOT_MARKDOWNS = list(ROOT.glob("*.md"))

SKIP_DIR_PARTS = {".venv", "node_modules", "__pycache__", ".git"}

# 规则相关豁免路径
SQLITE_EXCEPTION_PATHS = {
    ROOT / "backend" / "app" / "config.py",
    ROOT / "backend" / "app" / "config.example.py",
    ROOT / "backend" / "tests" / "test_config_validation.py",
    CHECKER_PATH,
}

PLAN_SEED_MIGRATION_PATTERN = re.compile(r"backend[/\\]alembic[/\\]versions[/\\]048.*plans.*\.py$")


def is_under(path: Path, candidate: Path) -> bool:
    """判断 path 是否位于 candidate 目录下（或等于 candidate）。"""
    try:
        path.relative_to(candidate)
        return True
    except ValueError:
        return False


def iter_scanned_files(
    extensions: Iterable[str] | None = None,
    include_markdown: bool = False,
) -> Iterable[Path]:
    """遍历扫描范围内的文件。"""
    exts = set(extensions) if extensions else None
    for directory in SCAN_DIRS:
        if not directory.exists():
            continue
        for path in directory.rglob("*"):
            if not path.is_file():
                continue
            if any(part in SKIP_DIR_PARTS for part in path.parts):
                continue
            if exts and path.suffix.lower() not in exts:
                continue
            yield path
    if include_markdown:
        for path in ROOT_MARKDOWNS:
            if path.is_file() and (exts is None or path.suffix.lower() in exts):
                yield path


def is_adr_or_docs(path: Path) -> bool:
    """判断文件是否属于 docs/ 或 ADR 文档（用于 SQLite 规则引用豁免）。"""
    if is_under(path, ROOT / "docs"):
        return True
    if path.name.startswith("ADR-") and path.suffix == ".md":
        return True
    return False


def read_text(path: Path) -> str:
    """读取文本，失败时返回空字符串（避免二进制文件崩溃）。"""
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except (OSError, UnicodeDecodeError):
        return ""


def line_no(text: str, pos: int) -> int:
    """根据字符位置返回 1-based 行号。"""
    return text.count("\n", 0, pos) + 1


def get_line(text: str, lineno: int) -> str:
    """返回指定行的内容。"""
    lines = text.splitlines()
    if 1 <= lineno <= len(lines):
        return lines[lineno - 1]
    return ""


# ---------------------------------------------------------------------------
# 违规记录
# ---------------------------------------------------------------------------

class Violation:
    def __init__(self, rule: str, path: Path, lineno: int, context: str) -> None:
        self.rule = rule
        self.path = path
        self.lineno = lineno
        self.context = context.strip()

    def __str__(self) -> str:
        rel = self.path.relative_to(ROOT)
        return f"[{self.rule}] {rel}:{self.lineno}: {self.context}"


# ---------------------------------------------------------------------------
# AST 辅助
# ---------------------------------------------------------------------------

class _CallVisitor(ast.NodeVisitor):
    def __init__(self, target_names: set[str]) -> None:
        self.target_names = target_names
        self.calls: list[ast.Call] = []

    def visit_Call(self, node: ast.Call) -> None:
        name = _get_call_name(node)
        if name in self.target_names:
            self.calls.append(node)
        self.generic_visit(node)


class _DecoratorVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.functions: list[tuple[ast.AsyncFunctionDef | ast.FunctionDef, list[ast.expr]]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self.functions.append((node, node.decorator_list))
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self.functions.append((node, node.decorator_list))
        self.generic_visit(node)


def _get_call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _string_arg_value(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _is_pytest_fixture_decorator(decorator: ast.expr) -> bool:
    if isinstance(decorator, ast.Call):
        decorator = decorator.func
    if isinstance(decorator, ast.Attribute):
        return (
            isinstance(decorator.value, ast.Name)
            and decorator.value.id in ("pytest_asyncio", "pytest")
            and decorator.attr == "fixture"
        )
    if isinstance(decorator, ast.Name):
        return decorator.id == "fixture"
    return False


# ---------------------------------------------------------------------------
# 检查规则实现
# ---------------------------------------------------------------------------

SQLITE_ENGINE_PATTERN = re.compile(
    r"sqlite://|sqlite\+aiosqlite|:\s*memory\s*:", re.IGNORECASE
)
AIOSQLITE_IMPORT_PATTERN = re.compile(
    r"^\s*(?:from\s+aiosqlite|import\s+aiosqlite)", re.MULTILINE | re.IGNORECASE
)
CREATE_TABLE_PATTERN = re.compile(r"CREATE\s+TABLE", re.IGNORECASE)
JSONB_COMPILER_PATTERN = re.compile(r"@compiles\s*\([^)]*JSONB[^)]*sqlite", re.IGNORECASE)
ADMIN_PLAN_CODE_PATTERN = re.compile(r"\bADMIN_PLAN_CODE\b")
STRATEGY_AUTHOR_PATTERN = re.compile(r"\bstrategy_author\b")
DAILI_PLACEHOLDER_PATTERN = re.compile(r"待填写")

# 20/50 与 monitor/watchlist 出现在同一行即视为可疑硬编码（聚焦套餐监控/自选上限）
LIMIT_KEYWORDS = r"monitor|watchlist|监控|自选"
LIMIT_20_50_PATTERN = re.compile(
    rf"^(?i:.*(?:{LIMIT_KEYWORDS}).*\b(20|50)\b.*)$",
    re.MULTILINE,
)
LIMIT_20_50_REVERSE_PATTERN = re.compile(
    rf"^(?i:.*\b(20|50)\b.*(?:{LIMIT_KEYWORDS}).*)$",
    re.MULTILINE,
)

# plan-limit-hardcode 排除项：时间字符串、技术指标参数、价格上下文
LIMIT_EXCLUSION_PATTERNS = [
    # 10:20 等时间字符串
    re.compile(r"\b\d{1,2}:\d{2}\b"),
    # SMA(20) / EMA(20) / RSI(14) / MACD / ATR / STD / POC / BB / DSA / VWAP 等技术指标参数
    re.compile(
        r"\b(?:SMA|EMA|RSI|MACD|ATR|STD|POC|BB|DSA|VWAP|OBV|CCI|KDJ|BOLL)\s*\(\s*\d+\s*\)",
        re.IGNORECASE,
    ),
    # 价格/周期上下文：monthly、yearly、price、¥、$
    re.compile(r"\b(?:monthly|yearly|price|price_)\b|[¥$]", re.IGNORECASE),
]


# 套餐 feature 关键词，用于提取候选 feature list
PLAN_FEATURE_KEYWORDS = {
    "monitor",
    "watchlist",
    "screener",
    "trend_selection",
    "stock_detail",
    "node_monitor",
    "in_app_message",
    "feishu_notification",
    "stock_memo",
    "advanced_export",
    "report",
    "alert",
    "notification",
    "export",
    "memo",
    "选股",
    "监控",
    "通知",
    "导出",
}


def check_sqlite_engine_strings() -> list[Violation]:
    """规则 1/2：backend runtime/tests/tools 禁止 SQLite/aiosqlite engine 字符串。"""
    violations: list[Violation] = []
    for path in iter_scanned_files(extensions={".py"}):
        if path in SQLITE_EXCEPTION_PATHS or is_adr_or_docs(path):
            continue
        if not (
            is_under(path, ROOT / "backend")
            or is_under(path, ROOT / "tools")
        ):
            continue
        text = read_text(path)
        for match in SQLITE_ENGINE_PATTERN.finditer(text):
            lineno = line_no(text, match.start())
            violations.append(
                Violation(
                    "sqlite-engine-string",
                    path,
                    lineno,
                    get_line(text, lineno),
                )
            )
            break  # 每文件只报一次
    return violations


def check_aiosqlite_imports() -> list[Violation]:
    """规则 2：backend runtime/tests/tools 禁止 import aiosqlite。"""
    violations: list[Violation] = []
    for path in iter_scanned_files(extensions={".py"}):
        if path in SQLITE_EXCEPTION_PATHS or is_adr_or_docs(path):
            continue
        if not (
            is_under(path, ROOT / "backend")
            or is_under(path, ROOT / "tools")
        ):
            continue
        text = read_text(path)
        if AIOSQLITE_IMPORT_PATTERN.search(text):
            lineno = 1
            for match in AIOSQLITE_IMPORT_PATTERN.finditer(text):
                lineno = line_no(text, match.start())
                break
            violations.append(
                Violation(
                    "aiosqlite-import",
                    path,
                    lineno,
                    get_line(text, lineno),
                )
            )
    return violations


def check_handwritten_schema() -> list[Violation]:
    """规则 3：backend/tests/ 禁止手写 CREATE TABLE / @compiles(JSONB, sqlite)。"""
    violations: list[Violation] = []
    tests_dir = ROOT / "backend" / "tests"
    if not tests_dir.exists():
        return violations
    for path in iter_scanned_files(extensions={".py"}):
        if not is_under(path, tests_dir):
            continue
        if path == CHECKER_PATH:
            continue
        text = read_text(path)
        for match in CREATE_TABLE_PATTERN.finditer(text):
            lineno = line_no(text, match.start())
            violations.append(
                Violation(
                    "handwritten-create-table",
                    path,
                    lineno,
                    get_line(text, lineno),
                )
            )
            break
        for match in JSONB_COMPILER_PATTERN.finditer(text):
            lineno = line_no(text, match.start())
            violations.append(
                Violation(
                    "sqlite-jsonb-compiler",
                    path,
                    lineno,
                    get_line(text, lineno),
                )
            )
            break
    return violations


def check_custom_db_session_fixture() -> list[Violation]:
    """规则 5：backend/tests/ 除 conftest.py 外禁止自定义 db_session fixture。"""
    violations: list[Violation] = []
    tests_dir = ROOT / "backend" / "tests"
    if not tests_dir.exists():
        return violations
    for path in iter_scanned_files(extensions={".py"}):
        if not is_under(path, tests_dir):
            continue
        if path.name == "conftest.py":
            continue
        text = read_text(path)
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        visitor = _DecoratorVisitor()
        visitor.visit(tree)
        for func, decorators in visitor.functions:
            if func.name != "db_session":
                continue
            if any(_is_pytest_fixture_decorator(d) for d in decorators):
                violations.append(
                    Violation(
                        "custom-db-session-fixture",
                        path,
                        func.lineno,
                        get_line(text, func.lineno),
                    )
                )
                break
    return violations


def check_pyproject_aiosqlite() -> list[Violation]:
    """规则 2：backend/pyproject.toml 禁止声明 aiosqlite。"""
    violations: list[Violation] = []
    pyproject_path = ROOT / "backend" / "pyproject.toml"
    if not pyproject_path.exists():
        return violations
    text = read_text(pyproject_path)
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return [Violation("pyproject-aiosqlite", pyproject_path, 1, "无法解析 pyproject.toml")]

    all_deps: list[str] = []
    all_deps.extend(data.get("project", {}).get("dependencies", []))
    for group in data.get("project", {}).get("optional-dependencies", {}).values():
        all_deps.extend(group)

    names = {dep.strip().split("[")[0].split("=")[0].split(">")[0].split("<")[0] for dep in all_deps}
    if "aiosqlite" in names:
        violations.append(
            Violation(
                "pyproject-aiosqlite",
                pyproject_path,
                1,
                "backend/pyproject.toml 仍声明 aiosqlite 依赖",
            )
        )
    return violations


def check_user_role() -> list[Violation]:
    """规则 7：backend 禁止 Role(name='user') / _ensure_role(..., 'user')。"""
    violations: list[Violation] = []
    backend_dir = ROOT / "backend"
    if not backend_dir.exists():
        return violations
    for path in iter_scanned_files(extensions={".py"}):
        if not is_under(path, backend_dir):
            continue
        text = read_text(path)
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _get_call_name(node)
            if name == "Role":
                for kw in node.keywords:
                    if kw.arg == "name" and _string_arg_value(kw.value) == "user":
                        violations.append(
                            Violation(
                                "user-role",
                                path,
                                node.lineno,
                                get_line(text, node.lineno),
                            )
                        )
            elif name == "_ensure_role":
                for arg in node.args:
                    if _string_arg_value(arg) == "user":
                        violations.append(
                            Violation(
                                "user-role",
                                path,
                                node.lineno,
                                get_line(text, node.lineno),
                            )
                        )
    return violations


def check_admin_plan_code() -> list[Violation]:
    """规则 8：全局禁止 ADMIN_PLAN_CODE。"""
    violations: list[Violation] = []
    for path in iter_scanned_files(extensions={".py", ".ts", ".tsx", ".js", ".jsx", ".toml", ".md"}, include_markdown=True):
        if path == CHECKER_PATH:
            continue
        text = read_text(path)
        for match in ADMIN_PLAN_CODE_PATTERN.finditer(text):
            lineno = line_no(text, match.start())
            violations.append(
                Violation(
                    "admin-plan-code",
                    path,
                    lineno,
                    get_line(text, lineno),
                )
            )
            break
    return violations


def check_strategy_author() -> list[Violation]:
    """规则：backend runtime/tests 与 frontend/src 禁止 strategy_author（历史 Alembic/ADR 除外）。"""
    violations: list[Violation] = []
    allowed_dirs = [
        ROOT / "backend" / "app",
        ROOT / "backend" / "tests",
        ROOT / "frontend" / "src",
    ]
    alembic_dir = ROOT / "backend" / "alembic" / "versions"
    docs_dir = ROOT / "docs"
    for path in iter_scanned_files(extensions={".py", ".ts", ".tsx", ".js", ".jsx", ".md"}, include_markdown=False):
        if not any(is_under(path, d) for d in allowed_dirs):
            continue
        if is_under(path, alembic_dir):
            continue
        if is_under(path, docs_dir):
            continue
        text = read_text(path)
        for match in STRATEGY_AUTHOR_PATTERN.finditer(text):
            lineno = line_no(text, match.start())
            violations.append(
                Violation(
                    "strategy-author",
                    path,
                    lineno,
                    get_line(text, lineno),
                )
            )
            break
    return violations


def _extract_python_string_list(node: ast.AST) -> tuple[tuple[str, ...], int] | None:
    """从 list/tuple/set 节点提取字符串常量列表（用于检测重复 feature list）。"""
    items: list[str] = []
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        for elt in node.elts:
            value = _string_arg_value(elt)
            if value is None:
                return None
            items.append(value)
    else:
        return None
    if not items:
        return None
    return tuple(sorted(items)), getattr(node, "lineno", 1)


def check_plan_value_hardcoding() -> list[Violation]:
    """规则 9：backend/app/ 与 frontend/src/ 禁止硬编码/重复套餐数值。

    启发式检测：
    1. 同行出现 20/50 与 monitor/watchlist/limit 等关键词视为可疑硬编码。
    2. 跨文件重复出现相同 feature 字符串列表视为重复定义。
    3. 跨文件重复出现 plan_code -> display_name 映射视为重复定义。
    """
    violations: list[Violation] = []
    target_dirs = [ROOT / "backend" / "app", ROOT / "frontend" / "src"]
    allowed_plan_seed = re.compile(r"backend[/\\]alembic[/\\]versions[/\\]048.*\.py$")

    # 1. 20/50 硬编码
    for path in iter_scanned_files(extensions={".py", ".ts", ".tsx", ".js", ".jsx", ".yaml", ".yml", ".json"}):
        if not any(is_under(path, d) for d in target_dirs):
            continue
        text = read_text(path)
        seen_lines: set[int] = set()
        for pattern in (LIMIT_20_50_PATTERN, LIMIT_20_50_REVERSE_PATTERN):
            for match in pattern.finditer(text):
                lineno = line_no(text, match.start())
                if lineno in seen_lines:
                    continue
                line = get_line(text, lineno)
                # 排除时间字符串、技术指标参数、价格上下文等误报
                if any(exc.search(line) for exc in LIMIT_EXCLUSION_PATTERNS):
                    continue
                seen_lines.add(lineno)
                violations.append(
                    Violation(
                        "plan-limit-hardcode",
                        path,
                        lineno,
                        line,
                    )
                )

    # 2. 跨文件重复 feature list
    feature_lists: dict[tuple[str, ...], list[tuple[Path, int, str]]] = defaultdict(list)
    plan_name_maps: dict[tuple[str, str], list[tuple[Path, int, str]]] = defaultdict(list)

    for path in iter_scanned_files(extensions={".py", ".ts", ".tsx", ".js", ".jsx"}):
        if not any(is_under(path, d) for d in target_dirs):
            continue
        if allowed_plan_seed.search(str(path)):
            continue
        text = read_text(path)

        # Python AST 提取 list/tuple/set
        if path.suffix == ".py":
            try:
                tree = ast.parse(text)
            except SyntaxError:
                tree = None
            if tree:
                for node in ast.walk(tree):
                    extracted = _extract_python_string_list(node)
                    if extracted is None:
                        continue
                    items, lineno = extracted
                    if any(kw in " ".join(items).lower() for kw in PLAN_FEATURE_KEYWORDS):
                        feature_lists[items].append((path, lineno, get_line(text, lineno)))

                    # dict literal 中的 plan_code -> display_name
                    if isinstance(node, ast.Dict):
                        for key, value in zip(node.keys, node.values, strict=False):
                            k = _string_arg_value(key) if key else None
                            v = _string_arg_value(value)
                            if k and v and ("observe" in k or "research" in k or "_20" in k or "_50" in k):
                                plan_name_maps[(k, v)].append((path, getattr(node, "lineno", 1), get_line(text, getattr(node, "lineno", 1))))

        # JS/TS 简单正则提取数组与对象映射
        else:
            # 数组：["a", "b", "c"]
            for match in re.finditer(r"\[([^\]]+)\]", text):
                content = match.group(1)
                strings = re.findall(r'["\']([^"\']+)["\']', content)
                if len(strings) >= 2 and any(kw in " ".join(strings).lower() for kw in PLAN_FEATURE_KEYWORDS):
                    items = tuple(sorted(strings))
                    lineno = line_no(text, match.start())
                    feature_lists[items].append((path, lineno, get_line(text, lineno)))

            # 对象映射：observe_20: "观察版" 或 "observe_20": "观察版"
            for match in re.finditer(r'(["\']?(?:observe|research)_\d+["\']?)\s*:\s*["\']([^"\']+)["\']', text):
                k = match.group(1).strip('"\'')
                v = match.group(2)
                lineno = line_no(text, match.start())
                plan_name_maps[(k, v)].append((path, lineno, get_line(text, lineno)))

    # 报告重复 feature list
    for _items, locations in feature_lists.items():
        if len(locations) <= 1:
            continue
        # 仅当出现在不同文件时才报告
        files = {loc[0] for loc in locations}
        if len(files) <= 1:
            continue
        for path, lineno, context in locations:
            violations.append(
                Violation(
                    "duplicate-plan-feature-list",
                    path,
                    lineno,
                    context,
                )
            )

    # 报告重复 plan name map
    for _key, locations in plan_name_maps.items():
        if len(locations) <= 1:
            continue
        files = {loc[0] for loc in locations}
        if len(files) <= 1:
            continue
        for path, lineno, context in locations:
            violations.append(
                Violation(
                    "duplicate-plan-name-map",
                    path,
                    lineno,
                    context,
                )
            )

    return violations


def check_daili_placeholder() -> list[Violation]:
    """规则：docs/current/ 与 docs/maps/ 禁止 '待填写' 占位符（v2 适配）。

    v2 调整：只在 current/maps（事实源）中检查占位符；
    archive/changes/根目录规则说明文档/AGENTS.md 可能引用"待填写"作为规则描述，
    不检查，避免误判。
    """
    violations: list[Violation] = []
    for path in iter_scanned_files(extensions={".md"}, include_markdown=True):
        # v2：只在 current/maps 中检查占位符
        if not (is_under(path, ROOT / "docs" / "current") or
                is_under(path, ROOT / "docs" / "maps")):
            continue
        text = read_text(path)
        for match in DAILI_PLACEHOLDER_PATTERN.finditer(text):
            lineno = line_no(text, match.start())
            violations.append(
                Violation(
                    "daili-placeholder",
                    path,
                    lineno,
                    get_line(text, lineno),
                )
            )
            break
    return violations


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

CHECKS = [
    ("SQLite engine strings", check_sqlite_engine_strings),
    ("aiosqlite imports", check_aiosqlite_imports),
    ("Handwritten schema", check_handwritten_schema),
    ("Custom db_session fixture", check_custom_db_session_fixture),
    ("aiosqlite in pyproject.toml", check_pyproject_aiosqlite),
    ("user role usage", check_user_role),
    ("ADMIN_PLAN_CODE", check_admin_plan_code),
    ("strategy_author", check_strategy_author),
    ("Plan value hardcoding/duplication", check_plan_value_hardcoding),
    ("待填写 placeholder", check_daili_placeholder),
]


def main() -> int:
    all_violations: list[Violation] = []
    failed_rules: list[str] = []
    passed_rules: list[str] = []

    for rule_name, check_func in CHECKS:
        violations = check_func()
        if violations:
            failed_rules.append(rule_name)
            all_violations.extend(violations)
        else:
            passed_rules.append(rule_name)

    if all_violations:
        print("Architecture rule violations found:\n")
        for v in all_violations:
            print(f"  {v}")
        print()

    print("Summary:")
    print(f"  Total violations: {len(all_violations)}")
    print(f"  Failed checks: {len(failed_rules)}")
    if failed_rules:
        for name in failed_rules:
            print(f"    - {name}")
    print(f"  Passed checks: {len(passed_rules)}")
    for name in passed_rules:
        print(f"    - {name}")

    return 1 if all_violations else 0


if __name__ == "__main__":
    sys.exit(main())
