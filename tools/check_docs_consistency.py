"""Stage-aware documentation consistency checks for Panji.

Exploration checks structural correctness and active-document contradictions without
forcing release artifacts to track every commit. Hardening additionally activates
acceptance/release freshness checks.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = REPO_ROOT / "docs"
AGENTS_FILE = REPO_ROOT / "AGENTS.md"
MAP_BASELINE_FILE = DOCS_DIR / "maps" / "00-system-overview.md"

ACTIVE_TOP_LEVEL_DIRS = {
    "prd",
    "maps",
    "changes",
    "runbooks",
    "contracts",
    "decisions",
    "acceptance",
    "evidence",
    "work",
    "archive",
    "current",  # legacy compatibility only
}

BASELINE_RE = re.compile(
    r"(?:Last verified code baseline|实现核对基线|核验提交)[:：]\s*`?([0-9a-fA-F]{40})`?"
)
ACCEPTANCE_BASELINE_RE = re.compile(r"\*\*基线\*\*[:：]\s*`?([0-9a-fA-F]{40})`?")
LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
PLACEHOLDER_RE = re.compile(r"待填写")
FEISHU_WEBHOOK_RE = re.compile(r"feishu_webhook|FEISHU_WEBHOOK")
CHANGE_REF_RE = re.compile(r"CHANGE-(\d{8})-(\d{3})")
REF_CLAIM_RE = re.compile(r"ref/.*(?:真源|运行依赖|fixture\s*生成器)|(?:真源|运行依赖|fixture\s*生成器).*ref/")

SAFE_WEBHOOK_CONTEXT = ("已删除", "禁止", "不得恢复", "legacy", "历史", "兼容")
SAFE_REF_CONTEXT = ("禁止", "不得", "参考", "人工阅读", "非运行依赖", "legacy", "历史")
HARDENING_ACCEPTANCE_FRESHNESS = 2


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""


def run_git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )


def get_stage() -> str:
    text = _read(AGENTS_FILE)
    match = re.search(r"PROJECT_STAGE\s*=\s*(EXPLORATION|HARDENING)", text)
    return match.group(1) if match else "UNKNOWN"


def _active_docs() -> list[Path]:
    files: list[Path] = []
    if not DOCS_DIR.exists():
        return files
    for path in DOCS_DIR.rglob("*.md"):
        rel = path.relative_to(DOCS_DIR)
        if rel.parts and rel.parts[0] == "archive":
            continue
        if rel.parts and rel.parts[0] == "current":
            # legacy current is read-only and does not gate active work
            continue
        files.append(path)
    return sorted(files)


def _all_markdown_for_links() -> list[Path]:
    # Active docs only. Archive link rot must not block current exploration work.
    return _active_docs()


def _is_valid_commit(sha: str) -> bool:
    result = run_git("cat-file", "-t", sha)
    return result.returncode == 0 and result.stdout.strip() == "commit"


def _is_ancestor(sha: str, rev: str = "HEAD") -> bool:
    return run_git("merge-base", "--is-ancestor", sha, rev).returncode == 0


def _commits_ahead(sha: str, rev: str = "HEAD") -> int | None:
    result = run_git("rev-list", "--count", rev, f"^{sha}")
    if result.returncode != 0:
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


def check_top_level_dirs() -> list[str]:
    if not DOCS_DIR.exists():
        return []
    errors: list[str] = []
    for child in DOCS_DIR.iterdir():
        if child.is_dir() and child.name not in ACTIVE_TOP_LEVEL_DIRS:
            errors.append(f"unregistered docs top-level directory: docs/{child.name}")
    return errors


def check_local_links() -> list[str]:
    errors: list[str] = []
    for path in _all_markdown_for_links():
        text = _read(path)
        for _label, raw_target in LINK_RE.findall(text):
            target = raw_target.strip()
            if not target or target.startswith(("http://", "https://", "mailto:", "#", "data:")):
                continue
            target = unquote(target.split("#", 1)[0])
            if not target:
                continue
            resolved = (path.parent / target).resolve()
            if not resolved.exists():
                errors.append(f"broken local link: {path.relative_to(REPO_ROOT)} -> {raw_target}")
    return errors


def check_placeholders() -> list[str]:
    """Placeholder check applies only to forward-looking active product/impl docs.

    Historical `docs/changes/` records and the Change TEMPLATE legitimately contain
    "待填写" as pending/recorded-state markers; forcing them to resolve placeholders
    on every Exploration iteration would recreate governance-driven busywork without
    guarding current product correctness. Runbooks may contain deliberate operational
    blanks, so they are also excluded here.
    """
    placeholders_scanned_dirs = {
        "prd", "maps", "contracts", "decisions", "acceptance", "evidence", "work",
    }
    errors: list[str] = []
    for path in _active_docs():
        rel = path.relative_to(DOCS_DIR)
        if rel.parts and rel.parts[0] not in placeholders_scanned_dirs:
            continue
        for line_no, line in enumerate(_read(path).splitlines(), 1):
            if PLACEHOLDER_RE.search(line):
                errors.append(f"active document contains placeholder: {path.relative_to(REPO_ROOT)}:{line_no}")
    return errors


def check_webhook_regression() -> list[str]:
    errors: list[str] = []
    candidates: list[Path] = []
    for folder in ("prd", "maps"):
        base = DOCS_DIR / folder
        if base.exists():
            candidates.extend(sorted(base.glob("*.md")))
    for path in candidates:
        for line_no, line in enumerate(_read(path).splitlines(), 1):
            if FEISHU_WEBHOOK_RE.search(line) and not any(ctx in line for ctx in SAFE_WEBHOOK_CONTEXT):
                errors.append(f"feishu_webhook restored as active behavior: {path.relative_to(REPO_ROOT)}:{line_no}")
    return errors


def check_ref_claims() -> list[str]:
    errors: list[str] = []
    candidates = [AGENTS_FILE]
    for folder in ("prd", "maps"):
        base = DOCS_DIR / folder
        if base.exists():
            candidates.extend(base.glob("*.md"))
    for path in candidates:
        for line_no, line in enumerate(_read(path).splitlines(), 1):
            if REF_CLAIM_RE.search(line) and not any(ctx in line for ctx in SAFE_REF_CONTEXT):
                errors.append(f"ref/ claimed as active truth/runtime dependency: {path.relative_to(REPO_ROOT)}:{line_no}")
    return errors


def _change_exists(change_id: str) -> bool:
    changes = DOCS_DIR / "changes"
    if not changes.exists():
        return False
    return any(p.is_file() and change_id in p.name for p in changes.rglob("*.md"))


def check_change_references() -> list[str]:
    errors: list[str] = []
    for path in _active_docs():
        for change_id in sorted(set(CHANGE_REF_RE.findall(_read(path)))):
            cid = f"CHANGE-{change_id[0]}-{change_id[1]}"
            if not _change_exists(cid):
                errors.append(f"unresolved Change reference: {path.relative_to(REPO_ROOT)} -> {cid}")
    return errors


def check_map_baseline() -> list[str]:
    """Map baseline is always required to be real/ancestral, but not ultra-fresh in Exploration."""
    if not MAP_BASELINE_FILE.exists():
        return []
    matches = BASELINE_RE.findall(_read(MAP_BASELINE_FILE))
    if not matches:
        return ["docs/maps/00-system-overview.md missing 40-char verified baseline"]
    sha = matches[0].lower()
    errors: list[str] = []
    if not _is_valid_commit(sha):
        return [f"map baseline is not a real git commit: {sha}"]
    if not _is_ancestor(sha):
        return [f"map baseline is not an ancestor of HEAD: {sha}"]
    # Map freshness is informational during Exploration and is intentionally not
    # a hard gate. Maps require separate user authorization to update; forcing a
    # commit-distance threshold would recreate governance-driven busywork.
    return errors


ACCEPTANCE_MATRIX_DATE_RE = re.compile(r"(?:20\d{2}[-_]\d{1,2}[-_]\d{1,2})")


def _acceptance_matrix_sort_key(path: Path) -> tuple:
    """Deterministic ordering: (is_dated, date_tuple, name). Never relies on filesystem mtime."""
    match = ACCEPTANCE_MATRIX_DATE_RE.search(path.name)
    if match:
        try:
            year, month, day = (int(part) for part in re.split(r"[-_]", match.group(0)))
            return (1, (year, month, day), path.name)
        except ValueError:
            pass
    return (0, (0, 0, 0), path.name)


def _latest_acceptance_matrix() -> Path | None:
    candidates: list[Path] = []
    for pattern in (
        "changes/**/PRD-Acceptance-Matrix-*.md",
        "acceptance/**/*Matrix*.md",
        "acceptance/**/*matrix*.md",
    ):
        candidates.extend(DOCS_DIR.glob(pattern))
    candidates = [p for p in candidates if p.is_file()]
    if not candidates:
        return None
    # 稳定命名规则：优先取带 YYYY-MM-DD 日期且日期最新者；不依赖文件系统 mtime。
    return max(candidates, key=_acceptance_matrix_sort_key)


def check_hardening_acceptance() -> list[str]:
    if get_stage() != "HARDENING":
        return []
    matrix = _latest_acceptance_matrix()
    if matrix is None:
        return ["HARDENING requires an acceptance matrix"]
    matches = ACCEPTANCE_BASELINE_RE.findall(_read(matrix))
    if not matches:
        return [f"HARDENING acceptance matrix missing baseline: {matrix.relative_to(REPO_ROOT)}"]
    sha = matches[0].lower()
    if not _is_valid_commit(sha):
        return [f"HARDENING acceptance baseline is not a real commit: {sha}"]
    if not _is_ancestor(sha):
        return [f"HARDENING acceptance baseline is not ancestor of HEAD: {sha}"]
    ahead = _commits_ahead(sha)
    if ahead is None:
        return [f"cannot calculate acceptance baseline distance: {sha}"]
    if ahead > HARDENING_ACCEPTANCE_FRESHNESS:
        return [
            f"HARDENING acceptance baseline stale: {ahead} commits > {HARDENING_ACCEPTANCE_FRESHNESS}"
        ]
    return []


def check_stage() -> list[str]:
    stage = get_stage()
    if stage not in {"EXPLORATION", "HARDENING"}:
        return ["AGENTS.md must declare PROJECT_STAGE = EXPLORATION or HARDENING"]
    return []


def check() -> list[str]:
    errors: list[str] = []
    for fn in (
        check_stage,
        check_top_level_dirs,
        check_local_links,
        check_placeholders,
        check_webhook_regression,
        check_ref_claims,
        check_change_references,
        check_map_baseline,
        check_hardening_acceptance,
    ):
        errors.extend(fn())
    return errors


def main() -> int:
    errors = check()
    if errors:
        print("Docs consistency FAILED:")
        for error in errors:
            print(f"- {error}")
        return 1
    print(f"Docs consistency PASS ({get_stage()} mode).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
