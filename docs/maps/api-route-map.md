# API Route Map

> 事实源：`backend/app/main.py` include_router 列表。本文只做入口地图。

## 1. Router 列表

| Router | 文件 | 能力 |
|---|---|---|
| health | `app/api/health.py` | 健康/ready/策略资产检查 |
| auth | `app/api/auth.py` | 登录、注册、刷新、当前用户 |
| me | `app/api/me.py` | 当前用户权益与访问上下文 |
| instruments | `app/api/instruments.py` | 股票主数据 |
| calendar | `app/api/calendar.py` | 交易日历 |
| market | `app/api/market.py` | 市场状态 |
| bars | `app/api/bars.py` | 行情查询；`page_size` 最大 4000，与 Node Cluster 15m=4000/1h=1200 契约对齐 |
| capture | `app/api/capture.py` | Capture Snapshot 专用 API |
| indicators | `app/api/indicators.py` | 策略指标实时计算；`bars` 最大 4000，与 bars API 及 Node Cluster 契约对齐；响应 `data.sqzmom_lb` 为全局技术指标，由 `app.strategy_assets.algorithms.features.sqzmom_lb.compute_sqzmom_lb` 计算，前端只渲染不计算 |
| structural_factors | `app/api/structural_factors.py` | 双周期结构状态因子 V1.8（约 50 字段，含 dsa_segment 段收益/斜率/效率/段级成交量、swing_range/price_position、price_vs_poc_atr/value_area_position、distance_to_bb_*_atr/sqz_on/sqz_off、客观 relation primary_dir/secondary_dir/trend_alignment 等）；`GET /api/v1/instruments/{id}/structural-factors`，由 `app.services.structural_factor_service.compute_structural_factors` 计算，前端只渲染不计算；无认证要求；250-500 bar lookback；契约详见 `docs/current/02-data-api-contracts.md` 第 10 节 |
| strategies | `app/api/strategies.py` | 策略目录/版本 |
| strategy_runs | `app/api/strategy_runs.py` | 策略运行/结果；`/strategy-runs/{run_id}/results` 以 `strategy_run_items` 为主表 LEFT JOIN `strategy_results` + `instruments`，返回全量 universe（含 succeeded/skipped/failed），新增 `item_status`/`reason_code`/`error_message` 字段。JOIN 策略：因 `strategy_run_items.result_id` 当前未回填（ALIGN-033 P2），统一改用 `(run_id, instrument_id)` 关联 `strategy_results`，包括 `selectinload` 替代批量加载、metric_filter 子查询、sort LEFT JOIN 三处 |
| monitor_states | `app/api/monitor_states.py` | 监控状态 |
| strategy_events | `app/api/strategy_events.py` | 策略事件 |
| notifications | `app/api/notifications.py` | 消息与通知渠道 |
| admin_subscription | `app/api/admin_subscription.py` | 订阅/邀请码/调度任务/Worker 心跳/消息投递管理 |
| admin_beta_applications | `app/api/admin_beta_applications.py` | 内测申请管理 |
| admin_after_close | `app/api/admin_after_close.py` | 盘后编排管理；`/after-close-runs/dsa-only` 支持 fallback 到最新可用交易日，覆盖率门禁使用 `coverage_raw` 原始值 |
| watchlist | `app/api/watchlist.py` | 用户自选股；`/watchlist/monitor-status` 无 MonitorState 或 payload 无效时通过 `MonitorSnapshotService` fallback 返回指标，单只失败单行降级 |
| stock_memos | `app/api/stock_memos.py` | 个股备忘录 |
| stock_detail_feishu | `app/api/stock_detail_feishu.py` | 个股详情发送飞书 |
| public_beta | `app/api/public_beta.py` | 公开内测申请 |
| plans | `app/api/plans.py` | 套餐列表 |
| metrics | `app/api/metrics.py` | Prometheus 指标 |

## 2. 权限核对要点

- 核心业务 API 必须 active subscription；
- Admin API 必须 admin；
- Capture API 必须 Capture Token；
- 消息/渠道必须按 JWT user_id 所有权隔离；
- 到期用户只允许历史消息只读和续期相关能力。

## 3. 修改 API 前检查

```text
1. 是否需要更新 current/02-data-api-contracts.md；
2. 是否需要更新 frontend adapter；
3. 是否需要 API 测试覆盖 active/expired/admin；
4. 是否需要更新 maps/api-route-map.md；
5. 是否需要 CHANGE 和 alignment。
```
