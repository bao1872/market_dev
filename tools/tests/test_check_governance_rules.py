from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
SPEC = importlib.util.spec_from_file_location("check_governance_rules", ROOT / "tools/check_governance_rules.py")
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
check = MODULE.check


def _copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


@pytest.fixture
def governance_repo(tmp_path: Path) -> Path:
    target = tmp_path / "repo"
    _copy(ROOT / "AGENTS.md", target / "AGENTS.md")
    shutil.copytree(ROOT / "rules", target / "rules")
    for relative in (
        "tools/check_governance_rules.py",
        "tools/tests/test_check_governance_rules.py",
        "backend/tests/test_verify_infra_safety.py",
        "scripts/ops/panji-verify",
        "scripts/verify/run_remote_verification.sh",
        "scripts/verify/verification_plan.py",
        "scripts/verify/verify_attempt.py",
        "scripts/verify/plans/targeted-pg.json",
        "scripts/verify/plans/migration-roundtrip.json",
        "scripts/verify/plans/full-closure.json",
    ):
        _copy(ROOT / relative, target / relative)
    # Protected manifest references these unchanged repository files. Minimal placeholders
    # make the fixture structurally equivalent without testing their implementation here.
    for relative in ("docker-compose.verify.yml",):
        p = target / relative
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# fixture\n", encoding="utf-8")
    return target


def test_current_stage_aware_contract_passes(governance_repo: Path) -> None:
    assert check(governance_repo) == []


def test_missing_exploration_stage_is_rejected(governance_repo: Path) -> None:
    p = governance_repo / "AGENTS.md"
    p.write_text(p.read_text().replace("PROJECT_STAGE = EXPLORATION", "PROJECT_STAGE = UNKNOWN"))
    errors = check(governance_repo)
    assert any("missing stage marker" in error for error in errors)


def test_correctness_gate_cannot_be_removed(governance_repo: Path) -> None:
    p = governance_repo / "AGENTS.md"
    p.write_text(p.read_text().replace("单元测试必须完成", "单元测试可选"))
    errors = check(governance_repo)
    assert any("missing correctness gate" in error for error in errors)


def test_compatibility_alias_cannot_become_second_authority(governance_repo: Path) -> None:
    p = governance_repo / "rules/20-market-data-indicators.md"
    p.write_text("# old authority\n" + "rule\n" * 30)
    errors = check(governance_repo)
    assert any("compatibility alias" in error for error in errors)


def test_hardening_rule_must_remain_triggered_only(governance_repo: Path) -> None:
    p = governance_repo / "rules/70-hardening-release.md"
    p.write_text(p.read_text().replace("不是 Exploration 默认流程", "是 Exploration 默认流程"))
    errors = check(governance_repo)
    assert any("70-hardening-release" in error for error in errors)


def test_registered_plan_set_is_required(governance_repo: Path) -> None:
    (governance_repo / "scripts/verify/plans/targeted-pg.json").unlink()
    errors = check(governance_repo)
    assert any("missing registered verification plan" in error for error in errors)


def test_protected_manifest_still_guards_verification(governance_repo: Path) -> None:
    p = governance_repo / "rules/PROTECTED_GOVERNANCE_FILES.json"
    data = json.loads(p.read_text())
    data["exact_paths"] = [x for x in data["exact_paths"] if x != "scripts/ops/panji-verify"]
    p.write_text(json.dumps(data))
    errors = check(governance_repo)
    assert any("protected manifest missing path" in error for error in errors)


def test_tool_specific_governance_is_rejected(governance_repo: Path) -> None:
    p = governance_repo / "rules/00-core-governance.md"
    p.write_text(p.read_text() + "\nCodex 可以跳过单元测试。\n")
    errors = check(governance_repo)
    assert any("tool-specific governance" in error for error in errors)
