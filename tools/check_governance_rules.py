"""Validate Panji's stage-aware governance contract.

The checker protects stable correctness/safety rules and verifies that
Exploration is the default routing mode while Hardening remains available
as an explicitly triggered path. It intentionally does not turn every
feature change into a release audit.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ACTIVE_RULES = {
    "README.md",
    "00-core-governance.md",
    "10-product-domain-invariants.md",
    "20-market-data-computation.md",
    "30-security-data-safety.md",
    "40-testing-quality.md",
    "50-git-development-flow.md",
    "60-runtime-frontend-acceptance.md",
    "70-hardening-release.md",
    "80-deployment-migration.md",
    "90-deprecated-forbidden.md",
}

COMPATIBILITY_ALIASES = {
    "20-market-data-indicators.md": "20-market-data-computation.md",
    "30-access-security.md": "30-security-data-safety.md",
    "80-deployment-data-safety.md": "80-deployment-migration.md",
    "81-remote-deployment-only.md": "80-deployment-migration.md",
}

REMOVED_RULES = {
    "60-trae-work.md",
    "70-trae-cn.md",
    "85-server-directory-boundaries.md",
    "AGENTS-MIGRATION-MAP.md",
}

PROTECTED_MANIFEST = "rules/PROTECTED_GOVERNANCE_FILES.json"
REQUIRED_PROTECTED_PATHS = {
    "AGENTS.md",
    "docker-compose.verify.yml",
    "tools/check_governance_rules.py",
    "tools/tests/test_check_governance_rules.py",
    "backend/tests/test_verify_infra_safety.py",
    "scripts/ops/panji-verify",
}
REQUIRED_PROTECTED_PREFIXES = {"rules/", "scripts/verify/"}

REQUIRED_PLANS = {
    "targeted-pg.json",
    "migration-roundtrip.json",
    "full-closure.json",
}

STAGE_MARKERS = (
    "PROJECT_STAGE = EXPLORATION",
    "FAST_ITERATION / EXPLORATION MODE",
    "Hardening Trigger",
)
CORRECTNESS_MARKERS = (
    "业务逻辑正确性必须确认",
    "代码逻辑必须审查",
    "单元测试必须完成",
    "API → Frontend 技术绑定必须验证",
    "禁止结果污染",
)
ROUTING_MARKERS = (
    "Value Before Governance",
    "Correctness Before Visibility",
    "Hypothesis Slice 完成即 STOP",
)

TOOL_NAMES = ("TRAE CN", "TRAE Work", "CodeBuddy", "Codex", "Cursor", "Copilot")
TOOL_NEUTRAL_CONTEXT = ("不按", "不区分", "同一套", "已废弃", "禁止恢复", "工具专属")

FORBIDDEN_EXECUTABLE_TOKENS = (
    "docker system prune -a",
    "docker system prune -af",
    "docker image prune -a",
    "docker volume prune",
    "down -v",
)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""


def _executable_shell(path: Path) -> str:
    return "\n".join(
        line for line in _read(path).splitlines() if not line.lstrip().startswith("#")
    )


def _check_rule_layout(root: Path, errors: list[str]) -> None:
    rules_dir = root / "rules"
    actual = {p.name for p in rules_dir.glob("*.md")}
    expected = ACTIVE_RULES | set(COMPATIBILITY_ALIASES)

    for name in sorted(expected - actual):
        errors.append(f"missing rule file: rules/{name}")
    for name in sorted(actual - expected):
        errors.append(f"unregistered rule file: rules/{name}")
    for name in REMOVED_RULES:
        if (rules_dir / name).exists():
            errors.append(f"removed governance file restored: rules/{name}")

    for alias, target in COMPATIBILITY_ALIASES.items():
        text = _read(rules_dir / alias)
        if "Compatibility Alias" not in text:
            errors.append(f"compatibility alias lacks alias marker: rules/{alias}")
        if f"rules/{target}" not in text:
            errors.append(f"compatibility alias points to wrong target: rules/{alias}")
        if len(text.splitlines()) > 20:
            errors.append(f"compatibility alias contains duplicate authority: rules/{alias}")


def _check_agents(root: Path, errors: list[str]) -> None:
    text = _read(root / "AGENTS.md")
    for marker in STAGE_MARKERS:
        if marker not in text:
            errors.append(f"AGENTS.md missing stage marker: {marker}")
    for marker in CORRECTNESS_MARKERS:
        if marker not in text:
            errors.append(f"AGENTS.md missing correctness gate: {marker}")
    for marker in ROUTING_MARKERS:
        if marker not in text:
            errors.append(f"AGENTS.md missing exploration routing marker: {marker}")

    if "rules/README.md" not in text:
        errors.append("AGENTS.md must reference rules/README.md")
    for name in sorted(ACTIVE_RULES - {"README.md"}):
        if f"rules/{name}" not in text:
            errors.append(f"AGENTS.md missing active rule reference: rules/{name}")

    if "项目阶段或治理模式的变化不得自动触发 PRD 重写" not in text:
        errors.append("AGENTS.md missing governance-vs-PRD separation rule")


def _check_protected_manifest(root: Path, errors: list[str]) -> None:
    path = root / PROTECTED_MANIFEST
    try:
        manifest = json.loads(_read(path))
    except json.JSONDecodeError:
        errors.append(f"invalid JSON: {PROTECTED_MANIFEST}")
        return

    if manifest.get("schema_version") != 1:
        errors.append("protected governance manifest schema_version must remain 1")
    exact = set(manifest.get("exact_paths", []))
    prefixes = set(manifest.get("path_prefixes", []))
    for missing in sorted(REQUIRED_PROTECTED_PATHS - exact):
        errors.append(f"protected manifest missing path: {missing}")
    for missing in sorted(REQUIRED_PROTECTED_PREFIXES - prefixes):
        errors.append(f"protected manifest missing prefix: {missing}")


def _check_rule_semantics(root: Path, errors: list[str]) -> None:
    rules = root / "rules"
    required_markers = {
        "00-core-governance.md": ("Hypothesis Slice", "P0", "Two-Strike Architecture Rule"),
        "20-market-data-computation.md": ("future leakage", "Canonical", "daily Core 不依赖"),
        "30-security-data-safety.md": ("Exploration 不降低安全门槛", "bz_stock"),
        "40-testing-quality.md": ("Fast Iteration 不是少测试", "Modified-Scope Unit", "Full PURE_UNIT"),
        "60-runtime-frontend-acceptance.md": ("API", "frontend", "用户负责"),
        "70-hardening-release.md": ("不是 Exploration 默认流程", "Full RTM", "Release Decision"),
        "80-deployment-migration.md": ("M1", "M2", "M3", "默认不要求 production clone"),
        "90-deprecated-forbidden.md": ("每轮默认重型闭环", "不得用历史规则"),
    }
    for name, markers in required_markers.items():
        text = _read(rules / name)
        for marker in markers:
            if marker not in text:
                errors.append(f"rules/{name} missing contract marker: {marker}")


def _check_tool_neutrality(root: Path, errors: list[str]) -> None:
    for path in [root / "AGENTS.md", *(root / "rules").glob("*.md")]:
        for idx, line in enumerate(_read(path).splitlines(), 1):
            if any(name in line for name in TOOL_NAMES) and not any(
                marker in line for marker in TOOL_NEUTRAL_CONTEXT
            ):
                errors.append(f"tool-specific governance: {path.relative_to(root)}:{idx}")


def _check_verification_plans(root: Path, errors: list[str]) -> None:
    plans_dir = root / "scripts/verify/plans"
    actual = {p.name for p in plans_dir.glob("*.json")}
    for name in sorted(REQUIRED_PLANS - actual):
        errors.append(f"missing registered verification plan: scripts/verify/plans/{name}")

    for name in REQUIRED_PLANS:
        path = plans_dir / name
        if not path.exists():
            continue
        try:
            data = json.loads(_read(path))
        except json.JSONDecodeError:
            errors.append(f"invalid verification plan JSON: {path.relative_to(root)}")
            continue
        if data.get("schema_version") != 2:
            errors.append(f"verification plan must use schema_version=2: {path.relative_to(root)}")
        if data.get("name") != name.removesuffix(".json"):
            errors.append(f"verification plan name/path mismatch: {path.relative_to(root)}")

    entry = _executable_shell(root / "scripts/ops/panji-verify")
    for plan in ("targeted-pg", "migration-roundtrip", "full-closure"):
        if plan not in entry:
            errors.append(f"panji-verify does not register plan: {plan}")
    if "[0-9a-f]{40}" not in entry:
        errors.append("panji-verify must require complete 40-char SHA")

    runner = _executable_shell(root / "scripts/verify/run_remote_verification.sh")
    for marker in ("flock -n 9", "panji-verify-runtime:current", "panji-verify-python"):
        if marker not in runner:
            errors.append(f"verification runner missing safety marker: {marker}")
    for token in FORBIDDEN_EXECUTABLE_TOKENS:
        if token in runner:
            errors.append(f"verification runner contains forbidden destructive command: {token}")


def _check_ci_is_manual(root: Path, errors: list[str]) -> None:
    ci_path = root / ".github/workflows/ci.yml"
    if not ci_path.exists():
        return  # package-level self-check may omit unchanged CI
    text = _read(ci_path)
    on_match = re.search(r"(?ms)^on:\s*\n(?P<body>(?:^[ \t]+.*\n?)*)", text)
    body = on_match.group("body") if on_match else ""
    if "workflow_dispatch:" not in body:
        errors.append("ci.yml must keep workflow_dispatch")
    if re.search(r"(?m)^\s+(push|pull_request|schedule):", body):
        errors.append("ci.yml must not gain automatic triggers")


def check(root: Path) -> list[str]:
    errors: list[str] = []
    _check_rule_layout(root, errors)
    _check_agents(root, errors)
    _check_protected_manifest(root, errors)
    _check_rule_semantics(root, errors)
    _check_tool_neutrality(root, errors)
    _check_verification_plans(root, errors)
    _check_ci_is_manual(root, errors)
    return errors


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    errors = check(root)
    if errors:
        print("Governance check FAILED:")
        for error in errors:
            print(f"- {error}")
        return 1
    print("Governance check PASS: stage-aware Exploration/Hardening contract is consistent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
