# CI 门禁映射（PRD V2.0 §7.3）

本文档列出 PRD V2.0 §7.3 的 8 条 CI 门禁规则及其执行测试/检查工具。
最后更新：CP-14。

## 门禁规则与执行

### 1. Node 不得读取显示窗口

**规则**：Node Cluster 计算输入禁止使用 60/90/120 显示窗口参数。
90 是前端 defaultVisibleBars（飞书舞台），不传后端 API；250 是 DAILY_HISTORY_BARS（合同常量）。

**执行**：
- `tests/test_node_cluster_input_isolation.py` — 4 个合同验证 + 60/90/120 不变性
- `tools/check_architecture.py::check_v2_docs_structure` — 文档结构守护

### 2. Node daily/15m 不得实时

**规则**：Node Cluster 的 daily 和 15m 输入必须 `include_realtime=False, completed_only=True, adj=qfq`。

**执行**：
- `tests/test_node_cluster_input_isolation.py::test_load_node_cluster_inputs_uses_completed_qfq_for_daily`
- `tests/test_node_cluster_input_isolation.py::test_load_node_cluster_inputs_uses_completed_qfq_for_15m`

### 3. market 上下文不得回退 watchlist

**规则**：详情页从 market 筛选进入时，导航不得回退到 watchlist 上下文。

**执行**：
- `frontend: detailSourceLoadingContract.test.ts` — 13 场景（行情筛选→详情、刷新、前后退、上一只/下一只、上下文失效）

### 4. 前端不得用 quote 构造 K 线

**规则**：前端禁止从 quote 数据合成 K 线 bar，必须通过 MDAS/bars API 获取。

**执行**：
- `tests/test_capture_snapshot.py` — Atomic Chart Snapshot 端点验证
- `tests/test_indicators_api.py` — 指标 API 验证
- CP-5 已删除前端 quote→bar 合成路径

### 5. 图片失败不得 overall success

**规则**：飞书图片上传失败时，整个消息发送批次不得标记为 success。

**执行**：
- `tests/test_after_close_board_sync.py`
- `tests/test_delivery_worker_monitor_eligible.py`
- `docs/contracts/message-group.schema.json::invariants.image_failure_not_success`

### 6. interrupted 必须有 resume 路径

**规则**：盘后任务被中断后，必须有自动 resume 路径和 lease fencing。

**执行**：
- `tests/test_after_close_orchestrator.py`
- `tests/test_after_close_idempotent_dsa_pipeline.py`
- `tests/test_after_close_worker.py`
- `docs/contracts/after-close-recovery.schema.json::invariants`

### 7. Node/Chart/飞书/盘后相关代码改变时必须更新对应 current/contracts/CHANGE

**规则**：修改 Node Cluster/图表/飞书/盘后相关代码时，必须同步更新 `docs/current/`、`docs/contracts/` 和 `docs/changes/`。

**执行**：
- `tools/check_docs_consistency.py` — 文档一致性检查
- `tools/check_architecture.py::check_v2_docs_structure` — 必需文件存在性
- CP-14 新增：`docs/contracts/` 6 份机器合同文件

### 8. evidence 必须绑定最终 merge SHA 和镜像 SHA

**规则**：生产验收证据必须绑定到最终 merge SHA 和镜像 SHA，禁止绑定到中间 commit。

**执行**：
- `docs/evidence/` — 验收证据目录（待部署后填充）
- 部署门禁：用户明确批准 `批准分支部署，完成真实生产验收；merge前仍需停止。`

## Canonical 四链门禁（PRD V2.0 §6.2）

附加门禁：所有生产指标必须通过 `CanonicalComputationService.compute_with_mdas`/统一 Canonical InputProvider。

**执行**：
- `tests/test_algorithm_registry_architecture.py::TestFourChainDirectImportGate` — AST 门禁禁止四链模块直接 import kernel
- `tests/test_canonical_result_hash_matrix.py::test_real_four_chain_hash_matrix` — 四链 hash 一致性
- `tests/test_canonical_input_provider.py` — compute_with_mdas 行为验证

## tsc/eslint 可重复环境

**执行**：
- `.github/workflows/ci.yml::frontend-tsc` — `npm ci && npx tsc --noEmit`
- `.github/workflows/ci.yml::frontend-lint` — `npm ci && npm run lint`
- `frontend/package-lock.json` — 锁定依赖版本（143KB）
