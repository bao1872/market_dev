#!/usr/bin/env python3
"""
检查 docs/ 文档一致性（v2：MANIFEST 集中基线）。

用法:
    python tools/check_docs_consistency.py

v2 规则（docs/restructure-system-map-v2 之后）:
1. 只要求 docs/current/MANIFEST.md 有全局基线字段 `实现核对基线：<40位SHA>`；
2. MANIFEST 的 实现核对基线 必须是 40 位 SHA；
3. SHA 必须是真实 git 提交；
4. SHA 必须是当前 HEAD 的祖先；
5. current 其他文档不要求重复 baseline 字段；
6. archive 旧文档不参与 baseline 一致性检查；
7. 保留 docs 本地 Markdown 链接检查（docs/ 递归 + AGENTS.md）；
8. 保留 `待填写` 占位符检查；
9. 保留 feishu_webhook 当前方案回归阻断（current 文档，删除语境豁免）；
10. 保留 open-decisions 把 Webhook vs Platform App 写回 OPEN 的阻断。
11. 拒绝未授权 docs 顶层目录（CHANGE-20260718-002）：docs/ 直属子目录
    只允许 current/、maps/、changes/、archive/；禁止 analysis/、
    architecture-audits/ 等非规范目录（docs/ 根 .md 文件不受限）。
12. 校验 CHANGE 引用存在性（CHANGE-20260718-002）：扫描 docs/ 递归 + AGENTS.md
    中 `CHANGE-YYYYMMDD-NNN` 引用，验证对应 records/ 文件存在。
13. 必需新文档存在性（CHANGE-20260718-004）：要求
    docs/current/08-indicator-calculation-contracts.md 和
    docs/maps/indicator-computation-map.md 存在。
14. ref/ 隔离文本扫描（CHANGE-20260718-004）：扫描 docs/current/*.md +
    docs/maps/*.md + AGENTS.md，禁止把 ref/ 文件称为"真源"、"运行依赖"、
    "fixture 生成器"（应改用"参考源（人工阅读）"或"历史路径"）。
    判定逻辑与 backend/tests/test_ref_isolation.py 保持一致。
15. 必需 CHANGE 记录（CHANGE-20260718-004）：要求
    docs/changes/records/CHANGE-20260718-004.md 存在且被 CHANGELOG.md 引用。
16. MANIFEST baseline 新鲜度（CP-19 / CHANGE-20260722-001）：baseline SHA
    必须在 HEAD 的最近 BASELINE_FRESHNESS_WINDOW 个 commit 内。
    修复 PROMPT.md §4 指出的问题：旧规则只要求 baseline 是 HEAD 祖先，
    即使 baseline 落后 88 个 commit 仍能通过，导致文档与代码严重脱节。
    新规则要求 baseline 必须在最近 N 个 commit 内，强制每次 checkpoint
    提交时同步更新 MANIFEST baseline。

输出汇总：
- MANIFEST baseline SHA
- current 文档数量
- maps 文档数量
- 链接检查文件数量
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# 模块级路径（可被测试 monkeypatch 注入）
REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = REPO_ROOT / "docs"
CURRENT_DIR = DOCS_DIR / "current"
MANIFEST_FILE = CURRENT_DIR / "MANIFEST.md"
MAPS_DIR = DOCS_DIR / "maps"
ARCHIVE_DIR = DOCS_DIR / "archive"
AGENTS_FILE = REPO_ROOT / "AGENTS.md"

# 同时识别英文 `Last verified code baseline` 和中文 `实现核对基线`，要求 40 位 hex
# 兼容 ASCII `:` 和中文全角 `：`
# 允许可选反引号包围 SHA（v2 MANIFEST 使用 `sha` 格式）
BASELINE_RE = re.compile(
    r"(?:Last verified code baseline|实现核对基线)[:：]\s*`?([0-9a-fA-F]{40})`?"
)

# Webhook 回归检测：feishu_webhook 出现在 current 文档时，若行内包含以下删除语境关键词则豁免
WEBHOOK_DELETION_CONTEXT = [
    "已永久删除",
    "已删除",
    "删除",
    "禁止",
    "migration 055",
    "CHECK 约束",
    "adapter_type",
]

# OPEN 回归检测：open-decisions.md 中 Webhook 与以下关键词同时出现即视为 OPEN 回归
OPEN_REGRESSION_KEYWORDS = ["仍需决定", "未决", "OPEN"]

LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
PLACEHOLDER_RE = re.compile(r"待填写")
FEISHU_WEBHOOK_RE = re.compile(r"feishu_webhook")

# 规则 11：docs/ 直属子目录白名单（CHANGE-20260718-002）
# AGENTS v2 文档结构规范：docs 顶层允许的目录（PRD V2.0 §7.1 权威层级入口）。
# docs/ 根 .md 文件（如 README.md、INDEX.md）不受限，只约束子目录。
# [CP-14] 扩展为 PRD V2.0 §7.1 定义的完整目录集：contracts/decisions/runbooks/acceptance/evidence/work
ALLOWED_TOP_LEVEL_DIRS = {
    "current", "maps", "changes", "archive",  # 历史 v2 目录
    "contracts", "decisions", "runbooks", "acceptance", "evidence", "work",  # CP-14 新增
}

# 规则 12：CHANGE 引用正则（CHANGE-20260718-002）
# 匹配 CHANGE-YYYYMMDD-NNN 形式（无论是否在 markdown 链接/反引号中），
# 验证 docs/changes/records/CHANGE-YYYYMMDD-NNN.md 文件存在。
CHANGE_REF_RE = re.compile(r"CHANGE-(\d{8})-(\d{3})")
CHANGES_RECORDS_DIR = DOCS_DIR / "changes" / "records"
CHANGELOG_FILE = DOCS_DIR / "changes" / "CHANGELOG.md"

# 规则 13：必需新文档清单（CHANGE-20260718-004）
# Node Cluster 合同和指标计算地图必须在 current/ 和 maps/ 中存在。
# 路径在 check_required_new_docs_exist() 内从 CURRENT_DIR/MAPS_DIR 派生，
# 以支持测试 monkeypatch 注入临时路径。
REQUIRED_NEW_DOC_NAMES = (
    "08-indicator-calculation-contracts.md",
    "indicator-computation-map.md",
)

# 规则 14：ref/ 隔离文本扫描（CHANGE-20260718-004）
# 扫描 current/*.md + maps/*.md + AGENTS.md，禁止把 ref/ 称为真源/运行依赖。
# 复用 test_ref_isolation.py 的判定逻辑（简化版：检查禁止术语和近邻 claim）。
REF_PROHIBITED_TERMS = ("Pine 真源", "视觉真源")
REF_CLAIM_KEYWORDS = ("真源", "运行依赖", "fixture 生成器", "fixture生成器")
REF_SAFE_PATTERNS = (
    "称为",
    "参考源",
    "人工阅读",
    "非运行依赖",
    "历史路径",
    "git rm",
    "不再纳入 git 跟踪",
    "扫描禁止",
    "文本扫描禁止",
)
REF_WINDOW_BEFORE = 60
REF_WINDOW_AFTER = 80
REF_PROHIBITED_TERM_WINDOW = 40

# 规则 15：必需 CHANGE 记录（CHANGE-20260718-004）
# CHANGE-20260718-004.md 必须存在且被 CHANGELOG.md 引用。
# 路径在 check_required_change_documented() 内从 DOCS_DIR 派生，
# 以支持测试 monkeypatch 注入临时路径。
REQUIRED_CHANGE_ID = "CHANGE-20260718-004"

# 规则 16：MANIFEST baseline 新鲜度窗口（CP-19 / CHANGE-20260722-001）
# baseline SHA 必须在 HEAD 的最近 N 个 commit 内，防止 baseline 严重落后。
# 窗口大小 50 覆盖约 2-3 个 Phase 的 checkpoint 数量，平衡新鲜度与历史容错。
# 测试可通过 monkeypatch 注入 BASELINE_FRESHNESS_WINDOW 覆盖。
BASELINE_FRESHNESS_WINDOW = 50


def run_git(*args: str) -> subprocess.CompletedProcess[str]:
    """执行 git 命令（可被测试 mock 替换）。"""
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def is_valid_commit(sha: str) -> bool:
    """验证 SHA 是真实 git 提交。"""
    result = run_git("cat-file", "-t", sha)
    return result.returncode == 0 and result.stdout.strip() == "commit"


def is_ancestor_of_head(sha: str) -> bool:
    """验证 SHA 是当前 HEAD 的祖先。"""
    result = run_git("merge-base", "--is-ancestor", sha, "HEAD")
    return result.returncode == 0


def collect_current_docs() -> list[Path]:
    """收集 docs/current/*.md 文件（不递归，仅顶层）。

    包含 MANIFEST.md，用于链接/占位符/webhook 检查。
    """
    if not CURRENT_DIR.exists():
        return []
    return sorted(p for p in CURRENT_DIR.glob("*.md") if p.is_file())


def collect_maps_docs() -> list[Path]:
    """收集 docs/maps/*.md 文件（不递归，仅顶层）。"""
    if not MAPS_DIR.exists():
        return []
    return sorted(p for p in MAPS_DIR.glob("*.md") if p.is_file())


def collect_all_doc_files() -> list[Path]:
    """收集所有需检查链接/占位符的文档（docs/ 递归 + AGENTS.md）。

    注意：archive 目录下的旧文档参与链接/占位符检查，
    但不参与 baseline 一致性检查（见规则 6）。
    """
    files: list[Path] = []
    if DOCS_DIR.exists():
        files.extend(sorted(p for p in DOCS_DIR.rglob("*.md") if p.is_file()))
    if AGENTS_FILE.exists():
        files.append(AGENTS_FILE)
    return files


def extract_baselines(content: str) -> list[str]:
    """从文档内容提取所有 baseline SHA（40 位 hex）。"""
    return BASELINE_RE.findall(content)


def check_baseline_sha_format(shas: list[str]) -> list[str]:
    """规则 2：检查 baseline SHA 是否为合法 40 位 hex。"""
    errors: list[str] = []
    for sha in shas:
        if not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
            errors.append(f"SHA 格式非法（非 40 位 hex）: {sha}")
    return errors


def check_baseline_real_commit(shas: list[str]) -> list[str]:
    """规则 3：检查 baseline SHA 是否为真实 git 提交。"""
    errors: list[str] = []
    for sha in shas:
        if re.fullmatch(r"[0-9a-fA-F]{40}", sha) and not is_valid_commit(sha):
            errors.append(f"SHA 不是有效的 git 提交: {sha}")
    return errors


def check_baseline_ancestor(shas: list[str]) -> list[str]:
    """规则 4：检查 baseline SHA 是否为当前 HEAD 的祖先。"""
    errors: list[str] = []
    for sha in shas:
        if re.fullmatch(r"[0-9a-fA-F]{40}", sha) and is_valid_commit(sha):
            if not is_ancestor_of_head(sha):
                errors.append(f"SHA 不是当前 HEAD 的祖先: {sha}")
    return errors


def count_commits_ahead_of_baseline(sha: str) -> int | None:
    """规则 16 辅助：统计 baseline SHA 落后 HEAD 多少个 commit。

    使用 `git rev-list --count HEAD ^<sha>` 计算从 baseline 到 HEAD
    之间的 commit 数量（不含 baseline 自身）。

    Returns:
        commit 数量；如果 sha 不是 HEAD 祖先或 git 失败则返回 None。
    """
    if not is_ancestor_of_head(sha):
        return None
    result = run_git("rev-list", "--count", "HEAD", f"^{sha}")
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def check_baseline_freshness(shas: list[str]) -> list[str]:
    """规则 16：检查 baseline SHA 是否在 HEAD 的最近 N 个 commit 内（CP-19）。

    修复 PROMPT.md §4 指出的问题：旧规则 4 只要求 baseline 是 HEAD 祖先，
    即使 baseline 落后 88 个 commit 仍能通过，导致文档与代码严重脱节。

    新规则要求 baseline 到 HEAD 的 commit 距离不得超过
    BASELINE_FRESHNESS_WINDOW，强制每次 checkpoint 提交时同步更新
    MANIFEST baseline。
    """
    errors: list[str] = []
    for sha in shas:
        if not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
            continue  # 格式错误由规则 2 报告
        if not is_valid_commit(sha):
            continue  # 真实性错误由规则 3 报告
        if not is_ancestor_of_head(sha):
            continue  # 祖先错误由规则 4 报告
        ahead = count_commits_ahead_of_baseline(sha)
        if ahead is None:
            errors.append(
                f"无法计算 baseline 落后 HEAD 的 commit 数量: {sha}"
            )
            continue
        if ahead > BASELINE_FRESHNESS_WINDOW:
            errors.append(
                f"MANIFEST baseline 严重落后：{sha} 落后 HEAD {ahead} 个 commit，"
                f"超过窗口 {BASELINE_FRESHNESS_WINDOW}。"
                f"请在本次 checkpoint 提交时同步更新 docs/current/MANIFEST.md "
                f"中的实现核对基线到当前 HEAD。"
            )
    return errors


def check_webhook_regression(doc_path: Path, content: str) -> list[str]:
    """规则 9：current 文档不得把 feishu_webhook 写成当前方案（删除语境豁免）。"""
    errors: list[str] = []
    lines = content.splitlines()
    for i, line in enumerate(lines, start=1):
        if not FEISHU_WEBHOOK_RE.search(line):
            continue
        # 检查是否在删除语境中
        is_deletion_context = any(kw in line for kw in WEBHOOK_DELETION_CONTEXT)
        if not is_deletion_context:
            errors.append(
                f"第 {i} 行出现 feishu_webhook 但非删除语境，疑似重新写为当前方案"
            )
    return errors


def check_open_regression(doc_path: Path, content: str) -> list[str]:
    """规则 10：open-decisions.md 不得把 Webhook vs Platform App 写成 OPEN。

    判定：行含 "Webhook" 且含 "仍需决定"/"未决"/"OPEN"，且不含 "已决定"
    （已决定表示已闭环，不视为回归）。
    """
    errors: list[str] = []
    lines = content.splitlines()
    for i, line in enumerate(lines, start=1):
        if "Webhook" not in line:
            continue
        if "已决定" in line:
            # 已决定表示 Webhook vs Platform App 已闭环，不视为回归
            continue
        if any(kw in line for kw in OPEN_REGRESSION_KEYWORDS):
            errors.append(
                f"第 {i} 行将 Webhook 与未决关键词同时使用，疑似 OPEN 回归"
            )
    return errors


def strip_code_spans(content: str) -> str:
    """移除 markdown 行内代码与代码块，避免链接正则误匹配代码中的字符。"""
    content = re.sub(r"```[\s\S]*?```", "", content)
    content = re.sub(r"`[^`\n]+`", "", content)
    return content


def strip_fragment(link: str) -> str:
    """去掉 URL 中的锚点片段，仅保留文件路径部分。"""
    if link.startswith("file://"):
        return link.split("#", 1)[0]
    return link.split("#", 1)[0]


def looks_like_regex_or_placeholder(link: str) -> bool:
    """跳过明显是正则、模板或代码片段的伪链接。"""
    return bool(re.search(r"[\\{}*?^$|<>]", link))


def extract_local_links(doc_path: Path, content: str) -> list[tuple[str, Path]]:
    """从 markdown 链接中提取指向仓库内文件的引用，返回 (raw_link, resolved_path)。"""
    clean_content = strip_code_spans(content)
    links: list[tuple[str, Path]] = []
    for _text, raw_link in LINK_RE.findall(clean_content):
        link = strip_fragment(raw_link.strip())
        if not link or link.startswith(("#", "http://", "https://")):
            continue
        if link.startswith(("mailto:", "javascript:")):
            continue
        if looks_like_regex_or_placeholder(link):
            continue

        target: Path | None = None
        if link.startswith("file://"):
            abs_path = Path(link[len("file://"):])
            try:
                rel = abs_path.relative_to(REPO_ROOT)
                target = REPO_ROOT / rel
            except ValueError:
                continue
        elif link.startswith("/"):
            target = REPO_ROOT / link.lstrip("/")
        else:
            target = doc_path.parent / link

        if target is not None:
            links.append((raw_link, target))
    return links


def check_placeholders(relative_path: str, content: str) -> list[str]:
    """规则 8：检查待填写占位符。"""
    errors: list[str] = []
    for match in PLACEHOLDER_RE.finditer(content):
        line = content[: match.start()].count("\n") + 1
        errors.append(f"第 {line} 行存在 '待填写' 占位符")
    return errors


def check_links(doc_path: Path, content: str) -> list[str]:
    """规则 7：检查本地 Markdown 链接是否指向存在文件。"""
    errors: list[str] = []
    for raw_link, target in extract_local_links(doc_path, content):
        if not target.exists():
            errors.append(f"引用文件不存在: {raw_link} -> {target.relative_to(REPO_ROOT)}")
    return errors


def check_unauthorized_top_level_dirs() -> list[str]:
    """规则 11：拒绝未授权 docs 顶层目录（CHANGE-20260718-002）。

    docs/ 直属子目录只允许 current/maps/changes/archive。
    docs/ 根 .md 文件不受限（只约束子目录）。
    """
    errors: list[str] = []
    if not DOCS_DIR.exists():
        return errors
    for child in sorted(DOCS_DIR.iterdir()):
        if not child.is_dir():
            continue
        name = child.name
        if name not in ALLOWED_TOP_LEVEL_DIRS:
            errors.append(
                f"未授权的 docs 顶层目录: docs/{name}/（只允许 "
                f"{sorted(ALLOWED_TOP_LEVEL_DIRS)}）"
            )
    return errors


def check_change_references(doc_files: list[Path]) -> list[str]:
    """规则 12：校验 CHANGE 引用存在性（CHANGE-20260718-002）。

    扫描所有 doc_files 中 `CHANGE-YYYYMMDD-NNN` 引用，
    验证 docs/changes/records/CHANGE-YYYYMMDD-NNN.md 文件存在。

    历史引用（指向已被删除/未创建的 record）会被检出。
    跳过 archive/ 目录（历史快照，不要求引用可达）。

    records 目录从 DOCS_DIR 派生（支持测试 monkeypatch 注入临时路径）。
    """
    errors: list[str] = []
    records_dir = DOCS_DIR / "changes" / "records"
    for doc_path in doc_files:
        # archive 历史快照不参与 CHANGE 引用可达性检查
        try:
            rel = doc_path.relative_to(ARCHIVE_DIR)
            _ = rel  # 在 archive 下，跳过
            continue
        except ValueError:
            pass
        content = doc_path.read_text(encoding="utf-8")
        seen_in_doc: set[str] = set()
        for match in CHANGE_REF_RE.finditer(content):
            ref_id = f"CHANGE-{match.group(1)}-{match.group(2)}"
            if ref_id in seen_in_doc:
                continue
            seen_in_doc.add(ref_id)
            target = records_dir / f"{ref_id}.md"
            if not target.exists():
                errors.append(
                    f"引用了不存在的 CHANGE record: {ref_id} "
                    f"(在 {doc_path.relative_to(REPO_ROOT)})"
                )
    return errors


def _is_ref_problematic_line(line: str) -> bool:
    """规则 14 辅助：检查一行是否把 ref/ 称为真源/运行依赖/fixture 生成器。

    判定逻辑与 backend/tests/test_ref_isolation.py 的 _is_problematic_ref_claim
    保持一致（同步更新）：
    - 行含 `ref/` AND 含禁止术语（"Pine 真源"/"视觉真源"）AND 禁止术语附近无安全模式
    - OR 行含 `ref/` AND ref/ 窗口内含声明关键词 AND 整行无安全模式
    """
    if "ref/" not in line:
        return False

    # 检查禁止术语（局部窗口安全模式检查）
    for term in REF_PROHIBITED_TERMS:
        idx = 0
        while True:
            pos = line.find(term, idx)
            if pos < 0:
                break
            window_start = max(0, pos - REF_PROHIBITED_TERM_WINDOW)
            window_end = min(len(line), pos + len(term) + REF_PROHIBITED_TERM_WINDOW)
            local_window = line[window_start:window_end]
            if not any(kw in local_window for kw in REF_SAFE_PATTERNS):
                return True
            idx = pos + len(term)

    # 检查通用声明关键词（整行安全模式检查）
    if any(kw in line for kw in REF_SAFE_PATTERNS):
        return False
    ref_positions: list[int] = []
    idx = 0
    while True:
        pos = line.find("ref/", idx)
        if pos < 0:
            break
        ref_positions.append(pos)
        idx = pos + 4
    for pos in ref_positions:
        start = max(0, pos - REF_WINDOW_BEFORE)
        end = min(len(line), pos + REF_WINDOW_AFTER)
        window = line[start:end]
        if any(kw in window for kw in REF_CLAIM_KEYWORDS):
            return True
    return False


def check_required_new_docs_exist() -> list[str]:
    """规则 13：必需新文档必须存在（CHANGE-20260718-004）。

    要求：
    - docs/current/08-indicator-calculation-contracts.md（Node Cluster 合同）
    - docs/maps/indicator-computation-map.md（指标计算地图）

    这两份文档是 CHANGE-20260718-004 的核心交付物，必须在 Section 4 创建。

    路径从 CURRENT_DIR/MAPS_DIR 派生（支持测试 monkeypatch）。
    """
    errors: list[str] = []
    # 第一个文件在 current/，第二个在 maps/
    required_paths = (
        CURRENT_DIR / REQUIRED_NEW_DOC_NAMES[0],
        MAPS_DIR / REQUIRED_NEW_DOC_NAMES[1],
    )
    for doc_path in required_paths:
        if not doc_path.exists():
            try:
                rel = doc_path.relative_to(REPO_ROOT)
            except ValueError:
                rel = doc_path
            errors.append(
                f"缺少必需文档: {rel}（CHANGE-20260718-004 要求）"
            )
    return errors


def check_current_docs_no_ref_dependency() -> list[str]:
    """规则 14：current/maps/AGENTS 文档不得把 ref/ 称为真源/运行依赖（CHANGE-20260718-004）。

    扫描 docs/current/*.md + docs/maps/*.md + AGENTS.md，
    禁止把 ref/ 文件称为"真源"、"运行依赖"、"fixture 生成器"。
    允许的表述：参考源（人工阅读）、历史路径、派生文件、git rm 状态等。

    历史目录（docs/changes/records/、docs/archive/）不参与扫描。

    判定逻辑与 backend/tests/test_ref_isolation.py 保持一致。
    """
    errors: list[str] = []
    files: list[Path] = []
    if CURRENT_DIR.exists():
        files.extend(sorted(p for p in CURRENT_DIR.glob("*.md") if p.is_file()))
    if MAPS_DIR.exists():
        files.extend(sorted(p for p in MAPS_DIR.glob("*.md") if p.is_file()))
    if AGENTS_FILE.exists():
        files.append(AGENTS_FILE)

    for doc_path in files:
        content = doc_path.read_text(encoding="utf-8")
        rel = doc_path.relative_to(REPO_ROOT)
        for i, line in enumerate(content.splitlines(), start=1):
            if _is_ref_problematic_line(line):
                errors.append(
                    f"{rel}:{i}: 把 ref/ 称为真源/运行依赖/fixture 生成器"
                    f"（应改用'参考源（人工阅读）'或'历史路径'）: {line.strip()[:200]}"
                )
    return errors


def check_required_change_documented() -> list[str]:
    """规则 15：必需 CHANGE 记录必须存在且被 CHANGELOG 引用（CHANGE-20260718-004）。

    要求：
    - docs/changes/records/CHANGE-20260718-004.md 必须存在
    - docs/changes/CHANGELOG.md 必须引用 CHANGE-20260718-004

    CHANGE-20260718-004 是本次 Node Cluster 合同 + ref 隔离的核心变更记录，
    必须在 Section 4.7 创建并加入 CHANGELOG。

    路径从 DOCS_DIR 派生（支持测试 monkeypatch 注入临时路径）。
    """
    errors: list[str] = []
    records_dir = DOCS_DIR / "changes" / "records"
    record_path = records_dir / f"{REQUIRED_CHANGE_ID}.md"
    changelog_path = DOCS_DIR / "changes" / "CHANGELOG.md"

    if not record_path.exists():
        try:
            rel = record_path.relative_to(REPO_ROOT)
        except ValueError:
            rel = record_path
        errors.append(
            f"缺少必需 CHANGE record: {rel}（CHANGE-20260718-004 要求）"
        )
    if changelog_path.exists():
        changelog_content = changelog_path.read_text(encoding="utf-8")
        if REQUIRED_CHANGE_ID not in changelog_content:
            errors.append(
                f"CHANGELOG.md 未引用 {REQUIRED_CHANGE_ID}"
                f"（CHANGE-20260718-004 要求记录在 CHANGELOG）"
            )
    else:
        try:
            rel = changelog_path.relative_to(REPO_ROOT)
        except ValueError:
            rel = changelog_path
        errors.append(
            f"缺少 CHANGELOG 文件: {rel}"
        )
    return errors


def check_manifest_baseline() -> tuple[list[str], str | None]:
    """v2 规则 1-4：检查 docs/current/MANIFEST.md 的全局基线字段。

    Returns:
        (errors, baseline_sha)：错误列表与解析到的 baseline SHA（无则为 None）
    """
    errors: list[str] = []

    if not MANIFEST_FILE.exists():
        errors.append(f"缺少 MANIFEST 文件: {MANIFEST_FILE.relative_to(REPO_ROOT)}")
        return errors, None

    content = MANIFEST_FILE.read_text(encoding="utf-8")
    shas = extract_baselines(content)

    # 规则 1：MANIFEST 必须有 实现核对基线 字段
    if not shas:
        errors.append("MANIFEST.md 缺少 实现核对基线 字段")
        return errors, None

    # 规则 2：SHA 格式校验
    errors.extend(check_baseline_sha_format(shas))

    # 规则 3：真实提交校验
    errors.extend(check_baseline_real_commit(shas))

    # 规则 4：祖先校验
    errors.extend(check_baseline_ancestor(shas))

    # 规则 16：新鲜度校验（CP-19 / CHANGE-20260722-001）
    # baseline 必须在 HEAD 的最近 BASELINE_FRESHNESS_WINDOW 个 commit 内
    errors.extend(check_baseline_freshness(shas))

    # v2 不再要求多文档 baseline 一致性（MANIFEST 是唯一基线头）
    # 取第一个合法 SHA 作为统一 baseline
    baseline_sha = shas[0] if shas else None
    return errors, baseline_sha


def main() -> int:
    all_errors: list[str] = []
    placeholder_files: list[str] = []

    # === 第一阶段：MANIFEST 集中基线检查（v2 规则 1-4）===
    print(f"检查文档目录: {DOCS_DIR.relative_to(REPO_ROOT)}")
    print(f"MANIFEST 文件: {MANIFEST_FILE.relative_to(REPO_ROOT)}\n")

    manifest_errors, baseline_sha = check_manifest_baseline()
    if manifest_errors:
        all_errors.extend([f"MANIFEST: {e}" for e in manifest_errors])
        print("[FAIL] docs/current/MANIFEST.md")
        for e in manifest_errors:
            print(f"       - {e}")
    else:
        print("[PASS] docs/current/MANIFEST.md")
    print(f"       MANIFEST baseline: {baseline_sha or '(未解析)'}")

    # === 第一阶段补充：docs 顶层目录结构检查（v2 规则 11，CHANGE-20260718-002）===
    print()
    tld_errors = check_unauthorized_top_level_dirs()
    if tld_errors:
        all_errors.extend(tld_errors)
        print("[FAIL] docs/ 顶层目录结构")
        for e in tld_errors:
            print(f"       - {e}")
    else:
        print("[PASS] docs/ 顶层目录结构（仅 current/maps/changes/archive）")

    # === 第二阶段：current 文档回归检查（webhook + open-decisions）===
    current_docs = collect_current_docs()
    print(f"\n共扫描 {len(current_docs)} 个 current 文档（webhook/open 回归检查）\n")

    for doc_path in current_docs:
        relative = doc_path.relative_to(REPO_ROOT)
        content = doc_path.read_text(encoding="utf-8")
        doc_errors: list[str] = []

        # 规则 9：Webhook 回归（所有 current 文档）
        doc_errors.extend(check_webhook_regression(doc_path, content))

        # 规则 10：OPEN 回归（仅 open-decisions.md）
        if doc_path.name == "open-decisions.md":
            doc_errors.extend(check_open_regression(doc_path, content))

        if doc_errors:
            all_errors.extend([f"{relative}: {e}" for e in doc_errors])
            print(f"[FAIL] {relative}")
            for e in doc_errors:
                print(f"       - {e}")
        else:
            print(f"[PASS] {relative}")

    # === 第三阶段：链接与占位符检查 ===
    # 链接检查：docs/ 递归 + AGENTS.md（所有文档）
    # 占位符检查：仅 current + maps（v2 事实源，不应有占位符；
    #            archive/changes/规则说明文档可能引用"待填写"作为规则描述，不检查）
    all_doc_files = collect_all_doc_files()
    maps_docs = collect_maps_docs()
    placeholder_check_files = set(current_docs) | set(maps_docs)
    print(f"\n共扫描 {len(all_doc_files)} 个文档文件（链接检查）")
    print(f"current 文档数量: {len(current_docs)}")
    print(f"maps 文档数量: {len(maps_docs)}")
    print(f"链接检查文件数量: {len(all_doc_files)}")
    print(f"占位符检查文件数量: {len(placeholder_check_files)}（仅 current + maps）\n")

    for doc_path in all_doc_files:
        relative = doc_path.relative_to(REPO_ROOT)
        content = doc_path.read_text(encoding="utf-8")
        # 复用 Phase 2 同名变量；不重复类型注解以避免 mypy no-redef。
        doc_errors = []

        # 链接检查：所有文档
        link_errors = check_links(doc_path, content)
        doc_errors.extend(link_errors)

        # 占位符检查：仅 current + maps（v2 事实源）
        if doc_path in placeholder_check_files:
            placeholder_errors = check_placeholders(str(relative), content)
            doc_errors.extend(placeholder_errors)
            if placeholder_errors:
                placeholder_files.append(str(relative))

        if doc_errors:
            all_errors.extend([f"{relative}: {e}" for e in doc_errors])
            print(f"[FAIL] {relative}")
            for e in doc_errors:
                print(f"       - {e}")
        # PASS 行不打印，避免输出过长

    # === 第四阶段：CHANGE 引用可达性检查（v2 规则 12，CHANGE-20260718-002）===
    print()
    change_ref_errors = check_change_references(all_doc_files)
    if change_ref_errors:
        all_errors.extend(change_ref_errors)
        print("[FAIL] CHANGE 引用可达性")
        for e in change_ref_errors:
            print(f"       - {e}")
    else:
        print("[PASS] CHANGE 引用可达性（所有 CHANGE-YYYYMMDD-NNN 引用目标存在）")

    # === 第五阶段：CHANGE-20260718-004 新规则（13/14/15）===
    print()

    # 规则 13：必需新文档必须存在
    required_docs_errors = check_required_new_docs_exist()
    if required_docs_errors:
        all_errors.extend(required_docs_errors)
        print("[FAIL] 必需新文档存在性（规则 13）")
        for e in required_docs_errors:
            print(f"       - {e}")
    else:
        print(
            "[PASS] 必需新文档存在性（规则 13："
            "08-indicator-calculation-contracts.md + indicator-computation-map.md）"
        )

    # 规则 14：current/maps/AGENTS 不得把 ref/ 称为真源/运行依赖
    ref_dep_errors = check_current_docs_no_ref_dependency()
    if ref_dep_errors:
        all_errors.extend(ref_dep_errors)
        print("[FAIL] ref/ 隔离文本扫描（规则 14）")
        for e in ref_dep_errors:
            print(f"       - {e}")
    else:
        print(
            "[PASS] ref/ 隔离文本扫描（规则 14：current/maps/AGENTS 未把 ref/ 称为真源）"
        )

    # 规则 15：必需 CHANGE 记录必须存在且被 CHANGELOG 引用
    required_change_errors = check_required_change_documented()
    if required_change_errors:
        all_errors.extend(required_change_errors)
        print("[FAIL] 必需 CHANGE 记录（规则 15）")
        for e in required_change_errors:
            print(f"       - {e}")
    else:
        print(
            f"[PASS] 必需 CHANGE 记录（规则 15：{REQUIRED_CHANGE_ID} 存在且 CHANGELOG 引用）"
        )

    # === 汇总 ===
    print()
    print("=" * 60)
    if all_errors:
        print(f"文档一致性检查未通过，共 {len(all_errors)} 个问题。")
        if placeholder_files:
            print(f"发现待填写占位符的文件: {', '.join(placeholder_files)}")
        for e in all_errors:
            print(f"  - {e}")
        return 1
    else:
        print(
            f"全部通过。MANIFEST baseline: {baseline_sha or 'N/A'}，"
            f"current 文档 {len(current_docs)} 个，"
            f"maps 文档 {len(maps_docs)} 个，"
            f"链接检查文件 {len(all_doc_files)} 个。"
        )
        return 0


if __name__ == "__main__":
    sys.exit(main())
