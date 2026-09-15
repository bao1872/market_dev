#!/usr/bin/env python3
"""T0 — final changed-files static gate.

Verifies that the FINAL changed files (between an explicit BASE and HEAD) are
lint-clean, instead of proving a linter "would work" on a synthetic probe.

This closes three false-green holes that the previous `origin/dev...HEAD` default
produced:

  * after `push origin/dev`, origin/dev == HEAD => empty diff => silent skip;
  * deleted files were handed to ruff/mypy/eslint (missing path);
  * no manifest recorded WHAT was actually checked.

Scope / constraints (Task 005-A):
  * This is a local verification tier, NOT a new governance Level; it does not
    replace Level 1/2/3 routing.
  * Changed-file identity must be explicit (--base / --head); it never assumes
    origin/dev represents "this task".
  * No production business code is touched; AGENTS/rules/panji-verify are
    out of scope; no second deploy/verify path is added.

Checkers:
  * Python  -> real Ruff on the final files.
  * Python  -> changed-file Mypy (reuses the existing methodology:
    `--no-incremental --follow-imports=skip --show-error-codes`, applied to the
    files T0 itself identified, so untracked junk like `.tmp_test/` is excluded).
  * Frontend (TS/TSX/JS/JSX) -> ESLint on the changed files (skipped when the
    binary or the files are absent).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"
FRONTEND = REPO_ROOT / "frontend"

PY_SUFFIXES = {".py"}
FE_SUFFIXES = {".ts", ".tsx", ".js", ".jsx"}

# Untracked paths under these fragments are never worth linting (scratch dirs,
# build output, venvs). Used so the worktree mode can include genuinely new
# source files without dragging in `.tmp_*` / `dist*` / `.workbuddy` junk.
JUNK_PATH_FRAGMENTS = (
    ".tmp",
    "node_modules",
    "/dist",
    "dist-marketing",
    ".workbuddy",
    "__pycache__",
    ".venv",
    "experiments/",
)


def _is_junk_path(path: str) -> bool:
    return any(frag in path for frag in JUNK_PATH_FRAGMENTS)


@dataclass
class FileChange:
    status: str
    path: str
    deleted: bool = False


@dataclass
class Manifest:
    base: str
    head: str
    changed_paths: list[str] = field(default_factory=list)
    existing_changed_paths: list[str] = field(default_factory=list)
    deleted_paths: list[str] = field(default_factory=list)
    python_files: list[str] = field(default_factory=list)
    frontend_files: list[str] = field(default_factory=list)
    other_files: list[str] = field(default_factory=list)


def parse_name_status(raw: str) -> list[FileChange]:
    """Parse `git diff --name-status` output into FileChange entries.

    Pure (no git/IO). Handles M/A/D/R/C/T and renamed old->new paths:
    a rename/copy yields one existing entry (new path) and one deleted entry
    (old path), so the deleted old path is never passed to a checker.
    """
    changes: list[FileChange] = []
    for line in raw.splitlines():
        line = line.rstrip("\n")
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status_field = parts[0]
        letter = status_field[0]
        if letter in ("R", "C"):
            old_path = parts[1]
            new_path = parts[2] if len(parts) > 2 else parts[1]
            changes.append(FileChange("A", new_path, deleted=False))
            changes.append(FileChange("D", old_path, deleted=True))
        elif letter == "D":
            changes.append(FileChange("D", parts[1], deleted=True))
        else:
            changes.append(FileChange(letter, parts[1], deleted=False))
    return changes


def _git(args: list[str]) -> str:
    res = subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {res.stderr.strip()}")
    return res.stdout


def git_diff_name_status(base: str, head: str, worktree: bool) -> str:
    """Return `git diff --name-status` output for the requested range.

    Committed range uses `BASE...HEAD` (excludes untracked). The worktree mode
    unions the unstaged and staged diffs against BASE (also excludes untracked,
    which keeps `.tmp_*` junk out of the manifest).
    """
    if worktree:
        chunks = [
            _git(["diff", "--name-status", base]),
            _git(["diff", "--name-status", "--cached", base]),
        ]
        try:
            untracked = _git(["ls-files", "--others", "--exclude-standard"]).splitlines()
        except RuntimeError:
            untracked = []
        for path in untracked:
            suffix = Path(path).suffix
            if suffix in PY_SUFFIXES or suffix in FE_SUFFIXES:
                if not _is_junk_path(path):
                    chunks.append(f"A\t{path}")
        return "\n".join(chunks)
    return _git(["diff", "--name-status", f"{base}...{head}"])


def classify(base: str, head: str, changes: list[FileChange]) -> Manifest:
    manifest = Manifest(base=base, head=head)
    seen: set[str] = set()
    for change in changes:
        if change.path in seen:
            continue
        seen.add(change.path)
        manifest.changed_paths.append(change.path)
        if change.deleted:
            manifest.deleted_paths.append(change.path)
            continue
        manifest.existing_changed_paths.append(change.path)
        suffix = Path(change.path).suffix
        if suffix in PY_SUFFIXES:
            manifest.python_files.append(change.path)
        elif suffix in FE_SUFFIXES:
            manifest.frontend_files.append(change.path)
        else:
            manifest.other_files.append(change.path)
    return manifest


def _resolve_ruff() -> str:
    local = BACKEND / ".venv" / "bin" / "ruff"
    return str(local) if local.exists() else "ruff"


def run_ruff(python_files: list[str]) -> tuple[int, str]:
    """Run Ruff on the final Python files.

    Backend files are checked with the backend config (cwd=backend); non-backend
    files (e.g. this script) are checked with the backend config applied
    explicitly so they meet the same standard. Deleted files are never passed in.
    """
    if not python_files:
        return 0, "(no python files to lint)"
    ruff = _resolve_ruff()
    backend_files = [p for p in python_files if p.startswith("backend/")]
    other_files = [p for p in python_files if not p.startswith("backend/")]
    outputs: list[str] = []
    rc = 0
    if backend_files:
        rel = [p[len("backend/"):] for p in backend_files]
        cmd = [ruff, "check", "--output-format", "concise", *rel]
        res = subprocess.run(cmd, cwd=str(BACKEND), capture_output=True, text=True)
        rc = rc or res.returncode
        if res.stdout.strip():
            outputs.append(res.stdout.strip())
        if res.stderr.strip():
            outputs.append(res.stderr.strip())
    if other_files:
        cmd = [
            ruff,
            "check",
            "--config",
            str(BACKEND / "pyproject.toml"),
            "--output-format",
            "concise",
            *other_files,
        ]
        res = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True)
        rc = rc or res.returncode
        if res.stdout.strip():
            outputs.append(res.stdout.strip())
        if res.stderr.strip():
            outputs.append(res.stderr.strip())
    return rc, "\n".join(outputs).strip()


def run_mypy(python_files: list[str]) -> tuple[int, str]:
    """Run changed-file Mypy on the final Python files (reuses the existing
    changed-file methodology, applied to T0's own authoritative file list)."""
    backend_py = [p for p in python_files if p.startswith("backend/")]
    non_backend_py = [p for p in python_files if not p.startswith("backend/")]
    outputs: list[str] = []
    rc = 0
    venv_py = BACKEND / ".venv" / "bin" / "python"
    if not venv_py.exists():
        return 2, "[T0] backend/.venv missing; cannot run mypy"
    if backend_py:
        rel = [p[len("backend/"):] for p in backend_py]
        cmd = [
            str(venv_py),
            "-m",
            "mypy",
            "--no-incremental",
            "--follow-imports=skip",
            "--show-error-codes",
            *rel,
        ]
        res = subprocess.run(cmd, cwd=str(BACKEND), capture_output=True, text=True)
        rc = rc or res.returncode
        if res.stdout.strip():
            outputs.append(res.stdout.strip())
        if res.stderr.strip():
            outputs.append(res.stderr.strip())
    if non_backend_py:
        cmd = [
            str(venv_py),
            "-m",
            "mypy",
            "--no-incremental",
            "--follow-imports=skip",
            "--show-error-codes",
            "--config-file",
            str(BACKEND / "pyproject.toml"),
            *non_backend_py,
        ]
        res = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True)
        rc = rc or res.returncode
        if res.stdout.strip():
            outputs.append(res.stdout.strip())
        if res.stderr.strip():
            outputs.append(res.stderr.strip())
    return rc, "\n".join(outputs).strip()


def run_eslint(frontend_files: list[str], no_eslint: bool) -> tuple[int, str, bool]:
    """Run ESLint on changed frontend files. Returns (rc, output, skipped)."""
    if no_eslint:
        return 0, "(eslint skipped by --no-eslint)", True
    if not frontend_files:
        return 0, "(no frontend files to lint)", True
    eslint = FRONTEND / "node_modules" / ".bin" / "eslint"
    if not eslint.exists():
        return 0, "(eslint binary missing; skipped)", True
    rel = [
        str(Path(p).relative_to("frontend"))
        for p in frontend_files
        if p.startswith("frontend/")
    ]
    if not rel:
        return 0, "(no frontend files to lint)", True
    res = subprocess.run([str(eslint), *rel], cwd=str(FRONTEND), capture_output=True, text=True)
    out = (res.stdout + res.stderr).strip()
    return res.returncode, out, False


def evaluate(manifest: Manifest, results: dict, allow_empty: bool) -> bool:
    if not manifest.changed_paths:
        return bool(allow_empty)
    return all(rc == 0 for rc, _ in (results["ruff"], results["mypy"])) and (
        results["eslint"][2] or results["eslint"][0] == 0
    )


def _rev_parse(ref: str) -> str:
    try:
        return _git(["rev-parse", "--short", ref]).strip()
    except RuntimeError:
        return ref


def format_report(manifest: Manifest, results: dict, passed: bool) -> str:
    ruff_rc, ruff_out = results["ruff"]
    mypy_rc, mypy_out = results["mypy"]
    es_rc, es_out, es_skip = results["eslint"]
    lines = [
        "=== T0 FINAL CHANGED-FILES GATE ===",
        f"BASE_SHA   : {manifest.base} ({_rev_parse(manifest.base)})",
        f"HEAD_SHA   : {manifest.head}",
        f"changed    : {len(manifest.changed_paths)}",
        f"deleted    : {len(manifest.deleted_paths)}",
        f"python     : {manifest.python_files}",
        f"frontend   : {manifest.frontend_files}",
        f"other      : {manifest.other_files}",
        f"deleted    : {manifest.deleted_paths}",
        "--- ruff ---",
        f"rc={ruff_rc}",
        ruff_out,
        "--- mypy ---",
        f"rc={mypy_rc}",
        mypy_out,
        "--- eslint ---",
        f"skipped={es_skip} rc={es_rc}",
        es_out,
        "=== T0 " + ("PASS" if passed else "FAIL") + " ===",
    ]
    return "\n".join(line for line in lines if line != "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="T0 final changed-files static gate")
    parser.add_argument("--base", required=True, help="explicit base SHA/ref (required)")
    parser.add_argument("--head", default="HEAD", help="head SHA/ref (default HEAD)")
    parser.add_argument(
        "--worktree",
        action="store_true",
        help="check uncommitted working tree vs --base instead of --head",
    )
    parser.add_argument("--json", action="store_true", help="emit a JSON report")
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="do not fail when the manifest is empty (e.g. base==head)",
    )
    parser.add_argument("--no-eslint", action="store_true")
    parser.add_argument("--no-mypy", action="store_true")
    args = parser.parse_args(argv)

    head_label = "WORKTREE (uncommitted)" if args.worktree else args.head
    try:
        raw = git_diff_name_status(args.base, args.head, args.worktree)
    except RuntimeError as exc:
        print(f"[T0] git failed: {exc}", file=sys.stderr)
        return 2

    changes = parse_name_status(raw)
    manifest = classify(args.base, head_label, changes)

    results: dict = {}
    results["ruff"] = run_ruff(manifest.python_files)
    results["mypy"] = (0, "(mypy skipped by --no-mypy)") if args.no_mypy else run_mypy(
        manifest.python_files
    )
    results["eslint"] = run_eslint(manifest.frontend_files, args.no_eslint)

    passed = evaluate(manifest, results, args.allow_empty)
    report = format_report(manifest, results, passed)
    print(report)

    if args.json:
        payload = {
            "base": manifest.base,
            "head": manifest.head,
            "changed_count": len(manifest.changed_paths),
            "deleted_count": len(manifest.deleted_paths),
            "python_files": manifest.python_files,
            "frontend_files": manifest.frontend_files,
            "other_files": manifest.other_files,
            "deleted_paths": manifest.deleted_paths,
            "ruff_rc": results["ruff"][0],
            "mypy_rc": results["mypy"][0],
            "eslint_rc": results["eslint"][0],
            "eslint_skipped": results["eslint"][2],
            "passed": passed,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    if not manifest.changed_paths and not args.allow_empty:
        print(
            "[T0] EMPTY MANIFEST: nothing was actually checked. This usually means "
            "BASE==HEAD (e.g. origin/dev==HEAD after push). Pass explicit "
            "--base/--head, or --allow-empty if this is intentional.",
            file=sys.stderr,
        )
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
