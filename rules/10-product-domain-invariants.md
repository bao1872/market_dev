# 10 产品域不变量

> 来源：AGENTS.md §七.1-4、§七.6

## 产品边界

盘迹是 A 股研究、全市场特征计算、自选股盘中监控和消息投递平台。

不做：

- 自动交易；
- 券商账户连接；
- 资金管理；
- 收益承诺；
- 单一指标买卖信号；
- 普通用户修改生产算法参数。

## 策略规则

当前生产只保留 `dsa_selector` 与 `watchlist_monitor`。

多策略组合已废弃，不得从旧代码或旧文档恢复。

## DSA 规则

- DSA 对全市场 computable universe 计算特征；
- 不得在计算阶段按方向、强弱、matched、用户筛选提前删除股票；
- 发布必须满足严格完整性门禁；
- `partial_failed` 不得发布。

## 自选和监控

- 有效会员添加自选后自动进入盘中监控；
- 不创建 MonitoringPlan；
- 到期用户保留历史数据，但不能读取、修改、监控或产生新投递。

## 飞书接入

唯一接入方式：`feishu_platform_app`。

禁止恢复：

- `feishu_webhook` / `FEISHU_WEBHOOK`；
- 独立管理员飞书 App；
- 独立管理员接收人配置。

管理员内测申请通知必须复用管理员用户自己的 active `feishu_platform_app` NotificationChannel。

### 盘中监控触发口径

- 盘中监控触发只依赖**最新已完成 1m bar**；
- `source_bar_time` 来自最新已完成 1m bar，剔除最后一根可能未完成的 bar；
- 飞书盘中截图业务默认 `timeframe=1d`，实时性由 Capture Snapshot `1d + include_realtime=True` 的 partial daily 合成保证；
- 修截图/清晰度/缓存不得改变 `watchlist_monitor` 事件计算口径；
- `monitor_batch_service` 计算输入 `bars_daily` / `bars_15min` 必须 `include_realtime=False`。

### 两类监控 + 固定组合图稳定不变量（CHANGE-20260728-010）

- watchlist_monitor 只保留两类触发事件：**结构**（SMC BOS/CHoCH/EQH/EQL/OB first touch）和**筹码共识**（node_cluster_touch）；布林带不再触发盘中监控事件。
- 任一结构或筹码共识事件触发时，飞书截图固定使用"结构 + 筹码共识"组合视图（`FEISHU_CAPTURE_VIEW='structure_node'`），固定图层：`node + smc + volume`，`boll=false`；不再按事件类别切换单指标视图。
- 事件文字只描述实际触发事件类型；图片同时展示两类指标，二者语义解耦。
- combined Ready = `nodeReady && smcContractReady`（SMC 数组允许为空，无事件时 SMC 结构仍需存在，避免前端永久 loading）。
- 截图调用方 timeout 必须 > Capture 渲染最大 90s，固定 `CAPTURE_HTTP_TIMEOUT_SECONDS=120`。
- 个股详情"发送到飞书"弹窗无指标选择器，固定发送同一张组合图；请求体不携带 `indicator_view`，后端兼容接收旧字段但忽略。
- 历史 `CaptureJob.indicator_view` 字段和旧 `node_cluster`/`bollinger`/`smc` 值仅作读取兼容，新业务只写 `structure_node`。
- Bollinger 算法本体、盘后 Bollinger 计算、个股详情页布林带图层工具栏不受本不变量禁止范围。
