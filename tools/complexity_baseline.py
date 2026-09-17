"""Emit a deterministic JSON baseline for repository structure complexity."""
from __future__ import annotations

import ast
import json
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_ROOTS = (ROOT / "backend" / "app", ROOT / "frontend" / "src")
TEST_ROOTS = (ROOT / "backend" / "tests", ROOT / "tools" / "tests")
CORE_FILES = (
    ROOT / "backend" / "app" / "services" / "after_close_orchestrator.py",
    ROOT / "backend" / "app" / "services" / "bars_scheduler_service.py",
    ROOT / "backend" / "app" / "worker.py",
)


def _source_files(roots: Iterable[Path]) -> list[Path]:
    files: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        files.extend(root.rglob("*.py"))
        files.extend(root.rglob("*.ts"))
        files.extend(root.rglob("*.tsx"))
    return sorted(set(files))


def _loc(paths: Iterable[Path]) -> int:
    return sum(len(path.read_text(encoding="utf-8", errors="replace").splitlines()) for path in paths)


def _module(path: Path) -> str:
    rel = path.relative_to(ROOT / "backend").with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _python_graph() -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    for path in sorted((ROOT / "backend" / "app").rglob("*.py")):
        module = _module(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        deps: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app."):
                deps.add(node.module)
            elif isinstance(node, ast.Import):
                deps.update(alias.name for alias in node.names if alias.name.startswith("app."))
        graph[module] = deps
    return graph


def _cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indexes: dict[str, int] = {}
    lows: dict[str, int] = {}
    components: list[list[str]] = []

    def visit(node: str) -> None:
        nonlocal index
        indexes[node] = lows[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for dep in graph[node]:
            if dep not in graph:
                continue
            if dep not in indexes:
                visit(dep)
                lows[node] = min(lows[node], lows[dep])
            elif dep in on_stack:
                lows[node] = min(lows[node], indexes[dep])
        if lows[node] == indexes[node]:
            component: list[str] = []
            while True:
                member = stack.pop()
                on_stack.remove(member)
                component.append(member)
                if member == node:
                    break
            if len(component) > 1:
                components.append(sorted(component))

    for node in sorted(graph):
        if node not in indexes:
            visit(node)
    return sorted(components, key=lambda item: (-len(item), item))


def _nested_imports(path: Path) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        and any(isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)) for parent in _parents(node, parents))
    )


def _parents(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> Iterable[ast.AST]:
    current = parents.get(node)
    while current is not None:
        yield current
        current = parents.get(current)


def main() -> None:
    production = _source_files(PRODUCTION_ROOTS)
    tests = _source_files(TEST_ROOTS)
    graph = _python_graph()
    largest = sorted(
        ((len(path.read_text(encoding="utf-8").splitlines()), path.relative_to(ROOT).as_posix()) for path in production),
        reverse=True,
    )[:20]
    payload = {
        "schema_version": 1,
        "production_loc": _loc(production),
        "test_loc": _loc(tests),
        "python_cycle_count": len(_cycles(graph)),
        "python_cycles": _cycles(graph),
        "core_nested_imports": {
            path.relative_to(ROOT).as_posix(): _nested_imports(path) for path in CORE_FILES
        },
        "largest_production_files": [
            {"path": path, "loc": loc} for loc, path in largest
        ],
        "notes": {
            "sql_round_trips": "runtime instrumentation required; static counts are not evidence",
            "latency_and_memory": "capture per business-chain benchmark, not in structural baseline",
        },
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
