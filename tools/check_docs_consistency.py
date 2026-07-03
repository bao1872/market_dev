#!/usr/bin/env python3
"""
检查 docs/ 与 AGENTS.md 的文档一致性。

用法:
    python tools/check_docs_consistency.py

检查项（advice §8 第 1-10 条）:
1. 所有 docs/current/*.md 和 docs/README.md 必须有 `实现核对基线：<40位SHA>`；
2. 零匹配必须失败；
3. 缺失基线字段必须失败（设计基线日期/实现核对基线/实现核对分支/最近一致性检查日期）；
4. 非法 SHA 必须失败（非 40 位 hex 或 git 验证失败）；
5. SHA 不是当前 HEAD 祖先必须失败；
6. current 文档之间 baseline 不一致必须失败；
7. current 文档重新写 feishu_webhook 为当前方案必须失败（删除语境豁免）；
8. current 文档把 Webhook vs Platform App 写成 OPEN 必须失败；
9. 本地 Markdown 链接检查和 `待填写` 检查继续保留；
10. 输出显示扫描 current 文档数量、解析 baseline 数量、统一 baseline SHA。
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
README_FILE = DOCS_DIR / "README.md"
AGENTS_FILE = REPO_ROOT / "AGENTS.md"

# 同时识别英文 `Last verified code baseline` 和中文 `实现核对基线`，要求 40 位 hex
# 兼容 ASCII `:` 和中文全角 `：`
BASELINE_RE = re.compile(
    r"(?:Last verified code baseline|实现核对基线)[:：]\s*([0-9a-fA-F]{40})"
)

# 必需的基线头部字段（docs/current/*.md 和 docs/README.md 必须全部包含）
REQUIRED_BASELINE_FIELDS = [
    "设计基线日期",
    "实现核对基线",
    "实现核对分支",
    "最近一致性检查日期",
]

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

# OPEN 回归检测：17-open-decisions.md 中 Webhook 与以下关键词同时出现即视为 OPEN 回归
OPEN_REGRESSION_KEYWORDS = ["仍需决定", "未决", "OPEN"]

LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
PLACEHOLDER_RE = re.compile(r"待填写")
FEISHU_WEBHOOK_RE = re.compile(r"feishu_webhook")


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
    """收集 docs/current/*.md 文件（不递归，仅顶层）。"""
    if not CURRENT_DIR.exists():
        return []
    return sorted(p for p in CURRENT_DIR.glob("*.md") if p.is_file())


def collect_all_doc_files() -> list[Path]:
    """收集所有需检查链接/占位符的文档（docs/ 递归 + AGENTS.md）。"""
    files: list[Path] = []
    if DOCS_DIR.exists():
        files.extend(sorted(p for p in DOCS_DIR.rglob("*.md") if p.is_file()))
    if AGENTS_FILE.exists():
        files.append(AGENTS_FILE)
    return files


def extract_baselines(content: str) -> list[str]:
    """从文档内容提取所有 baseline SHA（40 位 hex）。"""
    return BASELINE_RE.findall(content)


def check_required_fields(doc_path: Path, content: str) -> list[str]:
    """规则 3：检查必需基线字段是否齐全。"""
    errors: list[str] = []
    for field in REQUIRED_BASELINE_FIELDS:
        # 允许 `>` 后有空格，兼容 ASCII/全角冒号
        pattern = re.compile(rf">\s*{re.escape(field)}[:：]\s*\S+")
        if not pattern.search(content):
            errors.append(f"缺少必需基线字段: {field}")
    return errors


def check_baseline_sha_format(doc_path: Path, shas: list[str]) -> list[str]:
    """规则 4：检查 baseline SHA 是否为合法 40 位 hex。"""
    errors: list[str] = []
    for sha in shas:
        if not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
            errors.append(f"SHA 格式非法（非 40 位 hex）: {sha}")
    return errors


def check_baseline_real_commit(doc_path: Path, shas: list[str]) -> list[str]:
    """规则 4：检查 baseline SHA 是否为真实 git 提交。"""
    errors: list[str] = []
    for sha in shas:
        if re.fullmatch(r"[0-9a-fA-F]{40}", sha) and not is_valid_commit(sha):
            errors.append(f"SHA 不是有效的 git 提交: {sha}")
    return errors


def check_baseline_ancestor(doc_path: Path, shas: list[str]) -> list[str]:
    """规则 5：检查 baseline SHA 是否为当前 HEAD 的祖先。"""
    errors: list[str] = []
    for sha in shas:
        if re.fullmatch(r"[0-9a-fA-F]{40}", sha) and is_valid_commit(sha):
            if not is_ancestor_of_head(sha):
                errors.append(f"SHA 不是当前 HEAD 的祖先: {sha}")
    return errors


def check_webhook_regression(doc_path: Path, content: str) -> list[str]:
    """规则 7：current 文档不得把 feishu_webhook 写成当前方案（删除语境豁免）。"""
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
    """规则 8：17-open-decisions.md 不得把 Webhook vs Platform App 写成 OPEN。

    判定：行含 "Webhook" 且含 "仍需决定"/"未决"，且不含 "已决定"（已决定表示已闭环）。
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
    """规则 9：检查待填写占位符。"""
    errors: list[str] = []
    for match in PLACEHOLDER_RE.finditer(content):
        line = content[: match.start()].count("\n") + 1
        errors.append(f"第 {line} 行存在 '待填写' 占位符")
    return errors


def check_links(doc_path: Path, content: str) -> list[str]:
    """规则 9：检查本地 Markdown 链接是否指向存在文件。"""
    errors: list[str] = []
    for raw_link, target in extract_local_links(doc_path, content):
        if not target.exists():
            errors.append(f"引用文件不存在: {raw_link} -> {target.relative_to(REPO_ROOT)}")
    return errors


def main() -> int:
    all_errors: list[str] = []
    placeholder_files: list[str] = []

    # === 第一阶段：基线检查（docs/current/*.md + docs/README.md）===
    current_docs = collect_current_docs()
    baseline_files: list[Path] = []
    if README_FILE.exists():
        baseline_files.append(README_FILE)
    baseline_files.extend(current_docs)

    all_baselines: list[tuple[Path, str]] = []
    baseline_doc_count = 0

    print(f"检查文档目录: {DOCS_DIR.relative_to(REPO_ROOT)}")
    print(f"共扫描 {len(baseline_files)} 个基线文档（current + README）\n")

    for doc_path in baseline_files:
        relative = doc_path.relative_to(REPO_ROOT)
        content = doc_path.read_text(encoding="utf-8")
        doc_errors: list[str] = []

        # 规则 3：必需字段
        doc_errors.extend(check_required_fields(doc_path, content))

        # 规则 1/4：提取 baseline 并校验格式
        shas = extract_baselines(content)
        if shas:
            baseline_doc_count += 1
            for sha in shas:
                all_baselines.append((doc_path, sha))
            doc_errors.extend(check_baseline_sha_format(doc_path, shas))
            # 规则 4：真实提交校验
            doc_errors.extend(check_baseline_real_commit(doc_path, shas))
            # 规则 5：祖先校验
            doc_errors.extend(check_baseline_ancestor(doc_path, shas))
        else:
            doc_errors.append("未找到 实现核对基线 字段")

        # 规则 7：Webhook 回归（仅 current 文档）
        if doc_path.parent == CURRENT_DIR:
            doc_errors.extend(check_webhook_regression(doc_path, content))

        # 规则 8：OPEN 回归（仅 17-open-decisions.md）
        if doc_path.name == "17-open-decisions.md":
            doc_errors.extend(check_open_regression(doc_path, content))

        if doc_errors:
            all_errors.extend([f"{relative}: {e}" for e in doc_errors])
            print(f"[FAIL] {relative}")
            for e in doc_errors:
                print(f"       - {e}")
        else:
            print(f"[PASS] {relative}")

    # 规则 2：零匹配失败
    if baseline_files and baseline_doc_count == 0:
        all_errors.append("零匹配：扫描了基线文档但未解析到任何 baseline SHA")

    # 规则 6：baseline 一致性
    unique_shas = {sha for _path, sha in all_baselines}
    if len(unique_shas) > 1:
        all_errors.append(
            f"baseline 不一致：发现 {len(unique_shas)} 个不同 SHA: {sorted(unique_shas)}"
        )

    unified_sha = next(iter(unique_shas)) if len(unique_shas) == 1 else None

    print()
    print("-" * 50)
    print(f"解析 baseline 数量: {len(all_baselines)}")
    print(f"统一 baseline SHA: {unified_sha or '(不一致或零匹配)'}")

    # === 第二阶段：链接与占位符检查（docs/ 递归 + AGENTS.md）===
    all_doc_files = collect_all_doc_files()
    print(f"\n共扫描 {len(all_doc_files)} 个文档文件（链接+占位符检查）\n")

    for doc_path in all_doc_files:
        relative = doc_path.relative_to(REPO_ROOT)
        content = doc_path.read_text(encoding="utf-8")
        doc_errors: list[str] = []

        link_errors = check_links(doc_path, content)
        placeholder_errors = check_placeholders(str(relative), content)
        doc_errors.extend(link_errors)
        doc_errors.extend(placeholder_errors)

        if placeholder_errors:
            placeholder_files.append(str(relative))

        if doc_errors:
            all_errors.extend([f"{relative}: {e}" for e in doc_errors])
            print(f"[FAIL] {relative}")
            for e in doc_errors:
                print(f"       - {e}")
        else:
            print(f"[PASS] {relative}")

    # === 汇总 ===
    print()
    print("=" * 50)
    if all_errors:
        print(f"文档一致性检查未通过，共 {len(all_errors)} 个问题。")
        if placeholder_files:
            print(f"发现待填写占位符的文件: {', '.join(placeholder_files)}")
        for e in all_errors:
            print(f"  - {e}")
        return 1
    else:
        print(
            f"全部通过。扫描 current 文档 {len(current_docs)} 个，"
            f"解析 baseline {len(all_baselines)} 个，"
            f"统一 SHA: {unified_sha or 'N/A'}。"
        )
        return 0


if __name__ == "__main__":
    sys.exit(main())
