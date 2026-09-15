"""T0 — final changed-files static gate (single authoritative owner).

T0 verifies that the FINAL changed files (between an explicit BASE and HEAD) are
lint/type/compile clean, instead of proving a linter "would work" on a synthetic
probe. This closes the false-green holes that the previous `origin/dev...HEAD`
default produced:

  * after `push origin/dev`, origin/dev == HEAD => empty diff => silent skip;
  * deleted files were handed to ruff/mypy/eslint (missing path);
  * no manifest recorded WHAT was actually checked.

Single owner (Task 005-R / Commit A):
  t0_gate.py is the ONLY authoritative source of changed-file identity AND the
  ONLY executor of every checker. The deprecated `scripts/quality/mypy-changed.sh`
  wrapper has been removed; this script is the sole entrypoint.

This implements the T0 already defined in rules/40-testing-quality.md. It is a
local verification tier, NOT a new governance Level; it does not replace
Level 1/2/3 routing, and it touches no production business code.

T0 contract (exploration-simplified policy, consistent with rules/40):
  Python changed files   -> Ruff (HARD), py_compile (HARD).
  New backend/app/*.py    -> Mypy (HARD).
  Modified legacy backend/app/*.py -> Mypy (ADVISORY: runs, reports WARN, does
                          not block). Pre-existing legacy type debt is recorded,
                          not force-fixed by an unrelated bugfix.
  backend/tests/**/*.py   -> NOT in the Mypy gate (Ruff + py_compile + T1 only).
  scripts/quality/*.py    -> Ruff + py_compile + own tests (not production Mypy).
  Frontend changed source -> ESLint, TypeScript static check (tsc --noEmit).

Hard rules:
  * Changed-file identity is explicit (--base / --head); it never assumes
    origin/dev represents "this task".
  * A deleted file lives ONLY in deleted_paths; it is never passed to a checker.
  * A language with changed source but MISSING required tooling is a hard FAIL
    (TOOLING_MISSING). Tool absence must never degrade to a silent PASS.
  * Git copy (C) keeps the old file; only the new path is an existing change.
    Git rename (R) deletes the old path and adds the new path.
  * Mypy on modified legacy production files is ADVISORY: it runs and is reported
    (WARN), but a non-zero result does NOT block T0. New production files and
    tooling-missing remain HARD failures.
  * Committed-range identity is fail-closed: BASE must be an ancestor of HEAD,
    otherwise the three-dot diff silently uses the merge-base and does NOT
    represent the claimed task delta (IDENTITY_INVALID). Worktree mode is exempt
    because HEAD is the current working tree, not a fixed ancestor chain.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
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
    python_status: dict[str, str] = field(default_factory=dict)
    frontend_files: list[str] = field(default_factory=list)
    other_files: list[str] = field(default_factory=list)


@dataclass
class CheckerResult:
    rc: int
    out: str
    skipped: bool = False
    tooling_missing: bool = False
    elapsed: float = 0.0


def parse_name_status(raw: str) -> list[FileChange]:
    """Parse `git diff --name-status` output into FileChange entries (pure).

    Git status semantics:
      * M/A/T            -> existing changed path.
      * D                -> deleted path (lives only in deleted_paths).
      * R<score> a b     -> rename: `b` is the existing change, `a` is deleted.
      * C<score> a b     -> copy:   `b` is the existing change; `a` STILL EXISTS
                            and is intentionally NOT recorded as deleted (a copy
                            leaves the source untouched).

    A copy therefore yields a single existing entry; a rename yields one
    existing entry plus one deleted entry. Deleted paths are never passed to a
    checker.
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
        if letter == "R":
            old_path = parts[1]
            new_path = parts[2] if len(parts) > 2 else parts[1]
            changes.append(FileChange("A", new_path, deleted=False))
            changes.append(FileChange("D", old_path, deleted=True))
        elif letter == "C":
            new_path = parts[2] if len(parts) > 2 else parts[1]
            changes.append(FileChange("A", new_path, deleted=False))
        elif letter == "D":
            changes.append(FileChange("D", parts[1], deleted=True))
        else:
            changes.append(FileChange(letter, parts[1], deleted=False))
    return changes


def _git(args: list[str]) -> str:
    res = subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True,
        text=True, check=False,
    )
    if res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {res.stderr.strip()}")
    return res.stdout


def git_diff_name_status(base: str, head: str, worktree: bool) -> str:
    """Return `git diff --name-status` output for the requested range.

    Committed range (default) uses `BASE...HEAD`, which compares two commits and
    therefore EXCLUDES untracked, unstaged, and staged-not-in-HEAD changes. Only
    `--worktree` unions the unstaged diff, the staged (cached) diff, and
    genuinely new untracked source files (filtered by suffix + junk).
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
            if (suffix in PY_SUFFIXES or suffix in FE_SUFFIXES) and not _is_junk_path(path):
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
            manifest.python_status[change.path] = change.status
        elif suffix in FE_SUFFIXES:
            manifest.frontend_files.append(change.path)
        else:
            manifest.other_files.append(change.path)
    return manifest


def _resolve_ruff() -> str:
    local = BACKEND / ".venv" / "bin" / "ruff"
    return str(local) if local.exists() else "ruff"


def _resolve_mypy_python() -> str | None:
    venv_py = BACKEND / ".venv" / "bin" / "python"
    return str(venv_py) if venv_py.exists() else None


def _resolve_eslint() -> str | None:
    eslint = FRONTEND / "node_modules" / ".bin" / "eslint"
    return str(eslint) if eslint.exists() else None


def _resolve_tsc() -> str | None:
    tsc = FRONTEND / "node_modules" / ".bin" / "tsc"
    return str(tsc) if tsc.exists() else None


def _collect(res: subprocess.CompletedProcess, outputs: list[str]) -> None:
    if res.stdout.strip():
        outputs.append(res.stdout.strip())
    if res.stderr.strip():
        outputs.append(res.stderr.strip())


def run_ruff(python_files: list[str]) -> CheckerResult:
    """Run Ruff on the final Python files.

    Backend files are checked with the backend config (cwd=backend); non-backend
    files (e.g. this script) are checked with the backend config applied
    explicitly so they meet the same standard. A missing ruff binary is a hard
    FAIL (TOOLING_MISSING), never a silent pass. Deleted files are never passed.
    """
    if not python_files:
        return CheckerResult(0, "(no python files to lint)", skipped=True)
    ruff = _resolve_ruff()
    backend_files = [p for p in python_files if p.startswith("backend/")]
    other_files = [p for p in python_files if not p.startswith("backend/")]
    outputs: list[str] = []
    rc = 0
    try:
        if backend_files:
            rel = [p[len("backend/"):] for p in backend_files]
            res = subprocess.run(
                [ruff, "check", "--output-format", "concise", *rel],
                cwd=str(BACKEND),
                capture_output=True,
                text=True, check=False,
            )
            rc = rc or res.returncode
            _collect(res, outputs)
        if other_files:
            res = subprocess.run(
                [
                    ruff,
                    "check",
                    "--config",
                    str(BACKEND / "pyproject.toml"),
                    "--output-format",
                    "concise",
                    *other_files,
                ],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True, check=False,
            )
            rc = rc or res.returncode
            _collect(res, outputs)
    except FileNotFoundError:
        return CheckerResult(
            2, f"[T0] ruff binary missing (resolved={ruff}) (TOOLING_MISSING)", tooling_missing=True
        )
    return CheckerResult(rc, "\n".join(outputs).strip())


def run_py_compile(python_files: list[str]) -> CheckerResult:
    """Byte-compile the final Python files with `python -m py_compile`.

    Syntactic / bytecode validation only; zero extra config. The caller already
    excludes deleted files, so none are passed here.
    """
    if not python_files:
        return CheckerResult(0, "(no python files to compile)", skipped=True)
    try:
        res = subprocess.run(
            [sys.executable, "-m", "py_compile", *python_files],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True, check=False,
        )
    except FileNotFoundError:
        return CheckerResult(2, "[T0] python interpreter missing (TOOLING_MISSING)", tooling_missing=True)
    out = (res.stdout + res.stderr).strip()
    return CheckerResult(res.returncode, out)


def run_mypy(python_files: list[str]) -> CheckerResult:
    """Run changed-file Mypy on the final Python files.

    Reuses the existing changed-file methodology (`--no-incremental
    --follow-imports=skip --show-error-codes`), applied to T0's own
    authoritative file list, so untracked junk like `.tmp_test/` is excluded.
    Backend files run from backend/; non-backend files use the backend config
    explicitly. A missing venv python is a hard FAIL (TOOLING_MISSING).
    """
    backend_py = [p for p in python_files if p.startswith("backend/")]
    non_backend_py = [p for p in python_files if not p.startswith("backend/")]
    venv_py = _resolve_mypy_python()
    if venv_py is None:
        return CheckerResult(2, "[T0] backend/.venv missing; cannot run mypy (TOOLING_MISSING)", tooling_missing=True)
    outputs: list[str] = []
    rc = 0
    try:
        if backend_py:
            rel = [p[len("backend/"):] for p in backend_py]
            res = subprocess.run(
                [venv_py, "-m", "mypy", "--no-incremental", "--follow-imports=skip", "--show-error-codes", *rel],
                cwd=str(BACKEND),
                capture_output=True,
                text=True, check=False,
            )
            rc = rc or res.returncode
            _collect(res, outputs)
        if non_backend_py:
            res = subprocess.run(
                [
                    venv_py,
                    "-m",
                    "mypy",
                    "--no-incremental",
                    "--follow-imports=skip",
                    "--show-error-codes",
                    "--config-file",
                    str(BACKEND / "pyproject.toml"),
                    *non_backend_py,
                ],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True, check=False,
            )
            rc = rc or res.returncode
            _collect(res, outputs)
    except FileNotFoundError:
        return CheckerResult(2, "[T0] mypy invocation failed (TOOLING_MISSING)", tooling_missing=True)
    return CheckerResult(rc, "\n".join(outputs).strip())


def run_eslint(frontend_files: list[str]) -> CheckerResult:
    """Run ESLint on changed frontend files.

    A missing eslint binary WITH frontend changes is a hard FAIL
    (TOOLING_MISSING). When there are no frontend files the checker is skipped
    (which does not block the gate). The previous "missing binary => skipped =>
    PASS" false-green is removed.
    """
    if not frontend_files:
        return CheckerResult(0, "(no frontend files to lint)", skipped=True)
    eslint = _resolve_eslint()
    if eslint is None:
        return CheckerResult(2, "[T0] frontend eslint binary missing (TOOLING_MISSING)", tooling_missing=True)
    rel = [
        str(Path(p).relative_to("frontend"))
        for p in frontend_files
        if p.startswith("frontend/")
    ]
    if not rel:
        return CheckerResult(0, "(no frontend files to lint)", skipped=True)
    res = subprocess.run([eslint, *rel], cwd=str(FRONTEND), capture_output=True, text=True, check=False)
    out = (res.stdout + res.stderr).strip()
    return CheckerResult(res.returncode, out)


def run_tsc(frontend_files: list[str]) -> CheckerResult:
    """Run the TypeScript static check (`tsc --noEmit`) on the frontend project.

    `tsc --noEmit` validates the whole project per tsconfig (correctness-first;
    TypeScript needs project context, so it cannot type-check a single file in
    isolation). Triggered whenever frontend source changed. A missing tsc binary
    is a hard FAIL (TOOLING_MISSING). Real project-check timing is measured and
    reported; any T0-budget tuning belongs to later commits and must not alter
    rules/40.
    """
    if not frontend_files:
        return CheckerResult(0, "(no frontend files to type-check)", skipped=True)
    tsc = _resolve_tsc()
    if tsc is None:
        return CheckerResult(2, "[T0] frontend tsc binary missing (TOOLING_MISSING)", tooling_missing=True)
    res = subprocess.run([tsc, "--noEmit"], cwd=str(FRONTEND), capture_output=True, text=True, check=False)
    out = (res.stdout + res.stderr).strip()
    return CheckerResult(res.returncode, out)


# ADVISORY checkers report WARN and do NOT block T0. New production python files
# and tooling-missing remain HARD failures (see module docstring).
ADVISORY_CHECKERS = frozenset({"mypy_legacy"})


def evaluate(manifest: Manifest, results: dict[str, CheckerResult], allow_empty: bool) -> bool:
    """A gate passes iff every SCHEDULED HARD checker (non-skipped, non-advisory)
    returned rc==0 AND no checker was TOOLING_MISSING. Advisory checkers (e.g.
    mypy_legacy) may be non-zero and still pass. An empty manifest is a hard
    FAIL unless --allow-empty is explicit.
    """
    if not manifest.changed_paths:
        return bool(allow_empty)
    for name, r in results.items():
        if r.skipped:
            continue
        if r.tooling_missing:
            return False
        if r.rc != 0 and name not in ADVISORY_CHECKERS:
            return False
    return True


def _rev_parse(ref: str) -> str:
    try:
        return _git(["rev-parse", "--short", ref]).strip()
    except RuntimeError:
        return ref


def _format_checker(name: str, r: CheckerResult, advisory: bool = False) -> list[str]:
    if r.skipped:
        tag = "SKIP"
    elif r.tooling_missing:
        tag = "MISSING"
    elif r.rc == 0:
        tag = "PASS"
    elif advisory:
        tag = "WARN"
    else:
        tag = "FAIL"
    lines = [f"--- {name} [{tag}] rc={r.rc} elapsed={r.elapsed:.2f}s ---"]
    if r.out.strip():
        lines.append(r.out.strip())
    return lines


def format_report(manifest: Manifest, results: dict[str, CheckerResult], passed: bool) -> str:
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
    ]
    for name, r in results.items():
        lines.extend(_format_checker(name, r, advisory=name in ADVISORY_CHECKERS))
    lines.append("=== T0 " + ("PASS" if passed else "FAIL") + " ===")
    return "\n".join(line for line in lines if line != "")


def _assert_ancestor(base: str, head: str) -> bool:
    """Fail-closed identity gate for committed (non-worktree) ranges.

    A committed `BASE...HEAD` three-dot diff is really
    `merge-base(BASE, HEAD)..HEAD`. If BASE is NOT an ancestor of HEAD, Git
    silently falls back to the merge-base and emits a manifest that does NOT
    represent the claimed task delta. For the authoritative changed-file owner
    that is an identity error, not a usable result.
    """
    res = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "merge-base", "--is-ancestor", base, head],
        capture_output=True,
        text=True, check=False,
    )
    return res.returncode == 0


def _timed(fn, *args) -> CheckerResult:
    t0 = time.perf_counter()
    r = fn(*args)
    r.elapsed = time.perf_counter() - t0
    return r


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
    args = parser.parse_args(argv)

    head_label = "WORKTREE (uncommitted)" if args.worktree else args.head

    # Committed range identity gate (fail-closed). A three-dot diff is actually
    # merge-base(BASE, HEAD)..HEAD; if BASE is not an ancestor of HEAD, Git would
    # silently fall back to the merge-base and emit a manifest that does NOT
    # represent the claimed task delta. Refuse it. Worktree mode is exempt because
    # HEAD is the current working tree, not a fixed ancestor chain.
    if not args.worktree and not _assert_ancestor(args.base, args.head):
        print(
            "[T0] IDENTITY_INVALID: BASE is not an ancestor of HEAD. Refusing to derive a "
            "three-dot merge-base manifest, because it would silently use the merge-base and "
            "NOT represent the claimed task delta.",
            file=sys.stderr,
        )
        return 2

    try:
        raw = git_diff_name_status(args.base, args.head, args.worktree)
    except RuntimeError as exc:
        print(f"[T0] git failed: {exc}", file=sys.stderr)
        return 2

    changes = parse_name_status(raw)
    manifest = classify(args.base, head_label, changes)

    # Mypy policy (exploration-simplified): new backend/app/*.py => HARD;
    # modified legacy backend/app/*.py => ADVISORY (WARN, non-blocking);
    # backend/tests and scripts/quality are NOT in the production Mypy gate.
    prod_py = [p for p in manifest.python_files if p.startswith("backend/app/")]
    new_production = [p for p in prod_py if manifest.python_status.get(p) == "A"]
    modified_legacy = [p for p in prod_py if manifest.python_status.get(p) != "A"]
    results: dict[str, CheckerResult] = {
        "ruff": _timed(run_ruff, manifest.python_files),
        "py_compile": _timed(run_py_compile, manifest.python_files),
        "mypy_new": _timed(run_mypy, new_production),
        "mypy_legacy": _timed(run_mypy, modified_legacy),
        "eslint": _timed(run_eslint, manifest.frontend_files),
        "tsc": _timed(run_tsc, manifest.frontend_files),
    }

    passed = evaluate(manifest, results, args.allow_empty)
    print(format_report(manifest, results, passed))

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
            "checkers": {
                name: {
                    "rc": r.rc,
                    "skipped": r.skipped,
                    "tooling_missing": r.tooling_missing,
                    "advisory": name in ADVISORY_CHECKERS,
                    "elapsed_s": round(r.elapsed, 3),
                }
                for name, r in results.items()
            },
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
