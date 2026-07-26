#!/usr/bin/env python3
"""项目治理规则静态检查器（Standalone）。

用法：
    python tools/check_governance_rules.py

说明：
- 不导入 backend，不连接数据库。
- 检查 rules/ 与 AGENTS.md 的治理一致性。
- 退出码：0 表示无违规，1 表示有违规。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.resolve()
AGENTS_FILE = ROOT / "AGENTS.md"
RULES_DIR = ROOT / "rules"
MAP_FILE = RULES_DIR / "AGENTS-MIGRATION-MAP.md"

REQUIRED_RULES_FILES = [
    "README.md",
    "00-core-governance.md",
    "10-product-domain-invariants.md",
    "20-market-data-indicators.md",
    "30-access-security.md",
    "40-testing-quality.md",
    "50-git-development-flow.md",
    "60-trae-work.md",
    "70-trae-cn.md",
    "80-deployment-data-safety.md",
    "85-server-directory-boundaries.md",
    "90-deprecated-forbidden.md",
    "AGENTS-MIGRATION-MAP.md",
]

AGENTS_MAX_LINES = 300

# 必须覆盖的 AGENTS 章节（§一至§十一 + §七.1-23）
REQUIRED_AGENTS_SECTIONS = [
    "§一", "§二", "§三", "§四", "§五", "§六", "§七",
    "§八", "§九", "§十", "§十一",
]
REQUIRED_HARD_RULES = [f"§七.{i}" for i in range(1, 24)]

# 不允许出现的虚假 CURRENT 语句
FORBIDDEN_FALSE_CURRENT = [
    "TRAE Work 可以切换分支",
    "TRAE Work 固定直接工作在 dev",
    "自动部署已启用",
    "自动部署已经启用",
    "/opt/panji-deploy 已存在",
    "Capability V2 已成为 CURRENT",
    "Capability V2 已经成为 CURRENT",
    "rules 已替代 AGENTS",
    "rules 已经替代 AGENTS",
]

PLACEHOLDER_RE = re.compile(r"待填写")
LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""


def collect_rules_files() -> list[Path]:
    if not RULES_DIR.exists():
        return []
    return sorted(p for p in RULES_DIR.glob("*.md") if p.is_file())


class Violation:
    def __init__(self, rule: str, message: str) -> None:
        self.rule = rule
        self.message = message

    def __str__(self) -> str:
        return f"[{self.rule}] {self.message}"


def check_required_files_exist() -> list[Violation]:
    violations: list[Violation] = []
    for name in REQUIRED_RULES_FILES:
        path = RULES_DIR / name
        if not path.exists():
            violations.append(Violation("required-file-missing", f"rules/{name} 不存在"))
    return violations


def check_agents_references_rules_readme() -> list[Violation]:
    violations: list[Violation] = []
    text = read_text(AGENTS_FILE)
    if "rules/README.md" not in text:
        violations.append(
            Violation("agents-missing-rules-readme-ref", "AGENTS.md 未引用 rules/README.md")
        )
    return violations


def check_agents_has_entry_for_each_rules_file() -> list[Violation]:
    violations: list[Violation] = []
    text = read_text(AGENTS_FILE)
    for name in REQUIRED_RULES_FILES:
        if name == "AGENTS-MIGRATION-MAP.md":
            # 索引文件不要求 AGENTS 必须有独立入口，但应在 AGENTS 或 README 中可被引用
            continue
        ref = f"rules/{name}"
        if ref not in text:
            violations.append(
                Violation(
                    "agents-missing-rules-entry",
                    f"AGENTS.md 未包含 {ref} 入口",
                )
            )
    return violations


def check_agents_line_count() -> list[Violation]:
    violations: list[Violation] = []
    text = read_text(AGENTS_FILE)
    line_count = len(text.splitlines())
    if line_count > AGENTS_MAX_LINES:
        violations.append(
            Violation(
                "agents-too-long",
                f"AGENTS.md 行数 {line_count} 超过上限 {AGENTS_MAX_LINES}",
            )
        )
    return violations


def check_migration_map_coverage() -> list[Violation]:
    violations: list[Violation] = []
    text = read_text(MAP_FILE)
    for section in REQUIRED_AGENTS_SECTIONS:
        if section not in text:
            violations.append(
                Violation(
                    "migration-map-missing-section",
                    f"AGENTS-MIGRATION-MAP.md 缺少章节 {section}",
                )
            )
    for rule in REQUIRED_HARD_RULES:
        if rule not in text:
            violations.append(
                Violation(
                    "migration-map-missing-hard-rule",
                    f"AGENTS-MIGRATION-MAP.md 缺少硬规则 {rule}",
                )
            )
    return violations


def check_rules_readme_not_pending() -> list[Violation]:
    violations: list[Violation] = []
    readme = RULES_DIR / "README.md"
    text = read_text(readme)
    # 不允许 README 仍声称 rules 尚未生效
    forbidden = [
        "rules 尚未生效",
        "rules/ 尚未生效",
        "rules 尚未替代",
        "Phase 1 并行验证状态",
    ]
    for phrase in forbidden:
        if phrase in text:
            violations.append(
                Violation(
                    "rules-readme-still-pending",
                    f"rules/README.md 仍包含 '{phrase}'，应反映 Phase 2 已激活",
                )
            )
    return violations


def check_no_false_current() -> list[Violation]:
    violations: list[Violation] = []
    scan_files = [AGENTS_FILE] + collect_rules_files()
    for path in scan_files:
        text = read_text(path)
        for phrase in FORBIDDEN_FALSE_CURRENT:
            if phrase in text:
                rel = path.relative_to(ROOT) if path.is_absolute() else path
                violations.append(
                    Violation(
                        "false-current-statement",
                        f"{rel} 包含虚假 CURRENT 语句: '{phrase}'",
                    )
                )
    return violations


def check_no_placeholder() -> list[Violation]:
    violations: list[Violation] = []
    scan_files = [AGENTS_FILE] + collect_rules_files()
    for path in scan_files:
        text = read_text(path)
        for match in PLACEHOLDER_RE.finditer(text):
            line = text[: match.start()].count("\n") + 1
            rel = path.relative_to(ROOT) if path.is_absolute() else path
            # 排除描述规则文本本身（如 "无'待填写'占位符"）
            line_content = text.splitlines()[line - 1] if line <= len(text.splitlines()) else ""
            if "无" in line_content and "占位符" in line_content:
                continue
            if "禁止" in line_content and "占位符" in line_content:
                continue
            violations.append(
                Violation(
                    "placeholder-found",
                    f"{rel}:{line} 存在 '待填写' 占位符",
                )
            )
    return violations


def check_rules_internal_links() -> list[Violation]:
    violations: list[Violation] = []
    for path in collect_rules_files():
        text = read_text(path)
        for _text_match, raw_link in LINK_RE.findall(text):
            link = raw_link.strip()
            if not link or link.startswith(("#", "http://", "https://", "mailto:")):
                continue
            if link.startswith(("file://", "mailto:", "javascript:")):
                continue
            target = path.parent / link.split("#", 1)[0]
            if not target.exists():
                rel = path.relative_to(ROOT) if path.is_absolute() else path
                violations.append(
                    Violation(
                        "rules-broken-link",
                        f"{rel} 引用文件不存在: {raw_link}",
                    )
                )
    return violations


def check_sync_not_runtime_dependency() -> list[Violation]:
    violations: list[Violation] = []
    sync_runtime_indicators = [
        ("backend", ROOT / "backend"),
        ("frontend/src", ROOT / "frontend" / "src"),
        ("scripts", ROOT / "scripts"),
    ]
    # 匹配 sync/ 后跟路径字符（至少一个字母/下划线开头，避免匹配正则 flag 如 sync/i）
    # 要求 sync/ 后跟 [A-Za-z_] 开头，且后跟更多路径字符或非正则 flag 上下文
    # 排除 sync/i、sync/g、sync/m、sync/s、sync/u、sync/y 等单字母正则 flag（后跟非路径字符）
    sync_ref_re = re.compile(r"sync/[A-Za-z_][A-Za-z0-9_./-]*")
    scan_targets: list[tuple[str, Path]] = [
        ("AGENTS.md", AGENTS_FILE),
    ]
    # rules/
    for p in collect_rules_files():
        scan_targets.append((str(p.relative_to(ROOT)), p))
    # docs/current, docs/maps
    for d in [ROOT / "docs" / "current", ROOT / "docs" / "maps"]:
        if d.exists():
            for p in d.glob("*.md"):
                scan_targets.append((str(p.relative_to(ROOT)), p))
    # Compose files
    for fname in ["docker-compose.yml", "docker-compose.prod.yml", "docker-compose.live.yml"]:
        p = ROOT / fname
        if p.exists():
            scan_targets.append((fname, p))

    for label, path in scan_targets:
        text = read_text(path)
        for match in sync_ref_re.finditer(text):
            line = text[: match.start()].count("\n") + 1
            line_content = text.splitlines()[line - 1] if line <= len(text.splitlines()) else ""
            # 允许在描述性文本中提到 sync/ 不是真源
            if any(kw in line_content for kw in ["不是正式真源", "不得作为运行时依赖", "不作为运行时真源", "非正式真源", "临时中转站", "不是真源", "草案", "不依赖"]):
                continue
            violations.append(
                Violation(
                    "sync-runtime-dependency",
                    f"{label}:{line} 引用 sync/ 路径，疑似作为运行时依赖: {match.group(0)}",
                )
            )
    # backend / frontend / scripts 目录扫描 import 引用
    # 只检测明确的 import / require / open / read 等运行时引用，避免误报正则 flag
    runtime_ref_re = re.compile(
        r"(?:import\s+|from\s+|require\(\s*['\"]|open\(\s*['\"]|read_text\(\s*['\"]|read\(\s*['\"]|Path\(\s*['\"])(sync/[A-Za-z_][A-Za-z0-9_./-]*)"
    )
    for label, directory in sync_runtime_indicators:
        if not directory.exists():
            continue
        for p in directory.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix not in {".py", ".ts", ".tsx", ".js", ".jsx", ".yml", ".yaml"}:
                continue
            if any(part in {"node_modules", "__pycache__", ".venv", "dist", ".git"} for part in p.parts):
                continue
            text = read_text(p)
            for match in runtime_ref_re.finditer(text):
                rel = p.relative_to(ROOT)
                violations.append(
                    Violation(
                        "sync-runtime-dependency",
                        f"{rel} 运行时引用 sync/ 路径: {match.group(1)}",
                    )
                )
    return violations


def check_trae_work_required_phrases() -> list[Violation]:
    violations: list[Violation] = []
    work_file = RULES_DIR / "60-trae-work.md"
    text = read_text(work_file)
    required = [
        "trae/agent-",
        "origin/dev",
        "git push origin HEAD:dev",
        "禁止 force push",
    ]
    for phrase in required:
        if phrase not in text:
            violations.append(
                Violation(
                    "trae-work-missing-required-phrase",
                    f"rules/60-trae-work.md 缺少必要内容: '{phrase}'",
                )
            )
    return violations


def check_autodeploy_still_planned() -> list[Violation]:
    violations: list[Violation] = []
    readme = RULES_DIR / "README.md"
    text = read_text(readme)
    if "PLANNED" not in text:
        violations.append(
            Violation(
                "autodeploy-not-marked-planned",
                "rules/README.md 未声明任何 PLANNED 项（自动部署应保持 PLANNED）",
            )
        )
    return violations


def main() -> int:
    violations: list[Violation] = []

    violations.extend(check_required_files_exist())
    violations.extend(check_agents_references_rules_readme())
    violations.extend(check_agents_has_entry_for_each_rules_file())
    violations.extend(check_agents_line_count())
    violations.extend(check_migration_map_coverage())
    violations.extend(check_rules_readme_not_pending())
    violations.extend(check_no_false_current())
    violations.extend(check_no_placeholder())
    violations.extend(check_rules_internal_links())
    violations.extend(check_sync_not_runtime_dependency())
    violations.extend(check_trae_work_required_phrases())
    violations.extend(check_autodeploy_still_planned())

    print("=" * 60)
    print("治理规则检查（check_governance_rules.py）")
    print("=" * 60)

    if violations:
        print(f"\n发现 {len(violations)} 个违规：\n")
        for v in violations:
            print(f"  {v}")
        print("\n" + "=" * 60)
        print(f"结果: FAIL（{len(violations)} 个违规）")
        print("=" * 60)
        return 1

    print("\n所有治理规则检查通过。")
    print("=" * 60)
    print("结果: PASS")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
