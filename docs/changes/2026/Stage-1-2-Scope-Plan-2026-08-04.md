# 阶段 1-2 范围规划与依赖图

**基线**: `8690ccccfca12737923c5088290aa883a663bcac`
**生成日期**: 2026-08-04
**来源依据**: `docs/changes/2026/PRD-Acceptance-Matrix-2026-08-04.md`
**目的**: 在进入实现前明确阶段 1（盘后编排 + 管理后台）与阶段 2（Feature Snapshot 性能与资源闭环）的工作范围、条目切分、依赖顺序、风险与产出。

---

## 1. 阶段 1：盘后编排 + 管理后台闭环

### 1.1 范围与目标

收敛所有未证伪的盘后编排（AC 系列）与管理后台/权限（PA / PV2 系列）实现缺口，使
`after_close_closed = proven` 与 `admin_pipeline_closed = proven`。**本阶段不涉及 Feature Snapshot 性能**（属阶段 2），不涉及 Review 业务闭环（属阶段 5），不涉及行情/导航（属阶段 4）。

### 1.2 涉及条目

| 子范围 | 来源 | 矩阵条目数 | 关键 ID |
|---|---|---|---|
| 远程/本地调度 | AC-01~03 | 3 | AC-01, AC-02, AC-03 |
| 计算与发布合同 | AC-04~14 | 11 | AC-04, AC-05, AC-06, AC-07, AC-08, AC-09, AC-10, AC-11, AC-12, AC-13, AC-14 |
| 旧路径与单入口 | AC-15, AC-16(2) | 2 | AC-15, AC-16(2) |
| 增量发布 | AC-08(2), AC-09(2), AC-10(2), AC-14(2) | 4 | AC-08(2), AC-09(2), AC-10(2), AC-14(2) |
| 阶段依赖 | AC-17, AC-18, AC-19 | 3 | AC-17, AC-18, AC-19 |
| 7 步状态机+复盘 | AC-70~73, AC-72A | 5 | AC-70, AC-71, AC-72, AC-72A, AC-73 |
| 权限模型 V1 | PA-01~03, PA-10~13, PA-20~21, PA-30~31 | 11 | PA-01, PA-02, PA-03, PA-10, PA-11, PA-12, PA-13, PA-20, PA-21, PA-30, PA-31 |
| 权限模型 V2 前端 | PV2-01~05 | 5 | PV2-01, PV2-02, PV2-03, PV2-04, PV2-05 |
| 权限模型 V2 后端合同 | PV2-B01~B09 | 9 | PV2-B01, PV2-B02, PV2-B03, PV2-B04, PV2-B05, PV2-B05a, PV2-B06, PV2-B07, PV2-B08, PV2-B09 |

合计：**53 条矩阵条目**（AC 28 + PA 11 + PV2 14）。

### 1.3 任务切片

#### 1.A 盘后状态机与单入口（AC-15, AC-16(2), AC-70）

- 任务 A1：核查 after_close 任务 `job_name`/`run_type` 全局唯一性，禁止 `dsa_only`。
- 任务 A2：实现 7 步状态机 `refreshing_daily→syncing_boards→checking_coverage→computing_features→publishing→computing_review→watchlist_ready`。
- 任务 A3：旧 `dsa_only` queued/running 记录只读识别；通过 cancel/interrupted/retry 路径收敛。

#### 1.B 远程自动 + 本地手动（AC-01~03）

- 任务 B1：远程 Scheduler 触发链（数据 ready → after_close 启动）端到端验证。
- 任务 B2：本地默认不启动 Scheduler；提供 CLI 入口 `scripts/after_close_cli.py` 支持单股/股票池/全市场。
- 任务 B3：本地调试不写共享库默认开关（`LOCAL_AFTERCLOSE_WRITE=0`）。

#### 1.C Readiness、Run 隔离、发布门禁（AC-04~14）

- 任务 C1：日线 readiness 检查 + 不满足原因输出。
- 任务 C2：单 run 隔离；局部调试/失败 run 不得自动成为正式结果。
- 任务 C3：计算与发布分离；发布指针 `factor_publications` 两阶段切换。
- 任务 C4：核心 `CORE_PUBLICATION_MIN_COVERAGE = 0.98` 强制门禁。
- 任务 C5：状态机 6 态 `pending/running/partial/completed/failed/published`。
- 任务 C6：部分失败时保留成功/失败/跳过/待重试范围。
- 任务 C7：幂等与补跑；同 input hash+version 不重算。

#### 1.D 跨 Worker 领取与心跳（AC-12, AC-18）

- 任务 D1：`SELECT FOR UPDATE SKIP LOCKED` + `lease_epoch` fencing。
- 任务 D2：30 秒 heartbeat 续 lease；stale watchdog 仅在 lease 过期 + heartbeat 不健康时回收。
- 任务 D3：chip_consensus Worker 在现有 after-close 容器内领取；不新增常驻容器。
- 任务 D4：`auto_resume_interrupted_after_close_runs` 覆盖 orchestrator + chip_consensus 两类 `interrupted`。

#### 1.E 增量发布与分层指针（AC-08(2)~10(2), 14(2), 17, 19)

- 任务 E1：`stock_core` publication pointer 小事务原子切换；`trade_date` NOT NULL。
- 任务 E2：`publish_market_aggregation` 验证 `source_core_run_id` 等于 stock_core pointer。
- 任务 E3：用户/详情/筛选只读 stock_core pointer；无 pointer 时回退 `published_at IS NOT NULL`。
- 任务 E4：聚合失败只重试聚合，不反改 core。
- 任务 E5：`is_stale` 真源为 `bars_daily.max(trade_date)`。
- 任务 E6：`stock_core pointer → board_analysis pointer → market_review_run` 顺序触发。

#### 1.F 复盘编排集成（AC-70, 71, 72, 72A, 73, RV-AC-01~04）

- 任务 F1：AC-70 7 步中 `computing_review` 子步骤（create/compute/publish）显式调用。
- 任务 F2：review 幂等重跑合同（同 pointer 命中则复用 run；任一上游变则新建 run）。
- 任务 F3：时间线合同（防负数耗时；缺一端 → duration=null；跨 attempt 不得配对）。
- 任务 F4：管理诊断操作（cancel/reconcile/restart/force）四态语义分离 + 二次确认。
- 任务 F5：review 冷启动（insufficient_history → 显示 raw/coverage + reason；禁用基于 normalized 的筛选）。

#### 1.G 权限模型 V1（PA-01~03, 10~13, 20~21, 30~31）

- 任务 G1：三类 capability 独立授予（self_selection/market_data/research_replay）。
- 任务 G2：自选数量由 capability `watchlist_limit` 控制；后端校验不依赖前端。
- 任务 G3：邀请码 30 天周期 + 起算点规则（now/old_expires_at/兑换当天）。
- 任务 G4：邀请码可重用性、激活/到期、撤销语义。

#### 1.H 权限模型 V2 前端展示（PV2-01~05）

- 任务 H1：默认入口矩阵（admin→overview；无 capability→forbidden；各单 capability 默认路由）。
- 任务 H2：`resolve_effective_access` 前端消费 + `/forbidden` 兜底。
- 任务 H3：legacy fallback 显式 `source=legacy_plan_fallback` 标记，UI 可见。
- 任务 H4：会员列表展示 capability 摘要 + 三张固定卡（self_selection/market_data/research_replay）。

#### 1.I 权限模型 V2 后端合同（PV2-B01~B09）

- 任务 I1：统一 `apply_capability_grant` 入口；`grant_days` 为唯一确定性期限输入。
- 任务 I2：场景化 `materialize_legacy`（注册 False / 旧用户续期 True / 管理员 True）。
- 任务 I3：统一 `SELECT User FOR UPDATE` 锁顺序。
- 任务 I4：tombstone `source="admin_revoke"` 撤销合同（重复撤销幂等、保留 granted_by）。
- 任务 I5：同事务结构化审计；`mutation_type` 精确区分（grant/extend/extend_and_quota_change/regrant/quota_change/revoke）。
- 任务 I6：独立 `change_self_selection_quota` 入口（不修改 expires_at）。
- 任务 I7：商业状态解耦（`resolve_commercial_status` fail-closed；六态 none/pending/active/expired/revoked/cancelled）。
- 任务 I8：`actor_user_id` 与 source 严格绑定（admin_grant+actor 必存在；invite_code+actor 必为 None）。
- 任务 I9：审计 request_id 复用请求链 `x-request-id`，禁止伪造。

### 1.4 依赖图

```text
G1 权限 capability 独立 ─┐
                         ├─► H1 默认入口矩阵
                         │      │
G2 watchlist_limit 后端 ─┤      ├─► I1 统一 grant 入口
                         │      │      │
G3 30 天周期 ────────────┘      │      ├─► I2 场景化物化
                                │      │      │
                                │      ├─► I3 锁顺序
                                │      │      │
                                │      └─► I5 审计 mutation_type ─► I6 quota_change 独立入口
                                │
                                ├─► I4 tombstone 撤销 ─► I8 actor/source 绑定
                                │
                                └─► I7 商业状态解耦

B1 远程触发 ─┐
             ├─► A1 唯一任务入口 ─► A2 7 步状态机 ─► C1 readiness ─► C2 run 隔离
B2 本地 CLI ─┘                                                            │
                                                                           ▼
                                                              C3 计算/发布分离 ─► C4 coverage 门禁
                                                                           │
                                                                           ▼
                                                              C5 6 态 + C6 部分失败 + C7 幂等
                                                                           │
                                                                           ▼
                                            D1 跨 Worker 领取 + D2 heartbeat + D3 chip_consensus Worker
                                                                           │
                                                                           ▼
                                            E1 stock_core pointer 原子切换 ─► E2 source_core 校验
                                                                           │
                                                                           ▼
                                            E3 读取端统一接入 pointer ─► E4 聚合失败隔离
                                                                           │
                                                                           ▼
                                            E5 is_stale 真源 = bars_daily.max(trade_date)
                                                                           │
                                                                           ▼
                                            E6 stock_core → board_analysis → market_review_run
                                                                           │
                                                                           ▼
                                            F1 computing_review 子步骤显式 ─► F2 review 幂等
                                                                           │
                                                                           ▼
                                            F3 时间线合同 ─► F4 管理诊断四态 ─► F5 review 冷启动
```

### 1.5 风险与回退点

| 风险 | 影响 | 回退点 |
|---|---|---|
| 7 步状态机与现有 8 步存在中间记录 | 历史 run 状态显示错乱 | 旧 step 映射到新 step；新代码只写新 step；旧 run 只读映射 |
| 跨 Worker fencing 与现有 lock 冲突 | 任务被错回收 | D1/D2 单 PR 引入；保留旧路径灰度开关 |
| `materialize_legacy` 行为变更 | 旧用户权限被替换/消失 | I2 场景化开关；新邀请码注册强制不物化 |
| `resolve_commercial_status` fail-closed | 现存 expired 商业周期显式失败 | 旧路径 only 改 `get_effective_subscription_status`；其他入口迁移在同 PR |
| chip_consensus Worker 集成 | 现有 after-close 容器资源紧张 | D3 不新增容器；并发上限与 1.G 配置项联动 |
| 时间线合同替换 | 旧 run 步骤显示为负数 | F3 缺一端显式 null + warning；不强行填充 |

### 1.6 完成判据

- `after_close_closed = proven`：AC 28 条全部 ✅（含 AC-70~73）。
- `admin_pipeline_closed = proven`：PA 11 + PV2 14 条全部 ✅。
- 相关单测与端到端测试（参见 §4 验证矩阵）全部通过。
- `docs/maps/30-after-close.md` 与 `docs/maps/60-permissions-admin.md` 已同步更新为已核验事实。

### 1.7 产出

- 1 份 `docs/changes/YYYY/CHANGE-YYYYMMDD-NNN-after-close-closure.md`
- 1 份 `docs/changes/YYYY/CHANGE-YYYYMMDD-NNN-permission-v2-closure.md`
- 同步更新 `docs/maps/30-after-close.md` 和 `docs/maps/60-permissions-admin.md`
- 矩阵中 53 条 [待填充] 替换为真实证据

---

## 2. 阶段 2：Feature Snapshot 性能与资源闭环

### 2.1 范围与目标

收敛 AC-16（Feature Snapshot 批处理性能合同）相关实现缺口，使 `feature_snapshot_closed = proven` 且 `performance_contract_passed = proven`。**本阶段不涉及算法正确性**（属阶段 3 跨入口验证），不涉及前端展示（属阶段 4）。

### 2.2 涉及条目

| 子范围 | 矩阵条目 | 关键点 |
|---|---|---|
| 批处理性能 | AC-16 + 4 子项 | 批量读取 / 有限并发 / 批级持久化 / 指标输出 / 基准比较 |
| 算法正确性（占位） | QM-01~63 | 本阶段只验证"是否走批量链"；语义正确性归阶段 3 |

### 2.3 任务切片

#### 2.A 批量入口与读链

- 任务 A1：确认 `feature_snapshot` 入口强制走 MDAS 批量 API；移除快照服务对行情 Repository 的直连调用。
- 任务 A2：复权口径统一在 MDAS 批量接口中处理；快照服务不得自行实现复权。
- 任务 A3：同一 `symbol × period × trade_date` 的 canonical bars frame + 诊断 hash 在批内复用。

#### 2.B 并发与心跳

- 任务 B1：定义有限并发上限（环境变量 `FS_MAX_CONCURRENCY`，默认 8）。
- 任务 B2：使用有界并发（信号量/限流器），禁止无界 `asyncio.gather` / `ProcessPool` 池。
- 任务 B3：每批 heartbeat + progress；资源紧张时自动降并发（CPU/内存阈值）。

#### 2.C 批级持久化

- 任务 C1：成功快照走 batch upsert/flush；不得逐股 commit。
- 任务 C2：调用方仍持有整日期事务；失败率超阈值可整体 rollback；已发布快照保护不变。
- 任务 C3：单股失败隔离 + `lease_epoch` fencing；不阻塞其他股票。

#### 2.D 指标与基准

- 任务 D1：暴露低基数 metrics：`batch_count / query_count / commit_count / durations / fallback_count / effective_concurrency`。
- 任务 D2：构建基准 fixture（fix 全市场 5000 只股票 + 250 交易日）对比：
  - 查询次数不随 `symbol_count` 增长（O(batch) 而非 O(N)）。
  - commit 次数 = O(batch)。
  - 整体耗时相对旧链降 50%。
- 任务 D3：CI 引入性能回归门禁（p95 耗时上限 + fallback 比例上限）。

### 2.4 依赖图

```text
A1 批量入口（移除直连）──► A2 复权统一在 MDAS ──► A3 canonical frame 复用
                                  │
                                  ▼
                       B1 并发上限 + B2 有界并发 + B3 降并发
                                  │
                                  ▼
                       C1 批级 upsert + C2 整日期事务 + C3 单股隔离
                                  │
                                  ▼
                       D1 metrics 输出 ──► D2 基准 fixture ──► D3 性能门禁
```

### 2.5 风险与回退点

| 风险 | 影响 | 回退点 |
|---|---|---|
| MDAS 批量 API 改动涉及多家调用方 | 范围扩散 | 阶段 2 仅做"走批量"验证；接口签名变更单独立项 |
| 性能门禁在低配 CI 抖动 | 红绿不稳定 | D3 阈值留 20% buffer；首次落地允许 10% 容差 |
| 批级 commit 引入事务放大 | 长事务锁竞争 | C2 失败率超阈值走 rollback；正常路径拆短批 |
| 旧直连路径存在隐式调用 | 回退不彻底 | A1 删除直连后必须有 fail-closed 错误码 |

### 2.6 完成判据

- `feature_snapshot_closed = proven`：AC-16 主体 + 4 子项全部 ✅。
- `performance_contract_passed = proven`：D2 基准达成 + D3 门禁在 CI 绿。
- 矩阵中 AC-16 + 4 子项的 [待填充] 替换为真实证据（代码路径 + 指标截图/日志）。

### 2.7 产出

- 1 份 `docs/changes/YYYY/CHANGE-YYYYMMDD-NNN-feature-snapshot-batch-closure.md`
- 1 份 `docs/maps/20-quant-model.md` 增量（FS 性能部分）
- 1 份性能基准报告（嵌入 Change 文件或独立 runbook）

---

## 3. 阶段 1-2 整体依赖与时间建议

### 3.1 横向依赖

```text
阶段 1.G/H/I 权限 V2 ─► 阶段 1.A-F 盘后/管理后台（I1~I9 解析函数复用）
              │
              └─► 阶段 1.F 管理诊断四态（F4 复用 PV2-B08 actor/source 绑定）

阶段 1.D 跨 Worker ─► 阶段 1.E 增量发布（D1 lease_epoch 复用 E1 pointer 切换锁）
              │
              └─► 阶段 2.C 批级持久化（C3 复用 lease_epoch fencing）

阶段 1.C 发布门禁 ─► 阶段 2.D 性能门禁（C4 覆盖率概念与 D3 p95 阈值同源）
```

### 3.2 建议落地顺序

1. **阶段 1.I 权限 V2 后端合同**（I1 → I2 → I3 → I4 → I5 → I6 → I7 → I8 → I9）：
   先把解析与锁基础打稳，下游管理后台与盘后管理 API 才有稳定判权入口。
2. **阶段 1.G/H 权限 V1 收口 + V2 前端展示**：补齐三类 capability 与默认入口矩阵。
3. **阶段 1.A-B 状态机与本地/远程**：
   7 步状态机为后续所有 E/F 的运行容器，必须先收敛。
4. **阶段 1.C-D 发布门禁 + 跨 Worker**：在状态机内接入 readiness、coverage、lease/heartbeat。
5. **阶段 1.E 增量发布**：
   stock_core pointer 原子切换 + 读取端统一接入。
6. **阶段 1.F 复盘编排集成**：7 步中 `computing_review` 显式调用 + 时间线合同 + 管理诊断四态。
7. **阶段 2.A-D Feature Snapshot 性能**：在阶段 1 完成后做性能收口，避免与状态机变更交叉。

### 3.3 阶段 1-2 完成后的全局状态

```text
after_close_closed = proven
admin_pipeline_closed = proven
feature_snapshot_closed = proven
performance_contract_passed = proven
first_pyramid_core_code = largely_closed   (阶段 3 收口)
smc_core_code = largely_closed             (阶段 4 收口)
review_core_code = largely_closed          (阶段 5 收口)

code_ready = false                         (阶段 6 才翻转)
deployment_phase_ready = false
data_closed = false
```

---

## 4. 验证矩阵（与 §1.1 / §2.2 矩阵条目一一对应）

| 验证类型 | 阶段 1 | 阶段 2 |
|---|---|---|
| 行为单测 | AC-04~14, AC-16(2), AC-70, PA-01~03, PA-10~13 | AC-16 子项 1~3 |
| 集成测试 | AC-01, AC-03, AC-12, AC-18, AC-17/19, F1~F3, PV2-B01~B09 | AC-16 子项 1+3+4 |
| 端到端 | AC-08(2), AC-09(2), AC-10(2), AC-14(2), F4 管理诊断 | D2 基准 + D3 性能门禁 |
| 真实数据 | AC-01 远程触发、AC-06 readiness、AC-18 chip worker、AC-70 7 步、PV2-04 抽屉 | D2 全市场 fixture（建议先小数据集 → 全市场） |
| 回退演练 | F4 cancel/reconcile/restart/force 四态语义 | C2 失败率阈值 rollback |

---

## 5. 不在本阶段范围

明确剔除（避免越界）：

- 阶段 3：第一金字塔跨入口完整闭环（QM-63 + Review §27 + MX-20 字段一致）。
- 阶段 4：SMC 语义一致性（QM-13/21/24）+ 行情导航（MX-05/10/40~64）。
- 阶段 5：Review 业务闭环（§0~27 章节中未在 AC-70/71/72/73 覆盖的部分）。
- 阶段 6：代码验收门（Ruff/Mypy/TSC/ESLint/Build/Architecture/Docs/Governance）。
- 阶段 7：受控部署 + 真实数据闭环（SR 系列 + Preflight + Canary + 单日全量）。

---

## 6. 配套记录位置

- 阶段 1 实现记录：`docs/changes/YYYY/CHANGE-YYYYMMDD-NNN-after-close-closure.md`
- 阶段 1 权限 V2 记录：`docs/changes/YYYY/CHANGE-YYYYMMDD-NNN-permission-v2-closure.md`
- 阶段 2 实现记录：`docs/changes/YYYY/CHANGE-YYYYMMDD-NNN-feature-snapshot-batch-closure.md`
- 阶段 1 Map 更新：`docs/maps/30-after-close.md`、`docs/maps/60-permissions-admin.md`
- 阶段 2 Map 更新：`docs/maps/20-quant-model.md`（FS 性能段）
- 阶段 1-2 矩阵更新：`docs/changes/2026/PRD-Acceptance-Matrix-2026-08-04.md`（替换 53 条 [待填充]）
