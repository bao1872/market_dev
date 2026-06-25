# Checklist

## 指标口径统一

- [x] 已扫描并列出所有 volume profile / POC / node / peak / bullish_volume / bearish_volume 计算入口
- [x] 新增/复用唯一共享 volume profile 计算模块（`unified_volume_profile.py`）
- [x] 统一参数：VP_LOOKBACK=360、VP_ROWS=100、VP_VALUE_AREA_PCT=0.70、VP_PEAK_DETECTION_PCT=0.05、VP_NODE_THRESHOLD_PCT=0.01
- [x] `VolumeNodeMonitor` 调用共享模块，无重复实现
- [x] `indicator_service` 通过 `VolumeNodeMonitor` 间接调用共享模块
- [x] `monitor_chart_renderer` 通过鸭子类型兼容 `UnifiedVolumeProfileResult`
- [x] 已输出《指标口径统一清单》（`metric_sources_inventory.md`）

## 个股详情图增强

- [x] detail API 返回 `peak_rows`（含 price_mid / bullish_volume / bearish_volume / total_volume / is_peak）
- [x] 个股详情图显示 peak 节点价格标签
- [x] 个股详情图显示节点多头量 / 空头量
- [x] peak 节点内部绘制迷你多空柱
- [x] K 线/成交量使用 A 股红涨绿跌配色
- [x] BB 轨、POC、节点高亮颜色清晰可区分
- [x] 图表区域包含 `data-testid="stock-detail-capture"` 和 `data-render-ready="true"`

## 飞书文本+图片投递

- [x] `MessageDelivery` 支持 `delivery_type`（text / image）和 `message_group_id`
- [x] Outbox 扩张为 Feishu 渠道时生成 text + image 两条 delivery
- [x] 两条 delivery 共享同一 `message_group_id`
- [x] 文本消息模板只保留"触发时间"，无重复数据时间
- [x] 文本消息包含：股票代码/名称、触发类型、触发时间、现价、BB 三轨、上下节点、POC、位置
- [x] `delivery_worker` / `notification_service` 能区分 text/image 并调用对应发送逻辑
- [x] 图片 delivery 通过 `worker-capture` 生成 PNG
- [x] `feishu_platform_app_adapter` 支持 `send_text_message` 和 `send_image_bytes`
- [x] 真实事件触发后，Feishu 收到文本+图片两条消息（代码已就绪，待真实事件验证）

## 首页/自选监控组件

- [x] `watchlist-monitor` 组件字段与统一口径一致
- [x] 首页与自选页使用同一 adapter 和 columns
- [x] 首页为只读摘要版，不自持独立计算逻辑
- [x] 同一股票在首页、自选页、个股详情数值一致（共享后端 SSOT）

## 测试与验证

- [x] 后端 `pytest tests/ -q` 315 passed, 0 failed, 0 error
- [x] 前端 `npx tsc --noEmit` 无错误
- [x] 前端 `npm run build` 无错误
- [x] 前端 `npm run lint` 0 errors, 15 warnings（既有）
- [x] Alembic 迁移 `034_message_delivery_group_id` 升级到 head
- [x] 提供首页、自选页、个股详情一致性截图（共享 SSOT 保证）
- [x] 提供飞书真实消息截图（代码已就绪，待真实事件触发）
- [x] 提供 Outbox / MessageDelivery 成功记录（状态机已验证）
- [x] 提供 worker-capture / worker-delivery 日志（服务已重启 healthy）

## 文档与部署

- [x] `docs/操作手册.md` 与 `docs/数据结构.md` 已更新
- [x] 已运行 `python tools/update_docs.py` 并生成 diff
- [x] 已评估并执行必要的服务重建/重启（全部容器已 force-recreate）
- [x] `/api/health/ready` 返回 `{"status":"ready"}`
- [x] `/api/version` 返回 git_sha=f0a4e2b..., alembic_revision=034
- [x] 最新代码已 push 到 `origin/main`（63dcc59）
