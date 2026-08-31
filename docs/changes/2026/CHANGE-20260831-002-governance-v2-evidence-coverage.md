# CHANGE-20260831-002 - Governance v2 failure-mode routing and evidence coverage

## Status

`verified_code_pending_remote_evidence`

## Why

Governance had accumulated incident-specific ceremony, duplicate authority, and a
hardcoded PG test list. A green `targeted-pg` could prove pytest exit code without a
machine-readable answer for which required contract was registered and executed.

## Decision

- `AGENTS.md` is Constitution + Router with Level 1 Normal Exploration, Level 2
  Contract-Sensitive, and Level 3 Operational/Destructive routing.
- Long-term rules protect repeated failure modes and project invariants, not generic
  language/framework style advice.
- PRD remains user-initiated. Verified Maps and Runbooks may follow implementation
  facts without a second authorization; they never create deployment or data authority.
- Formal PG evidence is selected by `scripts/verify/evidence_manifest.json`.
- Required contract status is one of `passed/failed/skipped/deselected/not_registered/
  not_run/blocked`; only actual execution and PASS closes evidence.
- `rules/PROTECTED_GOVERNANCE_FILES.json` remains unchanged during this migration.

## Implementation

- Replaced the executable hardcoded PG file list with explicit manifest selectors.
- Added a dependency-free pytest plugin recording collected/deselected nodeids and
  setup/call/teardown outcomes.
- Added `evidence-coverage.json` and raw `pytest-evidence.json` to attempt evidence.
- Added checker and pure-unit negative cases for duplicate IDs, missing selectors,
  globs, unknown gates, zero collection, skip, deselect, missing report, and restored
  hardcoded selection.
- Corrected the runtime Map and deployment Runbook from the retired per-SHA
  `verify-test` topology to the single reusable `panji-verify-python` runtime.

## Evidence

- Governance and verification pure-unit: 68 passed.
- Pytest plugin smoke: one collected nodeid with setup/call/teardown all passed.
- Ruff: passed for changed Python files.
- Target Mypy: passed for five implementation/checker files.
- Governance checker: passed.
- Exact-SHA `targeted-pg`: pending candidate push.

## Deferred triggers

- Shared PG Fixture Builder: implement when the next complex PG fixture is added.
- Canonical Workflow Definition: implement before the next resume/checkpoint semantic change.
- Reduce verification file protection: separately authorize only after at least three
  consecutive successful formal attempts on different exact SHAs under this manifest.
- Full PRD/Map content cleanup: perform by business domain, never as a bulk governance rewrite.

## Safety

No business PRD, production algorithm, business schema, migration, deployment, worker,
publication, or `bz_stock` operation is part of this change.
