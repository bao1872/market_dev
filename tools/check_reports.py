#!/usr/bin/env python3
"""reports/ 长期报告管理体系静态检查器（Standalone）。

用法：
    python tools/check_reports.py

说明：
- 不导入 backend，不连接数据库。
- 检查 reports/ 目录结构与报告一致性。
- 退出码：0 表示无违规，1 表示有违规。

15 个检查组，覆盖 SHA 完整性、秘密检测、模板章节、状态、命名等约束。
"""

from __future__ import annotations

import re
import subprocess
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

# 三字段 SHA 语义（替代旧的单一 End SHA）
SHA_FIELDS = ["Base SHA", "Implementation SHA", "Report Published Through SHA"]
SHA_HEX_RE = re.compile(r"^[0-9a-f]{40}$")

# ──────────────────────────────────────────────────────────────
# 秘密检测
# ──────────────────────────────────────────────────────────────

# 允许的占位值（大小写不敏感）
SECRET_PLACEHOLDERS = {
    "<redacted>",
    "redacted",
    "***",
    "example",
    "placeholder",
    "xxx",
    "xxxx",
    "<value>",
    "<secret>",
    "<token>",
    "<password>",
    "<sha>",
    "<commit>",
    "n/a",
    "na",
}

# 赋值模式：key = value（等号后必须跟 ASCII 值才视为赋值）
# 不匹配 "禁止保存 password=" 这类无值的说明文字
# 不匹配 "password= 字段是否存在" 这类中文描述（value 必须以 ASCII 字符开头）
ASSIGN_RE = re.compile(
    r"\b(password|token|secret|database_url)\s*=\s*([A-Za-z0-9_\-:.+/=\"'`<>*]+)",
    re.IGNORECASE,
)

# PEM 私钥起始标记（无条件 FAIL）
PEM_PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")


def is_placeholder(value: str) -> bool:
    """判断赋值右侧是否为允许的占位值。"""
    v = value.strip().strip("\"'`").strip()
    # 去掉行尾标点（如句号、逗号）
    v = v.rstrip(".,;:")
    # 剥离引号/反引号后为空（如 value 仅为 " 或 ` 或 '）视为占位
    if not v:
        return True
    return v.lower() in SECRET_PLACEHOLDERS


def check_line_for_secret(line: str) -> str | None:
    """检查单行是否包含真实秘密。返回违规说明或 None。"""
    # 1. PEM 私钥起始标记 — 无条件 FAIL
    if PEM_PRIVATE_KEY_RE.search(line):
        return "包含 PEM PRIVATE KEY 标记"
    # 2. 赋值模式 — 仅当 = 后有非占位值时 FAIL
    for m in ASSIGN_RE.finditer(line):
        value = m.group(2)
        if is_placeholder(value):
            continue
        return f"包含疑似秘密赋值: {m.group(0)}"
    return None


# ──────────────────────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────────────────────


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""


def git_cat_file_exists(sha: str) -> bool:
    """检查 sha 是否为仓库内有效对象（commit）。"""
    try:
        result = subprocess.run(
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
            cwd=ROOT,
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def git_is_ancestor(ancestor: str, descendant: str) -> bool:
    """检查 ancestor 是否为 descendant 的祖先（或等于）。"""
    try:
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", ancestor, descendant],
            cwd=ROOT,
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


def extract_sha_field(text: str, field: str) -> str | None:
    """从 Markdown 文本中提取 `- Field: value` 形式的 SHA 字段值。"""
    m = re.search(rf"^- {re.escape(field)}:\s*(.+)$", text, re.MULTILINE)
    if not m:
        return None
    return m.group(1).strip()


def is_empty_or_placeholder(value: str) -> bool:
    if not value:
        return True
    if value.startswith("（") or value.startswith("("):
        return True
    return value.lower() in {"待填写", "tbd", "todo", "pending"}


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


def is_legacy_report(path: Path) -> bool:
    """archive/ 下的报告且含 Legacy Report Metadata 头部，视为历史报告。
    历史报告不改写原始内容，跳过模板章节/字段强检查。
    """
    if "archive" not in path.parts:
        return False
    text = read_text(path)
    return "Legacy Report Metadata" in text


# ──────────────────────────────────────────────────────────────
# 检查组 1-12（基础结构）
# ──────────────────────────────────────────────────────────────


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
    m_report = re.search(r"^- Report:\s*(.+)$", text, re.MULTILINE)
    m_path = re.search(r"^- Path:\s*(.+)$", text, re.MULTILINE)
    if not m_report or not m_path:
        violations.append(
            Violation("latest-incomplete", "LATEST.md 缺少 Report 或 Path 字段")
        )
        return violations
    report_id = m_report.group(1).strip()
    path_str = m_path.group(1).strip()
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
    rows = re.findall(r"^\|\s*([^|\s][^|]*?)\s*\|", text, re.MULTILINE)
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
        for line_no, line in enumerate(text.splitlines(), 1):
            msg = check_line_for_secret(line)
            if msg:
                violations.append(
                    Violation(
                        "report-contains-secret",
                        f"{path.relative_to(ROOT)}:{line_no} {msg}: {line.strip()[:80]}",
                    )
                )
    return violations


def check_sync_outbox_no_reports() -> list[Violation]:
    violations: list[Violation] = []
    if not SYNC_OUTBOX.exists():
        return violations
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


# ──────────────────────────────────────────────────────────────
# 检查组 13：报告 SHA 三字段 + Push Result 完整性（含祖先与一致性）
# ──────────────────────────────────────────────────────────────


def check_report_sha_and_push_result() -> list[Violation]:
    """统一检查组 13：覆盖以下约束
    - 三 SHA 字段存在（Base / Implementation / Report Published Through）
    - 三 SHA 为 40 位十六进制
    - Implementation SHA / Report Published Through SHA 为有效 commit
    - Base SHA 是 Implementation SHA 的祖先（或相等）
    - Implementation SHA 是 Report Published Through SHA 的祖先（或相等）
    - Push Result 非空、非占位
    - LATEST.md 三 SHA 与目标报告一致
    - INDEX.md 表头含 Implementation SHA 列
    - INDEX.md 中报告行的 Implementation SHA 与报告一致
    """
    violations: list[Violation] = []

    # 13a. 报告 SHA 字段存在性、格式、commit 有效性、祖先关系
    report_shas: dict[Path, dict[str, str]] = {}
    for path in collect_reports():
        if is_legacy_report(path):
            continue
        text = read_text(path)
        shas: dict[str, str] = {}
        for field in SHA_FIELDS:
            value = extract_sha_field(text, field)
            if value is None:
                violations.append(
                    Violation(
                        "report-missing-sha-field",
                        f"{path.relative_to(ROOT)} 缺少 {field} 字段",
                    )
                )
                continue
            if is_empty_or_placeholder(value):
                violations.append(
                    Violation(
                        "report-empty-sha-field",
                        f"{path.relative_to(ROOT)} {field} 为空或占位",
                    )
                )
                continue
            shas[field] = value
            if not SHA_HEX_RE.match(value):
                violations.append(
                    Violation(
                        "report-sha-not-40hex",
                        f"{path.relative_to(ROOT)} {field} 不是 40 位十六进制: {value}",
                    )
                )

        # commit 有效性
        for field in ("Implementation SHA", "Report Published Through SHA"):
            sha = shas.get(field)
            if sha and SHA_HEX_RE.match(sha):
                if not git_cat_file_exists(sha):
                    violations.append(
                        Violation(
                            "report-sha-not-commit",
                            f"{path.relative_to(ROOT)} {field} {sha} 不是仓库内有效 commit",
                        )
                    )

        # 祖先关系
        base = shas.get("Base SHA")
        impl = shas.get("Implementation SHA")
        rpt = shas.get("Report Published Through SHA")
        if base and impl and SHA_HEX_RE.match(base) and SHA_HEX_RE.match(impl):
            if not git_is_ancestor(base, impl):
                violations.append(
                    Violation(
                        "report-sha-not-ancestor",
                        f"{path.relative_to(ROOT)} Base SHA {base} 不是 Implementation SHA {impl} 的祖先",
                    )
                )
        if impl and rpt and SHA_HEX_RE.match(impl) and SHA_HEX_RE.match(rpt):
            if not git_is_ancestor(impl, rpt):
                violations.append(
                    Violation(
                        "report-sha-not-ancestor",
                        f"{path.relative_to(ROOT)} Implementation SHA {impl} 不是 Report Published Through SHA {rpt} 的祖先",
                    )
                )

        if shas:
            report_shas[path] = shas

    # 13b. Push Result 非空
    for path in collect_reports():
        if is_legacy_report(path):
            continue
        text = read_text(path)
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
            if is_empty_or_placeholder(value):
                violations.append(
                    Violation(
                        "report-empty-push-result",
                        f"{path.relative_to(ROOT)} Push Result 为空或占位",
                    )
                )

    # 13c. LATEST.md 与目标报告 SHA 一致
    if REPORTS_LATEST.exists():
        latest_text = read_text(REPORTS_LATEST)
        m_path = re.search(r"^- Path:\s*(.+)$", latest_text, re.MULTILINE)
        if m_path:
            rel = m_path.group(1).strip()
            target = ROOT / rel
            if target.exists() and not is_legacy_report(target):
                report_text = read_text(target)
                for field in SHA_FIELDS:
                    latest_val = extract_sha_field(latest_text, field)
                    report_val = extract_sha_field(report_text, field)
                    if (
                        latest_val
                        and report_val
                        and not is_empty_or_placeholder(latest_val)
                        and not is_empty_or_placeholder(report_val)
                        and latest_val != report_val
                    ):
                        violations.append(
                            Violation(
                                "latest-sha-mismatch",
                                f"LATEST.md {field} ({latest_val}) 与目标报告 ({report_val}) 不一致",
                            )
                        )

    # 13d. INDEX.md 表头含 Implementation SHA 列
    if REPORTS_INDEX.exists():
        index_text = read_text(REPORTS_INDEX)
        if "Implementation SHA" not in index_text:
            violations.append(
                Violation(
                    "index-missing-impl-sha-column",
                    "INDEX.md 表头缺少 'Implementation SHA' 列",
                )
            )
        # 13e. INDEX 行的 Implementation SHA 与报告一致
        for path, shas in report_shas.items():
            impl = shas.get("Implementation SHA")
            if not impl:
                continue
            # 在 INDEX 中查找该报告 ID 所在行
            report_id = path.stem
            for line in index_text.splitlines():
                if report_id in line and impl not in line:
                    # 该行未包含正确的 Implementation SHA
                    # 仅当该行不是表头/分隔线时报告
                    if line.startswith("|") and "---" not in line and "Report ID" not in line:
                        violations.append(
                            Violation(
                                "index-impl-sha-mismatch",
                                f"INDEX.md 中 {report_id} 行未包含 Implementation SHA {impl}",
                            )
                        )
                        break

    return violations


# ──────────────────────────────────────────────────────────────
# 检查组 14-15：Deployment / Database 章节
# ──────────────────────────────────────────────────────────────


def check_report_deployment_status_exists() -> list[Violation]:
    violations: list[Violation] = []
    for path in collect_reports():
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
        ("报告 SHA 三字段 + Push Result 完整性", check_report_sha_and_push_result),
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
