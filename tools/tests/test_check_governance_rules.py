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
        ("no_bootstrap_detach", "local deploy entry missing contract signal"),
        ("no_head_restore", "local deploy entry missing contract signal"),
        ("no_previous_sha_tiers", "server deploy implementation missing contract signal"),
        ("no_first_live_detect", "server deploy implementation missing contract signal"),
        ("no_migration_state", "server deploy implementation missing contract signal"),
        ("split_image_tag_group", "server deploy implementation missing contract signal"),
        ("runtime_sha_rename", "breaks single-file bind mount inode"),
        ("migration_failure_recreate", "must not recreate containers"),
        ("global_prune", "global system prune"),
        ("ci_db_test_flag", "forbidden standalone test-db"),
        ("test_db_url", "forbidden standalone test-db"),
        ("postgres_service", "forbidden standalone test-db"),
        ("prod_server", "forbidden production-stage term"),
        ("prod_deploy", "forbidden production-stage term"),
        ("prod_db", "forbidden production-stage term"),
    ],
)
def test_governance_regressions_are_rejected(
    governance_repo: Path, mutation: str, expected: str
) -> None:
    local_entry = governance_repo / "scripts/ops/panji-test-deploy"
    server_impl = governance_repo / "scripts/deploy/panji-deploy.sh"

    if mutation == "no_bootstrap_detach":
        # 回潮：本地入口不再自举到目标 SHA，直接跑服务器当前工作树的脚本。
        path = local_entry
        path.write_text(read_text(path).replace("checkout -f --detach", "checkout -f"), encoding="utf-8")
    elif mutation == "no_head_restore":
        path = local_entry
        path.write_text(read_text(path).replace("trap restore_head EXIT", "# no trap"), encoding="utf-8")
    elif mutation == "no_previous_sha_tiers":
        # 回潮：删掉四级解析的兜底标识，退回「状态文件缺失即强制 migration」。
        path = server_impl
        path.write_text(read_text(path).replace("unknown_baseline", "legacy_missing"), encoding="utf-8")
    elif mutation == "no_first_live_detect":
        path = server_impl
        path.write_text(
            read_text(path).replace("detect_first_live_deploy()", "legacy_detect()"), encoding="utf-8"
        )
    elif mutation == "no_migration_state":
        path = server_impl
        path.write_text(
            read_text(path).replace("handle_migration_failure()", "legacy_fail()"), encoding="utf-8"
        )
    elif mutation == "split_image_tag_group":
        # 回潮：只构建"受影响的那一个"镜像，破坏共享 GIT_SHA tag 组。
        path = server_impl
        path.write_text(
            read_text(path).replace(
                "ENV_IMAGE_TAG_GROUP=(backend frontend worker-capture)",
                "ENV_IMAGE_TAG_GROUP=(backend)",
            ),
            encoding="utf-8",
        )
    elif mutation == "runtime_sha_rename":
        # 回潮：用 rsync 覆盖 RUNTIME_SHA，单文件挂载 inode 失效。
        path = server_impl
        text = read_text(path).replace(
            'printf \'%s\' "${TARGET_SHA}" > "${sha_file}" || fail "无法原地写入 ${sha_file}"',
            'rsync -a /tmp/x "${LIVE_ROOT}/RUNTIME_SHA"',
        )
        path.write_text(text, encoding="utf-8")
    elif mutation == "migration_failure_recreate":
        path = server_impl
        text = read_text(path).replace(
            '    log "结论: migration_failed_requires_inspection"',
            '    ${COMPOSE_CMD} up -d --force-recreate backend\n'
            '    log "结论: migration_failed_requires_inspection"',
        )
        path.write_text(text, encoding="utf-8")
    elif mutation == "global_prune":
        path = server_impl
        text = read_text(path).replace(
            "    run_cmd docker builder prune -f",
            "    run_cmd docker system prune -af",
        )
        path.write_text(text, encoding="utf-8")
    elif mutation == "role_file":
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
    elif mutation == "ci_db_test_flag":
        # 回潮：rules/40 重新出现独立 CI 临时数据库开关
        path = governance_repo / "rules/40-testing-quality.md"
        path.write_text(read_text(path) + "\nPANJI_CI_DB_TEST=1 识别 CI 环境\n", encoding="utf-8")
    elif mutation == "test_db_url":
        # 回潮：rules/40 重新出现独立测试库 URL 变量
        path = governance_repo / "rules/40-testing-quality.md"
        path.write_text(read_text(path) + "\nTEST_DATABASE_URL=postgresql://.../bz_stock_test\n", encoding="utf-8")
    elif mutation == "postgres_service":
        # 回潮：ci.yml 重新出现 postgres:16 service
        path = governance_repo / ".github/workflows/ci.yml"
        path.write_text(
            read_text(path) + "\nservices:\n  postgres:\n    image: postgres:16\n",
            encoding="utf-8",
        )
    elif mutation == "prod_server":
        # 回潮：AGENTS.md 重新出现"生产服务器"
        path = governance_repo / "AGENTS.md"
        path.write_text(read_text(path) + "\n部署到生产服务器。\n", encoding="utf-8")
    elif mutation == "prod_deploy":
        # 回潮：runbook 重新出现"生产部署"
        path = governance_repo / "docs/runbooks/development-deployment.md"
        path.write_text(read_text(path) + "\n执行生产部署。\n", encoding="utf-8")
    elif mutation == "prod_db":
        # 回潮：rules 重新出现"生产库"
        path = governance_repo / "rules/80-deployment-data-safety.md"
        path.write_text(read_text(path) + "\n写入生产库。\n", encoding="utf-8")

    assert any(expected in error for error in check(governance_repo))


def test_historical_technical_identifiers_allowed(governance_repo: Path) -> None:
    """历史技术标识符（panji-prod / APP_ENV=production 等）不误报为生产阶段术语。"""
    assert check(governance_repo) == []


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")
