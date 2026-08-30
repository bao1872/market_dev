# CHANGE-20260830-001 — Bars Bounded Spawn ProcessPool

- **Status**: `implemented_unconfirmed`
- **Base SHA**: `0c3abaaf801085cd790d10f052084ffff73eefa0`
- **Scope**: F1B-2 only; no F1C, deployment, production configuration, production DB write, worker restart, or after-close trigger.

## Change

`BarsSchedulerService.refresh_all_instruments()` now supports a bounded provider-I/O
ProcessPool when `PANJI_BARS_FETCH_PROCESSES > 1`:

- default remains `1`, which executes the F1B-1 serial canonical path without creating a pool;
- one `spawn` ProcessPool is created per daily refresh invocation and reused for `d`, `15m`, and `60m`;
- submission is period-scoped and bounded by `workers * 2`;
- barriers remain `d provider+persistence -> post-d -> 15m provider+persistence -> 60m`;
- child processes perform provider I/O only and return serializable payloads;
- the parent performs daily preparation, adjustment-factor calculation/mapping, validation, and all DB persistence serially;
- retry remains instrument+period scoped (`MAX_RETRIES=3`, `RETRY_DELAY=5`) through a non-blocking ready-at queue;
- `BrokenProcessPool` and serialization infrastructure failures fail closed;
- cancellation stops new submission, cancels pending futures, and shuts down the pool;
- `backfill_all_instruments()` remains serial regardless of process configuration.

The configuration range is 1–8. Production configuration is intentionally unchanged.

## Evidence Before Exact-SHA PG

- F1B-1 + F1B-2 focused tests: 40 passed.
- Extended bars/scheduler regression: 54 passed, 35 PostgreSQL tests skipped under `PURE_UNIT_TEST=1`.
- Ruff, py_compile, and target Mypy: passed.
- Three synthetic benchmark runs (24 instruments, d raw + xdxr + supplement + 15m + 60m):

| workers | mean wall | mean items/s | mean speedup vs 1 | max inflight | max child RSS |
|---:|---:|---:|---:|---:|---:|
| 1 | 4.64s | 15.53 | 1.00x | 0 | N/A |
| 2 | 3.41s | 21.16 | 1.36x | 4 | 161 MiB |
| 3 | 2.62s | 27.51 | 1.77x | 6 | 161 MiB |

Workers=3 remained about 23% faster than workers=2 on mean wall time. With the historical
parent baseline near 964 MiB, three measured children imply an estimated total near 1.45 GiB,
below the 4 GiB container limit. Therefore the deployment-stage candidate is 3, while the code
default remains 1.

## Pending

- Commit and push the candidate to `origin/dev`.
- Run registered `targeted-pg` against the exact remote SHA. The existing registered
  `test_pg_bars_provider_persistence_f1b1.py` now contains the F1B-2 scheduler spawn canary;
  verifier policy and registration are unchanged.
- User acceptance and any later production environment change remain separate actions.
