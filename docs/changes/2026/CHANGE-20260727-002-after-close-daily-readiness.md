# CHANGE-20260727-002：盘后 AC-04 日线 readiness 修复与 P0 Redis 隔离复核

状态：已完成（Phase 5A：修复 AC-04，关闭 P0/P1，未启用自动部署，未改 SMC/Bollinger/Node Cluster 算法）
日期：2026-07-27
对应 PRD：`docs/prd/30-after-close.md`、`docs/prd/80-system-runtime.md`
对应 Map：`docs/maps/30-after-close.md`、`docs/maps/80-system-runtime.md`、`docs/maps/00-system-overview.md`、`docs/maps/technical/codebase-modules.md`

## 1. 变更原因

- **AC-04 冲突（P1）**：`docs/prd/30-after-close.md` AC-04 要求盘后编排 readiness 仅依赖目标交易日日线数据；Phase 4 审计发现 `after_close_orchestrator.py` 的 `checking_coverage` 步骤仍强制检查 15m 覆盖率（`intraday_result["ready"]`），导致日线已就绪但 15m 未就绪时无法进入 `computing_features`，与 PRD 冲突。
- **P0 Redis 隔离复核**：Phase 4 标记“本地调试若误连远程 Redis DB 0 可能消费正式队列/发布正式结果”为 P0 风险。Phase 5A 需定向核验所有入口是否统一经过 `Settings` 并在 development 下 fail-closed。
- **分支一致性补验**：Phase 4 服务器曾从脏分支创建本地 `experiment` 并提交 WIP；本机从 `dev` 创建 `experiment` 并推送 origin。三处 `experiment` SHA 可能不一致，必须先核验对齐。

## 2. 分支一致性补验

### 2.1 三处分支前后 SHA

| 分支 | 本地（前→后） | origin（前→后） | 服务器（前→后） |
|---|---|---|---|
| `main` | `13a0ef3` → `13a0ef3` | `13a0ef3` → `13a0ef3` | `13a0ef3`（检出）→ `13a0ef3`（检出，干净） |
| `dev` | `72dcd6c` → 本轮提交 SHA | `72dcd6c` → 本轮提交 SHA（push 后） | 无本地 dev → 补建 tracking `72dcd6c` |
| `experiment` | `069ebcc` → `069ebcc` | `069ebcc` → `069ebcc` | `623ad87`（分叉）→ `069ebcc`（对齐） |

### 2.2 服务器 experiment 分叉处理

- 服务器原 `experiment` tip = `623ad87`，含 16 个 V2.1 唯一提交，与 `origin/experiment`（`069ebcc`）分叉。
- 为服务器 `experiment` tip 创建 annotated tag `archive/server-experiment-wip-20260727`（tag object `40fb4ab2`）。
- tag 已 push origin 并用 `git ls-remote origin refs/tags/archive/server-experiment-wip-20260727` 验证存在。
- 服务器删除分叉的本地 `experiment`，按 `origin/experiment` 重新创建 tracking 分支。
- 未执行 `reset --hard`，未丢失唯一提交（保存在 archive tag 中）。
- WIP commit 通过密钥模式扫描（仅文档和测试 fixture，无生产密钥）。

### 2.3 最终对齐结果

- 本地、origin、服务器三处 `main`/`dev`/`experiment` 同名分支 SHA 一致（dev 在本轮 push 后一致）。
- 服务器补建 `dev` tracking 分支；本地 `experiment` 已设置 upstream 跟踪 `origin/experiment`。

## 3. P0 Redis 隔离复核（已关闭）

### 3.1 入口审计

| 入口 | 路径 | 是否经过 `Settings` | 证据 |
|---|---|---|---|
| Backend Redis Client | `backend/app/core/redis_client.py:L41,L58` | 是（`settings.redis_url`） | `from_url(get_settings().redis_url)` |
| DB engine 启动校验 | `backend/app/db.py:L26` | 是（模块加载触发 `get_settings()`） | 启动时即 fail-closed |
| after-close Worker | `backend/app/worker.py` | 是（通过 `app.db.AsyncSessionLocal`，无直接 Redis URL 读取） | 无硬编码 Redis URL |
| 手动 after-close API | `backend/app/api/admin_after_close.py` | 是（通过 `get_db`，无直接 Redis URL 读取） | 无硬编码 Redis URL |

### 3.2 校验规则核验（`backend/app/config.py`）

- `_resolve_redis_url`：缺失 `REDIS_URL` 抛 `MissingRequiredSettingError`。
- `_validate_redis_url`：development 环境下 DB 0 抛 `InvalidDatabaseURLError`；隐式 DB 0（无 `/N` 后缀）同样拒绝。
- 无 `localhost:6379/0` 默认回退。
- DB 15 允许（本地专用逻辑 DB）。

### 3.3 测试证据

`backend/tests/test_config_validation.py` 已覆盖：
- DB 0 拒绝；
- 隐式 DB 0 拒绝；
- DB 15 通过；
- 默认 `localhost` 拒绝；
- 缺 `REDIS_URL` 拒绝。

### 3.4 结论

P0 Redis 隔离风险已关闭并核验，无需新增代码或测试。

## 4. AC-04 修复

### 4.1 根因

`backend/app/services/after_close_orchestrator.py` 的 `checking_coverage` 步骤同时检查日线覆盖率和 15m intraday readiness：

```python
# 修复前（伪代码）
if not daily_coverage_ok or not intraday_result["ready"]:
    # 标记 failed
```

导致日线已就绪但 15m 未就绪时整个 after-close run 被阻塞，与 PRD30 AC-04“不再以 15m 数据作为主计算要求”冲突。

### 4.2 修改路径与符号

- 文件：`backend/app/services/after_close_orchestrator.py`
- 位置：`execute_after_close_run` 中 `checking_coverage` 步骤（L1277-L1334）
- 修改内容：
  - 移除 `BarsCoverageService.compute_intraday_coverage` 调用；
  - 移除 `intraday_result["ready"]` 判断；
  - 仅保留 `daily_coverage_ok = batch_result.daily_coverage is not None and batch_result.daily_coverage >= 0.9` 检查；
  - 日线不足仍标记 `failed` 并保留清晰原因；
  - `compute_intraday_coverage` 函数保留在 `BarsCoverageService` 供其他链路使用，未删除。

### 4.3 行为前后对比

| 场景 | 修复前 | 修复后 |
|---|---|---|
| daily OK + 15m OK | 进入 `computing_features` | 进入 `computing_features` |
| daily OK + 15m missing | **failed（阻塞）** | **进入 `computing_features`** |
| daily missing + 15m OK | failed | failed |
| daily missing + 15m missing | failed | failed |

### 4.4 不变式保留

- run 幂等、claim/re-claim、`published_run_id`、计算/发布分离、状态机行为不变。
- Scheduler 和手动入口复用同一 `execute_after_close_run` readiness 函数，禁止双实现。
- 全局 15m 数据、15m readiness 工具、其他链路的 15m 逻辑未删除。

### 4.5 测试证据

新增 3 个测试（`backend/tests/test_after_close_orchestrator.py`）：

| 测试 | 场景 | 预期 |
|---|---|---|
| `test_ac04_daily_ready_15m_missing_allows_proceed` | daily_coverage=0.95，15m missing | succeeded；`compute_intraday_coverage` 不被调用 |
| `test_ac04_daily_missing_blocks` | daily_coverage=0.5 | failed |
| `test_ac04_no_intraday_readiness_in_after_close_source` | AST 解析源码 | `execute_after_close_run` 中无 `compute_intraday_coverage` 调用 |

所有测试使用 mock/fixture，不连接共享数据库或 Redis。

## 5. 不变项

- 未实现 QM-50/QM-51 板块/指数聚合；
- 未改 SMC/Bollinger/Node Cluster 算法；
- 未启用自动部署；
- 未运行 Docker、构建、E2E、Migration、回填、全市场任务；
- 未启动 Worker，未写 Redis；
- AC-03 未实际运行，继续标记“已实现未运行核验”；
- AC-13 语义本轮只核验，未擅自修改；
- PRD30 未改（未发现内部自相矛盾）。

## 6. Maps 更新

| Map | 更新内容 |
|---|---|
| `docs/maps/30-after-close.md` | AC-04 状态 `部分实现` → `已实现并核验`；AC-06 移除 intraday 引用；§1 readiness 描述；§3 主要入口；§4 调用链；§7 P0/P1 标记已关闭 |
| `docs/maps/80-system-runtime.md` | 核验状态/提交；§4 Git 与 CI 三处分支 SHA、experiment 对齐、archive tag、服务器分支 |
| `docs/maps/00-system-overview.md` | 核验状态/提交；§6 P0/P1 标记已关闭，剩余 P1/P2 索引 |
| `docs/maps/technical/codebase-modules.md` | 核验状态/提交；§4 公共入口新增 after-close readiness 权威入口 |

## 7. 后续

- Phase 5B 建议：第一金字塔因子对齐（SMC 成交量信息 P1）。
- AC-03 本地完整链路运行核验待后续。
- 自动部署链路启用待用户确认后单独进行。
