# AGENTS.md - Panji governance constitution and router

This file is the entry contract for every IDE, coding assistant, and automation
agent working in this repository. It owns project stage, task routing, authority,
authorization, and the highest safety boundaries. Product semantics, implementation
facts, test details, and operating commands live in their dedicated owners.

## 1. Project stage

`PROJECT_STAGE = EXPLORATION`

The default goal is the shortest trustworthy feedback loop:

`confirmed behavior -> implementation -> required evidence -> real runtime -> human evaluation`

Exploration removes unrelated release ceremony. It never weakens business
correctness, code correctness, evidence truthfulness, security, or data safety.

## 2. Non-negotiable principles

1. **One semantic, one owner.** Lifecycle, readiness, lineage, canonical result,
   state transition, and workflow semantics have one production owner. Other code
   may consume, derive, or validate; it must not redefine them.
2. **Evidence must match the claim.** A test existing, collecting, or returning
   exit code zero is not proof that a required contract ran and passed.
3. **Tests consume production contracts.** Artifact, serialization, identity,
   lineage, status, and version fixtures reuse production encoders, decoders,
   repositories, factories, and domain helpers unless malformed input is the target.
4. **Classify failures before changing production.** Distinguish `STALE_TEST`,
   `INVALID_FIXTURE`, `RUNTIME_BUG`, `INFRA_BUG`, and `UNKNOWN`. `UNKNOWN` is not
   authority to modify production behavior.
5. **No false-green.** Never hide missing execution, skip, fallback, partial
   completion, wrong identity, stale pointer, future leakage, or runtime failure.
6. **Risk determines process strength.** Use the highest applicable governance
   level; do not promote incident ceremony into every normal task.
7. **Scope completion means stop expanding.** STOP is a scope boundary, not an
   extra approval gate for ordinary completed work.

## 3. Governance level router

### Level 1 - Normal Exploration

Typical work:

- UI, copy, local interaction, and presentation;
- a small bug with already-defined behavior;
- a simple API field that does not change a stable contract;
- a local refactor that does not change ownership, persistence, or workflow state.

Required path:

`relevant PRD/Map -> implement -> modified-scope tests -> commit/push when requested`

Level 1 does not default to formal PG, checkpoint commits, second audit, full
closure, large reports, exact-SHA freeze, or release certification.

### Level 2 - Contract-Sensitive

Triggered by any change to:

- canonical computation or semantic ownership;
- lineage, readiness, publication, pointer, artifact, version, or identity;
- workflow state, resume, retry, idempotency, fencing, or exactly-once behavior;
- API/persistence contracts shared across services;
- test registration or evidence coverage.

Required path:

1. Identify the production semantic owner.
2. Trace producer -> persistence -> consumer.
3. Classify existing tests and fixtures.
4. Run modified-scope unit and contract tests.
5. Run targeted PG when the claim depends on PostgreSQL, transaction, persistence,
   migration, or real remote runtime behavior.
6. Close evidence against the exact contract claim.

An independent audit may be requested when ownership or blast radius is unclear.

### Level 3 - Operational / Destructive

Triggered by:

- Migration, deployment, production runtime mutation, or worker restart;
- `bz_stock` access that can write or mutate data;
- bootstrap, backfill, repair, withdrawal, deletion, or destructive cleanup;
- secrets, permissions, protected Owner data, or irreversible operations.

Required path:

1. Current-task explicit authorization.
2. Exact code, runtime, database, and migration identity.
3. The registered fail-closed runner and environment.
4. Machine-readable evidence and precise resource cleanup.
5. Re-authorization if the action exceeds the approved target or scope.

When more than one level applies, the highest level wins.

## 4. Authority map

| Question | Authority |
|---|---|
| What should the product do? | `docs/prd/` |
| What does the current system do and where? | Code/runtime evidence, summarized by `docs/maps/` |
| Which engineering invariant is mandatory? | `rules/` |
| How is an operation performed? | `docs/runbooks/` |
| Why did an important change happen? | `docs/changes/` and Git |

Implementation never rewrites confirmed product behavior. A Map never defines a
requirement. A Runbook never becomes the owner of a governance or product rule.
Plans, assumptions, and unverified results must not be written as current facts.

## 5. Reading route

Read only what the task needs:

1. This file.
2. `rules/README.md` and the rules selected by its router.
3. Relevant PRD and Map.
4. Relevant code/runtime evidence.
5. A Change or Runbook only when history or operation steps matter.

Do not load every PRD, Map, Change, Runbook, or rule for a Level 1 task.

## 6. Documentation authority

- **PRD:** modify only when the user explicitly starts or authorizes a PRD task.
- **Maps:** may be updated with a code task when verified implementation entry,
  owner, data flow, contract, or runtime facts changed and stale text would mislead.
- **Runbooks:** may be updated when commands or operating steps have been exercised
  successfully or are covered by verified automation contracts.
- **Changes:** record only important behavior, contract, schema, governance, or
  operating-model changes. Small fixes may remain Git-only.
- **Governance:** modify `AGENTS.md`, `rules/`, governance checkers, or the protected
  governance domain only with explicit current-task governance authorization.

Factual Map/Runbook synchronization does not authorize deployment or data access.
PRD, Maps, Runbooks, and Changes must still state verification status honestly.

`rules/PROTECTED_GOVERNANCE_FILES.json` is the machine-readable protected domain.
It remains in force until a separately authorized change narrows it. Modifying any
listed file requires reading the manifest and keeping rules, implementation,
checker, and tests aligned.

## 7. Task execution

Before editing:

- classify the governance level;
- state the behavior/contract being changed and its owner;
- inspect current code and existing user changes;
- identify required tests and any real-runtime evidence;
- identify actions needing authorization.

During implementation:

- prefer existing owners and repository patterns;
- make the smallest complete vertical change;
- do not duplicate a contract in tests, orchestration, resume maps, or docs;
- preserve trade-date, point-in-time, canonical, transaction, and error semantics;
- keep retries, concurrency, queues, and resource use bounded;
- do not fix unrelated deferred debt.

Completion claims must name the evidence actually obtained. Engineering evidence
does not replace user product judgment, and user-visible output does not replace
engineering evidence.

## 8. Git contract

- Work directly on `dev`; do not create branches unless explicitly authorized.
- Do not modify, merge, or push `main`.
- `experiments` is only for explicitly authorized isolated experiments.
- Fetch and inspect local/remote ancestry before editing.
- Preserve user changes and unrelated untracked files.
- Stage exact files; never use broad staging for task completion.
- Do not amend, rebase shared history, or force push unless explicitly authorized.
- A push is not proof of tests, deployment, runtime alignment, or user acceptance.

Detailed flow is owned by `rules/50-git-development-flow.md`.

## 9. Highest safety boundaries

Without explicit current-task authorization, never:

- perform destructive or irreversible actions with unclear scope;
- write, migrate, repair, backfill, or delete data in `bz_stock`;
- deploy or mutate the stable remote runtime;
- restart workers, trigger Scheduler/AfterClose/full-market jobs, or publish results;
- alter protected Owner identity, credentials, roles, permissions, or subscriptions;
- expose or commit secrets, passwords, private keys, or production credentials;
- create unregistered local/CI/remote test databases;
- use mock, fallback, stale data, or another SHA as formal evidence;
- manually patch a remote checkout or schema to make verification pass;
- delete persistent data, shared containers, networks, volumes, or unknown resources;
- restore deprecated verification, deployment, or governance paths.

Local/CI database tests are forbidden. Pure tests use `PURE_UNIT_TEST=1`. Formal PG
tests use the registered `scripts/ops/panji-verify` plans against only
`bz_stock_verify_<40-character-sha>` in the approved remote verification runtime.

Remote server, database, port, path, identity, migration, resource, cleanup, and
deployment details are owned by `rules/30-security-data-safety.md`,
`rules/80-deployment-migration.md`, `rules/90-deprecated-forbidden.md`, the verified
runtime Map, and the current Runbook. Do not reconstruct them from chat memory.

## 10. Rule router

| Trigger | Required rule |
|---|---|
| Any task | `rules/00-core-governance.md` |
| Market data, PIT, canonical, strategy computation | `rules/20-market-data-computation.md` |
| Cross-cutting implementation | `rules/25-engineering-implementation.md` |
| Accounts, secrets, real data | `rules/30-security-data-safety.md` |
| Tests, evidence, contracts, PG | `rules/40-testing-quality.md` |
| Commit, push, checkpoint | `rules/50-git-development-flow.md` |
| Runtime/API/frontend acceptance | `rules/60-runtime-frontend-acceptance.md` |
| Release/hardening trigger | `rules/70-hardening-release.md` |
| Migration, remote verification, deployment, cleanup | `rules/80-deployment-migration.md` |
| Deprecated or forbidden paths | `rules/90-deprecated-forbidden.md` |

The more specific safety or business invariant wins. Exploration routing can reduce
unrelated process, never correctness, evidence truthfulness, security, or data safety.
