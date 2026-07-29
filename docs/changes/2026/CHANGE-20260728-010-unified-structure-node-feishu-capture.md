# CHANGE-20260728-010：盘中监控双类别 + 飞书固定组合图

状态：代码+目标纯单元测试+TSC+ESLint+Ruff 通过；浏览器真实链路验收待用户手工
日期：2026-07-28
类型：behavior + architecture
领域：盘中监控 / 飞书截图 / 行情体验

相关 PRD：
- `../../prd/50-watchlist-intraday.md`：WI-02（监控双类别）、WI-12（图片与文字解耦）
- `../../prd/40-market-stock-experience.md`：MX-30（飞书固定组合图）

相关 Maps：
- `../../maps/50-watchlist-intraday.md`：§2（监控事件类别）、§4（截图链路）
- `../../maps/40-market-stock-experience.md`：§6（飞书发送链路）
- `../../maps/backend-module-map.md`、`indicator-computation-map.md`、`api-route-map.md`、`test-coverage-map.md`

相关 Rules：
- `../../../rules/10-product-domain-invariants.md`：两类监控+固定组合图稳定不变量
- `../../../rules/20-market-data-indicators.md`：组合 Capture 合同

相关提交：
- 基线：95277a8（dev = origin/dev）
- 本轮 commit：待填写

## 1. 变更摘要

watchlist_monitor 盘中事件由原 3 类（结构 / 筹码共识 / 布林带）收敛为 2 类（结构 / 筹码共识）。
任一事件触发时，飞书截图固定使用"结构 + 筹码共识"组合视图（FEISHU_CAPTURE_VIEW='structure_node'），
不再按事件类型切换单指标视图；事件文字仍只描述实际触发事件，与图片语义解耦。

个股详情"发送到飞书"弹窗取消指标单选，固定发送同一张组合图。

仅删除"布林带盘中监控/飞书视图"；Bollinger 算法、普通个股详情页布林带图层、盘后模块对 BB 的合法使用保留。

## 2. 行为变化

### 2.1 watchlist_monitor 监控事件类别

| 维度 | 旧 | 新 |
|---|---|---|
| 触发事件类别 | 结构 / 筹码共识 / 布林带 | 结构 / 筹码共识 |
| BB 事件计算 | BollingerMonitor 委托 | 移除（BB 算法保留，但不进入监控） |
| state schema version | 2（含 bb 命名空间） | 3（仅 node_cluster/smc/market/degraded） |
| 旧 state 读取兼容 | — | v1/v2 旧 bb 字段仅历史回读 |
| SMC 子状态回写 | smc_episode_tracker 未写回父 curr_state | 显式回写 namespace + 顶层，保证 episode 连续 |

### 2.2 飞书截图视图

| 维度 | 旧 | 新 |
|---|---|---|
| 视图选择 | 事件类型 → indicator_view 映射（node_cluster/bollinger/smc） | 固定 FEISHU_CAPTURE_VIEW='structure_node' |
| 图层组合 | 每张图只渲染一个指标 | 固定组合：node + smc + volume；boll=false |
| Capture query | 透传 indicator_view 切换图层 | 固定 indicator_view=structure_node，后端忽略渲染逻辑 |
| 缓存键 | iv=node_cluster/bollinger/smc | iv=structure_node |
| CaptureJob.indicator_view | 按事件类型写入 | 固定写入 structure_node |
| combined Ready | 单指标各自 Ready | nodeReady && smcContractReady（SMC 数组允许为空） |
| 截图超时 | 默认（30s） | 120s（> Capture 渲染最大 90s） |

### 2.3 个股详情飞书发送

| 维度 | 旧 | 新 |
|---|---|---|
| 弹窗 UI | 三选一 radio（node_cluster/bollinger/smc） | 无选择器，固定说明"将发送：结构 + 筹码共识组合图" |
| 请求体 | 携带 indicator_view | 不携带（旧字段兼容接收但忽略） |
| 文字消息 | 含 BB 字段（upper/lower/mid） | 移除 BB 字段，含 node + smc 字段 |
| 截图 payload | 透传 indicator_view | 固定 indicator_view=structure_node |
| image_resource_refs | 携带 indicator_view | 固定携带 structure_node |

### 2.4 manifest 变化

`backend/app/strategy_assets/manifests/watchlist_monitor.yaml`：
- name 由 "BB+节点监控" 改为 "结构+筹码共识监控"
- version 升级到 1.3.0
- 移除 BB 参数 / 输出 / event_types / chart_layers
- 保留 Node 250/4000/2 和 SMC 算法参数不变

## 3. 代码修改清单

### 3.1 后端

| 文件 | 变更 |
|---|---|
| `backend/app/strategy/monitors/watchlist_monitor.py` | 移除 BollingerMonitor 委托；合并状态只保留 node_cluster/smc/market/degraded；STATE_VERSION=3；修复 SMC 子状态回写 |
| `backend/app/strategy_assets/manifests/watchlist_monitor.yaml` | 删除 BB 参数/输出/事件/图层；name 改为"结构+筹码共识监控"；version=1.3.0 |
| `backend/app/services/monitor_batch_service.py` | 删除 BB emoji/severity/计数；概览改为"自选股N只｜触发M只｜结构X｜筹码共识Y"；SMC 五类归为结构，node_cluster_touch 归为筹码共识；按 event.id 去重；修复 summary 与 items 重复；截图固定 FEISHU_CAPTURE_VIEW；超时 120s |
| `backend/app/constants/indicator_view.py` | 新增 FEISHU_CAPTURE_VIEW='structure_node'；新增 EVENT_CATEGORY_STRUCTURE/EVENT_CATEGORY_NODE_CONSENSUS；新增 EVENT_TYPE_TO_CATEGORY 映射；保留 EVENT_TYPE_TO_INDICATOR_VIEW 历史回读 |
| `backend/app/constants/capture.py` | 新增 FEISHU_CAPTURE_PRESET 组合视图（node+smc+volume，boll=false）；CAPTURE_HTTP_TIMEOUT_SECONDS=120；旧 3 套 preset 标记 _legacy |
| `backend/app/api/capture.py` | 强制 resolved_indicator_view=FEISHU_CAPTURE_VIEW；强制 include_smc=True；忽略 URL indicator_view 参数 |
| `backend/app/services/stock_capture_service.py` | 文档说明新业务固定 iv=structure_node；保留缓存键维度区分新旧 |
| `backend/app/api/stock_detail_feishu.py` | SendFeishuRequest.indicator_view 标记历史兼容；不透传 indicator_view 到服务层 |
| `backend/app/services/stock_detail_feishu_service.py` | 忽略入参 indicator_view，强制 resolved_indicator_view=FEISHU_CAPTURE_VIEW；移除 build_monitor_event_text 的 BB 字段；capture_payload 固定 indicator_view=structure_node；httpx timeout=120s |
| `backend/app/services/message_builder.py` | 新增 structure_node 视图分支，同时展示 node + smc 字段；移除 BB 字段处理 |
| `backend/app/capture_main.py` | 文档说明固定组合视图 |

### 3.2 前端

| 文件 | 变更 |
|---|---|
| `frontend/src/api/endpoints.ts` | IndicatorView 类型新增 'structure_node'；新增 FEISHU_CAPTURE_VIEW 常量；SendFeishuRequest.indicator_view 标记历史兼容；sendStockDetailFeishu 不再透传 indicator_view |
| `frontend/src/features/stock-research/stockResearchTypes.ts` | 新增 FEISHU_CAPTURE_LAYER_PRESET（node=true, smc=true, boll=false）；INDICATOR_VIEW_LAYER_PRESETS 添加 structure_node 映射；INDICATOR_VIEW_LABELS/VALUES 包含新视图 |
| `frontend/src/features/stock-research/useStockDetailFeishu.ts` | 移除 selectedIndicatorView 状态；sendFeishuMutation 不再传递 indicator_view |
| `frontend/src/pages/StockDetailPage.tsx` | 删除三选一 radio；新增组合图说明文案 |
| `frontend/src/pages/CaptureStockPage.tsx` | 固定 indicatorView=FEISHU_CAPTURE_VIEW；computeCombinedReady 替代 computeTypeSpecificReady（nodeReady && smcContractReady） |
| `frontend/src/components/StrategyChart.tsx` | 截图模式固定使用 FEISHU_CAPTURE_LAYER_PRESET；注释说明新行为 |
| `frontend/src/components/MobileIndicatorStage.tsx` | 注释说明 indicatorView 固定为 structure_node |

### 3.3 测试

| 文件 | 变更 |
|---|---|
| `backend/tests/test_smc_monitor.py` | TestWatchlistMonitorNamespaces 5 测试更新：移除 bb 命名空间断言，改用 node_cluster 验证；state_version=3 |
| `backend/tests/test_indicator_view.py` | INDICATOR_VIEW_VALUES 从 3 值改为 4 值；FEISHU_CAPTURE_PRESETS 从 3 套改为 4 套（含 _legacy 标记） |
| `backend/tests/test_monitor_batch_text_content.py` | 概览断言从 "密集区 1" 改为 "结构 0｜筹码共识 1" |

## 4. 不变量

1. **仅两类监控事件**：watchlist_monitor 只触发结构（SMC BOS/CHoCH/EQH/EQL/OB first touch）和筹码共识（node_cluster_touch）。布林带不再触发盘中事件。
2. **固定组合图**：任一事件触发时，飞书截图固定使用 FEISHU_CAPTURE_VIEW='structure_node'（node + smc + volume，boll=false）。
3. **事件文字与图片解耦**：文字只描述实际触发事件类型；图片同时展示两类指标。
4. **combined Ready**：nodeReady && smcContractReady，SMC 数组允许为空（无事件时 SMC 结构仍需存在）。
5. **历史兼容**：旧 CaptureJob.indicator_view 字段和旧 node_cluster/bollinger/smc 值仅作读取兼容，新业务只写 structure_node。
6. **BB 算法保留**：Bollinger 算法、普通个股详情页布林带图层、盘后模块对 BB 的合法使用不受影响。

## 5. 测试

### 5.1 纯单元测试（本地通过）

- `test_smc_monitor.py`：56 tests 通过
- `test_smc_monitor_five_event_types.py`：5 tests 通过
- `test_indicator_view.py`：70 tests 通过
- `test_monitor_batch_text_content.py`：8 tests 通过
- `test_stock_capture_service.py`：4 tests 通过
- `test_volume_node_monitor.py`：19 tests 通过
- 合计 168 tests 通过

### 5.2 静态检查

- TSC：通过（0 errors）
- ESLint（修改文件）：通过（0 errors，3 pre-existing warnings）
- Ruff（修改 Python 文件）：通过

### 5.3 浏览器冒烟

- 登录受限（Owner 账户受 AGENTS.md §8 保护），需用户手工验收。
- 验收要点：
  - /stock/:symbol 飞书弹窗无指标选择器，文案为"将发送：结构 + 筹码共识组合图"
  - /capture/stock/:symbol 页面 data-render-ready 在 node + smc 数据就绪后转为 true

## 6. 兼容性与迁移

- 不新增 migration、依赖、Compose、部署脚本
- 不删除历史 CaptureJob.indicator_view 字段和旧数据
- 旧 node_cluster/bollinger/smc preset 标记 _legacy，仅供历史回读
- 旧 URL indicator_view 参数由前端 CaptureStockPage 屏蔽，后端兼容接收但忽略

## 7. 未解决问题

1. 浏览器真实链路验收待用户手工完成（登录受限）
2. 真实盘中运行未核验（本地不启动 Scheduler/Worker）
3. 生产环境 GoAccess/盘后任务未在本轮范围
