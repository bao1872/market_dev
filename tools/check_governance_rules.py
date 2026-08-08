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

try:
    import yaml
except ImportError:  # pragma: no cover - Compose 门禁在缺少 PyYAML 时跳过并告警
    yaml = None

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
    # 18: protected governance paths must actually exist on disk.
    for relative in sorted(exact):
        if not (root / relative).is_file():
            errors.append(f"protected governance path does not exist: {relative}")


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


# ---------------------------------------------------------------------------
# Always-On Safety Guards (restored from pre-stage-aware governance).
# Exploration may reduce release ceremony, but deployment/data-safety regression
# guards remain always-on. These are NOT release-gate checkers: they protect
# concrete machine contracts (deploy SHAs, migration state machine, compose
# resource budgets, protected DB/owner/prune/test-db/workflow boundaries) that
# must never regress regardless of project stage.
# ---------------------------------------------------------------------------

DEPLOY_ENTRY = "scripts/ops/panji-test-deploy"
DEPLOY_IMPL = "scripts/deploy/panji-deploy.sh"

# 1-6,7: local control entry + server deploy safety signals (exact-SHA, detach,
# HEAD restore, previous runtime SHA identity, first-live detection, migration
# state machine + failure path).
LOCAL_DEPLOY_SIGNALS = (
    "origin/dev",
    "merge-base --is-ancestor",
    "panji-prod-preflight",
    "panji-prod-ssh",
    "checkout -f --detach",
    "trap restore_head EXIT",
)
SERVER_DEPLOY_SIGNALS = (
    "origin/dev",
    "merge-base --is-ancestor",
    "docker-compose.prod.yml -f docker-compose.live.yml",
    'git diff --name-only "${PREVIOUS_SHA}" "${TARGET_SHA}"',
    "RUNTIME_SHA",
    "resolve_previous_runtime_sha()",
    "PREVIOUS_SHA_SOURCE",
    "unknown_baseline",
    "previous_runtime_sha_unknown",
    "running_version",
    "PANJI_BOOTSTRAP_PREVIOUS_SHA",
    "detect_first_live_deploy()",
    "apply_first_live_deploy_override()",
    "MIGRATION_ATTEMPTED",
    "MIGRATION_SUCCEEDED",
    "SERVICES_RESTARTED",
    "handle_migration_failure()",
    "migration_failed_requires_inspection",
)
FORBIDDEN_DEPLOY_CODE = {
    "image prune -a": "global image prune",
    "system prune": "global system prune",
    "volume prune": "volume prune",
    "container prune": "unrelated container prune",
    "down -v": "volume-destructive compose command",
    "origin/main": "main deployment source",
    "PANJI_TEST_SKIP_PREFLIGHT": "preflight bypass switch",
}

# 8: forbidden global prune tokens anywhere in deploy scripts.
# 10: Compose stateful service resource budget / healthcheck.
COMPOSE_PROD = "docker-compose.prod.yml"
COMPOSE_STATEFUL = {"postgres", "redis", "umami"}
COMPOSE_RESOURCE_FIELDS = (
    "mem_limit", "mem_reservation", "cpus", "pids_limit", "logging", "stop_grace_period",
)
COMPOSE_STATEFUL_FIELDS = ("restart", "healthcheck", "volumes")

# 11-14: deploy long-command timeout, OOM evidence, cleanup disk evidence.
DEPLOY_REQUIRED_SIGNALS = {
    "COMPOSE_PARALLEL_LIMIT=1": "serialized build/restart",
    "run_with_timeout": "long-command timeout",
    "post_deploy_resource_check": "post-deploy resource recheck",
    "OOMKilled": "container OOM check",
    "RestartCount": "container restart-count check",
    "docker stats --no-stream": "high-watermark collection",
    "IMAGES_BUILT": "image-built cleanup tiers",
    "cleanup_disk_before_mb": "pre-cleanup disk evidence",
    "cleanup_disk_after_mb": "post-cleanup disk evidence",
    "docker rmi": "targeted old-SHA image reclamation",
}

# 9: forbidden standalone test-DB tokens across active docs + CI postgres service.
FORBIDDEN_TEST_DB_TOKENS = {
    "PANJI_CI_DB_TEST": "standalone CI temp-DB switch",
    "TEST_DATABASE_URL": "standalone test-DB URL",
    "bz_stock_test": "standalone test-DB name",
    "postgres-integration-tests": "CI standalone PG integration job",
}
VERIFY_DB_ALLOWED_TOKENS = (
    "bz_stock_verify_", "PANJI_REMOTE_VERIFY_DB_TEST", "DS-110",
    "远程验证数据库", "远程临时验证数据库",
)
PROHIBITION_MARKERS = (
    "禁止", "不得", "永不", "排除", "已永久删除", "已删除", "不允许",
    "禁止引入", "禁止创建", "禁止使用", "例外", "唯一允许",
    "禁止恢复", "不恢复", "不得恢复", "不能作为",
)

# 15,16: explicit authorization gates (governance/PRD/Maps/Runbooks + plan-scoped doc).
AUTH_GATES = {
    "只有用户在当前任务中明确要求调整治理体系": "governance change authorization",
    "只有用户在当前任务中明确要求新增、修改或校准 PRD": "PRD change authorization",
    "只有用户在当前任务中明确要求更新 Maps": "Maps change authorization",
    "只有用户在当前任务中明确要求更新 Runbooks": "Runbooks change authorization",
    "计划授权不得隐式覆盖 PRD、Maps、Runbooks 或治理文档": "plan-scoped document gate",
    "每次远程验证或调试尝试结束后，无论成功、失败、取消或超时": "per-attempt verification cleanup",
}

# 17: verification cleanup fail-closed + single-entry + no forbidden exec.
VERIFY_CLEANUP_SIGNALS = ("blocked_cleanup", "panji-verify-python")
VERIFY_CLEANUP_FORBIDDEN = ("compose down", "--remove-orphans", '"down"', "down -v", "--rmi", "docker cp")


def _check_always_on_safety(root: Path, errors: list[str]) -> None:
    # 15,16: authorization gates must remain explicit in AGENTS.md.
    agents_text = _read(root / "AGENTS.md")
    for marker, label in AUTH_GATES.items():
        if marker not in agents_text:
            errors.append(f"AGENTS.md missing explicit {label} gate")

    # 1-6,7: deploy control entry + server implementation contract signals.
    local_entry = root / DEPLOY_ENTRY
    deploy_impl = root / DEPLOY_IMPL
    if not local_entry.is_file():
        errors.append(f"missing local deployment control entry: {DEPLOY_ENTRY}")
    if not deploy_impl.is_file():
        errors.append(f"missing server deploy implementation: {DEPLOY_IMPL}")
    if local_entry.is_file():
        local_code = _executable_shell(local_entry)
        for signal in LOCAL_DEPLOY_SIGNALS:
            if signal not in local_code:
                errors.append(f"local deploy entry missing contract signal: {signal}")
        for token, reason in FORBIDDEN_DEPLOY_CODE.items():
            if token in local_code:
                errors.append(f"forbidden deployment implementation ({reason}): {token}")
    if deploy_impl.is_file():
        server_code = _executable_shell(deploy_impl)
        for signal in SERVER_DEPLOY_SIGNALS:
            if signal not in server_code:
                errors.append(f"server deploy implementation missing contract signal: {signal}")
        for token, reason in FORBIDDEN_DEPLOY_CODE.items():
            if token in server_code:
                errors.append(f"forbidden deployment implementation ({reason}): {token}")

        # 7: RUNTIME_SHA single-file bind mount must be updated in place (not rename/rsync).
        write_sha = re.search(r"(?ms)^write_runtime_sha\(\)\s*\{.*?^\}", server_code)
        if write_sha is None:
            errors.append("server deploy implementation missing write_runtime_sha()")
        elif re.search(r"(?:rsync|mv)\s+[^\n]*RUNTIME_SHA", write_sha.group(0)):
            errors.append("RUNTIME_SHA updated via rename/rsync breaks single-file bind mount inode")

        # 6: migration failure path must not recreate containers.
        migration_fail = re.search(r"(?ms)^handle_migration_failure\(\)\s*\{.*?^\}", server_code)
        if migration_fail is not None and re.search(r"up -d|force-recreate", migration_fail.group(0)):
            errors.append("migration failure path must not recreate containers")

        # 11,13,14: deploy long-command timeout, OOM evidence, cleanup disk evidence.
        for signal, reason in DEPLOY_REQUIRED_SIGNALS.items():
            if signal not in server_code:
                errors.append(f"deploy script missing {reason} contract: {signal}")

    # 8: forbidden global prune / preflight bypass in deploy scripts.
    for script in (DEPLOY_ENTRY, DEPLOY_IMPL):
        path = root / script
        if path.is_file():
            code = _executable_shell(path)
            for token, reason in FORBIDDEN_DEPLOY_CODE.items():
                if token in code:
                    errors.append(f"forbidden deployment code ({reason}) in {script}: {token}")

    # 9: forbidden standalone test-DB tokens across active docs/ci (DS-110 allow-listed).
    active_bases = [root / "AGENTS.md", *sorted((root / "rules").glob("*.md")),
                    root / "docs/prd", root / "docs/maps", root / "docs/runbooks",
                    root / ".github/workflows/ci.yml"]
    for base in active_bases:
        files = sorted(base.rglob("*.md")) if base.is_dir() else [base] if base.is_file() else []
        for path in files:
            rel = path.relative_to(root)
            if "changes" in rel.parts:
                continue
            lines = _read(path).splitlines()
            for line_no, line in enumerate(lines, 1):
                # 若本行或前 3 行内出现“禁止/不得/禁止恢复”等语境，则本行属于禁止清单声明本身，不误报。
                context_window = " ".join(lines[max(0, line_no - 4):line_no])
                if any(m in context_window for m in PROHIBITION_MARKERS):
                    continue
                for token, reason in FORBIDDEN_TEST_DB_TOKENS.items():
                    if token in line and not any(a in line for a in VERIFY_DB_ALLOWED_TOKENS):
                        errors.append(f"forbidden standalone test-db ({reason}): {rel}:{line_no}")

    # 9: CI must not add a standalone postgres:16 test service.
    ci_path = root / ".github/workflows/ci.yml"
    if ci_path.is_file() and re.search(
        r"(?m)^\s*services:\s*\n\s*postgres:\s*\n\s*image:\s*postgres:16",
        _read(ci_path),
    ):
        errors.append("forbidden standalone test-db (CI standalone postgres:16 test service)")

    # 10: Compose stateful service resource budget / healthcheck.
    compose_path = root / COMPOSE_PROD
    if compose_path.is_file():
        if yaml is None:
            errors.append("Compose resource guard cannot run: PyYAML missing")
        else:
            try:
                data = yaml.safe_load(_read(compose_path)) or {}
            except Exception as exc:  # noqa: BLE001
                errors.append(f"Compose parse failed: {exc}")
                data = {}
            svcs = data.get("services", {}) or {}
            for name, svc in svcs.items() if isinstance(svcs, dict) else []:
                if not isinstance(svc, dict):
                    errors.append(f"{COMPOSE_PROD} service:{name} is not a dict")
                    continue
                for field in COMPOSE_RESOURCE_FIELDS:
                    if field not in svc:
                        errors.append(f"{COMPOSE_PROD} service:{name} missing resource limit: {field}")
                if name in COMPOSE_STATEFUL:
                    for field in COMPOSE_STATEFUL_FIELDS:
                        if field not in svc:
                            errors.append(f"{COMPOSE_PROD} stateful service:{name} missing field: {field}")

    # 17: verification cleanup fail-closed + single reusable runtime protection.
    cleanup = _read(root / "scripts/verify/cleanup_runner.py")
    for signal in VERIFY_CLEANUP_SIGNALS:
        if signal not in cleanup:
            errors.append(f"verification cleanup missing contract signal: {signal}")
    for token in VERIFY_CLEANUP_FORBIDDEN:
        if token in cleanup:
            errors.append(f"forbidden verification cleanup (destroys reusable runtime): {token}")
    if "panji-verify-run" in _read(root / "scripts/verify/cleanup_runner.py"):
        errors.append("verification cleanup references removed entry: panji-verify-run")

    # 19: workflow set must be exactly ['ci.yml'] (no second auto-deploy workflow).
    workflows = sorted(p.name for p in (root / ".github/workflows").glob("*.yml"))
    if workflows != ["ci.yml"]:
        errors.append(f"workflow set must be exactly ['ci.yml'], got {workflows}")


def check(root: Path) -> list[str]:
    errors: list[str] = []
    _check_rule_layout(root, errors)
    _check_agents(root, errors)
    _check_protected_manifest(root, errors)
    _check_rule_semantics(root, errors)
    _check_tool_neutrality(root, errors)
    _check_verification_plans(root, errors)
    _check_ci_is_manual(root, errors)
    _check_always_on_safety(root, errors)
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
