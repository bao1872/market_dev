# 25 Engineering implementation invariants

This rule owns only repeated, project-relevant implementation failure modes. It is
not a Python, React, SQL, or DataFrame style guide. Product semantics belong to PRD
and production domain owners; testing, safety, Git, and deployment have separate rules.

## 1. Existing owner first

Before adding logic, locate the current owner of the semantic:

- producer and source identity;
- canonical computation;
- persistence and transaction boundary;
- decoder/read model;
- state transition and workflow order;
- API or frontend consumer.

New code should call or extend that owner. Do not copy a lifecycle rule into an
orchestrator, retry map, test fixture, API adapter, frontend selector, or document.

If no owner exists, create the narrowest owner that can serve the current real
consumer. A second abstraction requires a second demonstrated use case, except for
security and data-correctness boundaries.

## 2. One semantic, one machine owner

The following must not have independently maintained implementations:

- lineage and source-run resolution;
- readiness and publication eligibility;
- canonical result selection and pointers;
- stage order, checkpoint ranking, resume position, and allowed transitions;
- status normalization and terminal-state meaning;
- artifact encoding/decoding and version binding.

Other layers may derive views or validate invariants, but must not reinterpret the
semantic. If temporary duplicate representations are unavoidable, one is declared
canonical and a machine test proves all derived copies remain equivalent.

## 3. Production path reuse

Development, tests, experiments, and formal runtime must share the production
semantic path wherever the claim concerns production behavior.

Reuse production:

- encoders and decoders;
- repositories and transaction boundaries;
- domain factories and identity helpers;
- request builders and canonical calculators;
- workflow definitions and state readers.

Hand-built payloads are allowed only when malformed or legacy input is the explicit
test target. Test helpers may simplify setup but cannot become a second contract owner.

## 4. Computation and side effects

Keep pure preparation separate from external effects:

`input -> validate/prepare -> canonical value -> persist/publish/notify`

This separation must preserve one implementation of the actual formula. It must not
create a production path and a test-only reimplementation.

Side effects require explicit ownership and boundaries:

- transaction scope and commit visibility;
- retry and idempotency identity;
- timeout and cancellation behavior;
- partial/failure reporting;
- resource acquisition and cleanup.

No background task, import, helper constructor, or adapter may silently create a
business side effect.

## 5. Failure transparency

Failures retain enough information to distinguish invalid input, stale test,
fixture error, runtime bug, provider failure, infrastructure failure, and cancellation.

Forbidden patterns:

- broad exception swallowing;
- returning success after required work did not run;
- silent source or algorithm fallback;
- replacing missing/unknown with zero or empty success;
- retrying an infrastructure-fatal condition as a normal business item;
- modifying production behavior before a test failure is classified.

Fallbacks must be product-approved, observable, and represented in status/evidence.

## 6. Bounded work and resources

Concurrency, queues, retries, batches, caches, and result buffers must have explicit
bounds derived from the workload and runtime budget.

Required properties where applicable:

- bounded in-flight work;
- finite retry count and delay policy;
- cancellation stops new submission;
- cleanup runs on success, failure, timeout, and interruption;
- no concurrent writes through one non-concurrent session/owner;
- progress increases only for completed work, not heartbeat;
- infrastructure-fatal failures fail closed instead of switching paths silently.

Performance work must measure the real bottleneck and preserve correctness. A faster
path that changes request semantics, canonical preparation, persistence, or ordering
is a behavior change, not an optimization.

## 7. Time, identity, and determinism

All market and workflow code must keep these explicit:

- market/trade date versus wall-clock time;
- point-in-time membership and source version;
- stable instrument/run/task identity;
- canonical ordering where completion is concurrent;
- idempotency key and conflict semantics;
- timezone and period boundary.

Never infer these from latest row, process-local time, insertion order, or an
unversioned current snapshot when the contract requires historical identity.

## 8. Database implementation

Database work follows access patterns and ownership, not convenience:

- one clear transaction boundary per business operation;
- bulk read/write for batch paths;
- no unbounded query loops or implicit N+1 behavior;
- constraints and indexes justified by the actual contract;
- no network/provider call hidden inside persistence;
- no parallel use of one `AsyncSession`;
- persistence errors remain distinguishable from preparation/provider errors.

Schema and migration safety is owned by `rules/80-deployment-migration.md`.

## 9. API and frontend implementation

Routers adapt transport and authorization; domain services own business semantics.
DTOs must preserve meaningful unavailable, partial, blocked, and error states.

The frontend consumes API facts and may derive presentation state. It must not become
a second owner for canonical calculations, readiness, lineage, or workflow status.

API changes must trace every active consumer and preserve or deliberately migrate:

- field identity, type, nullability, and enum meaning;
- error shape;
- pagination and result bounds;
- URL/state hydration and navigation ownership where relevant.

## 10. Worker and workflow implementation

Workers and orchestrators coordinate owners; they do not reimplement them.

Changes to stage order, checkpoint, resume, retry, skip logic, or downstream
exactly-once behavior are Level 2. They require fresh, crash, resume, and idempotency
coverage for the affected path.

Until a canonical workflow definition exists, any duplicate stage/checkpoint maps
must be treated as derived copies and protected by consistency tests. The next change
to resume/checkpoint semantics triggers creation of that canonical definition.

## 11. External providers

Provider access has one explicit boundary with:

- request identity and time/count semantics;
- finite timeout and retry;
- validated, serializable response shape when crossing processes;
- no silent switch to another source;
- source/timestamp evidence when required by the product contract.

Provider I/O, canonical preparation, and database persistence remain separate owners.

## 12. Completion

Implementation is complete only when:

- the semantic owner is clear and not duplicated;
- failure and unavailable states remain truthful;
- modified-scope tests pass;
- required contract/PG/runtime evidence matches the claim;
- generated or temporary resources are bounded and cleaned;
- factual Maps/Runbooks are synchronized when their verified facts changed;
- deferred work names a concrete trigger.

Generic style preferences are enforced by the existing formatter, linter, type
checker, framework conventions, and code review. They are not governance authority.
