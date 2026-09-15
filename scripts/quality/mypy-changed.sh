#!/usr/bin/env bash
# [Corrective-3.2 §P0-mypy-gate] + [Task 005-R / Commit A]
#
# THIN WRAPPER. mypy-changed.sh is no longer an independent changed-file Mypy
# implementation. It delegates to the single authoritative owner:
#
#     t0_gate.py --mypy-only
#
# which owns BOTH the changed-file manifest AND the Mypy execution. This removes
# the duplicate BASE/HEAD/worktree/untracked/deleted logic that used to live
# here (and the committed-range untracked pollution + BASE_ENV drift).
#
# Mypy still passes only when the changed Python files are type-clean, satisfying
# the PRD Gate 1 "Mypy exit code 0" definition without being blocked by
# historical baseline errors in untouched files.
#
# Usage:
#   mypy-changed.sh --base <SHA/ref> --head <SHA/ref>   # T0 recommended: explicit range
#   mypy-changed.sh --base <SHA/ref> --worktree         # check uncommitted working tree
#   mypy-changed.sh                                     # compat: BASE defaults to origin/dev (warns)
#
# NOTE: the base is ONLY read from --base. The BASE environment variable is
# intentionally NOT honoured (it used to be claimed but was silently overwritten,
# which hid empty manifests). Pass --base explicitly.
#
# Exit codes:
#   0  => changed files pass Mypy
#   1  => at least one changed file has a Mypy error
#   2  => cannot resolve changed files (git / venv missing) or bad arguments

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
BACKEND="$REPO_ROOT/backend"
T0_GATE="$SCRIPT_DIR/t0_gate.py"

BASE=""
HEAD="HEAD"
WORKTREE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --base) BASE="${2:-}"; shift 2;;
    --head) HEAD="${2:-}"; shift 2;;
    --worktree) WORKTREE=1; shift;;
    *) echo "[mypy-changed] unknown arg: $1" >&2; exit 2;;
  esac
done

if [[ -z "$BASE" ]]; then
  # BASE env var is intentionally NOT honoured; only --base is accepted. This
  # avoids a silent fallback that hides an empty manifest (the false green we
  # are closing). --base is required for real T0 runs.
  BASE="origin/dev"
  echo "[mypy-changed] WARN: no explicit --base given, falling back to '$BASE'." >&2
  echo "[mypy-changed] WARN: when driven by T0 you MUST pass explicit --base/--head," >&2
  echo "[mypy-changed] WARN: otherwise origin/dev==HEAD after push yields an empty diff (false green)." >&2
fi

VENV_PY="$BACKEND/.venv/bin/python"
if [[ ! -x "$VENV_PY" ]]; then
  echo "[mypy-changed] backend/.venv not found; create the venv first" >&2
  exit 2
fi

if [[ ! -f "$T0_GATE" ]]; then
  echo "[mypy-changed] t0_gate.py not found at $T0_GATE" >&2
  exit 2
fi

WORKTREE_FLAG=""
if [[ $WORKTREE -eq 1 ]]; then
  WORKTREE_FLAG="--worktree"
fi

echo "[mypy-changed] delegating to t0_gate.py --mypy-only (base=${BASE} head=${HEAD} worktree=${WORKTREE})" >&2
"$VENV_PY" "$T0_GATE" --base "$BASE" --head "$HEAD" $WORKTREE_FLAG --mypy-only
status=$?

if [[ $status -eq 0 ]]; then
  echo "[mypy-changed] changed files pass Mypy (exit 0)"
else
  echo "[mypy-changed] changed files have Mypy errors (exit $status)" >&2
fi
exit $status
