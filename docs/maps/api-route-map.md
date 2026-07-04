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
| bars | `app/api/bars.py` | 行情查询 |
| capture | `app/api/capture.py` | Capture Snapshot 专用 API |
| indicators | `app/api/indicators.py` | 策略指标实时计算 |
| strategies | `app/api/strategies.py` | 策略目录/版本 |
| strategy_runs | `app/api/strategy_runs.py` | 策略运行/结果 |
| monitor_states | `app/api/monitor_states.py` | 监控状态 |
| strategy_events | `app/api/strategy_events.py` | 策略事件 |
| notifications | `app/api/notifications.py` | 消息与通知渠道 |
| admin_subscription | `app/api/admin_subscription.py` | 订阅/邀请码管理 |
| admin_beta_applications | `app/api/admin_beta_applications.py` | 内测申请管理 |
| admin_after_close | `app/api/admin_after_close.py` | 盘后编排管理；`/after-close-runs/dsa-only` 支持 fallback 到最新可用交易日校验覆盖率 |
| watchlist | `app/api/watchlist.py` | 用户自选股；`/watchlist/monitor-status` 无 MonitorState 时通过 `MonitorSnapshotService` fallback 返回指标 |
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
