# 自选与盘中监控 Map

核验状态：部分核验（§4.1 图片捕获链路已核验，其余待核验）
最后核验日期：2026-07-28（§4.1）
核验分支：未核验
核验提交：c8da6c2（基线）+ 本地未提交修改（CHANGE-20260728-001）
核验范围：§4.1 图片捕获链路基于代码+测试核验；其余待核验
对应 PRD：`../prd/50-watchlist-intraday.md`
事实所有权：自选存储、排序、盘中任务、异常消息、转发和权限入口

> 本文件必须基于真实代码、数据、日志或运行结果填写。不得根据 PRD 推测实现已经存在。

## 1. PRD 实现映射

| PRD 条款 | 当前实现入口 | 状态 | 验证证据 |
|---|---|---|---|
| WI-01 | 自选 API/表/Store 待核验 | 部分实现 | 未核验 |
| WI-02 | 自选数量限制待核验 | 部分实现 | 未核验 |
| WI-03 | 排序语义待核验 | 已知曾有偏差 | 未核验 |
| WI-04 | 用户隔离待核验 | 未核验 | 未核验 |
| WI-10 至 WI-15 | 盘中监控链路待核验 | 部分实现 | 未核验 |

## 2. 自选数据流

```text
用户操作
→ 前端自选状态
→ 自选 API
→ 权限和数量校验
→ 自选存储
→ 行情/详情来源列表
→ 盘中监控对象
```

## 3. 权威入口

| 能力 | 前端入口 | API | Service | 存储 |
|---|---|---|---|---|
| 添加自选 | 待核验 | 待核验 | 待核验 | 待核验 |
| 删除自选 | 待核验 | 待核验 | 待核验 | 待核验 |
| 自选排序 | 待核验 | 待核验 | 待核验 | 待核验 |
| 数量限制 | 待核验 | 待核验 | 待核验 | 权限配置待核验 |
| 盘中监控 | 待核验 | 待核验 | 待核验 | 待核验 |

## 4. 盘中监控

### 4.1 监控事件类别（CHANGE-20260728-010）

`watchlist_monitor` 只保留两类触发事件：

- **结构（EVENT_CATEGORY_STRUCTURE）**：SMC BOS/CHoCH/EQH/EQL/OB first touch
- **筹码共识（EVENT_CATEGORY_NODE_CONSENSUS）**：node_cluster_touch

布林带（Bollinger）不再触发盘中监控事件；Bollinger 算法本体保留供盘后与个股详情页图层使用。

state schema version=3（CHANGE-20260728-010）：
- 命名空间：`node_cluster` / `smc` / `market` / `degraded`（移除 `bb`）
- 旧 v1/v2 state 含 bb 字段仅历史回读，新业务不再生成
- SMC 子状态显式回写 namespace + 顶层，保证 episode 连续

事件类别映射入口：`backend/app/constants/indicator_view.py::EVENT_TYPE_TO_CATEGORY`
- 仅用于文字与统计归类（结构 X｜筹码共识 Y），不再决定截图图层

### 4.2 图片捕获链路（CHANGE-20260728-001 + CHANGE-20260728-010）

权威入口：`backend/app/services/monitor_batch_service.py::_send_chart_images_via_outbox`

截图粒度：每个唯一 `(instrument_id, event_id)`，非每股票一次。

```text
monitor_batch_service
→ 按 instrument 分组事件
→ per event 遍历：
    → is_supported_event_type(event_type, payload) 检查
       ├─ 已支持：结构 / 筹码共识事件
       └─ 未支持：写 CaptureJob(status=failed, error_code=UNSUPPORTED_INDICATOR_VIEW)，跳过
    → 构建 focus_event、生成 capture token
    → capture_run_id = f"monitor-{inst_id}-{event.id}-{indicator_view}"
    → 输出文件名含 event.id 与 indicator_view
    → 创建 CaptureJob(status=pending, indicator_view=FEISHU_CAPTURE_VIEW)
    → 调用 capture worker（同事件多用户只截图 1 次）
    → 每个有资格用户创建 image Outbox
    → 失败隔离：单事件失败只记 CaptureJob，不阻塞其他事件
```

[CHANGE-20260728-010] 截图固定组合视图：
- `indicator_view` 固定为 `FEISHU_CAPTURE_VIEW='structure_node'`（不再按事件类型映射 smc/node_cluster/bollinger）
- 图层固定：`node + smc + volume`，`boll=false`
- 后端 `/capture/stocks/{id}/snapshot` 强制 `include_smc=True`
- combined Ready = `nodeReady && smcContractReady`（events/order_blocks 为数组允许为空；swing_bias 为 number 1/-1/0）
- 纯函数实现位置：`frontend/src/features/stock-research/captureReady.ts`（[P0 补丁 2026-07-29] 提取，可独立单元测试）
- 截图调用方 timeout=120s（`CAPTURE_HTTP_TIMEOUT_SECONDS`）

幂等键：`user_id + instrument_id + event_id + indicator_view`
- 同事件重试不重复
- 不同事件即使同一分钟也不互相去重
- 新业务 indicator_view 固定为 structure_node，幂等键维度仍保留以区分新旧业务

事件类型 → indicator_view 历史映射（仅供旧数据回读）：`backend/app/constants/indicator_view.py::EVENT_TYPE_TO_INDICATOR_VIEW`
- BOS/CHoCH/EQH/EQL/OB first touch → smc（历史值）
- node_cluster_touch → node_cluster（历史值）
- bb_*_touch → bollinger（历史值，新业务不再触发）
- 未知类型 → UNSUPPORTED_INDICATOR_VIEW（显式跳过，不回退 node_cluster）

### 4.3 待核验

- 触发器；
- 数据周期；
- 自选对象生成；
- 异常规则；
- 消息内容（文本链路）；
- Feishu 或其他转发；
- 频率；
- 盘中与盘后状态隔离。

## 5. 转发角色

当前产品规则是“可标记异常但不下结论”。实现中需核验是否存在角色、权限或操作入口。

## 6. 已知偏差

- 自选排序在不同页面曾出现跳变；
- 盘中和自选权限是否完全统一需核验；
- 自选数量是否由后端强制执行需核验。

## 7. 前端验证结果（Phase 5B-0）

**验证环境**：本地原生 Backend (port 8000) + Frontend (port 8008) + SSH 隧道；admin token；2026-07-27。

自选与盘中监控相关路由在本轮通过 `/overview` → 自选入口与 `/market` → 行情列表的 SPA 客户端重定向验证。`/watchlist` 路由本身为重定向目标（HTTP 200，SPA 客户端跳转），未发现运行时错误。

详细路由验证表见 `docs/maps/40-market-stock-experience.md` §8 与 `docs/maps/80-system-runtime.md` §10。本轮未对自选排序在不同页面间的一致性、自选数量后端强制执行做深度核验（保留为已知偏差）。
