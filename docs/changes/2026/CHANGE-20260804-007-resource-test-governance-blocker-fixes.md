# CHANGE-20260804-007: 治理垂直切片阻断性修复 — 修正 P0/P1 缺陷并撤回过度声明

- 日期：2026-08-04
- 类型：governance+architecture+ops+contract
- 领域：治理规则 / 部署运维 / 测试合同 / 容器资源 / 长任务资源治理
- 关联 PRD：`docs/prd/80-system-runtime.md`
- 关联 Maps：`docs/maps/80-system-runtime.md`
- 关联 Changes：CHANGE-20260804-004（治理垂直切片首版，经审查判定不通过）
- 数据操作：**零写入、零部署**（本轮仍仅本地验证、提交并推送 `origin/dev`；未连接远程服务器、未 dry-run、未执行真实部署）

## 1. 为什么改：审查结论

用户对 `f27bb3c`（CHANGE-20260804-004）静态审查判定**不通过**，指出至少 2 个 P0 代码缺陷与 4 个 P1 合同缺口。IDE 上一版总结把「字段出现、局部测试通过」高估为「业务语义已落实」。经逐一核对代码，以下指控**全部属实**：

1. **P0-1 预算超限仍写 succeeded**：`feature_snapshot_backfill.py` finalize 在 `stop_reason` 非空时仍按 `failure_rate <= threshold` 写 `STATUS_SUCCEEDED`，未处理的 instruments 未进入分母 → partial 伪装 success，且 `--resume` 会跳过错误的 succeeded run。
2. **P0-2 应用预算 ≥ 容器上限**：Review `DEFAULT_BOOTSTRAP_MEMORY_BUDGET_MB=1536`、Feature 默认 `1024`，均 ≥ 重 worker 容器 `mem_limit=1024m` → 容器先于应用 OOM。
3. **P1-1 并发未禁**：`--workers>1` 仍可用，并行路径 `backfill_instrument_first_parallel` 无预算/stop_reason/checkpoint，违反 DS-107 并发=1。
4. **P1-2 stock core 未落实**：IDE 自行缩小规则范围（"stock core 被外层覆盖"），未在规则/代码/文档间达成一致，形成"规则说必须有、代码没有、文档宣称完成"。
5. **P1-3 resume_token 非真断点**：`make_checkpoint()` 只序列化 processed/total/peak/metadata，无业务游标（last_instrument/last_trade_date/run_id/input_hash），且 Feature/Review 未真正读取它续跑。
6. **P1-4 checker 过浅**：只查字段/字符串存在性，不查预算<上限、禁 workers>1、partial→failed、resume 消费、stock core 边界。
7. **部署脚本缺口**：rsync/compose config/总时长无超时；OOM 只查 3 容器；无 swap 采集；stats 失败被 `|| true` 吞掉；回滚一次性重建无波次/超时/验证。

## 2. 撤回 / 修正 004 的过度声明

004 的以下结论**不成立或需降级**，以本 Change 为准：

| 004 原表述 | 实际状态 | 本 Change 处置 |
|---|---|---|
| `long_task_safe_resume = true` | 不成立（token 非真断点） | 实现 business_cursor + --resume 消费 input_hash |
| Review/Feature Snapshot 已统一安全恢复 | 仅加状态字段 | 补 partial→failed 门禁 + 真断点 |
| 三条长任务主链已落实 | stock core 未落实 | DS-107 明确 stock core 边界（非独立长任务） |
| checker 已断言资源合同 | 只断言字段/字符串 | 新增 5 项行为级门禁 |
| 未完成任务不会写 success | Feature Snapshot 有反例 | 强制 failed（published_at 保持 NULL） |
| 修改范围为「5 个提交」 | 实际一个提交含 5 工作包 | 本 Change 为追加阻断性修复提交 |

## 3. 本次修改范围

### 修复 1（P0）：Feature Snapshot partial→success
- `feature_snapshot_backfill.py` 单/并行 finalize：`stop_reason` 非 None **或** `processed != total` 时强制 `run_status=STATUS_FAILED`（`published_at` 保持 NULL → watchlist 不读、`--resume` 不跳过）；`stop_reason`/`peak_rss_mb` 记入 run metadata；新增行为门禁标记 `[P0-budget-partial-failed]`。

### 修复 2（P0）：应用预算显著低于容器上限
- `review_bootstrap_service.py`：`DEFAULT_BOOTSTRAP_MEMORY_BUDGET_MB` 1536→**768**（= 重 worker 1024m × 0.75）。
- `feature_snapshot_backfill.py`：默认 `memory_budget_mb` 1024→**768**（函数签名 + CLI 默认 + 文档）。

### 修复 3（P1）：并发固定为 1
- `feature_snapshot_backfill.py`：`parse_args` 拒绝 `--workers != 1`（`parser.error`），移除 warnings 导入，模块文档/帮助同步；并行路径保留但已被拒绝（防御性 guard `processed != total → failed`）。

### 修复 4（P1）：stock core 边界权威定义
- `rules/80` DS-107：新增「stock core 边界」——`first_pyramid` core 是单股同步纯计算、非独立批量长任务，内存由外层 Feature Snapshot 批量预算门禁统一治理；若未来引入独立入口须另行满足全部字段。

### 修复 5（P1）：真实可消费业务断点
- `long_task_budget.py`：`LongTaskBudgetState` 新增 `business_cursor`（input_hash/last_instrument_index/last_trade_date/run_id/chunk_index 等），随 checkpoint 序列化/恢复。
- `feature_snapshot_backfill.py`：新增 `_input_hash()`；持久化 `resume_token`/`input_hash`/`last_instrument_index`/`last_trade_date` 到 run metadata；`--resume` 读取上一 run 的 `input_hash` 做参数漂移一致性校验（真正消费 checkpoint）。

### 修复 6（P1）：行为级 checker
- `tools/check_governance_rules.py`：新增 5 项行为门禁（应用预算 < 容器 mem_limit、禁 `--workers>1`、partial→failed 标记、resume 消费 input_hash、stock core 边界声明）+ 4 项部署脚本信号（rsync 限时 / compose config 限时 / 总时长硬上限 / swap 采集）；新增工具 `_parse_memory_mb`/`_extract_int_after`。

### 修复 7（P1）：部署脚本 DS-103/104 补全
- `panji-deploy.sh`：
  - rsync 全部改用 `run_with_timeout TIMEOUT_RSYNC_SECONDS`；
  - 新增 `validate_compose_config`（`compose config` 限时校验，构建前执行）；
  - 新增 `TIMEOUT_TOTAL_DEPLOY_SECONDS` 总时长硬上限（外层 `timeout` 包裹 main）+ `TIMEOUT_COMPOSE_CONFIG_SECONDS`；
  - `post_deploy_resource_check`：OOM/RestartCount 覆盖全部关键容器（不再只查 3 个）、限制生效覆盖全部重 worker（不再只查 backend）、新增 swap 采集、`docker stats` 失败不再被 `|| true` 吞掉（采集失败判部署失败）；
  - `rollback` 改为复用波次重启 + 回滚后资源复检，不再一次性重建。

### 修复 8（文档诚实化）
- 新增本 Change（007），撤回 004 过度声明。
- 更新 `docs/changes/INDEX.md`。

## 4. 前后关键差异

| 维度 | 修改前（004） | 修改后（007） |
|---|---|---|
| 预算超限 run 状态 | 可能写 succeeded（partial 伪装） | 强制 failed，published_at NULL |
| Review 预算 | 1536 MB（> 容器 1024m） | 768 MB（< 容器 1024m × 0.75） |
| Feature 预算 | 1024 MB（= 容器） | 768 MB（< 容器 × 0.75） |
| 并发 | `--workers>1` 可用 | 拒绝非 1 |
| stock core | 未声明边界 | 明确非独立长任务、外层治理 |
| resume | token 非真断点 | business_cursor + input_hash 一致性消费 |
| checker | 字段/字符串 | + 行为级断言 |
| 部署 | rsync/config/总时长无超时、OOM 3 容器、无 swap、stats 吞错、回滚一次性 | 全限时、OOM 全容器、swap、stats 失败即失败、回滚波次+复检 |

## 5. 验证结果

- `tools/check_governance_rules.py`：PASS
- `tools/check_docs_consistency.py`：全部通过
- `tools/tests/test_check_governance_rules.py`：38 passed（含 5 项行为门禁 + 3 项部署信号回归）
- `tools/tests/test_long_task_budget.py`：11 passed（含 business_cursor 断点 roundtrip）
- 后端修改文件 `py_compile`：全部通过
- `scripts/deploy/panji-deploy.sh`：`bash -n` 通过
- `test_check_docs_consistency.py` 的 5 个失败为**既有失败**（stash 验证），非本次引入。

## 6. 诚实边界 / 待办

- **仍未部署**：本轮仅本地验证、提交并推送 `origin/dev`；用户检查远端代码通过后再单独授权真实服务器 dry-run 与部署。
- **容器 mem_limit 与应用预算为保守初值**：768 vs 1024 的关系已由 checker 断言（预算 < 上限且余量 ≥ 20%），但**真实高水位仍未实测**，部署后须按 `docker stats --no-stream` 收紧（禁止只采集不限制）。
- **stock core 采用「规则边界」而非独立实现**：若未来引入独立于 Feature Snapshot 的 stock core 批量入口，必须另行满足 DS-107 全部字段。
- 未连接远程服务器、未 dry-run、未部署、未修改共享开发业务数据库、未创建分支、未 force push、未碰 `main`。
