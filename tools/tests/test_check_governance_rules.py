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
        "scripts/verify/evidence_manifest.json",
        "scripts/verify/pytest_evidence_plugin.py",
        "scripts/verify/cleanup_runner.py",
        "scripts/verify/prepare_verify_environment.py",
        "scripts/verify/plans/targeted-pg.json",
        "scripts/verify/plans/migration-roundtrip.json",
        "scripts/verify/plans/full-closure.json",
        "scripts/ops/panji-test-deploy",
        "scripts/deploy/panji-deploy.sh",
        "docker-compose.prod.yml",
        "docker-compose.live.yml",
        ".github/workflows/ci.yml",
    ):
        _copy(ROOT / relative, target / relative)
    manifest = json.loads((ROOT / "scripts/verify/evidence_manifest.json").read_text())
    for contract in manifest["contracts"]:
        for selector in contract["test_selectors"]:
            relative = Path("backend") / selector.split("::", 1)[0]
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
    p.write_text(p.read_text().replace("Evidence must match the claim", "Evidence is optional"))
    errors = check(governance_repo)
    assert any("missing correctness gate" in error for error in errors)


def test_forbidden_rule_file_cannot_be_restored(governance_repo: Path) -> None:
    p = governance_repo / "rules/30-access-security.md"
    p.write_text("# second authority\n")
    errors = check(governance_repo)
    assert any("forbidden governance file exists" in error for error in errors)


def test_hardening_rule_must_remain_triggered_only(governance_repo: Path) -> None:
    p = governance_repo / "rules/70-hardening-release.md"
    p.write_text(p.read_text().replace("不是 Exploration 默认流程", "是 Exploration 默认流程"))
    errors = check(governance_repo)
    assert any("70-hardening-release" in error for error in errors)


def test_registered_plan_set_is_required(governance_repo: Path) -> None:
    (governance_repo / "scripts/verify/plans/targeted-pg.json").unlink()
    errors = check(governance_repo)
    assert any("missing registered verification plan" in error for error in errors)


def test_engineering_implementation_rule_routing(governance_repo: Path) -> None:
    # 25 已注册为 Active Rule 且内容契约 marker 存在时，整体 contract 必须通过。
    assert "25-engineering-implementation.md" in MODULE.ACTIVE_RULES
    assert check(governance_repo) == []

    # 篡改 AGENTS.md 移除 25 引用，必须被 _check_agents 检出。
    agents = governance_repo / "AGENTS.md"
    agents.write_text(agents.read_text().replace(
        "rules/25-engineering-implementation.md", "rules/99-missing.md"))
    errors = check(governance_repo)
    assert any("missing active rule reference: rules/25-engineering-implementation.md" in e for e in errors)

    # 篡改 25 内容移除 production-path owner marker，必须被语义闸门检出。
    rule = governance_repo / "rules/25-engineering-implementation.md"
    rule.write_text(rule.read_text().replace("Production path reuse", "Copied test path"))
    errors = check(governance_repo)
    assert any("25-engineering-implementation.md missing contract marker" in e for e in errors)


def test_evidence_manifest_duplicate_contract_is_rejected(governance_repo: Path) -> None:
    path = governance_repo / "scripts/verify/evidence_manifest.json"
    data = json.loads(path.read_text())
    data["contracts"].append(dict(data["contracts"][0]))
    path.write_text(json.dumps(data))
    assert any("duplicate evidence contract_id" in e for e in check(governance_repo))


def test_evidence_manifest_missing_selector_is_rejected(governance_repo: Path) -> None:
    path = governance_repo / "scripts/verify/evidence_manifest.json"
    data = json.loads(path.read_text())
    data["contracts"][0]["test_selectors"] = ["tests/test_missing_contract.py"]
    path.write_text(json.dumps(data))
    assert any("registered evidence selector is missing" in e for e in check(governance_repo))


def test_evidence_manifest_glob_is_rejected(governance_repo: Path) -> None:
    path = governance_repo / "scripts/verify/evidence_manifest.json"
    data = json.loads(path.read_text())
    data["contracts"][0]["test_selectors"] = ["tests/test_pg_*.py"]
    path.write_text(json.dumps(data))
    assert any("selector must be explicit" in e for e in check(governance_repo))


def test_evidence_manifest_gate_requires_evidence(governance_repo: Path) -> None:
    path = governance_repo / "scripts/verify/evidence_manifest.json"
    data = json.loads(path.read_text())
    for contract in data["contracts"]:
        contract["gate"] = ["targeted-pg"]
    path.write_text(json.dumps(data))
    assert any("full-closure" in e and "no required evidence" in e for e in check(governance_repo))


def test_verify_attempt_cannot_restore_hardcoded_selector(governance_repo: Path) -> None:
    path = governance_repo / "scripts/verify/verify_attempt.py"
    path.write_text(path.read_text().replace(
        '"pytest",\n                "-p",',
        '"pytest",\n                "tests/test_pg_legacy.py",\n                "-p",',
    ))
    assert any("must not hardcode test selectors" in e for e in check(governance_repo))


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


# ---------------------------------------------------------------------------
# P1: panji-verify default plan routing (Exploration = targeted-pg).
# These are source-level static regression tests (panji-verify is a shell entry
# whose argument parsing must not be exercised by launching a remote SSH session).
# ---------------------------------------------------------------------------

def _panji_verify_source() -> str:
    return (ROOT / "scripts/ops/panji-verify").read_text(encoding="utf-8")


def test_panji_verify_default_plan_is_targeted_pg() -> None:
    src = _panji_verify_source()
    assert 'PLAN="full-closure"' not in src, "full-closure must not be the default plan"
    assert 'PLAN="targeted-pg"' in src, "targeted-pg must be the default plan"
    # 默认赋值必须位于 --plan 参数解析（while 循环内的 "--plan)" 分支）之前，
    # 保证无 --plan 时使用 targeted-pg。不能匹配 usage 字符串中的 "--plan"。
    default_idx = src.index('PLAN="targeted-pg"')
    param_idx = src.index("--plan)")
    assert default_idx < param_idx


def test_panji_verify_explicit_plan_routing() -> None:
    src = _panji_verify_source()
    for plan in ("targeted-pg", "migration-roundtrip", "full-closure"):
        # --plan 必须能显式覆盖默认；三个 plan 都必须被 case 注册。
        assert "--plan)" in src, "--plan must parse args"
        assert "--plan) PLAN=" in src, "--plan must assign PLAN"
        assert plan in src, f"registered plan missing: {plan}"
    # case 分支必须包含三个合法 plan（不匹配即 fail closed exit 80）。
    assert "targeted-pg|migration-roundtrip|full-closure" in src


def test_panji_verify_arbitrary_plan_fails_closed() -> None:
    src = _panji_verify_source()
    # 未注册 plan 必须落入 fail-closed 分支（unregistered plan + exit 80）。
    assert "unregistered plan" in src
    assert "exit 80" in src
    # fail-closed 分支必须位于合法 plan 注册之后。
    assert src.index("targeted-pg|migration-roundtrip|full-closure") < src.index("unregistered plan")


# ---------------------------------------------------------------------------
# P0: Always-On Safety guards restored. Mutation-style regression tests:
# breaking a concrete machine contract must be detected by the checker.
# ---------------------------------------------------------------------------

def test_always_on_safety_local_detach_head_restore(governance_repo: Path) -> None:
    p = governance_repo / "scripts/ops/panji-test-deploy"
    p.write_text(p.read_text().replace("trap restore_head EXIT", "# removed head restore"))
    errors = check(governance_repo)
    assert any("missing contract signal: trap restore_head EXIT" in e for e in errors)


def test_always_on_safety_previous_runtime_sha_identity(governance_repo: Path) -> None:
    p = governance_repo / "scripts/deploy/panji-deploy.sh"
    p.write_text(p.read_text().replace("resolve_previous_runtime_sha()", "resolve_old()"))
    errors = check(governance_repo)
    assert any("resolve_previous_runtime_sha()" in e for e in errors)


def test_always_on_safety_migration_state_machine(governance_repo: Path) -> None:
    p = governance_repo / "scripts/deploy/panji-deploy.sh"
    p.write_text(p.read_text().replace("MIGRATION_ATTEMPTED", "MIG_DONE"))
    errors = check(governance_repo)
    assert any("MIGRATION_ATTEMPTED" in e for e in errors)


def test_always_on_safety_migration_failure_no_recreate(governance_repo: Path) -> None:
    # 构造一个在 migration 失败路径里执行 up -d 的实现，必须被检出。
    p = governance_repo / "scripts/deploy/panji-deploy.sh"
    code = p.read_text()
    code = code.replace("handle_migration_failure() {", "handle_migration_failure() {")
    # 插入一段 handle_migration_failure 含 up -d 的实现，替换原失败路径。
    marker_start = code.index("handle_migration_failure() {")
    # 用一段完整函数体替换，使其包含 up -d
    injection = (
        "handle_migration_failure() {\n"
        "  echo 'migration failed' >&2\n"
        "  docker compose up -d --force-recreate backend\n"
        "  return 1\n"
        "}\n"
    )
    # 找原函数结束的大括号，粗略替换：截取到下一个 '}\n' 结尾的独立函数块
    end = code.index("\n}\n", marker_start) + len("\n}\n")
    code = code[:marker_start] + injection + code[end:]
    p.write_text(code)
    errors = check(governance_repo)
    assert any("migration failure path must not recreate containers" in e for e in errors)


def test_always_on_safety_runtime_sha_inode(governance_repo: Path) -> None:
    p = governance_repo / "scripts/deploy/panji-deploy.sh"
    # 把 write_runtime_sha() 实现替换为用 rsync 更新 RUNTIME_SHA（破坏单文件 bind mount inode）。
    code = p.read_text()
    if "write_runtime_sha()" not in code:
        assert any("missing write_runtime_sha()" in e for e in check(governance_repo))
        return
    start = code.index("write_runtime_sha() {")
    end = code.index("\n}\n", start) + len("\n}\n")
    mutated = (
        "write_runtime_sha() {\n"
        "  rsync /tmp/runtime-sha RUNTIME_SHA\n"
        "  return 0\n"
        "}\n"
    )
    p.write_text(code[:start] + mutated + code[end:])
    errors = check(governance_repo)
    assert any("rename/rsync breaks single-file bind mount" in e for e in errors)


def test_always_on_safety_forbidden_global_prune(governance_repo: Path) -> None:
    p = governance_repo / "scripts/deploy/panji-deploy.sh"
    p.write_text(p.read_text() + "\ndocker image prune -a\n")
    errors = check(governance_repo)
    assert any("forbidden deployment code" in e and "image prune" in e for e in errors)


def test_always_on_safety_forbidden_standalone_test_db(governance_repo: Path) -> None:
    p = governance_repo / "rules/30-security-data-safety.md"
    p.write_text(p.read_text() + "\nCI 使用 TEST_DATABASE_URL 连接独立测试库。\n")
    errors = check(governance_repo)
    assert any("forbidden standalone test-db" in e for e in errors)


def test_always_on_safety_compose_resource_guard(governance_repo: Path) -> None:
    p = governance_repo / "docker-compose.prod.yml"
    text = p.read_text().replace("mem_limit:", "memory_limit:")
    p.write_text(text)
    errors = check(governance_repo)
    assert any("missing resource limit" in e for e in errors)


def test_always_on_safety_deploy_oom_evidence(governance_repo: Path) -> None:
    p = governance_repo / "scripts/deploy/panji-deploy.sh"
    p.write_text(p.read_text().replace("OOMKilled", "OOMState"))
    errors = check(governance_repo)
    assert any("OOM check" in e for e in errors)


def test_always_on_safety_cleanup_fail_closed(governance_repo: Path) -> None:
    p = governance_repo / "scripts/verify/cleanup_runner.py"
    p.write_text(p.read_text().replace("blocked_cleanup", "cleanup_ok"))
    errors = check(governance_repo)
    assert any("verification cleanup missing contract signal: blocked_cleanup" in e for e in errors)


def test_always_on_safety_protected_path_exists(governance_repo: Path) -> None:
    (governance_repo / "scripts/ops/panji-verify").unlink()
    errors = check(governance_repo)
    assert any("protected governance path does not exist: scripts/ops/panji-verify" in e for e in errors)


def test_always_on_safety_workflow_set_no_second_workflow(governance_repo: Path) -> None:
    p = governance_repo / ".github/workflows/ci.yml"
    p.write_text("# placeholder\n")
    extra = governance_repo / ".github/workflows/deploy.yml"
    extra.write_text("name: deploy\n", encoding="utf-8")
    errors = check(governance_repo)
    assert any("workflow set must be exactly" in e for e in errors)


def test_always_on_safety_workflow_yaml_bypass_is_detected(governance_repo: Path) -> None:
    # GitHub Actions 同时支持 *.yml 与 *.yaml；新增 deploy.yaml 必须被检出。
    p = governance_repo / ".github/workflows/ci.yml"
    p.write_text("# placeholder\n")
    extra = governance_repo / ".github/workflows/deploy.yaml"
    extra.write_text("name: deploy\n", encoding="utf-8")
    errors = check(governance_repo)
    assert any("workflow set must be exactly" in e for e in errors)


def test_always_on_safety_default_plan_must_be_targeted_pg(governance_repo: Path) -> None:
    # 把默认赋值 PLAN="targeted-pg" 改回 PLAN="full-closure"，checker 必须 FAIL。
    p = governance_repo / "scripts/ops/panji-verify"
    p.write_text(p.read_text().replace('PLAN="targeted-pg"', 'PLAN="full-closure"'))
    errors = check(governance_repo)
    assert any("default plan must not be PLAN=\"full-closure\"" in e for e in errors)
    assert any("default plan must be PLAN=\"targeted-pg\"" in e for e in errors)


def test_always_on_safety_default_plan_assignment_required(governance_repo: Path) -> None:
    # 删除默认赋值 PLAN="targeted-pg"（例如完全移除默认），checker 必须 FAIL。
    p = governance_repo / "scripts/ops/panji-verify"
    p.write_text(p.read_text().replace('PLAN="targeted-pg"', 'PLAN=""'))
    errors = check(governance_repo)
    assert any("default plan must be PLAN=\"targeted-pg\"" in e for e in errors)
