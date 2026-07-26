#!/usr/bin/env python3
"""reports/ 长期报告管理体系静态检查器（Standalone）。

用法：
    python tools/check_reports.py

说明：
- 不导入 backend，不连接数据库。
- 检查 reports/ 目录结构与报告一致性。
- 退出码：0 表示无违规，1 表示有违规。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.resolve()
REPORTS_DIR = ROOT / "reports"
REPORTS_README = REPORTS_DIR / "README.md"
REPORTS_INDEX = REPORTS_DIR / "INDEX.md"
REPORTS_LATEST = REPORTS_DIR / "LATEST.md"
REPORTS_TEMPLATE = REPORTS_DIR / "templates" / "TASK-REPORT-TEMPLATE.md"
REPORTS_CURRENT = REPORTS_DIR / "current"
REPORTS_ARCHIVE = REPORTS_DIR / "archive"
AGENTS_FILE = ROOT / "AGENTS.md"
RULES_40 = ROOT / "rules" / "40-testing-quality.md"
SYNC_OUTBOX = ROOT / "sync" / "outbox"

# 报告命名规则：REPORT-YYYYMMDD-NNN-任务短名称.md
REPORT_NAME_RE = re.compile(
    r"^REPORT-\d{4}\d{2}\d{2}-\d{3}-[a-z0-9]+(-[a-z0-9]+)*\.md$"
)

# 允许的报告状态
ALLOWED_STATUSES = {
    "COMPLETED",
    "PARTIAL",
    "BLOCKED",
    "FAILED",
    "SUPERSEDED",
}

# 报告必须包含的 15 个章节（0-14）
REQUIRED_SECTIONS = [
    "## 0. Report Metadata",
    "## 1. User Request",
    "## 2. Scope",
    "## 3. Starting State",
    "## 4. Actions Performed",
    "## 5. Files Changed",
    "## 6. Behavior Before and After",
    "## 7. Validation",
    "## 8. Git Operations",
    "## 9. Deployment Status",
    "## 10. Database and Migration",
    "## 11. Risks and Known Gaps",
    "## 12. Blockers and User Decisions",
    "## 13. Next Recommended Action",
    "## 14. Final Summary",
]

# 报告中不允许出现的明显秘密标记
SECRET_PATTERNS = [
    r"PRIVATE KEY",
    r"password\s*=",
    r"token\s*=",
    r"secret\s*=",
    r"DATABASE_URL\s*=",
]
SECRET_RE = re.compile("|".join(SECRET_PATTERNS), re.IGNORECASE)


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""


class Violation:
    def __init__(self, rule: str, message: str) -> None:
        self.rule = rule
        self.message = message

    def __str__(self) -> str:
        return f"[{self.rule}] {self.message}"


def collect_reports() -> list[Path]:
    """收集 current/ 与 archive/ 下所有 .md 报告文件（不含 README）。"""
    reports: list[Path] = []
    if REPORTS_CURRENT.exists():
        for p in REPORTS_CURRENT.glob("*.md"):
            if p.name == "README.md":
                continue
            if p.is_file():
                reports.append(p)
    if REPORTS_ARCHIVE.exists():
        for p in REPORTS_ARCHIVE.rglob("*.md"):
            if p.name == "README.md":
                continue
            if p.is_file():
                reports.append(p)
    return sorted(reports)


def collect_current_reports() -> list[Path]:
    """仅收集 current/ 下的报告（不含 README、不含 archive）。"""
    reports: list[Path] = []
    if REPORTS_CURRENT.exists():
        for p in REPORTS_CURRENT.glob("*.md"):
            if p.name == "README.md":
                continue
            if p.is_file():
                reports.append(p)
    return sorted(reports)


def is_legacy_report(path: Path) -> bool:
    """archive/ 下的报告且含 Legacy Report Metadata 头部，视为历史报告。
    历史报告不改写原始内容，跳过模板章节/字段强检查。
    """
    if "archive" not in path.parts:
        return False
    text = read_text(path)
    return "Legacy Report Metadata" in text


def check_required_files_exist() -> list[Violation]:
    violations: list[Violation] = []
    required = [
        REPORTS_README,
        REPORTS_INDEX,
        REPORTS_LATEST,
        REPORTS_TEMPLATE,
        REPORTS_CURRENT / "README.md",
        REPORTS_ARCHIVE / "README.md",
    ]
    for path in required:
        if not path.exists():
            violations.append(
                Violation("required-file-missing", f"{path.relative_to(ROOT)} 不存在")
            )
    return violations


def check_latest_path_exists() -> list[Violation]:
    violations: list[Violation] = []
    if not REPORTS_LATEST.exists():
        return violations  # 已在 required-file-missing 报告
    text = read_text(REPORTS_LATEST)
    # 提取 Path: 字段
    m = re.search(r"^- Path:\s*(.+)$", text, re.MULTILINE)
    if not m:
        violations.append(
            Violation("latest-missing-path", "LATEST.md 缺少 Path 字段")
        )
        return violations
    rel = m.group(1).strip()
    target = ROOT / rel
    if not target.exists():
        violations.append(
            Violation(
                "latest-path-not-exist",
                f"LATEST.md 指向的文件不存在: {rel}",
            )
        )
    return violations


def check_latest_report_id_consistency() -> list[Violation]:
    violations: list[Violation] = []
    if not REPORTS_LATEST.exists():
        return violations
    text = read_text(REPORTS_LATEST)
    # 提取 Report ID
    m_report = re.search(r"^- Report:\s*(.+)$", text, re.MULTILINE)
    m_path = re.search(r"^- Path:\s*(.+)$", text, re.MULTILINE)
    if not m_report or not m_path:
        violations.append(
            Violation("latest-incomplete", "LATEST.md 缺少 Report 或 Path 字段")
        )
        return violations
    report_id = m_report.group(1).strip()
    path_str = m_path.group(1).strip()
    # 文件名应以 report_id 开头
    filename = Path(path_str).name
    if not filename.startswith(report_id):
        violations.append(
            Violation(
                "latest-id-mismatch",
                f"LATEST.md Report ID '{report_id}' 与文件名 '{filename}' 不一致",
            )
        )
    return violations


def check_index_contains_latest() -> list[Violation]:
    violations: list[Violation] = []
    if not REPORTS_LATEST.exists() or not REPORTS_INDEX.exists():
        return violations
    latest_text = read_text(REPORTS_LATEST)
    index_text = read_text(REPORTS_INDEX)
    m = re.search(r"^- Report:\s*(.+)$", latest_text, re.MULTILINE)
    if not m:
        return violations
    report_id = m.group(1).strip()
    if report_id not in index_text:
        violations.append(
            Violation(
                "index-missing-latest",
                f"INDEX.md 未包含最新报告 {report_id}",
            )
        )
    return violations


def check_index_no_duplicate_ids() -> list[Violation]:
    violations: list[Violation] = []
    if not REPORTS_INDEX.exists():
        return violations
    text = read_text(REPORTS_INDEX)
    # 提取表格行中的 Report ID（第一列）
    rows = re.findall(r"^\|\s*([^|\s][^|]*?)\s*\|", text, re.MULTILINE)
    # 跳过表头分隔
    seen: dict[str, int] = {}
    for row in rows:
        rid = row.strip()
        if rid in ("Report ID", "---", "Report"):
            continue
        if rid.startswith("---"):
            continue
        seen[rid] = seen.get(rid, 0) + 1
    for rid, count in seen.items():
        if count > 1:
            violations.append(
                Violation(
                    "index-duplicate-id",
                    f"INDEX.md 中 Report ID '{rid}' 重复 {count} 次",
                )
            )
    return violations


def check_report_naming() -> list[Violation]:
    violations: list[Violation] = []
    for path in collect_reports():
        if not REPORT_NAME_RE.match(path.name):
            violations.append(
                Violation(
                    "report-name-invalid",
                    f"{path.relative_to(ROOT)} 命名不符合 REPORT-YYYYMMDD-NNN-*.md",
                )
            )
    return violations


def check_report_required_sections() -> list[Violation]:
    violations: list[Violation] = []
    for path in collect_reports():
        # 历史报告不改写原始内容，跳过模板章节强检查
        if is_legacy_report(path):
            continue
        text = read_text(path)
        for section in REQUIRED_SECTIONS:
            if section not in text:
                violations.append(
                    Violation(
                        "report-missing-section",
                        f"{path.relative_to(ROOT)} 缺少章节 '{section}'",
                    )
                )
    return violations


def check_report_status_allowed() -> list[Violation]:
    violations: list[Violation] = []
    for path in collect_reports():
        text = read_text(path)
        # 历史报告从 Legacy Report Metadata 提取 Status
        if is_legacy_report(path):
            m = re.search(r"^- Status:\s*(\S+)", text, re.MULTILINE)
        else:
            m = re.search(r"^- Status:\s*(.+)$", text, re.MULTILINE)
        if not m:
            violations.append(
                Violation(
                    "report-missing-status",
                    f"{path.relative_to(ROOT)} 缺少 Status 字段",
                )
            )
            continue
        status = m.group(1).strip()
        # 允许状态值后跟其他说明（如 `COMPLETED` 或 `COMPLETED `）
        # 但取第一个 token
        token = status.split()[0] if status.split() else ""
        if token not in ALLOWED_STATUSES:
            violations.append(
                Violation(
                    "report-status-not-allowed",
                    f"{path.relative_to(ROOT)} 状态 '{token}' 不在允许列表 {sorted(ALLOWED_STATUSES)}",
                )
            )
    return violations


def check_no_secrets_in_reports() -> list[Violation]:
    violations: list[Violation] = []
    for path in collect_reports():
        text = read_text(path)
        # 跳过模板文件中描述性的 "PRIVATE KEY" 等说明
        # 只检查实际值（= 后跟非空内容）
        for line_no, line in enumerate(text.splitlines(), 1):
            if line.strip().startswith("#"):
                continue
            if line.strip().startswith(">"):
                continue
            # 检查是否含秘密标记
            m = SECRET_RE.search(line)
            if m:
                # 排除说明性文字（如 "密码、Token、SSH 私钥"）
                if any(
                    x in line
                    for x in ["密码、Token", "Token、SSH", "SSH 私钥", "数据库连接", "secret="]
                ):
                    if "secret=" in line and "=" in line.split("secret=")[1]:
                        # 实际值存在
                        pass
                    else:
                        continue
                violations.append(
                    Violation(
                        "report-contains-secret",
                        f"{path.relative_to(ROOT)}:{line_no} 可能包含秘密: {line.strip()[:80]}",
                    )
                )
    return violations


def check_sync_outbox_no_reports() -> list[Violation]:
    violations: list[Violation] = []
    if not SYNC_OUTBOX.exists():
        return violations  # 已删除，符合要求
    for p in SYNC_OUTBOX.glob("*.md"):
        if p.is_file():
            violations.append(
                Violation(
                    "sync-outbox-contains-report",
                    f"sync/outbox/{p.name} 不允许继续保存 Markdown 报告，应迁移到 reports/",
                )
            )
    return violations


def check_agents_references_latest() -> list[Violation]:
    violations: list[Violation] = []
    if not AGENTS_FILE.exists():
        return violations
    text = read_text(AGENTS_FILE)
    if "reports/LATEST.md" not in text:
        violations.append(
            Violation(
                "agents-missing-latest-ref",
                "AGENTS.md 未引用 reports/LATEST.md",
            )
        )
    return violations


def check_rules_40_references_reports_readme() -> list[Violation]:
    violations: list[Violation] = []
    if not RULES_40.exists():
        return violations
    text = read_text(RULES_40)
    if "reports/README.md" not in text:
        violations.append(
            Violation(
                "rules-40-missing-reports-readme-ref",
                "rules/40-testing-quality.md 未引用 reports/README.md",
            )
        )
    return violations


def check_report_push_result_and_end_sha() -> list[Violation]:
    violations: list[Violation] = []
    for path in collect_reports():
        # 历史报告不改写原始内容，跳过 Push Result / End SHA 强检查
        if is_legacy_report(path):
            continue
        text = read_text(path)
        # 检查 Push Result 字段非空
        m_push = re.search(r"^- Push Result:\s*(.+)$", text, re.MULTILINE)
        if not m_push:
            violations.append(
                Violation(
                    "report-missing-push-result",
                    f"{path.relative_to(ROOT)} 缺少 Push Result 字段",
                )
            )
        else:
            value = m_push.group(1).strip()
            if not value or value.startswith("（") or value.lower() in {"待填写", "tbd", ""}:
                violations.append(
                    Violation(
                        "report-empty-push-result",
                        f"{path.relative_to(ROOT)} Push Result 为空或占位",
                    )
                )
        # 检查 End SHA 字段非空
        m_end = re.search(r"^- End SHA:\s*(.+)$", text, re.MULTILINE)
        if not m_end:
            violations.append(
                Violation(
                    "report-missing-end-sha",
                    f"{path.relative_to(ROOT)} 缺少 End SHA 字段",
                )
            )
        else:
            value = m_end.group(1).strip()
            if not value or value.startswith("（") or value.lower() in {"待填写", "tbd"}:
                violations.append(
                    Violation(
                        "report-empty-end-sha",
                        f"{path.relative_to(ROOT)} End SHA 为空或占位",
                    )
                )
    return violations


def check_report_deployment_status_exists() -> list[Violation]:
    violations: list[Violation] = []
    for path in collect_reports():
        # 历史报告不改写原始内容，跳过 Deployment Status 章节强检查
        if is_legacy_report(path):
            continue
        text = read_text(path)
        if "## 9. Deployment Status" not in text:
            violations.append(
                Violation(
                    "report-missing-deployment-status",
                    f"{path.relative_to(ROOT)} 缺少 Deployment Status 章节",
                )
            )
    return violations


def check_report_database_section_exists() -> list[Violation]:
    violations: list[Violation] = []
    for path in collect_reports():
        # 历史报告不改写原始内容，跳过 Database and Migration 章节强检查
        if is_legacy_report(path):
            continue
        text = read_text(path)
        if "## 10. Database and Migration" not in text:
            violations.append(
                Violation(
                    "report-missing-database-section",
                    f"{path.relative_to(ROOT)} 缺少 Database and Migration 章节",
                )
            )
    return violations


def main() -> int:
    checks = [
        ("reports/README.md 存在", check_required_files_exist),
        ("LATEST.md Path 指向真实文件", check_latest_path_exists),
        ("LATEST.md Report ID 一致", check_latest_report_id_consistency),
        ("INDEX.md 包含 LATEST 报告", check_index_contains_latest),
        ("INDEX.md Report ID 不重复", check_index_no_duplicate_ids),
        ("报告命名符合 REPORT-YYYYMMDD-NNN-*.md", check_report_naming),
        ("报告包含固定 15 章节", check_report_required_sections),
        ("报告状态使用允许值", check_report_status_allowed),
        ("报告不含明显秘密", check_no_secrets_in_reports),
        ("sync/outbox 不保存 Markdown 报告", check_sync_outbox_no_reports),
        ("AGENTS.md 引用 reports/LATEST.md", check_agents_references_latest),
        ("rules/40 引用 reports/README.md", check_rules_40_references_reports_readme),
        ("报告 Push Result 与 End SHA 非空", check_report_push_result_and_end_sha),
        ("报告 Deployment Status 章节存在", check_report_deployment_status_exists),
        ("报告 Database and Migration 章节存在", check_report_database_section_exists),
    ]

    all_violations: list[Violation] = []
    print("=" * 60)
    print("reports 报告体系检查（check_reports.py）")
    print("=" * 60)
    for name, fn in checks:
        try:
            violations = fn()
        except Exception as exc:  # noqa: BLE001
            violations = [Violation("checker-error", f"{name}: {exc}")]
        status = "PASS" if not violations else "FAIL"
        print(f"[{status}] {name}")
        for v in violations:
            print(f"  {v}")
            all_violations.append(v)

    print("=" * 60)
    if all_violations:
        print(f"结果: FAIL（{len(all_violations)} 项违规）")
        return 1
    print("结果: PASS")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
