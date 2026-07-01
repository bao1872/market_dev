#!/usr/bin/env python3
"""
检查 docs/ 与 AGENTS.md 的文档一致性。

用法:
    python tools/check_docs_consistency.py

检查项:
1. 所有 "Last verified code baseline: <SHA>" 中的 SHA 必须是真实提交且为 HEAD 的祖先。
2. 文档中引用的本地文件（markdown 链接）必须存在。
3. docs/ 下不得出现 "待填写" 占位符。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = REPO_ROOT / "docs"
AGENTS_FILE = REPO_ROOT / "AGENTS.md"
BASELINE_RE = re.compile(r"Last verified code baseline:\s*([0-9a-fA-F]+)")
LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
PLACEHOLDER_RE = re.compile(r"待填写")


def run_git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def is_valid_commit(sha: str) -> bool:
    result = run_git("cat-file", "-t", sha)
    return result.returncode == 0 and result.stdout.strip() == "commit"


def is_ancestor_of_head(sha: str) -> bool:
    result = run_git("merge-base", "--is-ancestor", sha, "HEAD")
    return result.returncode == 0


def collect_doc_files() -> list[Path]:
    files: list[Path] = []
    if DOCS_DIR.exists():
        files.extend(sorted(p for p in DOCS_DIR.rglob("*.md") if p.is_file()))
    if AGENTS_FILE.exists():
        files.append(AGENTS_FILE)
    return files


def extract_baselines(content: str) -> list[str]:
    return BASELINE_RE.findall(content)


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


def check_baselines(content: str) -> list[str]:
    errors: list[str] = []
    for sha in extract_baselines(content):
        if not is_valid_commit(sha):
            errors.append(f"SHA {sha} 不是有效的 git 提交")
        elif not is_ancestor_of_head(sha):
            errors.append(f"SHA {sha} 不是当前 HEAD 的祖先")
    return errors


def check_placeholders(relative_path: str, content: str) -> list[str]:
    errors: list[str] = []
    for match in PLACEHOLDER_RE.finditer(content):
        line = content[: match.start()].count("\n") + 1
        errors.append(f"第 {line} 行存在 '待填写' 占位符")
    return errors


def check_links(doc_path: Path, content: str) -> list[str]:
    errors: list[str] = []
    for raw_link, target in extract_local_links(doc_path, content):
        if not target.exists():
            errors.append(f"引用文件不存在: {raw_link} -> {target.relative_to(REPO_ROOT)}")
    return errors


def main() -> int:
    files = collect_doc_files()
    if not files:
        print("未找到需要检查的文档文件。")
        return 1

    all_ok = True
    baseline_count = 0
    placeholder_files: list[str] = []

    print(f"检查文档目录: {DOCS_DIR.relative_to(REPO_ROOT)}")
    print(f"共扫描 {len(files)} 个文档文件\n")

    for doc_path in files:
        relative = doc_path.relative_to(REPO_ROOT)
        content = doc_path.read_text(encoding="utf-8")

        baseline_shas = extract_baselines(content)
        baseline_count += len(baseline_shas)

        baseline_errors = check_baselines(content)
        link_errors = check_links(doc_path, content)
        placeholder_errors = check_placeholders(str(relative), content)

        errors = baseline_errors + link_errors + placeholder_errors
        if placeholder_errors:
            placeholder_files.append(str(relative))

        if errors:
            all_ok = False
            print(f"[FAIL] {relative}")
            for err in errors:
                print(f"       - {err}")
            print()
        else:
            print(f"[PASS] {relative}")

    print("-" * 50)
    if all_ok:
        print(f"全部通过。共验证 {baseline_count} 个 baseline SHA，未发现待填写占位符。")
        return 0
    else:
        if placeholder_files:
            print(f"发现待填写占位符的文件: {', '.join(placeholder_files)}")
        print("文档一致性检查未通过，请修复上述问题。")
        return 1


if __name__ == "__main__":
    sys.exit(main())
