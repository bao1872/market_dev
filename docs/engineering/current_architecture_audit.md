# 盘迹工程现状审计基线（Task 001）

> 审计性质：**只分析，不修改**。未运行测试、未改代码、未启动服务、未新增除本文档外的任何文件。
> 审计方法：对 `backend/app`、`backend/tests`、`scripts` 做静态阅读与结构清点。
> 审计日期：2026-09-15

## 0. 审计边界与前置条件偏差（重要）

任务 001 的硬前置条件是「当前分支保持 `dev`」。实测分支为：

```
当前分支: verify/deploy-candidate-20260914
（git status 显示：ahead of origin/verify/deploy-candidate-20260914 by 1 commit）
```

**与前置条件不符 → 本次在 git add / commit / push 步骤停下，仅产出分析文档，等你显式裁决分支与推送方式后再执行提交。**

本审计报告本身是任务交付物，已按「请 IDE 新建 `docs/engineering/current_architecture_audit.md`」的要求产出，不触碰任何生产代码。

---

## 1. 后端架构地图

### 入口

| 入口 | 文件 | 角色 |
|---|---|---|
| API 入口 | `app/main.py` | FastAPI 应用，挂载 38 个 router，含 lifespan（启动恢复僵尸任务 / 种子 / 日历刷新）、Prometheus 中间件 |
| 统一后台入口 | `app/worker.py` | 单一 `python -m app.worker`，按 `WORKER_TYPE` 派发 11 类 worker（outbox/delivery/strategy_batch/bars_scheduler/strategy_scheduler/calendar_scheduler/monitor_scheduler/after_close_orchestrator/chip_consensus/auction_scheduler/watchdog/all） |
| Capture 入口 | `app/capture_main.py` | 个股详情截图快照服务 |

### 模块分层（调用链）

```
HTTP 请求
  ↓
API 层 (app/api, 38 routers)
  ↓
Service 层 (app/services, 169 个文件)
  ↓ ── 业务编排层 ──────────────────────────────
  │  after_close_orchestrator / auction_scheduler / chip_consensus /
  │  review_orchestrator / bars_scheduler / monitor / delivery / outbox
  ↓
Domain 层 (app/domain: auction / first_pyramid / review / shared)
  ↓
Strategy 层 (app/strategy: monitors / events/detectors / selectors / runtime)
  ↓
Repository 层 (app/repositories, 仅 7 个文件 — 极薄)
  ↓
Models (app/models, 49 个) + DB (app/db.py, AsyncSessionLocal)
  ↓
PostgreSQL + Redis（core/redis_client）
```

### 各层职责清点

- **API 入口（38 routers）**：`main.py` 挂载 health/auth/me/instruments/calendar/market/bars/capture/chart_snapshot/indicators/strategies/strategy_runs/monitor_states/strategy_events/notifications/watchlist/stock_memos/stock_context/board_analysis/review/auction 等。管理员路由 `admin_*` 占多数。
- **核心业务模块（services, 169 文件）**：可归为几族——
  - 盘后编排：`after_close_orchestrator.py`、`after_close_pipeline_service.py`、`eod_*_provider.py`
  - 竞价分析：`auction_*_service.py`（v32 / v21 / anchor / scan / scheduler / truth / publication）
  - Review/验证：`review_*_service.py`（orchestrator / scope / observation / metric / historical_ew / publication / attribution / cross_sectional）
  - 行情/因子：`bars_*_service.py`、`indicator_service.py`、`structural_factor_service.py`、`canonical_computation_service.py`、`feature_snapshot_service.py`
  - 基础设施：`scheduler_job_run_recovery_service.py`、`distributed_lock.py`、`idempotency.py`、`fenced_job_run_service.py`、`atomic_fact_contract_service.py`
- **数据访问（repositories, 7 文件）**：`bar_repository.py`、`monitor_state_repository.py`、`strategy_event_repository.py`、`strategy_result_repository.py` + 2 个 filter_helper + `__init__`。绝大多数数据访问直接散落在 service 层（SQLAlchemy session 在 service 内使用），**Repository 层极薄，不是统一数据访问边界**。
- **策略/研究模块**：
  - `app/strategy/`：`runtime.py`（策略运行时 ABC + MarketDataContext/StrategyResult/MonitorState/StrategyEventDraft）、`monitors/`（bollinger/smc/volume_node/watchlist）、`events/detectors/`（30 文件，事件检测）、`selectors/`（含 dsa_selector）、`budget.py`
  - `app/strategy_assets/`：`algorithms/`（36 .py，含 features/ 的 atr/bollinger/sqzmom/price_action 等算法 SSOT）、`manifests/`、`schemas/`
  - `app/research/`：`feature_computer.py`（per-bar 因果口径特征矩阵）、`feature_causality_registry.py`、`research_matrix_writer.py`
- **配置模块**：`app/config.py`（Pydantic Settings + 启动硬校验：拒绝 sqlite、环境/库名匹配、production JWT/SECRET 校验），实际值分离到 `config.local.py` / `config.test.py` / 外部 `CONFIG_FILE`，不入库。
- **后台任务模块**：全部收敛在 `worker.py` 的 `run_*_worker()` + `main()` 派发；状态机与租约在 worker 内。

---

## 2. 核心业务调用链

### 2.1 行情数据进入位置

- **实时行情**：`core/pytdx_adapter.py`、`services/realtime_market_*_provider.py`、`auction_quote_provider.py`
- **日线/历史**：`bars_fetch_worker.py`、`bars_scheduler_service.py`（`refresh_all_instruments`）、`pytdx_eod_snapshot_provider.py`、`ths_raw_daily_provider.py`、`eod_market_snapshot_provider.py`、`mootdx_calendar_provider.py`
- **外部源**：`wencai_client.py` / `wencai_board_provider.py`（板块）、`node_cluster_input_provider.py`

### 2.2 特征计算位置

- **生产单点快照**：`services/indicator_service.py` + `services/structural_factor_service.py`（仅最后一根 bar 的 single-snapshot）
- **研究 per-bar 全序列**：`research/feature_computer.py`（复用 `strategy_assets/algorithms/features/*` 与 `structural_factor_service` 固定参数作为 SSOT，33 个因果口径特征）
- **因子/质量**：`canonical_computation_service.py`、`factor_*_service.py`、`adjustment_factor_*`

### 2.3 策略判断位置

- **运行时契约**：`strategy/runtime.py` 的 `StrategyRuntime` ABC，定义 `execute`（selector）/ `calculate_state` + `detect_events`（monitor）
- **选择器**：`strategy/selectors/dsa_selector.py`（DSA 选股主逻辑）+ `strategy/budget.py`
- **监控器**：`strategy/monitors/*.py` + `strategy/events/detectors/*`（30 个事件检测器）
- **加载/版本**：`StrategyLoader`（`runtime.py`）按策略版本加载 manifest（`strategy_assets/manifests`）

### 2.4 信号生成位置

- 信号 = `StrategyEventDraft`（monitor 检测到）→ `strategy_events` 写入 → `event_recipient_service` / `notification_service` 分发 → `delivery_worker` 按渠道投递（Feishu 等）
- 选股结果 = `StrategyResult`（selector 输出）→ `strategy_result_repository` / `stock_core_publication_service` 发布

### 2.5 回测 / 验证位置

- **Review 子系统（核心验证层）**：`domain/review/`（filter_engine、metric_engine、scope_observation、attribution_engine、observation_primitives、tracking_state_machine、versions）+ `services/review_*_service.py`（orchestrator / scope / observation / metric / historical_ew / publication / attribution / cross_sectional）
- **历史回放/重建**：`review_historical_ew_db_shadow_runner.py`、`review_historical_scope_reconstruction_service.py`、`review_observation_prep_service.py`
- **验证 harness（PG 级）**：`scripts/ops/panji-verify`（AGENTS.md 注册的正式验证入口，针对 `bz_stock_verify_<sha>`）
- **实验性/离线**：`experiments/`（独立授权实验，非生产代码）、`scripts/review_compute_cli.py`、`scripts/review_historical_ew_db_shadow.py`

### 2.6 端到端链路（输入 → 结果）

```
外部行情源 (pytdx/ths/wencai/mootdx)
  → bars_scheduler_service / bars_fetch_worker / *_provider
    → bar_repository (PostgreSQL)
      → after_close_orchestrator (日线刷新 → DSA 选股 → 质量门禁 → 特征快照 → 发布)
        → strategy_assets/algorithms (特征) + strategy/selectors (DSA 判断) + strategy/monitors (盘中监控)
          → StrategyResult / StrategyEventDraft
            → publication_service (发布) + notification/delivery (信号)
              → 前端 (API via app/api/*)

验证侧独立闭环：
  domain/review + services/review_* + scripts/ops/panji-verify
    → Scope Observation / metric / filter / attribution 结果
      → review_publication_service → 前端 review 接口
```

---

## 3. 测试体系分析

测试位于 `backend/tests/`，共 **456** 个 `test_*.py`（含 `conftest.py`、`readiness_fixtures.py`、`allowlist.json`）。按子系统分类（近似计数）：

| 类型 | 位置 | 数量(约) | 覆盖对象 | 问题/评估 |
|---|---|---|---|---|
| integration (PG) | `test_*_pg*.py` / `test_*_postgres*.py` / `test_*_pg_integration.py` | ~20 (`auction_pg_integration`、`after_close_review_postgres_integration`、`board_sync_pg`、`historical_auction_backfill_pg`、`review_observation_persistence_pg`、`review_scope_evidence_pg` 等) | 真实 PG 链路、持久化契约 | 覆盖关键契约；但 PG 测试需 `bz_stock_verify_<sha>` 注册运行时，本地默认不跑（见 AGENTS.md §9） |
| contract / 架构 | `test_*_contract*.py`、`test_*_architecture*.py`、`test_api_v1_path_contract.py`、`test_algorithm_registry_architecture.py` | ~40+ | API 路径、原子事实契约、算法注册表架构、生产链契约 | 契约测试密度高，是保护核心逻辑的主阵地 |
| unit | 大量 `test_*.py`（非 pg/contract） | ~360+ | 各 service/domain 单元行为 | 面广；部分可能是 mock 重的薄单测，需甄别是否真保护生产语义 |
| verification (PG) | `scripts/ops/panji-verify` 驱动的正式验证 | 由契约测试子集承担 | 注册运行时的端到端验证 | 非普通 pytest 套件，需独立授权环境 |

重点子系统测试量级：

- `review_*`：**75** 个（验证/回测子系统测试最密集，与其业务核心地位匹配）
- `auction_*`：**31** 个（含 v32 生产链/持久化/围栏/域集成契约）
- `after_close_*`：**17** 个（orchestrator 控制流/契约/幂等/围栏）
- `first_pyramid_*`：**9**、`board_*`：**7**、`chip_*`：**7**、`monitor_*`：**10**、`dsa_*`：**10**、`bars_*`：**10**、`admin_*`：**13**、`feature_*`：**5**、`indicator_*`：**5**

### 判断

- **是否有真正保护核心逻辑的测试**：有，且密度高。契约测试（API path、atomic_fact_contract JSON、production_chain、fencing、idempotency）与 PG 集成测试直接守护核心语义，是降成本改造的安全网。
- **是否存在测试与生产脱节风险**：
  - `research/feature_computer.py` 复用 `strategy_assets/algorithms` 与 `structural_factor_service` 参数作为 SSOT，但研究矩阵（per-bar）与生产快照（single-snapshot）语义不同，若算法 SSOT 改动，单测可能各自 green 但两侧数值漂移——**存在两侧实现同步的风险点**。
  - Repository 层极薄，大量 SQL 直接写在 service 中，单元测试多靠 mock session，可能**测了 mock 而非真实查询语义**（潜在 false-green）。
- **false-green 风险**：AGENTS.md §2 明确禁止「测试存在 / 退出码 0 = 契约已运行通过」。本仓库有 `allowlist.json` 与 `panji-verify` 注册机制来对抗 false-green，但契约测试本身是否真消费生产 encoder/decoder（AGENTS.md §2.3）需逐文件核实，本审计未逐行验证。

---

## 4. 当前高风险区域（只列，不修改）

```
高风险: app/config.py
原因: 配置 / 环境 / 密钥 / 业务参数混合管理；虽有分离（local/test/example/外部文件），
     但任何环境切换或新参数加入都可能影响全局启动硬校验与多 worker 共享配置。

高风险: app/worker.py (2480 行)
原因: 统一 worker 入口，11 类 worker 派发 + 多 co-process（after_close / chip /
     review bootstrap 已物理删除 / auction scheduler）共享 _shutdown 与 SIGTERM drain；
     head-of-line blocking 修复后结构脆弱，状态机与租约逻辑集中，AI 改一处易破坏优雅停机契约。

高风险: app/services/after_close_orchestrator.py
原因: 盘后全流水线状态机（queued → refreshing_daily → syncing_boards → ... → succeeded），
     多步骤、异常 re-raise、DSA 异步轮询、幂等；是每日生产主链路，改动影响面最大。

高风险: app/domain/review/* + app/services/review_*_service.py
原因: 验证/回测子系统，75 个测试背后是复杂的 Scope Observation Model、
     filter_engine / metric_engine / attribution_engine / tracking_state_machine；
     语义密集、契约耦合强，是「机制去混淆」类实验的权威层。

高风险: app/research/feature_computer.py
原因: 研究特征矩阵直接 import 生产 strategy_assets.algorithms 与 structural_factor_service 参数；
     研究代码对生产内部实现存在强依赖，生产算法 SSR 改动可能静默影响研究结果。

高风险: app/contracts/atomic_fact_contract_v1.json 等契约 JSON
原因: JSON 契约是跨服务 truth 边界，版本演进需与生产链/持久化/前端同步，
     纯文本 diff 不易发现结构性不兼容。

高风险: alembic/ (190 文件)
原因: 迁移即生产数据形态变更；任何 model 改动需配套迁移并保持向后兼容，
     属 AGENTS.md Level 3 操作边界。

中风险: app/repositories/ (仅 7 文件)
原因: 数据访问边界不统一，SQL 散落 service；重构数据层前需先确权 ownership。
```

---

## 5. AI 修改风险分析（AI 最容易改错的地方）

按「改错概率 × 爆炸半径」排序：

1. **契约 / contract**（atomic_fact_contract JSON、API path contract、production_chain 契约）
   - 原因：跨服务边界、JSON 结构、版本演进；改错不会被普通单测立刻暴露。
2. **状态机 / async workflow**（`worker.py` co-process 派发、`after_close_orchestrator` 状态机、`tracking_state_machine.py`、`delivery_worker` 投递状态机）
   - 原因：async 并发、租约、SIGTERM drain、head-of-line 修复；局部改动易引发竞态或优雅停机失败。
3. **domain logic**（`domain/review/*` 的 filter/metric/attribution/scope_observation；`domain/auction/*`）
   - 原因：业务语义密集，单测覆盖虽高但语义正确性依赖契约，AI 易「数值对但语义偏」。
4. **database model / migration**（`models/*` + `alembic`）
   - 原因：属 Level 3，改错即生产数据形态风险。
5. **策略算法 SSOT**（`strategy_assets/algorithms/features/*`）
   - 原因：被生产快照与研究矩阵双向消费，改错同时污染两侧。
6. **配置 / 环境边界**（`config.py` + worker 环境变量）
   - 原因：多 worker 共享，环境误配难定位。

---

## 6. 后续降成本改造优先级建议（仅排序，本次不改）

> 目的：为下一步「第一批真正需要改的地方」提供输入。本审计不执行任何修改。

| 优先级 | 方向 | 依据 |
|---|---|---|
| P0 | 先固化契约测试安全网（atomic_fact_contract / API path / production_chain）再动代码 | 高密度契约已是护城河，改造前需确保不退化 |
| P1 | 收敛 `research → 生产内部实现` 的隐式依赖（feature_computer 对 algorithms/structural_factor 的 import） | 静默漂移风险，隔离后研究/生产可独立演进 |
| P2 | 统一数据访问边界（repositories 极薄、SQL 散落 service） | 降低 DB 改动爆炸半径，为迁移安全打底 |
| P3 | 拆分 `worker.py`（2480 行）与 `after_close_orchestrator` 状态机 | 降低 AI 改错概率与回归成本 |
| 观察 | `domain/review` 复杂度与 75 测试量级匹配，暂不建议大改，优先保护契约 | 避免优化错地方 |

---

## 附：本次未执行的动作

- ❌ 未运行测试（本任务仅静态分析；测试运行与否以你后续指令为准）
- ❌ 未修改任何代码
- ❌ 未执行 `git add / commit / push`（因分支前置条件 `dev` 未满足，见 §0）
