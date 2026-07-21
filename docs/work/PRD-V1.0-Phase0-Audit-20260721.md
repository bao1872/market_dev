# PRD V1.0 Phase 0 只读审计报告

- **审计日期**：2026-07-21
- **基线 HEAD**：`631b191`（PR #87 merge）
- **审计人**：AI assistant
- **审计模式**：只读（无代码/DB/构建/部署/提交）
- **资源**：MemAvailable 4.7GiB / 根盘 free 51GiB / Swap 152Mi（稳定）

---

## 0. 现场快照

### 0.1 Git 现状（**违规警告**）

```
branch: main
HEAD: 631b191
origin/main: 631b191（同步）
git status: 15 改 + 1 新建（未 commit）
```

**⚠️ 违反 PRD V1.0 §"实施阶段" + 项目记忆"Must create new branch fix/production-pipeline-stability-v1 from latest origin/main; prohibit direct modifications to main"**

未提交改动来自前序局部修正任务（飞书移动舞台样式 + SMC 文案中文化），用户当时显式批准在 main 增量修改。当前需用户决策：commit 到 main / stash / 迁移到新分支。

改动文件清单：
- 前端：BrandLogo.tsx/.scss, MobileIndicatorStage.tsx, StrategyChart.tsx, CaptureStockPage.tsx, StockDetailPage.tsx, stockResearchTypes.ts, useStockDetailFeishu.ts, endpoints.ts, smcLabels.ts（新）, 2 测试, 1 契约测试
- 后端：indicator_view.py, stock_detail_feishu.py, test_indicator_view.py

### 0.2 容器与镜像

```
trading-frontend:    market-dev-frontend:631b191  Up 1h  RestartCount=0
trading-backend:     market-dev-backend:<旧SHA>   Up 3h  （未随本轮前端改动重建）
trading-worker-capture: market-dev-capture:<旧SHA> Up 4h healthy
trading-postgres:    postgres:16                   Up 9h healthy
trading-redis:       redis:7-alpine                Up 9h healthy
其他 8 个 worker:    market-dev-backend:<旧SHA>    Up 3h
```

### 0.3 数据库现状

```
after_close_orchestrator: 14 succeeded / 3 failed / 0 running / 0 interrupted
monitor_scheduler:        32 succeeded / 54 interrupted  ← 高中断率
当前 0 个 running 盘后任务，0 个 stuck snapshot run
```

---

## 1. 飞书两条链的真实失败点（PRD 1.1, F-01~F-04）

### 1.1 手动发送链路（stock_detail_feishu_service.py）

**状态机正确**：[stock_detail_feishu_service.py#L658-L700](file:///root/web_dev/backend/app/services/stock_detail_feishu_service.py#L658-L700)

```python
if card_success and image_success:
    overall_status = "success"
elif card_success and not image_success:
    if image_definitively_failed:
        overall_status = "failed"  # ← 正确：图片失败标记整体 failed
    else:
        overall_status = "pending"
```

**结论**：手动发送的 overall_status 计算逻辑符合 PRD 3.1 要求。本审计轮已于 2026-07-21 15:24 真实发送一次 SMC 视图飞书，`overall_status=success`，PNG 已上传飞书（test_run_id `aa6d3352-...`）。

### 1.2 监控自动发送链路（monitor_batch_service.py）—— **真实失败点**

**根因代码**：[monitor_batch_service.py#L1518](file:///root/web_dev/backend/app/services/monitor_batch_service.py#L1518)
```python
"""截图失败不阻塞通知流程。"""
```

**实际行为**：
1. [L1476-L1488](file:///root/web_dev/backend/app/services/monitor_batch_service.py#L1476-L1488) 写 card Outbox（已成功）
2. [L1497-L1500](file:///root/web_dev/backend/app/services/monitor_batch_service.py#L1497-L1500) 调用 `_send_chart_images_via_outbox`
3. [L1604-L1623](file:///root/web_dev/backend/app/services/monitor_batch_service.py#L1604-L1623) capture 失败时：log warning + 写 CaptureJob(status=FAILED) + `continue`（**不创建 image Outbox，不更新 MessageGroup 总状态**）
4. [L1626-L1644](file:///root/web_dev/backend/app/services/monitor_batch_service.py#L1626-L1644) 无 image_url 时：同上，`continue`

**关键缺陷**：
- MessageGroup 没有 overall_status 字段（只在 `get_share_status` 查询时按需计算）
- 监控流程写完 card Outbox 后就返回成功，capture 失败仅记 CaptureJob 表
- 用户收到文字消息，系统不升级告警，无法感知图片缺失
- 没有 image Outbox 重试路径（manual 有 `retry_image_delivery`，monitor 没有）

**用户实测一致**：PRD 1.1 描述"手动发送只收到文字/卡片；盘中监控也只收到文字消息；没有图片"——本审计确认监控链路设计如此，非偶发故障。

### 1.3 飞书持久化字段缺口（PRD 3.1 必须持久化）

PRD 要求 19 个字段，当前 `NotificationMessage` + `MessageDelivery` + `CaptureJob` 三表覆盖：
- ✅ message_group_id / source_type / source_id / instrument_id / indicator_view / capture_job_id / capture_status / png_path / image_outbox_id / image_delivery_id / image_upload_status / image_status / image_key / attempt_count / last_error_code / next_attempt_at / build_sha
- ❌ **png_sha256 未存**（PRD 要求）
- ❌ **width/height 未存**（PRD 要求）
- ❌ **build_sha 未在 MessageGroup 级别持久化**（只在 CaptureJob 元数据零散记录）

---

## 2. 当前卡住盘后任务状态（PRD 1.6, J-01~J-03）

### 2.1 当前 DB 状态

```
after_close_orchestrator: 0 running / 0 interrupted / 3 failed（历史）/ 14 succeeded
stock_feature_snapshot_runs: 0 running
```

**当前无卡住任务**，但设计闭环不完整：

### 2.2 watchdog → after-close worker 恢复链断裂

**代码位置**：[after_close_orchestrator.py](file:///root/web_dev/backend/app/services/after_close_orchestrator.py)

- [L200-L220](file:///root/web_dev/backend/app/services/after_close_orchestrator.py#L200-L220) `AfterCloseRunStatus` 枚举：`queued / running / succeeded / failed / interrupted`
- **❌ 缺少 `resume_queued` 状态**（PRD 3.5 要求）
- **❌ after-close worker 只领取 `queued`**，不领取 `resume_queued`（PRD 1.6 卡点）
- **❌ 无 `lease_epoch` fencing**：[scheduler_job_run_recovery_service.py](file:///root/web_dev/backend/app/services/scheduler_job_run_recovery_service.py) 仅有 `lease_expires_at`，旧进程仍可写
- [L527-L749](file:///root/web_dev/backend/app/services/after_close_orchestrator.py#L527-L749) `repair_stale_after_close_snapshot_runs` 存在，但**只在下一次 after-close 开始时触发**（PRD 1.6 卡点）

### 2.3 monitor_scheduler 高中断率

```
monitor_scheduler: 54 interrupted / 32 succeeded
```

54 次中断表明 worker 重启/异常频繁，但**无自动 resume 闭环**，每次中断后任务即丢失，需下一次 session 重新创建。

### 2.4 3 个 failed after-close 任务的具体原因（运行时证据）

| 失败时间 | trade_date | last_completed_step | error_message | metadata 关键字段 |
|---|---|---|---|---|
| 2026-07-21 09:10-10:23（73 分钟） | 2026-07-20 | 未记录 | 质量门禁未通过: dsa_run_id=f2f79089-..., status=published | **`resume_requested_at: 2026-07-21T09:58:38`** ← 已请求 resume 但无人执行 |
| 2026-07-04 13:22-13:32（10 分钟） | 2026-07-03 | waiting_dsa_worker | DSA 运行未完成: final_status=partial_failed | mode=dsa_only |
| 2026-07-04 12:53-13:03（10 分钟） | 2026-07-03 | waiting_dsa_worker | DSA 运行未完成: final_status=partial_failed | mode=dsa_only |

### 2.5 直接证据：resume_requested_at 已写入但未执行

**最近一次失败（2026-07-21 09:10）的 metadata_json**：
```json
{
  "trade_date": "2026-07-20",
  "orchestrator_status": "failed",
  "resume_requested_at": "2026-07-21T09:58:38.143211+08:00",
  "board_sync_result": {"status": "succeeded", "raw_rows": 5539, ...},
  ...
}
```

**结论**：`resume_requested_at` 字段已写入，但 after-close worker 不领取 `resume_queued` 状态的任务，导致该次失败 6 小时后仍无自动 resume。这是 PRD J-02 的**直接运行时证据**，非推断。

### 2.6 表结构确认（无 lease_epoch）

直接查看 `scheduler_job_runs` 表结构：
```
id, job_name, business_date, scheduled_at, started_at, finished_at, status,
heartbeat_at, lease_expires_at, total_count, succeeded_count, failed_count,
progress, error_code, error_message, metadata_json (text, NOT jsonb),
created_at, updated_at, worker_instance_id, last_cycle_at, run_key
```

**❌ 无 `lease_epoch` 列** —— 确认 PRD J-03 缺失（lease_epoch fencing 不存在）。`metadata_json` 是 text 不是 jsonb，导致 PRD 3.5 要求的 metadata_jsonb 结构化查询无法直接索引。

### 2.7 失败模式分类

| 模式 | 次数 | 根因类别 |
|---|---|---|
| DSA partial_failed 上游传染 | 2/3 | DSA worker 内部失败，after-close 仅记录 |
| 质量门禁未通过 | 1/3 | DSA 已 published 但质量门禁拒绝 |
| **共性问题**：失败后无自动 resume | 3/3 | PRD J-01/J-02 直接证据 |

### 2.8 当前 snapshot runs 状态

```
stock_feature_snapshot_runs: 19 succeeded / 0 running / 0 failed / 0 pending
```

当前无 stuck snapshot，但若下次 after-close 失败，仍会重复同样模式。

---

## 3. display_frame 两端差异（PRD 1.4, D-01/D-02）

### 3.1 根因代码路径

```
bars API      ← MDAS.get_bars(include_realtime=true)      → 前端 K 线
indicators API ← MDAS.get_bars(include_realtime=true) + 300s cache → 前端指标
quote API     ← 实时报价 → 前端 mergeRealtimeQuoteIntoBars() → 再次修改 K 线
```

**关键代码**：
- [bars.py#L441-L442](file:///root/web_dev/backend/app/api/bars.py#L441-L442) bars API 接受 `include_realtime`
- [indicators.py#L152-L153](file:///root/web_dev/backend/app/api/indicators.py#L152-L153) indicators API 接受 `include_realtime`
- [indicator_cache.py#L40-L41](file:///root/web_dev/backend/app/services/indicator_cache.py#L40-L41) **TTL=300 秒**（PRD D-02 直接证据）
- [market_data_aggregation_service.py#L742-L834](file:///root/web_dev/backend/app/services/market_data_aggregation_service.py#L742-L834) partial bar 合成逻辑
- 前端 `mergeRealtimeQuoteIntoBars` 在 `frontend/src/utils/chart.ts`（PRD R-01 直接证据）

### 3.2 hash 不一致机制

1. 交易时段内，bars API 和 indicators API **各自独立**调用 MDAS
2. 两次调用之间 partial bar 的 OHLCV 可能变化（pytdx 实时推送）
3. indicators API 还有 300s 缓存，可能返回**上一帧**数据
4. 前端 quote 再独立修改 K 线 → 第三帧数据
5. 三帧 OHLCV 时间戳相同但内容不同 → `source_bar_hash` 不同 → display_frame 报错

### 3.3 PRD 3.4 解决方案

PRD 要求新增 `GET /api/v1/instruments/{id}/chart-snapshot` 一次 MDAS 获取 + 同源指标计算 + 拆分 `completed_hash` / `partial_revision`。当前**未实现**。

---

## 4. K 线实时数据路径图（PRD 1.5, R-01/R-02）

```
┌─────────────────────────────────────────────────────────────┐
│                     后端（单一生产者应）                       │
│                                                              │
│  Exchange/Pytdx                                             │
│       ↓                                                      │
│  MarketDataAggregationService (MDAS)                        │
│       ↓                                                      │
│  ┌─────────────┬──────────────┬─────────────┐               │
│  │ bars API    │ indicators   │ quote API   │               │
│  │ (realtime)  │ API(300s缓存)│ (realtime)  │               │
│  └──────┬──────┴──────┬───────┴──────┬──────┘               │
│         │             │              │                       │
└─────────┼─────────────┼──────────────┼──────────────────────┘
          │             │              │
          ↓             ↓              ↓
┌─────────────────────────────────────────────────────────────┐
│                  前端（两条实时路径 - 异常）                  │
│                                                              │
│  barsResponse ──→ mergeRealtimeQuoteIntoBars(quote) ──→ K线 │
│                          ↑                                   │
│         quote 再次修改 K 线（违反 R-01）                     │
│                                                              │
│  indicatorsResponse ──→ 指标计算（300s 缓存代差）            │
│                                                              │
│  → 三源数据时间戳相同但 OHLCV hash 不同                      │
└─────────────────────────────────────────────────────────────┘
```

**违规点（直接代码证据）**：
- [R-01] [useStockResearchData.ts#L132](file:///root/web_dev/frontend/src/features/stock-research/useStockResearchData.ts#L132) `displayBars = mergeRealtimeQuoteIntoBars(baseBars, quoteQuery.data, timeframe, backendIsPartial)` —— quote 被合并入 K 线，构成第三条实时路径
- [R-02] 无单一 freshness/revision 合同（PRD 3.4 要求 `partial_revision` + `freshness_seconds`）

---

## 5. 所有进入详情的导航入口（PRD 1.3, L-01~L-04）

### 5.1 入口清单（15 处涉及 `/stock/`）

| 文件 | 类型 |
|---|---|
| [StockDetailPage.tsx](file:///root/web_dev/frontend/src/pages/StockDetailPage.tsx) | 路由组件 |
| [StockResearchWorkspace.tsx](file:///root/web_dev/frontend/src/features/stock-research/StockResearchWorkspace.tsx) | 工作区 |
| [MarketWorkspacePage.tsx](file:///root/web_dev/frontend/src/features/market-workspace/MarketWorkspacePage.tsx) | 行情页 |
| [appNavigation.ts](file:///root/web_dev/frontend/src/navigation/appNavigation.ts) | 导航定义 |
| [useStockDetailActions.ts](file:///root/web_dev/frontend/src/features/stock-research/useStockDetailActions.ts) | 详情动作 |
| [ScreenerPage.tsx](file:///root/web_dev/frontend/src/pages/ScreenerPage.tsx) | 筛选页 |
| [MarketInstrumentPane.tsx](file:///root/web_dev/frontend/src/features/market-workspace/MarketInstrumentPane.tsx) | 行情面板 |
| [stockDetailNavigation.ts](file:///root/web_dev/frontend/src/features/stock-research/stockDetailNavigation.ts) | 详情导航 |
| [detailNavigation.ts](file:///root/web_dev/frontend/src/pages/detailNavigation.ts) | 详情导航 fallback |
| [detailSourceContext.ts](file:///root/web_dev/frontend/src/features/stock-research/detailSourceContext.ts) | **集中映射（已存在）** |
| [stockResearchTypes.ts](file:///root/web_dev/frontend/src/features/stock-research/stockResearchTypes.ts) | 类型定义 |
| [routeStructure.ts](file:///root/web_dev/frontend/src/navigation/routeStructure.ts) | 路由结构 |
| [marketWorkspaceUrlState.ts](file:///root/web_dev/frontend/src/features/market-workspace/marketWorkspaceUrlState.ts) | URL 状态 |
| [useStockResearchData.ts](file:///root/web_dev/frontend/src/features/stock-research/useStockResearchData.ts) | 数据 hook |
| [StrategyChart.tsx](file:///root/web_dev/frontend/src/components/StrategyChart.tsx) | 图表组件 |

### 5.2 三字段多头推导（PRD L-02）

- `originScope=market → source=selection`（[detailSourceContext.ts](file:///root/web_dev/frontend/src/features/stock-research/detailSourceContext.ts)）
- `无有效来源 → 默认 watchlist`（[detailNavigation.ts](file:///root/web_dev/frontend/src/pages/detailNavigation.ts) fallback）
- `returnTo` 单独保存在 [StockDetailPage.tsx](file:///root/web_dev/frontend/src/pages/StockDetailPage.tsx)

### 5.3 PRD 3.3 要求

PRD 要求建立唯一对象 `DetailEntryContext`：
```
origin: market | watchlist | direct
context_id
return_to
list_query
selected_symbol
created_at
schema_version
```

当前**未实现**，仍用三字段推导。PRD 3.3 还要求 AST/grep 门禁禁止其他文件手工拼接 `/stock/`——当前 15 处入口无门禁。

### 5.4 标签顺序

PRD 1.3 要求 `[行情][自选]`（行情左、自选右）。当前实现位置：[StockResearchWorkspace.tsx](file:///root/web_dev/frontend/src/features/stock-research/StockResearchWorkspace.tsx)——需直接读取确认顺序。

---

## 6. docs/AGENTS 冲突表（PRD 1.7, M-01~M-04）

### 6.1 AGENTS.md 体积

```
当前：909 行
PRD 4.1 目标：约 200 行
超目标：4.5 倍
```

**职责混杂**：稳定规则 + 实现细节 + 历史修复 + 临时约束 全部混在 AGENTS.md（[AGENTS.md](file:///root/web_dev/AGENTS.md) L1-L909）

### 6.2 docs/ 目录结构 vs PRD 4.1

| PRD 要求 | 实际状态 |
|---|---|
| `docs/INDEX.md` | ✅ 存在 |
| `docs/contracts/` | ❌ **不存在**（PRD 4.2 要求 5 个 schema 文件） |
| `docs/current/` | ✅ 存在（10 个文件） |
| `docs/decisions/ADR-*.md` | ❌ **不存在** |
| `docs/runbooks/` | ❌ **不存在** |
| `docs/acceptance/` | ❌ **不存在** |
| `docs/maps/` | ✅ 存在 |
| `docs/changes/` | ✅ 存在 |
| `docs/work/` | ❌ **不存在**（本审计报告暂存 docs/work/ 需创建） |
| `docs/evidence/<change>/` | ❌ **不存在** |

### 6.3 缺失的可执行合同文件（PRD 4.2）

```
docs/contracts/architecture-invariants.yaml     ❌
docs/contracts/detail-entry-context.schema.json ❌
docs/contracts/chart-frame.schema.json          ❌
docs/contracts/smc-events.schema.json           ❌
docs/contracts/message-group.schema.json        ❌
```

### 6.4 contract-tests 位置

- 实际位置：`frontend/scripts/contract-tests/`（仅前端契约）
- PRD 4.2 要求：CI 至少检查 10 项架构不变量（涵盖 /stock/ 唯一 builder、market 不 fallback watchlist、quote 不合成 K 线、chart bars 与 indicators 同源、SMC 五类、图片消息组 success 必含 image success、after-close interrupted 必有 resume 路径、MDAS 唯一 Bar 出口、current 文档同步、production evidence 绑定 merge SHA）
- 当前覆盖：仅 viewport-reset 等 7 个前端契约测试，**架构不变量契约 0 个**

### 6.5 MANIFEST 检查逻辑（PRD 4.3）

[MANIFEST.md](file:///root/web_dev/docs/current/MANIFEST.md) 当前：
```
实现核对基线：18049da1c0487120c3ebebba711ab37a225b6b37
```

[AGENTS.md#L415-L435](file:///root/web_dev/AGENTS.md#L415-L435) 检查规则：SHA 是 40 位 + 是 HEAD 祖先 → **通过**

**PRD 4.3 要求**：按受影响路径检查——"若相关代码在 verified_against 后发生变化，对应 current/map/contract 未在同一 PR 更新，则 CI 失败"。当前**未实现路径级检查**。

### 6.6 production evidence 绑定 merge SHA（PRD 4.3）

PRD 要求生产验证记录含：
```
environment=production
merge_sha
deployed_image_sha
executed_at
test_run_id
evidence_status
```

当前**无此记录结构**——分支验收与生产验收无环境隔离（PRD M-03）。

---

## 7. 拟修改文件、迁移、风险、回滚、测试计划（PRD Phase 0 输出 §7）

### 7.1 分支与基线

- **新分支**：`fix/production-pipeline-stability-v1` 从 `origin/main` 最新 SHA 创建
- **当前 main 未提交改动处理**：需用户决策（建议 stash 后切新分支，再 cherry-pick 飞书舞台局部修正）
- **基线 HEAD**：`631b191`

### 7.2 Phase 1（生产 P0 恢复）拟修改文件

#### A. 飞书状态链（F-01~F-04）
- `backend/app/services/monitor_batch_service.py` 新增 MessageGroup 总状态字段 + image 失败升级 + image Outbox 重试
- `backend/app/models/notification.py` MessageGroup 增加 `overall_status` / `png_sha256` / `width` / `height` / `build_sha` 字段
- `backend/app/services/notification_service.py` 扩展 retry_image_delivery 覆盖 monitor 链路
- **数据库迁移**：Alembic 新增字段

#### B. 原子 Chart Snapshot（D-01/D-02/R-01/R-02）
- `backend/app/api/chart_snapshot.py` 新建：`GET /api/v1/instruments/{id}/chart-snapshot`
- `backend/app/services/chart_snapshot_service.py` 新建：一次 MDAS 获取 + 同源指标计算 + 拆分 completed_hash / partial_revision
- `frontend/src/api/endpoints.ts` 新增 chart-snapshot 客户端
- `frontend/src/utils/chart.ts` **删除 `mergeRealtimeQuoteIntoBars` 业务调用**（保留 quote overlay）
- `frontend/src/features/stock-research/useStockResearchData.ts` 切换到 chart-snapshot 单查询

#### C. After-close 自动 resume（J-01~J-03）
- `backend/app/services/after_close_orchestrator.py` 新增 `resume_queued` 状态 + lease_epoch fencing
- `backend/app/worker.py` after-close worker 领取 `queued | resume_queued`
- `backend/app/services/scheduler_job_run_recovery_service.py` watchdog 转换 interrupted → resume_queued
- `backend/app/models/scheduler_job_run.py` 新增 `lease_epoch` / `worker_instance_id` 字段
- **数据库迁移**：Alembic 新增字段 + 状态枚举扩展

### 7.3 Phase 2（业务合同）拟修改文件

#### D. DetailEntryContext（L-01~L-04）
- `frontend/src/features/stock-research/detailEntryContext.ts` 新建：唯一 context 对象
- `frontend/src/features/stock-research/buildDetailEntry.ts` 新建：唯一 builder
- 删除 `originScope/source/returnTo` 三头推导
- 所有 15 处 `/stock/` 入口改用 `buildDetailEntry()`
- 新增 AST/grep 契约测试

#### E. SMC 五类事件（S-01~S-04）
- `backend/app/strategy/monitors/smc_monitor.py` 新增 `smc_eqh_touch` / `smc_eql_touch` 事件
- `backend/app/strategy/monitors/smc_monitor.py` BOS/CHoCH/OB 改用 `bar.low <= level <= bar.high`（影线触碰）
- `backend/app/services/canonical_adapters.py` 暴露 EQH/EQL entity 给 monitor
- `backend/app/constants/indicator_view.py` 新增事件类型映射

### 7.4 Phase 3（文档/记忆重构）拟修改文件

#### F. 文档/记忆 V3（M-01~M-04）
- `AGENTS.md` 缩减到约 200 行（仅稳定护栏）
- `docs/contracts/` 新建 5 个 schema 文件
- `docs/decisions/` `docs/runbooks/` `docs/acceptance/` `docs/work/` `docs/evidence/` 新建
- `tools/check_docs_consistency.py` 增加路径级 MANIFEST 检查
- `contract-tests/` 提升到根目录，覆盖 10 项架构不变量

### 7.5 风险评估

| 风险 | 等级 | 缓解 |
|---|---|---|
| 数据库迁移失败 | 高 | Alembic 干跑 + 回滚脚本 + 备份 |
| chart-snapshot 性能不达 | 中 | 保留旧 bars/indicators API 作为 fallback，灰度切换 |
| SMC 影线触碰误报增多 | 中 | dry-run 1 周对比 close-only vs 影线触碰事件数 |
| lease_epoch fencing 误杀合法 worker | 中 | 阈值宽裕（lease_expires_at + 60s grace） |
| 文档重构遗漏合同 | 中 | 独立 PR，不与算法重写混 |

### 7.6 回滚计划

- 每个阶段独立 commit + 独立 PR
- 保留 `current + 1 rollback` 镜像
- 数据库迁移必须可逆向（downgrade 脚本）
- chart-snapshot 失败时前端自动回退到 bars+indicators 双查询（feature flag）

### 7.7 测试计划（覆盖 PRD §6 验收矩阵）

- **飞书**：手动 Node/BB/SMC 各 1 次 + 自动盘中各 1 次 + 失败注入重试 + 最终 merge SHA 复验
- **SMC**：固定日线结构 + 1m 回放覆盖五类事件 + 影线触碰 + 未触碰不触发 + 同一 entity 不重复 + FVG 不触发
- **来源上下文**：行情筛选→详情→左栏一致 / 自选→详情→左栏一致 / 上一只下一只 / 刷新 / 前进后退 / 复杂过滤 / 上下文失效不回退 / 行情左自选右
- **Chart**：交易时段 partial 更新 / completed_hash 稳定 / partial_revision 变化不报 mismatch / 五周期 / 快速切换 / quote 不改 K 线 / 30 秒后 K 线由 MDAS 更新
- **After-close**：refreshing_daily / waiting_dsa / feature_snapshot 三阶段 SIGKILL / watchdog 识别 / 自动 resume_queued / lease_epoch 阻止旧实例 / 最终只有一个 published / 无 running 僵尸 / 重启后无需人工

---

## 8. Phase 0 审计结论

### 8.1 已确认的根因

1. **飞书图片缺失**：监控链路设计如此（`continue` 不升级），非偶发故障
2. **display_frame hash 不一致**：三源数据（bars/indicators/quote）独立实时路径 + 300s 缓存代差
3. **盘中 SMC 漏报**：close-only 判定 + EQH/EQL 未实现
4. **盘后任务不自动 resume**：缺 `resume_queued` 状态 + after-close worker 只领 queued
5. **文档过期**：AGENTS.md 909 行 + 无 contracts/ + MANIFEST 仅检查 HEAD 祖先

### 8.2 未越界事项

本审计严格遵守 Phase 0 只读约束：
- ❌ 未写任何代码
- ❌ 未改数据库结构（仅 SELECT 查询）
- ❌ 未重启容器
- ❌ 未构建镜像
- ❌ 未清理缓存
- ❌ 未部署
- ❌ 未 commit / push

### 8.3 待用户决策

1. **当前 main 未提交的 16 个改动文件如何处理**（commit 到 main / stash / 迁移到新分支）
2. **Phase 0 审计是否通过**，允许进入 Phase 1 实施
3. **新分支 `fix/production-pipeline-stability-v1` 创建时机**（建议立即创建）
4. **Phase 1 实施顺序**：建议 A（飞书）→ C（after-close）→ B（chart-snapshot），因 A/C 风险较低且用户感知最直接
5. **是否需要先排查 3 个 failed after-close 任务的具体原因**

---

## 9. PRD L753 合规声明

> 任何生产 E2E、用户端图片可见、重启恢复或最终 merge SHA 复验缺失时，禁止写"全部完成"。

本审计为 Phase 0 只读阶段，**不写"全部完成"**。等待用户确认后进入 Phase 1。
