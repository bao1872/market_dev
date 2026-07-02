> 文档状态：CURRENT DESIGN BASELINE  
> 基线日期：2026-07-02  
> 已核对代码基线：`6f5ae2cec6b24dbd1b7bf6f23477f5e6f5096822`（`refactor/access-v2-platform-recovery`）  
> 事实来源：代码库 + 项目负责人截至 2026-07-02 已确认的产品与架构要求  
> 维护要求：任何代码、配置、测试、部署或文档修改都必须同步更新相关当前设计文档，并新增 CHANGE 记录。  
> 注意：该代码基线用于设计核对，不代表已经满足合并 `main` 或生产发布条件。
> 对齐口径：`CURRENT` 表示已确认设计，不等同于代码已完成；代码未实现、未验证或生产表现不一致的内容，必须在 `18-code-doc-alignment.md` 标为 `KNOWN_GAP`。

# 08 数据模型设计

## 1. 账户与权限

| 实体 | 语义 |
|---|---|
| `users` | 用户身份和 active/disabled 状态 |
| `roles` / `user_roles` | 仅 `admin`、`member` 基础角色 |
| `plans` | 套餐、features、monitor_limit 等机器事实源 |
| `subscriptions` | 普通会员当前套餐、有效期、状态和 entitlement_snapshot |
| `invite_codes` / `invite_redemptions` | 邀请码和兑换审计 |
| `access_audit_logs` | 管理员敏感操作 before/after、request_id、ip_hash 和时间 |

管理员不创建 Subscription。旧 `memberships` 仅属于历史迁移，不是运行时实体。

## 2. 股票与行情

| 实体 | 语义 |
|---|---|
| `instruments` | 股票主数据、市场和 active 状态 |
| `trading_calendar` | 交易日和开闭市状态 |
| `bars_daily`、`bars_15min`、`bars_60min`、`bars_minute` | 已完成正式 Bar |
| 周线/月线 | 由日线按统一规则聚合，不重复定义业务源 |

partial 实时 Bar 仅存在于请求快照或短缓存，不写入完成 Bar 表。

## 3. 策略和发布

- `strategy_definitions`
- `strategy_versions`
- `strategy_runs`
- `strategy_run_items`
- `strategy_results`
- `strategy_result_metrics`

关键约束：同一运行和股票的结果唯一；published 批次不可变；每个 skipped/failed item 保存原因；运行计数可与结果表对账。

## 4. 自选、监控和事件

- `user_watchlist_items`：用户自选和 active/软删除状态；
- `monitor_states`：当前监控状态快照；
- `monitor_evaluations`：策略版本、股票和源 Bar 的唯一评估；
- `strategy_events`：稳定业务事件；
- `event_recipients`：事件与有效用户收件人关系。

订阅到期不删除这些历史数据，但禁止生成新评估收件人和投递。

## 5. 消息、截图和投递

- `notification_channels`
- `notification_messages`
- `outbox`
- `message_deliveries`
- `capture_jobs`

文字和图片通过 message_group 关联，分别保存状态、error_code、error_message、attempt 和时间。仅重试图片不能创建重复文字 Delivery。

## 6. 任务与运行

- `scheduler_job_runs`
- `job_run_events`
- `worker_heartbeats`

任务保存 business_date、run_key、状态、心跳、租约、实例、Git SHA、计数和错误。

## 7. 生命周期和迁移

- 软删除数据不等于业务可用；恢复时重新校验权限和额度；
- 时间统一使用带时区 UTC 存储或明确业务日期，A 股展示使用 Asia/Shanghai；
- 数值口径、单位和复权方式必须明确；
- 已应用历史 migration 不修改，只新增前向 migration；
- 新约束前先清理冲突数据并验证 downgrade/upgrade；
- Alembic 是唯一 DDL 事实源，测试不手写生产 Schema。
