"""Validate the repository's current governance and deployment contract."""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

CANONICAL_RULES = {
    "README.md",
    "00-core-governance.md",
    "10-product-domain-invariants.md",
    "20-market-data-indicators.md",
    "30-access-security.md",
    "40-testing-quality.md",
    "50-git-development-flow.md",
    "80-deployment-data-safety.md",
    "90-deprecated-forbidden.md",
}
REMOVED_PATHS = {
    "rules/60-trae-work.md",
    "rules/70-trae-cn.md",
    "rules/85-server-directory-boundaries.md",
    "rules/AGENTS-MIGRATION-MAP.md",
    "scripts/ops/panji-deploy-remote.sh",
    "scripts/deploy_live_runtime.sh",
    "scripts/sync_live_runtime.sh",
    ".github/workflows/deploy-production.yml",
    ".github/workflows/nightly.yml",
    ".github/workflows/release.yml",
}
DEPLOY_FILES = {
    "scripts/ops/panji-test-deploy",
    "scripts/deploy/panji-deploy.sh",
}
TOOL_NAMES = ("TRAE CN", "TRAE Work", "CodeBuddy", "Codex", "Cursor", "Copilot")
NEUTRAL_MARKERS = ("不按", "不区分", "同一套", "已删除", "已废弃", "禁止恢复", "原 `rules/")
CHANGE_ID_RE = re.compile(r"CHANGE-\d{8}-\d{3}")


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""


def shell_code(path: Path) -> str:
    return "\n".join(line for line in read(path).splitlines() if not line.lstrip().startswith("#"))


def check(root: Path) -> list[str]:
    errors: list[str] = []
    rules_dir = root / "rules"
    agents = root / "AGENTS.md"

    actual_rules = {path.name for path in rules_dir.glob("*.md")}
    for name in sorted(CANONICAL_RULES - actual_rules):
        errors.append(f"missing canonical rule: rules/{name}")
    for name in sorted(actual_rules - CANONICAL_RULES):
        errors.append(f"non-canonical rule file: rules/{name}")

    agents_text = read(agents)
    if "rules/README.md" not in agents_text:
        errors.append("AGENTS.md must reference rules/README.md")
    for name in sorted(CANONICAL_RULES - {"README.md"}):
        if f"rules/{name}" not in agents_text:
            errors.append(f"AGENTS.md missing rule entry: rules/{name}")
    for removed in sorted(REMOVED_PATHS):
        if (root / removed).exists():
            errors.append(f"removed path restored: {removed}")
        if removed.startswith("rules/") and removed in agents_text:
            errors.append(f"AGENTS.md references removed rule: {removed}")

    effective_files = [agents, *sorted(rules_dir.glob("*.md"))]
    future_state = re.compile(r"\bPLANNED\b|\bPhase\s*\d|^\s*>?\s*状态[：:]")
    for path in effective_files:
        doc_rel = path.relative_to(root)
        for line_no, line in enumerate(read(path).splitlines(), 1):
            if future_state.search(line):
                errors.append(f"future/staged governance state: {doc_rel}:{line_no}")
            if any(tool in line for tool in TOOL_NAMES) and not any(marker in line for marker in NEUTRAL_MARKERS):
                errors.append(f"tool-specific governance: {doc_rel}:{line_no}")

    local_entry = root / "scripts/ops/panji-test-deploy"
    server_impl = root / "scripts/deploy/panji-deploy.sh"
    for rel in sorted(DEPLOY_FILES):
        if not (root / rel).is_file():
            errors.append(f"missing deployment entry: {rel}")

    local_code = shell_code(local_entry)
    server_code = shell_code(server_impl)
    required_local = (
        "origin/dev",
        "merge-base --is-ancestor",
        "panji-prod-preflight",
        "panji-prod-ssh",
        # 首次 Live Mount 自举：必须先 detach 到目标 SHA 再执行目标工作树脚本
        "checkout -f --detach",
        "trap restore_head EXIT",
    )
    required_server = (
        "origin/dev",
        "merge-base --is-ancestor",
        "docker-compose.prod.yml -f docker-compose.live.yml",
        'git diff --name-only "${PREVIOUS_SHA}" "${TARGET_SHA}"',
        "RUNTIME_SHA",
        # 上一真实运行 SHA 解析（P0 修复：禁止用 checkout 后的 repo HEAD 当上一 SHA）
        "resolve_previous_runtime_sha()",
        "PREVIOUS_SHA_SOURCE",
        "unknown_baseline",
        "previous_runtime_sha_unknown",
        "running_version",
        "PANJI_BOOTSTRAP_PREVIOUS_SHA",
        "_resolve_version_sha",
        "_resolve_image_tag_sha",
        # 首次 Live Mount 检测与同步范围提升
        "detect_first_live_deploy()",
        "apply_first_live_deploy_override()",
        # migration 状态机与专用失败路径
        "MIGRATION_ATTEMPTED",
        "MIGRATION_SUCCEEDED",
        "SERVICES_RESTARTED",
        "handle_migration_failure()",
        "migration_failed_requires_inspection",
        # 环境镜像 tag 组整体构建
        "ENV_IMAGE_TAG_GROUP=(backend frontend worker-capture)",
    )
    for signal in required_local:
        if signal not in local_code:
            errors.append(f"local deploy entry missing contract signal: {signal}")
    for signal in required_server:
        if signal not in server_code:
            errors.append(f"server deploy implementation missing contract signal: {signal}")
    forbidden_code = {
        "COMPOSE_CMD_NO_LIVE": "compose without live overlay",
        "DEPLOYMENT_MODE=image": "image deployment mode",
        "origin/main": "main deployment source",
        "HEAD~1": "single-commit change classification",
        "down -v": "volume-destructive compose command",
        # 资源清理边界：永不允许全局 prune 或删除持久卷
        "image prune -a": "global image prune",
        "system prune": "global system prune",
        "volume prune": "volume prune",
        "container prune": "unrelated container prune",
    }
    for token, reason in forbidden_code.items():
        if token in local_code or token in server_code:
            errors.append(f"forbidden deployment implementation ({reason}): {token}")
    # RUNTIME_SHA 是单文件 bind mount 源：必须原地写入，rename/rsync 会换 inode
    write_sha = re.search(r"(?ms)^write_runtime_sha\(\)\s*\{.*?^\}", server_code)
    if write_sha is None:
        errors.append("server deploy implementation missing write_runtime_sha()")
    elif re.search(r"(?:rsync|mv)\s+[^\n]*RUNTIME_SHA", write_sha.group(0)):
        errors.append("RUNTIME_SHA updated via rename/rsync breaks single-file bind mount inode")
    # migration 失败路径不得触发任何容器重建
    migration_fail = re.search(r"(?ms)^handle_migration_failure\(\)\s*\{.*?^\}", server_code)
    if migration_fail is not None and re.search(r"up -d|force-recreate", migration_fail.group(0)):
        errors.append("migration failure path must not recreate containers")
    if re.search(r"\bssh\s+[^\n]*panji-prod", local_code):
        errors.append("local deploy entry bypasses scripts/ops/panji-prod-ssh")
    if re.search(r"https?://\d{1,3}(?:\.\d{1,3}){3}", local_code):
        errors.append("local deploy entry accesses production by raw IP")

    workflow_dir = root / ".github/workflows"
    workflows = sorted(path.name for path in workflow_dir.glob("*.yml"))
    if workflows != ["ci.yml"]:
        errors.append(f"workflow set must be exactly ['ci.yml'], got {workflows}")
    ci = read(workflow_dir / "ci.yml")
    on_match = re.search(r"(?ms)^on:\s*\n(?P<body>(?:^[ \t]+.*\n?)*)", ci)
    on_body = on_match.group("body") if on_match else ""
    if "workflow_dispatch:" not in on_body:
        errors.append("ci.yml must use workflow_dispatch")
    if re.search(r"(?m)^\s+(push|pull_request|schedule):", on_body):
        errors.append("ci.yml contains an automatic trigger")
    for token in ("panji-test-deploy", "panji-deploy.sh", "ssh panji-prod"):
        if token in shell_code(workflow_dir / "ci.yml"):
            errors.append(f"ci.yml contains deployment action: {token}")

    deploy_runbooks = sorted(path.name for path in (root / "docs/runbooks").glob("*deployment*.md"))
    if deploy_runbooks != ["development-deployment.md"]:
        errors.append(
            "current deployment runbook must be unique: "
            f"expected development-deployment.md, got {deploy_runbooks}"
        )
    runbook = read(root / "docs/runbooks/development-deployment.md")
    for signal in ("scripts/ops/panji-test-deploy", "scripts/deploy/panji-deploy.sh", "--dry-run"):
        if signal not in runbook:
            errors.append(f"deployment runbook missing signal: {signal}")

    change_dir = root / "docs/changes/2026"
    change_files = sorted(change_dir.glob("CHANGE-*.md"))
    ids = [match.group(0) for path in change_files if (match := CHANGE_ID_RE.match(path.name))]
    for change_id, count in Counter(ids).items():
        if count > 1:
            errors.append(f"duplicate Change ID: {change_id}")
    known_ids = set(ids)
    reference_files = [
        agents,
        *sorted(rules_dir.glob("*.md")),
        root / "docs/prd/80-system-runtime.md",
        root / "docs/maps/80-system-runtime.md",
        root / "docs/runbooks/development-deployment.md",
        root / "docs/changes/INDEX.md",
        *change_files,
    ]
    dangling_exemptions = ("从未落库", "悬空引用", "已并入", "历史记录", "重编号", "撞号")
    for path in reference_files:
        if "docs/changes/records" in str(path):
            continue
        for line_no, line in enumerate(read(path).splitlines(), 1):
            if any(marker in line for marker in dangling_exemptions):
                continue
            for change_id in CHANGE_ID_RE.findall(line):
                if change_id not in known_ids:
                    errors.append(f"dangling Change reference: {path.relative_to(root)}:{line_no} {change_id}")

    # [权限模型 V2] 独立/临时测试数据库路线永久禁止（只扫描活跃文件，允许在禁止清单/历史说明中）
    # 活跃文件：AGENTS / rules / docs(prd|maps|runbooks) / conftest / ci.yml / scripts
    _FORBIDDEN_TEST_DB_TOKENS = {
        "PANJI_CI_DB_TEST": "独立 CI 临时数据库开关",
        "TEST_DATABASE_URL": "独立测试数据库 URL",
        "bz_stock_test": "独立测试数据库名",
        "postgres-integration-tests": "CI 独立 PG 集成 job",
        "CI 临时 Postgres": "CI 临时 Postgres 唯一例外",
        "一次性临时 Postgres": "一次性临时 Postgres 路线",
    }
    active_doc_paths = [
        agents,
        *sorted(rules_dir.glob("*.md")),
        root / "docs/prd",
        root / "docs/maps",
        root / "docs/runbooks",
        root / "backend/tests/conftest.py",
        root / ".github/workflows/ci.yml",
    ]
    for base in active_doc_paths:
        if base.is_dir():
            files = sorted(base.rglob("*.md"))
        elif base.is_file():
            files = [base]
        else:
            continue
        for path in files:
            rel = path.relative_to(root)
            # 跳过历史 CHANGE（docs/changes）与禁止清单（rules/90）
            if "changes" in rel.parts or rel.name == "90-deprecated-forbidden.md":
                continue
            text = read(path)
            # ci.yml：额外检测独立 postgres 测试 service 块（image: postgres:16）
            if rel.name == "ci.yml" and re.search(
                r"(?m)^\s*services:\s*\n\s*postgres:\s*\n\s*image:\s*postgres:16", text
            ):
                errors.append(
                    f"forbidden standalone test-db (CI 独立 postgres:16 测试 service): {rel}"
                )
            for token, reason in _FORBIDDEN_TEST_DB_TOKENS.items():
                if token in text:
                    errors.append(
                        f"forbidden standalone test-db ({reason}): {rel} contains {token}"
                    )

    # [开发测试阶段] 禁止当前有效文档使用"生产阶段"业务术语（历史技术标识符 panji-prod 等除外）
    _FORBIDDEN_PROD_TERMS = (
        "生产服务器",
        "生产部署",
        "生产库",
        "正式库",
        "生产入口",
        "生产任务",
        "生产队列",
        "生产身份",
        "生产修改与部署版本合同",
    )
    for base in [agents, *sorted(rules_dir.glob("*.md")),
                 root / "docs/prd", root / "docs/maps", root / "docs/runbooks"]:
        if base.is_dir():
            files = sorted(base.rglob("*.md"))
        elif base.is_file():
            files = [base]
        else:
            continue
        for path in files:
            rel = path.relative_to(root)
            # 排除历史 CHANGE / archive / 明确的历史技术标识符解释段
            if "changes" in rel.parts or "archive" in rel.parts:
                continue
            text = read(path)
            for term in _FORBIDDEN_PROD_TERMS:
                if term in text:
                    errors.append(
                        f"forbidden production-stage term ({term}): {rel}（应为开发测试阶段术语，如远程开发运行服务器/共享开发业务数据库）"
                    )

    # [开发测试阶段] 盘后远程开发运行 Runbook 禁止 dev→main 合并 / 自动部署 / main HEAD 运行合同
    # "不自动部署"/"禁止自动部署"是合规声明，不误报（用负向后顾排除"不/禁止"前缀）。
    _FORBIDDEN_RUNBOOK_PATTERNS = (
        (r"dev 合并到 main", "dev→main 合并"),
        (r"(?<!不)(?<!禁止)自动部署", "自动部署流程"),
        (r"main HEAD", "main HEAD 运行合同"),
        (r"生产运行合同", "生产运行合同"),
    )
    after_close_runbook = root / "docs/runbooks/after-close-remote-development-run.md"
    if after_close_runbook.exists():
        runbook_text = read(after_close_runbook)
        for pattern, reason in _FORBIDDEN_RUNBOOK_PATTERNS:
            if re.search(pattern, runbook_text):
                errors.append(
                    f"forbidden runbook legacy flow ({reason}): docs/runbooks/after-close-remote-development-run.md"
                )

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).parent.parent)
    args = parser.parse_args(argv)
    errors = check(args.root.resolve())
    if errors:
        print(f"Governance check failed ({len(errors)}):")
        for error in errors:
            print(f"  - {error}")
        return 1
    print("Governance check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
