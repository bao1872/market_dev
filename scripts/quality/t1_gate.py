#!/usr/bin/env python3
"""T1 — modified-scope unit gate (single authoritative owner of test selection).

T1 runs ONLY the unit tests that are directly related to the changed files,
instead of the full 6000+ pure-unit suite (T6). It is the second, mandatory
Exploration verification tier defined in rules/40-testing-quality.md:

    `T0 -> T1 -> [T2] -> ...`

T1 REUSES t0_gate.py as the single authoritative owner of changed-file identity
(BASE / HEAD / worktree / untracked / rename / copy / delete). It does NOT
reimplement any of that logic; t1_gate only maps changed files to test
selectors and runs them. Adding a second diff implementation here would violate
the "one semantic, one owner" governance rule.

Hard rules (Task 005-R / Commit B):
  * Changed-file identity comes from t0_gate (no private diff logic here).
  * Backend production (backend/app/**) changed with NO backend targeted test
    => T1_REQUIRED (non-zero). Never silently upgrade to the full suite.
  * Frontend production (frontend/src/**) changed with NO frontend targeted test
    => T1_REQUIRED (non-zero).
  * Explicit selectors are validated against allowed test paths; `..`, absolute
    paths, or non-existent files are rejected (no shell eval / injection).
  * A selected test set that collects 0 tests (deselected / not found) => FAIL,
    never "nothing ran => PASS".
  * Quality infra (scripts/quality/*.py) + quality test (scripts/quality/tests/*)
    => auto-run the changed quality tests.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import t0_gate

# Monkeypatchable existence check (lets pure tests simulate deleted/missing files).
_existing = os.path.exists

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"
FRONTEND = REPO_ROOT / "frontend"
QUALITY = REPO_ROOT / "scripts" / "quality"

BACKEND_TEST_PREFIX = "backend/tests/"
BACKEND_APP_PREFIX = "backend/app/"
FRONTEND_PROD_PREFIX = "frontend/src/"
FE_PROD_SUFFIXES = (".ts", ".tsx", ".js", ".jsx")
FE_TEST_SUFFIXES = (".test.ts", ".test.tsx")
QUALITY_TEST_PREFIX = "scripts/quality/tests/"
QUALITY_INFRA_PREFIX = "scripts/quality/"

# Node test-runner summary emits lines like "ℹ tests 3" / "ℹ passing 3".
_FRONTEND_ZERO_TESTS = re.compile(r"tests\s+0\b", re.IGNORECASE)


@dataclass
class Selection:
    backend_production: list[str] = field(default_factory=list)
    backend_auto: list[str] = field(default_factory=list)
    frontend_production: list[str] = field(default_factory=list)
    frontend_auto: list[str] = field(default_factory=list)
    quality_infra: list[str] = field(default_factory=list)
    quality_auto: list[str] = field(default_factory=list)
    other: list[str] = field(default_factory=list)


def categorize(changed_paths: list[str]) -> Selection:
    """Map repo-relative changed paths to T1 test selectors (pure, no IO).

    Selectors are stored relative to the directory the runner executes in:
      * backend_auto  -> relative to backend/  (e.g. "tests/test_foo.py")
      * frontend_auto -> relative to frontend/ (e.g. "src/x/__tests__/f.test.ts")
      * quality_auto  -> repo-relative (e.g. "scripts/quality/tests/test_x.py")
    Existence is NOT checked here; validation happens in the runner.
    """
    sel = Selection()
    for p in changed_paths:
        if p.startswith(BACKEND_TEST_PREFIX) and p.endswith(".py"):
            sel.backend_auto.append(p[len("backend/"):])
        elif p.startswith(BACKEND_APP_PREFIX) and p.endswith(".py"):
            sel.backend_production.append(p)
        elif _is_frontend_test(p):
            # test files under frontend/src must be classified as tests, NOT
            # production, even though they share the .ts/.tsx suffix.
            sel.frontend_auto.append(p[len("frontend/"):])
        elif (
            p.startswith(FRONTEND_PROD_PREFIX)
            and p.endswith(FE_PROD_SUFFIXES)
        ):
            sel.frontend_production.append(p)
        elif p.startswith(QUALITY_TEST_PREFIX) and p.endswith(".py"):
            sel.quality_auto.append(p)
        elif (
            p.startswith(QUALITY_INFRA_PREFIX)
            and not p.startswith(QUALITY_TEST_PREFIX)
            and (p.endswith(".py") or p.endswith(".sh"))
        ):
            sel.quality_infra.append(p)
        else:
            sel.other.append(p)
    return sel


def _is_frontend_test(p: str) -> bool:
    if not p.startswith("frontend/"):
        return False
    if p.startswith("frontend/scripts/contract-tests/") and p.endswith(".test.ts"):
        return True
    return "/__tests__/" in p and p.endswith(FE_TEST_SUFFIXES)


def validate_backend_selectors(raw: str, repo_root: Path) -> tuple[list[str], list[str]]:
    """Validate comma-separated backend explicit selectors.

    Each entry is `path[::nodeid]`. The path part must be a real file under
    backend/tests/, must not be absolute, and must not contain `..`. Returns
    (accepted, rejected).
    """
    accepted: list[str] = []
    rejected: list[str] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        path_part = entry.split("::", 1)[0]
        if path_part.startswith("/") or ".." in path_part or not path_part.startswith("tests/"):
            rejected.append(entry)
            continue
        if not _existing(repo_root / "backend" / path_part):
            rejected.append(entry)
            continue
        accepted.append(entry)
    return accepted, rejected


def validate_frontend_selectors(raw: str, repo_root: Path) -> tuple[list[str], list[str]]:
    """Validate comma-separated frontend explicit selectors.

    Each entry is a path (relative to frontend/) that must exist and must not be
    absolute or contain `..`. Returns (accepted, rejected).
    """
    accepted: list[str] = []
    rejected: list[str] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        path_part = entry.split("::", 1)[0]
        if path_part.startswith("/") or ".." in path_part:
            rejected.append(entry)
            continue
        if not _existing(repo_root / "frontend" / path_part):
            rejected.append(entry)
            continue
        accepted.append(entry)
    return accepted, rejected


def changed_paths_via_t0(base: str, head: str, worktree: bool) -> list[str]:
    """Build the changed-file list by delegating to t0_gate (identity owner)."""
    raw = t0_gate.git_diff_name_status(base, head, worktree)
    changes = t0_gate.parse_name_status(raw)
    manifest = t0_gate.classify(base, head, changes)
    return manifest.changed_paths


def _resolve_pytest() -> str:
    venv = BACKEND / ".venv" / "bin" / "pytest"
    return str(venv) if _existing(venv) else "pytest"


def _resolve_tsx() -> str | None:
    tsx = FRONTEND / "node_modules" / ".bin" / "tsx"
    return str(tsx) if _existing(tsx) else None


def _pytest_summary(out: str) -> str:
    for line in reversed(out.splitlines()):
        low = line.lower()
        if "passed" in low or "failed" in low or "error" in low or "warning" in low:
            return line.strip()
    lines = out.splitlines()
    return lines[-1] if lines else ""


def run_backend_tests(selectors: list[str]) -> tuple[int, str]:
    """Run targeted backend pytest (PURE_UNIT). Returns (rc, human_summary)."""
    if not selectors:
        return 0, "(no backend tests selected)"
    pytest = _resolve_pytest()
    env = {
        **os.environ,
        "PURE_UNIT_TEST": "1",
        "APP_ENV": "test",
        "REDIS_URL": "redis://localhost:6379/15",
        "CAPTURE_STATIC_DIR": "/tmp/panji-ci-captures",
    }
    try:
        res = subprocess.run(
            [pytest, "-m", "not postgres and not external_data", "--tb=short", "-q", *selectors],
            cwd=str(BACKEND),
            env=env,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return 2, "pytest binary missing (TOOLING_MISSING)"
    rc = res.returncode
    out = (res.stdout + res.stderr).strip()
    # pytest returns 4 (usage error) / 5 (no tests collected) when a selector
    # matches nothing or is fully deselected => non-zero => FAIL (no false green).
    return rc, _pytest_summary(out)


def run_frontend_tests(selectors: list[str]) -> tuple[int, str]:
    """Run targeted frontend tests via tsx --test. Returns (rc, human_summary)."""
    if not selectors:
        return 0, "(no frontend tests selected)"
    tsx = _resolve_tsx()
    if tsx is None:
        return 2, "frontend tsx binary missing (TOOLING_MISSING)"
    try:
        res = subprocess.run(
            [tsx, "--test", *selectors],
            cwd=str(FRONTEND),
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return 2, "tsx binary missing (TOOLING_MISSING)"
    rc = res.returncode
    out = (res.stdout + res.stderr).strip()
    # tsx --test exits 0 even when a file yields 0 tests; that is still a
    # "nothing was actually checked" false green, so fail it explicitly.
    if rc == 0 and _FRONTEND_ZERO_TESTS.search(out):
        return 1, f"0 frontend tests executed (false green refused): {_pytest_summary(out)}"
    return rc, _pytest_summary(out)


def run_quality_tests(selectors: list[str]) -> tuple[int, str]:
    """Run targeted quality-infra pytest from scripts/quality. (rc, summary)."""
    if not selectors:
        return 0, "(no quality tests selected)"
    env = {**os.environ, "PURE_UNIT_TEST": "1"}
    try:
        res = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", *selectors],
            cwd=str(QUALITY),
            env=env,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return 2, "python interpreter missing (TOOLING_MISSING)"
    rc = res.returncode
    out = (res.stdout + res.stderr).strip()
    return rc, _pytest_summary(out)


def _format_selection(title: str, items: list[str], none_label: str = "(none)") -> str:
    if not items:
        return f"{title}:\n  {none_label}"
    return f"{title}:\n" + "\n".join(f"  {i}" for i in items)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="T1 modified-scope unit gate")
    parser.add_argument("--base", required=True, help="explicit base SHA/ref (required)")
    parser.add_argument("--head", default="HEAD", help="head SHA/ref (default HEAD)")
    parser.add_argument(
        "--worktree",
        action="store_true",
        help="check uncommitted working tree vs --base instead of --head",
    )
    parser.add_argument(
        "--backend-tests",
        default="",
        help="comma-separated explicit backend selectors (no shell eval)",
    )
    parser.add_argument(
        "--frontend-tests",
        default="",
        help="comma-separated explicit frontend selectors (no shell eval)",
    )
    args = parser.parse_args(argv)

    # Committed range identity gate (reuse t0_gate's owner logic, fail-closed).
    if not args.worktree and not t0_gate._assert_ancestor(args.base, args.head):
        print(
            "[T1] IDENTITY_INVALID: BASE is not an ancestor of HEAD. Refusing to derive a "
            "three-dot merge-base manifest, because it would not represent the claimed task delta.",
            file=sys.stderr,
        )
        return 2

    try:
        changed = changed_paths_via_t0(args.base, args.head, args.worktree)
    except RuntimeError as exc:
        print(f"[T1] git failed: {exc}", file=sys.stderr)
        return 2

    sel = categorize(changed)

    # Validate auto-selected test files exist on disk (drop deleted test files).
    backend_auto, backend_auto_rej = validate_backend_selectors(
        ",".join(sel.backend_auto), REPO_ROOT
    )
    frontend_auto, frontend_auto_rej = validate_frontend_selectors(
        ",".join(sel.frontend_auto), REPO_ROOT
    )

    # Validate explicit selectors; rejected ones are a hard failure (no silent skip).
    backend_explicit, backend_rej = validate_backend_selectors(args.backend_tests, REPO_ROOT)
    frontend_explicit, frontend_rej = validate_frontend_selectors(args.frontend_tests, REPO_ROOT)

    rejected = backend_rej + frontend_rej + backend_auto_rej + frontend_auto_rej

    has_backend_eval = bool(backend_auto or backend_explicit)
    has_frontend_eval = bool(frontend_auto or frontend_explicit)
    has_quality_eval = bool(sel.quality_auto)

    # T1_REQUIRED: production code changed but no targeted test evidence.
    required_reasons: list[str] = []
    if sel.backend_production and not has_backend_eval:
        required_reasons.append(
            "backend production changed but no backend targeted test identified"
        )
    if sel.frontend_production and not has_frontend_eval:
        required_reasons.append(
            "frontend production changed but no frontend targeted test identified"
        )

    backend_rc, backend_out = (0, "(not applicable)") if not has_backend_eval else run_backend_tests(
        backend_auto + backend_explicit
    )
    frontend_rc, frontend_out = (
        (0, "(not applicable)") if not has_frontend_eval else run_frontend_tests(
            frontend_auto + frontend_explicit
        )
    )
    quality_rc, quality_out = (
        (0, "(not applicable)") if not has_quality_eval else run_quality_tests(
            [q[len("scripts/quality/"):] for q in sel.quality_auto]
        )
    )

    # ---- output ----
    print("=== T1 MODIFIED-SCOPE UNIT ===")
    print()
    print(_format_selection("Backend production changed", sel.backend_production))
    print(_format_selection("Auto backend tests", backend_auto))
    print(_format_selection("Explicit backend tests", backend_explicit))
    print()
    print(_format_selection("Frontend production changed", sel.frontend_production))
    print(_format_selection("Auto frontend tests", frontend_auto))
    print(_format_selection("Explicit frontend tests", frontend_explicit))
    print()
    print(_format_selection("Quality infra changed", sel.quality_infra))
    print(_format_selection("Auto quality tests", sel.quality_auto))
    if rejected:
        print()
        print("Rejected selectors (unsafe or not found):")
        for r in rejected:
            print(f"  ! {r}")
    print()
    print(f"Backend result: {backend_out}")
    print(f"Frontend result: {frontend_out}")
    print(f"Quality result: {quality_out}")

    if required_reasons:
        print()
        print("=== T1 UNKNOWN / REQUIRED ===")
        print("production code changed but no targeted test evidence:")
        for r in required_reasons:
            print(f"  - {r}")
        return 2

    if rejected:
        print()
        print("=== T1 FAIL ===")
        print("rejected explicit/auto selectors must be fixed before this gate passes.")
        return 1

    overall_rc = 0
    failed = []
    if has_backend_eval and backend_rc != 0:
        failed.append("backend")
        overall_rc = 1
    if has_frontend_eval and frontend_rc != 0:
        failed.append("frontend")
        overall_rc = 1
    if has_quality_eval and quality_rc != 0:
        failed.append("quality")
        overall_rc = 1

    if overall_rc != 0:
        print()
        print(f"=== T1 FAIL === (failed: {', '.join(failed)})")
        return overall_rc

    if has_backend_eval or has_frontend_eval or has_quality_eval:
        print()
        print("=== T1 PASS ===")
    else:
        print()
        print("=== T1 NOT_APPLICABLE === (no production change, no targeted tests)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
