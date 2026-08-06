# CP4A PostgreSQL 验收矩阵

- 日期：2026-08-06
- 计划审计基线：`c2b436a63e236b6319aa41f472a6e29d93c4cddf`（origin/dev，V2.1 完全对齐开发计划基线）
- 历史诊断候选 SHA：`030e19e4f972d9ff965802d25fa31b233afd1af7`（已作废，仅诊断性证据）
- 历史候选 SHA：`899f31508c58fcaf329b1e752a6a6dbcb1766b4c`（CP4A Amendment 提交，已并入基线）
- 目标候选 SHA：`<40-char origin/dev SHA>`（待 Phase 6 本地门禁通过、提交推送后冻结）
- 验证库：`bz_stock_verify_<target_sha>`（待 Pass 2 创建，隔离，DS-110）
- 约束：每命令前后断言 `current_database()` 为验证库，禁止访问生产库 `bz_stock`；访问 bz_stock 须获得明确授权并证明只读
- 结论：**按《ref/开发计划.md》Phase 0-7 执行 V2.1 PRD 完全对齐。Phase 1-2 代码合同收口已完成并提交（08cf3dc）；Phase 3 运行级 refresh + 八个 canonical reason code 已完成（待提交）；剩余 Phase 4-5 补齐、Phase 6 本地门禁 + Phase 7 远程隔离验证（test_pg_*.py + Seed 幂等 + full synthetic E2E）全部通过后，方可关闭 CP4A。候选未验收前本矩阵保持待重验状态。**

## 状态说明（本次 CP4A Amendment）

诊断阶段（030e19e）在**远程修改业务文件 + 手工改验证库 schema + /tmp 临时脚本**下取得的
"Steps 1-7 PASS" 只构成**诊断性证据**（valuable_but_non_reproducible），不能视为某 Git SHA
的正式验收证据。本次 Amendment（Pass 1）把诊断暴露的真实缺陷**正式写回仓库**：

- 修复 Migration 087 + ORM：普通 UNIQUE → partial unique index；
- 统一全部 publication 写入点（`index_elements + index_where`）；
- 修复原子 publication（async `_has_supersede_columns`、FOR UPDATE、预生成 id、先 supersede）；
- 合并 PG 暴露业务修复（bb_mid、FieldAvailability、JSON serializer、multi-batch finalize、parameterHash）；
- 重写 seed（真实 `compute_review_core_with_run_items` 链，删除 hash()，不伪造终态）；
- 修复验证部署工具（VERIFY_PG_USER、RUNTIME_SHA 移出 repo、Python 探针、端口环境变量化）；
- 正式 PG 测试文件（`test_migration_087_*`、`test_pg_atomic_publication`、`test_pg_projection_lifecycle`、`test_pg_100_stock_call_counts`）。

## 验收项（待 Pass 2 新 SHA 重验）

| # | 验收项 | 状态 |
|---|--------|------|
| 1 | Preflight：HEAD==origin/dev==new_sha、工作树 clean、无远程业务代码 patch | **待重验** |
| 2 | 创建新验证库 `bz_stock_verify_<new_sha>`（不复用旧库） | **待重验** |
| 3 | current_database 断言（每命令前后） | **待重验** |
| 4 | Migration 087 闭环：upgrade → partial index/FK/audit 检查 → downgrade → upgrade → duplicate | **待重验** |
| 5 | 100 股真实 compute（五类 kernel、15m=0、StrategyRuntime=0、coverage、单股失败隔离） | **待重验** |
| 6 | 原子 stock_core publication 故障注入（各阶段失败旧 pointer 保留） | **待重验** |
| 7 | Projection 生命周期（per-batch/heartbeat/幂等） | **待重验** |
| 8 | Seed 两次幂等（无重复 publication/active child/RunItem/异常行增长） | **待重验** |
| 9 | CP4A 关闭 | **暂不**（待 Pass 2 全通过） |

## 本 Amendment 已完成的代码修复（Pass 1）

| # | 文件 | 缺陷 | 修复 |
|---|------|------|------|
| 1 | `087_stock_core_atomic_publication.py` | Migration 只加列/表，未处理普通 UNIQUE 与 supersede 冲突 | upgrade drop 普通 UNIQUE → 建同名 partial unique index；downgrade 还原 |
| 2 | `factor_publication.py` | ORM 用 `UniqueConstraint`，禁止 immutable history | 改 partial `Index(unique=True, postgresql_where=superseded_by IS NULL)` |
| 3 | `factor_publication_service.py` / `review_publication_service.py` / `board_analysis_service.py` / `board_facts_service.py` | `on_conflict_do_update(constraint=旧名)` 失效 | 改 `index_elements + index_where` |
| 4 | `stock_core_publication_service.py` | `_has_supersede_columns` 同步 execute → 恒 False | 改 async await |
| 5 | `stock_core_publication_service.py` | 新 pub 插入时旧 pub 仍 current → partial unique 冲突；无并发行锁 | 预生成 id + 先 supersede；旧 pointer `FOR UPDATE` 行锁串行化并发 |
| 6 | `structural_factor_service.py` | bb_df 列名 mid/upper/lower 不匹配 | 按实际 bb_mid/bb_upper/bb_lower 读取，兼容旧格式 |
| 7 | `first_pyramid_service.py` | fieldAvailability dict/model 合同不统一 | 兼容 dict 与 Pydantic |
| 8 | `core_artifact_service.py` | numpy/Timestamp 不能直接写 JSONB | `_json_safe_value` 递归安全化 |
| 9 | `strategy_batch_service.py` | 多批 projection 首批后 finalize completed | 有 pending 保持 running，全终态才 finalize |
| 10 | `core_artifact_codec.py` | 旧 snapshot 无 dsaProjection 块硬失败 | 从 first_pyramid 回退重建（需 parameterHash 持久化） |
| 11 | `seed_v21_verify_data.py` | 手工伪造 first_pyramid/availability/coverage/hash() | 核心经真实 `compute_review_core_with_run_items`，coverage 从真实 run 统计，删除 hash() |
| 12 | `panji-verify-deploy.sh` / `docker-compose.verify.yml` | `psql -U postgres`、RUNTIME_SHA 写 repo、curl 探针、固定端口 | VERIFY_PG_USER、RUNTIME_SHA 移出 repo、Python 探针、端口环境变量化 |

## 待办（关闭 CP4A 前置）

1. 冻结基线（`c2b436a`）并建立唯一相关 Change `CHANGE-20260806-005`（status=`verified_code_pending_acceptance`）。
2. Phase 1-5 代码合同核验/补齐（Compute Once、stock_core 原子发布 SSOT、chip 运行级 15m+RunItem、closure 六态、Seed 真实 producer + 四类场景硬断言 + PG 测试 `pytest.mark.postgres`）。
3. Phase 6 本地全量门禁全过（PURE_UNIT 全量 + Ruff + Mypy changed + compileall + 架构 + allowlist + 治理 + docs + git diff --check；前端 typecheck/lint/contract/build）→ 提交推送 → 冻结 `target_code_sha`。
4. Phase 7 远程隔离验证：新建验证库 `bz_stock_verify_<target_sha>` → Migration 087 闭环 → 100 股 compute → 原子 publication 故障注入 → projection 生命周期 → Seed 两次幂等 → full synthetic E2E → 每命令断言 `current_database()`。
5. 全部通过后关闭 CP4A，再连续执行 CP4B/C/D。

## 诚实边界

- 本次 Amendment 仅代码收口（Pass 1），尚未在任何新 SHA 上完成远程隔离重验（Pass 2）。
- 诊断阶段（030e19e）的"Steps 1-7 PASS"为诊断性证据，不构成正式验收。
- 本地全量 PURE_UNIT 单测有 9 个预存在失败（test_ac04 15m、calendar AST 环境错误、incremental 测试污染），均与本次改动无关（已在 base 验证复现）。
