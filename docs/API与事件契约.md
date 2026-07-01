> Last verified code baseline: 0cbfc84b993fd5fb4d767008c7896c8b8e1911be
> 负责人: 开发团队
> 事实来源: 代码库 + 配置文件
> 维护方式: 人工维护

# API 与事件契约

## 1. REST API 总览

事实源：`backend/app/main.py` 路由注册

所有路由无 `/api` 前缀（由 nginx 剥离）。完整 OpenAPI 文档：`http://<host>/api/docs`。

### 1.1 健康检查（无需认证）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 存活检查（200=ok） |
| GET | `/health/ready` | 就绪检查（策略资产 + 种子数据） |
| GET | `/version` | 构建版本（git_sha / build_time / alembic_revision） |
| GET | `/metrics` | Prometheus 指标端点 |

### 1.2 认证（公开）

事实源：`backend/app/api/auth.py`

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/auth/login` | 登录（email + password）→ access + refresh token + AccessProfile 权限上下文（见 9.1） |
| POST | `/auth/register` | 邀请码注册 → 创建账户 + 30 天会员 + token |
| POST | `/auth/renew` | 邀请码续期（需登录态） |
| POST | `/auth/refresh` | refresh token 刷新 |
| GET | `/me` | 当前用户信息（含角色列表） |
| GET | `/me/access` | 当前用户完整权限上下文（AccessProfileResponse，见 9.2） |
| GET | `/me/membership` | 当前用户会员状态 |
| GET | `/me/events/summary?date=YYYY-MM-DD` | 当前用户指定日期事件汇总 |

### 1.3 市场与行情

| 方法 | 路径 | 认证 | 说明 |
|------|------|------|------|
| GET | `/market/status` | 否 | 当前市场状态（交易日/时段/状态文本） |
| GET | `/instruments` | 否 | 股票主数据列表 |
| GET | `/instruments/{id}` | 否 | 股票详情 |
| GET | `/instruments/{id}/bars` | 否 | K 线数据（支持 period / start / end） |
| GET | `/instruments/{id}/indicators` | 否 | 策略指标实时计算 |
| GET | `/calendar` | 否 | 交易日历 |

### 1.4 策略（用户端）

| 方法 | 路径 | 认证 | 说明 |
|------|------|------|------|
| GET | `/strategies` | 否 | 策略列表（支持 kind 过滤） |
| GET | `/strategies/{key}` | 否 | 策略详情 |
| GET | `/strategies/{key}/versions` | 否 | 版本列表 |
| GET | `/strategies/{key}/versions/{version}/schema` | 否 | 版本 schema |
| GET | `/strategies/{key}/published-runs` | 是 | 已发布批次列表 |
| GET | `/strategies/{key}/results` | 是 | 查询策略结果（metric_filters / sort_by） |
| GET | `/strategies/{key}/runs` | 是（admin） | 运行历史 |
| GET | `/strategy-runs/{run_id}/results` | 是 | 运行结果（分页+筛选+排序） |
| GET | `/strategy-runs/{run_id}/results/{result_id}` | 是 | 单个结果详情 |

### 1.5 监控与事件

| 方法 | 路径 | 认证 | 说明 |
|------|------|------|------|
| GET | `/instruments/{id}/monitor-states` | 否 | 某股票的所有监控策略状态 |
| GET | `/strategies/{key}/monitor-states` | 否 | 某策略的所有股票状态（支持 version 过滤） |
| GET | `/instruments/{id}/events` | 否 | 某股票的事件 |
| GET | `/strategies/{key}/events` | 否 | 某策略的事件（支持 version 过滤） |
| GET | `/strategy-events/{event_id}` | 否 | 事件详情（含 snapshot） |

### 1.6 自选股

事实源：`backend/app/api/watchlist.py`

| 方法 | 路径 | 认证 | 说明 |
|------|------|------|------|
| GET | `/watchlist` | 是 | 当前用户自选列表 |
| POST | `/watchlist` | 是 | 加入自选（instrument_id） |
| DELETE | `/watchlist/{instrument_id}` | 是 | 移除自选（软删除） |
| GET | `/watchlist/monitor-status` | 是 | 自选股 + 监控状态聚合查询 |

### 1.7 通知

事实源：`backend/app/api/notifications.py`

| 方法 | 路径 | 认证 | 说明 |
|------|------|------|------|
| GET | `/messages` | 是 | 用户消息列表（支持 unread_only） |
| POST | `/messages/{id}/read` | 是 | 标记消息已读 |
| POST | `/notification-channels` | 是 | 创建通知渠道 |
| GET | `/notification-channels` | 是 | 用户渠道列表（脱敏） |
| PUT | `/notification-channels/{id}` | 是 | 更新渠道配置 |
| DELETE | `/notification-channels/{id}` | 是 | 删除渠道（软删除） |
| POST | `/notification-channels/{id}/verify` | 是 | 验证渠道 |
| POST | `/notification-channels/{id}/test` | 是 | 测试渠道投递 |
| POST | `/notification-previews` | 是 | 消息预览（站内 + 飞书 card JSON） |

> **认证变更（Phase 3）**：所有 notifications 端点改用 JWT Bearer token 认证（`Authorization: Bearer <token>`，由 `get_current_active_user` 注入用户身份），不再接受 `X-User-Id` 请求头。`verify_channel` 与 `test_channel` 新增渠道所有权校验：`channel.user_id` 必须等于当前登录用户，不匹配返回 403（`ChannelOwnershipError`）。详见 `docs/安全规范.md` 第 11.1 / 11.2 节。

## 1.8 管理员（需 admin 角色）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/admin/invite-codes` | 生成邀请码 |
| GET | `/admin/invite-codes` | 邀请码列表 |
| POST | `/admin/invite-codes/{id}/revoke` | 作废邀请码 |
| GET | `/admin/members` | 会员列表 |
| GET | `/admin/members/{user_id}/redemptions` | 用户兑换记录 |
| GET | `/admin/system-overview` | 系统概览 |
| POST | `/admin/strategies` | 创建策略 |
| POST | `/admin/strategies/{key}/versions/{version}/release` | 发布版本 |
| POST | `/admin/strategies/{key}/run` | 触发运行（创建 queued） |
| POST | `/admin/strategy-runs/{run_id}/publish` | 发布运行结果 |
| POST | `/admin/strategy-runs/{run_id}/retry` | 重试运行 |

## 2. 认证机制

事实源：`backend/app/core/deps.py`

### 2.1 Bearer Token

```
Authorization: Bearer <jwt_access_token>
```

- **token 类型**：`access`（API 认证）/ `refresh`（刷新）/ `capture`（截图短期令牌，仅通过 URL query parameter 用于截图端点，不可作为 Authorization Bearer 调用 API — 见 `docs/安全规范.md` 第 11.3 节）
- **算法**：HS256
- **access TTL**：3600 秒（1 小时）
- **refresh TTL**：604800 秒（7 天）

### 2.2 RBAC 依赖

```python
# 普通用户端点
@router.get("/watchlist", dependencies=[Depends(get_current_active_user)])

# 管理员端点
@router.post("/admin/invite-codes", dependencies=[Depends(require_roles("admin"))])
```

## 3. 消息 DTO 契约

事实源：`backend/app/schemas/notification.py`

### 3.1 NotificationMessageDTO

```python
class NotificationMessageDTO(BaseModel):
    title: str
    message_type: str          # MONITOR_EVENT / SYSTEM_ALERT 等
    template_key: str          # monitor_event / system_alert
    template_version: str      # 1.1.0
    summary: str               # ≤200 字符
    data_time: str             # ISO 时间
    resource_refs: dict        # 资源引用（如 event_id, instrument_id）
```

### 3.2 渠道配置（target_config）

**feishu_webhook**：
```json
{
  "webhook_url": "https://open.feishu.cn/open-apis/bot/v2/hook/xxxxx",
  "sign_secret": "签名密钥"
}
```

**feishu_platform_app**：
```json
{
  "app_id": "cli_xxxxx",
  "app_secret": "xxxxx",
  "receive_id": "bg33237",
  "receive_id_type": "user_id"
}
```

**脱敏规则**：`app_secret` / `sign_secret` 在 API 读取时仅显示末 4 位。

## 4. StrategyEvent 契约

详见 `docs/策略与指标口径.md` 第 6 节。

Schema 文件：`backend/app/strategy_assets/schemas/strategy_event.schema.json`

## 5. Outbox 事件契约

事实源：`backend/app/models/outbox.py` + `backend/app/services/outbox_relay.py`

### 5.1 Outbox 记录

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | UUID | 主键 |
| `aggregate_type` | Text | 聚合根类型（如 `strategy_run` / `selection_plan_run`） |
| `aggregate_id` | UUID (nullable) | 聚合根 ID |
| `event_type` | Text | 事件类型 |
| `payload` | JSONB | 事件负载 |
| `headers` | JSONB | 事件头（trace_id / tenant_id） |
| `status` | Text | `pending` / `processed` / `failed` / `deferred` |
| `retry_count` | Integer | 重试次数 |
| `next_attempt_at` | DateTime (nullable) | 下次可投递时间（deferred 状态使用） |
| `created_at` | DateTime | 创建时间 |
| `processed_at` | DateTime (nullable) | 处理完成时间 |

### 5.2 通知事件类型

```python
_NOTIFICATION_EVENT_TYPE = "notification.message.created"
```

Outbox Relay 收到此事件时：
1. 查询通知的目标渠道
2. 为每个渠道创建 `MessageDelivery(pending)`
3. 将 Outbox 记录标记为 `processed`

## 6. MessageDelivery 契约

事实源：`backend/app/models/notification.py`

### 6.1 字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | UUID | 主键 |
| `notification_message_id` | UUID FK | 关联消息 |
| `channel_id` | UUID FK | 关联渠道 |
| `status` | Text | `pending` / `sending` / `success` / `failed` / `retrying` / `dead` |
| `delivery_type` | Text | `text` / `image` / `card`（card 仅管理后台预览） |
| `attempt_count` | Integer | 已尝试次数 |
| `next_attempt_at` | DateTime (nullable) | 下次重试时间 |
| `last_error_code` | Text (nullable) | 最近错误码 |
| `provider_response` | JSONB (nullable) | 渠道返回 |
| `image_url` | Text (nullable) | 图片投递时截图 URL |
| `message_group_id` | Text (nullable) | 消息组 ID（关联同一事件的 text+image） |
| `idempotency_key` | Text (UNIQUE) | 投递幂等键 |
| `created_at` | DateTime | 创建时间 |

### 6.2 状态机

```
pending → sending → success（终态）
                  → failed → retrying → success / dead
                            → dead（终态，超过最大重试）
```

### 6.3 幂等键

```python
idempotency_key = SHA256(message_id + channel_id + delivery_type + image_url)
```

## 7. 错误响应格式

所有 API 错误遵循 FastAPI 默认格式：

```json
{
  "detail": "错误描述"
}
```

常见状态码：
- `400 Bad Request`：请求参数错误 / 邀请码无效
- `401 Unauthorized`：token 无效 / 过期 / 用户不存在
- `403 Forbidden`：用户状态非 active / 权限不足
- `404 Not Found`：资源不存在
- `409 Conflict`：重复创建（如自选股重复加入）
- `422 Unprocessable Entity`：请求体校验失败
- `503 Service Unavailable`：就绪检查失败（策略资产缺失 / 种子数据异常）

## 8. 分页约定

列表接口统一使用 `limit` + `offset` 参数：

```python
?limit=20&offset=0
```

响应：

```json
{
  "items": [...],
  "total": 100,
  "limit": 20,
  "offset": 0
}
```

## 9. 认证响应契约（Phase 2）

本节定义登录响应与权限上下文响应的字段契约。事实源：`backend/app/schemas/membership.py` + `backend/app/schemas/access.py`。权限上下文由 `app.services.access_control_service.get_access_context` 统一计算（唯一真源），登录与 `/me/access` 均只读不写 DB。

### 9.1 LoginResponse（破坏性变更）

`POST /auth/login` 响应。Phase 2 破坏性变更：移除旧字段 `membership_expired`（语义等价迁移至 `subscription_active = not membership_expired`），新增 10 个 AccessProfile 字段供前端路由分发与 UI 降级。

| 字段 | 类型 | 说明 |
|------|------|------|
| `access_token` | str | Access token |
| `refresh_token` | str | Refresh token |
| `token_type` | str | 固定 `bearer` |
| `expires_in` | int | Access token 有效期（秒，默认 3600） |
| `is_admin` | bool | 是否为管理员（`"admin" in roles`） |
| `roles` | list[str] | 角色名列表 |
| `subscription_required` | bool | 是否需要订阅（admin=false，member=true） |
| `subscription_active` | bool | 订阅是否有效（admin 豁免=true；member 实时计算） |
| `plan_code` | str \| null | 套餐代码（admin/无订阅=null） |
| `plan_display_name` | str \| null | 套餐展示名（过期订阅仍保留，便于前端降级提示） |
| `expires_at` | datetime \| null | 订阅过期时间（admin/无订阅=null） |
| `features` | list[str] | 功能特性列表 |
| `limits` | dict | 额度限制（monitor_limit / notification_channel_limit / message_retention_days） |
| `next_route` | str | 前端登录后跳转路由（admin→`/admin/overview`；active→`/overview`；expired→`/membership-expired`） |

### 9.2 AccessProfileResponse

`GET /me/access` 响应（认证：JWT Bearer token）。返回当前用户完整权限上下文，11 个字段与 `AccessContext` 完全对齐（仅作为 API 响应模型，解耦内部模型与外部契约）。供前端在 token 刷新后或路由守卫中按需拉取，避免依赖登录响应的快照。

| 字段 | 类型 | 说明 |
|------|------|------|
| `user_id` | str | 用户 ID（字符串化 UUID，与 JWT sub 声明一致） |
| `account_status` | str | 用户状态 active/disabled/pending |
| `roles` | list[str] | 角色名列表 |
| `is_admin` | bool | 是否为管理员 |
| `is_member` | bool | 是否为普通会员（`"user" in roles`，与 is_admin 对称） |
| `subscription_active` | bool | 订阅是否有效（admin 豁免=true；member 实时计算） |
| `plan_code` | str \| null | 套餐代码（admin/无订阅=null；过期订阅仍保留） |
| `plan_display_name` | str \| null | 套餐展示名（admin/无订阅=null；过期订阅仍保留） |
| `expires_at` | datetime \| null | 订阅过期时间（admin/无订阅=null） |
| `features` | list[str] | 功能特性列表（admin/无订阅=[]） |
| `limits` | dict | 额度限制 dict |

> **与 LoginResponse 的关系**：LoginResponse 的 10 个 AccessProfile 字段是 `AccessContext` 的子集（不含 `user_id` / `account_status` / `is_member`），并额外含 `next_route` / `subscription_required`；`AccessProfileResponse` 则完整暴露 `AccessContext` 的 11 个字段，不含 token 与 `next_route`。
