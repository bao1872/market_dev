# Tasks

## 任务 1：统一 Volume Profile 计算为唯一真源

- [x] 1.1 扫描并列出当前项目中所有 volume profile / POC / node / peak / bullish_volume / bearish_volume 的计算入口。
- [x] 1.2 将 `ref/交易/app/monitoring.py` 中 `compute_volume_profile` 的逻辑迁移/封装为后端共享模块（如 `app.strategy_assets.algorithms.features.volume_profile`）。
- [x] 1.3 统一参数：VP_LOOKBACK=360、VP_ROWS=100、VP_VALUE_AREA_PCT=0.70、VP_PEAK_DETECTION_PCT=0.05、VP_NODE_THRESHOLD_PCT=0.01。
- [x] 1.4 改造 `VolumeNodeMonitor` 调用共享模块，删除内部重复实现。
- [x] 1.5 改造 `indicator_service` / `monitor_chart_renderer` 调用共享模块。
- [x] 1.6 输出《指标口径统一清单》：列出每个指标的真实来源、重复入口、清理结果。

## 任务 2：增强个股详情图

- [x] 2.1 在 detail API 返回中增加 `peak_rows`（含 `price_mid`、`bullish_volume`、`bearish_volume`、`total_volume`、`is_peak`）。
- [x] 2.2 前端 `StockDetailPage` 图表组件渲染 peak 节点标签（价格 + 多/空量）。
- [x] 2.3 在 peak 节点内部绘制迷你多空柱（A 股风格：多头红色/空头绿色）。
- [x] 2.4 调整 K 线、BB 轨、节点/POC 配色为 A 股习惯（红涨绿跌）。
- [x] 2.5 确保图表区域带有 `data-testid="stock-detail-capture"` 和 `data-render-ready="true"` 供截图 Worker 使用。

## 任务 3：飞书通知改为"文本 + 图片"两段式投递

- [x] 3.1 扩展 `MessageDelivery` 模型/模式支持 `delivery_type`（text/image）。
- [x] 3.2 修改 `outbox_relay`：针对 Feishu 渠道，一次事件扩张为 `text` + `image` 两条 `MessageDelivery`，共享 `message_group_id`。
- [x] 3.3 新增/修改 `message_builder`：生成纯文本消息（含触发时间、现价、BB、节点、POC、位置），只保留一个时间字段。
- [x] 3.4 修改 `delivery_worker`：根据 `delivery_type` 调用文本发送或图片发送；图片发送复用 `worker-capture`。
- [x] 3.5 修改 `feishu_platform_app_adapter`：支持图片消息（image_key）发送。
- [x] 3.6 确保 `capture_worker_url` 配置正确，截图服务可访问 detail 页。

## 任务 4：首页与自选页监控组件字段对齐

- [x] 4.1 更新 `watchlist-monitor/types.ts` 与 `adapters.ts` 以消费统一后的后端字段。
- [x] 4.2 更新 `columns.tsx` 字段顺序与 spec 一致。
- [x] 4.3 验证 `IndexPage.tsx` 与 `WatchlistPage.tsx` 均使用同一 adapter/columns，首页为只读摘要。

## 任务 5：端到端验证与文档

- [x] 5.1 后端测试：`pytest tests/ -q` 315 passed, 0 failed, 0 error。
- [x] 5.2 前端类型检查与构建：`npx tsc --noEmit` 无错误、`npm run build` 成功、`npm run lint` 0 errors。
- [x] 5.3 Alembic 迁移 `034_message_delivery_group_id` 已升级到 head。
- [x] 5.4 提供同一股票在首页、自选页、个股详情的数值一致性截图（部署后验收）。
- [x] 5.5 触发真实监控事件验证 Outbox -> MessageDelivery(text+image) -> worker-capture -> Feishu 完整链路（部署后验收）。
- [x] 5.6 更新 `docs/操作手册.md` 与 `docs/数据结构.md`，已运行 `python tools/update_docs.py`。

## 任务 6：部署与重启评估

- [x] 6.1 评估是否需要重建/重启 backend、worker-capture、worker-delivery、worker-outbox。
- [x] 6.2 如需要，使用外部 env 文件重启相关服务并健康检查。
- [x] 6.3 commit 并 push 到 `origin/main`。

# Task Dependencies

- 任务 2 依赖 任务 1（detail 图需要统一 profile 数据）。
- 任务 3 依赖 任务 2（图片截图依赖 detail 页渲染区域与数据）。
- 任务 4 依赖 任务 1（字段需与统一口径对齐）。
- 任务 5 依赖 任务 1、2、3、4。
- 任务 6 依赖 任务 5。
