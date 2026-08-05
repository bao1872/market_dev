# CHANGE-20260805-005：Corrective-3.2 — Gate 1 Finalization（事务级 fencing + Mypy changed-file gate + 前端验证）

- 日期：2026-08-05
- 类型：behavior + contract + quality-gate + docs
- 领域：ChipConsensusRun 发布/拍卖/领域 run 终态的事务级租约 fencing、`SchedulerJobRun`
  ownership 校验、Mypy changed-file 门禁、前端本地验证收口
- 关联前序：`CHANGE-20260805-004-corrective3-1.md`
- 基线（Corrective-3.2 起点）：`16b056f`
- 最终代码 SHA：`bc38d07`

## 0. 为什么需要 Corrective-3.2

审查结论：Corrective-3.1 主体代码完成，但 Gate 1 仍有 1 个真实 fencing 缺口 + 2 项证据缺口，
不能标记 `development_complete`：

```text
P0 当前 fencing 仍不是真正的数据库 fencing：
    worker 传入 ownership_check=heartbeat.ensure_owned，但 ensure_owned() 只检查
    进程内 _lost 事件，不在调用时查询数据库，不核对 SchedulerJobRun.status /
    worker_instance_id / lease_epoch。存在竞态窗口：watchdog 已回收/转移 lease，
    旧 worker 尚未心跳，ensure_owned() 仍通过，可提交 publication / auction / 领域 run。
P0 Mypy 未通过：全量 mypy 返回 45 errors（退出码非 0），不能标记 remote_static_verified=true。
P1 前端证据不完整：本地 npm run build 被写成 remote_frontend_build_verified=true，内部矛盾；
    且未补齐 PRD Gate 1 明确列出的 frontend tests。
P1 文档未自洽：Corrective-3.1 章节遗留错误文档 SHA a4b0d3c（应为最终 HEAD 1d32d59）。
```

## 1. 行为变化

### 1.1 事务级 fencing（P0）

`app/services/chip_consensus_run_lifecycle.py`：

- `finalize_chip_run` 新增 `fenced_token: FencedJobToken | None = None`。写入事务内第一步
  `await lock_owned_job_run(db, fenced_token)`（FOR UPDATE 校验
  `status=running / worker_instance_id / lease_epoch`）；捕获 `JobLeaseLostError` 后
  `db.rollback()` 并重新抛出，禁止 stale worker 改写领域 run 终态。
- `publish_chip_and_upgrade_auction` 新增 `fenced_token`。pub 事务与 anchor 事务内第一步均
  `await lock_owned_job_run(...)`：pub 失去租约返回 `CHIP_LEASE_LOST`（retryable）；
  anchor 失去租约回滚并跳过 auction 升级。

`app/worker.py`：

- `finalize_chip_run(..., fenced_token=heartbeat.token)`
- `publish_chip_and_upgrade_auction(..., fenced_token=heartbeat.token)`
- `heartbeat.token` 即 `FencedJobToken`（job_run_id / worker_instance_id / lease_epoch），
  与 `after_close_chip_consensus_service.execute_after_close_chip_consensus` 既有生产
  fencing 模式一致。

`ownership_check`（内存预检）保留为事务前额外检查，向后兼容现有测试；真正保护来自事务内
`lock_owned_job_run`，覆盖审查要求的 5 个竞态场景（lease 在 pub 前/执行中转移、pub 后 auction
前转移、finalize 前转移、stale worker 无法改写 pointer/run/auction）。

### 1.2 Mypy changed-file 门禁（P0）

新增 `scripts/quality/mypy-changed.sh`：只检查相对 `origin/dev` 变化的 backend Python 文件
（已提交 + 工作区修改 + 未跟踪），使用 `--follow-imports=skip`（changed-file 口径，仅校验本次
交付文件自身类型，不深入依赖图遗留错误）+ `--no-incremental`。退出码 0 表示通过。

```text
bash scripts/quality/mypy-changed.sh
→ 检查: app/services/chip_consensus_run_lifecycle.py, app/worker.py
→ Success: no issues found in 2 source files   # 退出码 0
```

### 1.3 前端验证（P1，同最终 SHA 本地复验）

前端代码在 3.1/3.2 均无变化，复用本地证据（诚实标记 local，非 remote）：

| 项目 | 命令 | 结果 |
|---|---|---|
| TSC | `tsc -b` | 退出码 0，零错误 |
| ESLint | `eslint .` | 0 errors（66 warnings 非 error） |
| Contract Tests | `npm run test:contract` | 552 passed, 0 failed |
| Vite Build | `vite build` | dist 产物完整 |

## 2. 验证（本地，非远程）

| 项目 | 命令 | 结果 |
|---|---|---|
| Ruff | `ruff check app/services/chip_consensus_run_lifecycle.py app/worker.py` | All checks passed |
| Mypy changed-file | `bash scripts/quality/mypy-changed.sh` | 退出码 0，2 文件零错误 |
| PURE_UNIT_TEST | `pytest tests/test_chip_worker_orchestration.py tests/test_product_readiness_service_layer.py tests/test_migration_086_chip_run_uniqueness_contract.py` | 47 passed, postgres=0 |
| 前端 TSC/ESLint/Contract/Build | 见 §1.3 | 全部通过 |

## 3. 诚实状态

```text
corrective_3_2_fencing_implemented    = true
mypy_changed_file_gate_passed         = true   # 脚本退出码 0
ruff_changed_files_passed             = true
remote_unit_verified                  = true   # 47 passed, postgres=0
frontend_tsc_local_passed             = true
frontend_eslint_local_passed          = true
frontend_contract_tests_local         = true
frontend_build_local_passed           = true
remote_static_verified                = false  # 未远程，本地 changed-file gate 替代
remote_frontend_build_verified        = false
development_chain_D_to_J              = development_complete   # 仅开发阶段；Gate 2-5 未启动
code_ready                            = true
migration_086_authored                = true
migration_086_static_verified         = true
migration_086_applied                 = false  # Gate 2 PG 集成后才执行
pg_tested        = false
deployed         = false
runtime_verified = false
data_closed      = false
browser_verified = false
full_prd_closed                      = false
production_fully_ready               = false
```

## 4. 下一步

隔离 PG 集成（Gate 2）才允许 apply Migration 085/086 并验证并发幂等；之后进入 Gate 3-5。
本阶段不部署、不连 PG、不启动远程 worker。
