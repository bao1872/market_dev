from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
SPEC = importlib.util.spec_from_file_location(
    "check_governance_rules", ROOT / "tools/check_governance_rules.py"
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
check = MODULE.check


def _copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


@pytest.fixture
def governance_repo(tmp_path: Path) -> Path:
    target = tmp_path / "repo"
    _copy_file(ROOT / "AGENTS.md", target / "AGENTS.md")
    shutil.copytree(ROOT / "rules", target / "rules")
    _copy_file(ROOT / ".github/workflows/ci.yml", target / ".github/workflows/ci.yml")
    for relative in (
        "scripts/ops/panji-test-deploy",
        "scripts/deploy/panji-deploy.sh",
        "docs/prd/80-system-runtime.md",
        "docs/maps/80-system-runtime.md",
        "docs/runbooks/development-deployment.md",
        "docs/changes/INDEX.md",
    ):
        _copy_file(ROOT / relative, target / relative)
    shutil.copytree(ROOT / "docs/changes/2026", target / "docs/changes/2026")
    return target


def test_current_repository_contract_passes(governance_repo: Path) -> None:
    assert check(governance_repo) == []


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("role_file", "non-canonical rule file"),
        ("future_state", "future/staged governance state"),
        ("auto_ci", "automatic trigger"),
        ("old_deploy", "removed path restored"),
        ("duplicate_change", "duplicate Change ID"),
    ],
)
def test_governance_regressions_are_rejected(
    governance_repo: Path, mutation: str, expected: str
) -> None:
    if mutation == "role_file":
        (governance_repo / "rules/60-trae-work.md").write_text("# role\n", encoding="utf-8")
    elif mutation == "future_state":
        path = governance_repo / "rules/00-core-governance.md"
        path.write_text(read_text(path) + "\n> 状态：PLANNED\n", encoding="utf-8")
    elif mutation == "auto_ci":
        path = governance_repo / ".github/workflows/ci.yml"
        path.write_text(read_text(path).replace("  workflow_dispatch:\n", "  push:\n  workflow_dispatch:\n", 1), encoding="utf-8")
    elif mutation == "old_deploy":
        path = governance_repo / "scripts/deploy_live_runtime.sh"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    elif mutation == "duplicate_change":
        source = next((governance_repo / "docs/changes/2026").glob("CHANGE-*.md"))
        duplicate = source.with_name(source.stem + "-duplicate.md")
        shutil.copy2(source, duplicate)

    assert any(expected in error for error in check(governance_repo))


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")
