> 文档状态：CURRENT DESIGN BASELINE  
> 基线日期：2026-07-02  
> 已核对代码基线：`6f5ae2cec6b24dbd1b7bf6f23477f5e6f5096822`（`refactor/access-v2-platform-recovery`）  
> 事实来源：代码库 + 项目负责人截至 2026-07-02 已确认的产品与架构要求  
> 维护要求：任何代码、配置、测试、部署或文档修改都必须同步更新相关当前设计文档，并新增 CHANGE 记录。  
> 注意：该代码基线用于设计核对，不代表已经满足合并 `main` 或生产发布条件。
> 对齐口径：`CURRENT` 表示已确认设计，不等同于代码已完成；代码未实现、未验证或生产表现不一致的内容，必须在 `18-code-doc-alignment.md` 标为 `KNOWN_GAP`。

# 09 API 契约

## 1. 总体约定

- API 时间字段必须说明 UTC、业务日期或 Asia/Shanghai；
- 分页、排序和筛选只允许白名单；
- 401 表示未认证，403 表示已认证但无资格或无权限；
- 所有私有资源从 JWT 获取 user_id；
- 前端隐藏不能替代后端权限；
- 响应包含 request/diagnostic id 以便追踪。

## 2. 访问资格

| API 类型 | 普通有效会员 | 到期/无订阅 | Admin |
|---|---:|---:|---:|
| `/me/access`、`/plans`、续期 | 是 | 是 | 是 |
| 历史消息只读 | 是 | 是 | 是 |
| 趋势结果 | 是 | 否，403 | 是 |
| Watchlist 全部读写和状态 | 是 | 否，403 | 是 |
| 个股详情和行情研究接口 | 是 | 否，403 | 是 |
| 管理 API | 否 | 否 | 是 |

## 3. 趋势选股

设计契约：以下端点同时要求有效订阅和 `trend_selection` feature：

- `GET /strategies/{key}/published-runs`
- `GET /strategies/{key}/results`
- `GET /strategy-runs/{run_id}/results`
- `GET /strategy-runs/{run_id}/results/{result_id}`

代码基线核对：前两个端点当前只使用 `require_authenticated + require_feature`，后两个端点已使用 `require_active_subscription + require_feature`。该差异登记为 `ALIGN-007`，修复前不得宣称到期会员已被后端完全阻断。

结果响应至少包含：`source_total`、`filtered_total`、分页、运行计数和完整性信息。默认无隐式过滤。用户不能读取未发布或不完整批次。

## 4. Watchlist

设计契约：`GET/POST/DELETE /watchlist` 和监控状态端点全部要求有效订阅。

代码基线核对：当前路由仍主要使用 `get_current_active_user`，且 POST 调用了未收口的旧额度函数名。该差异登记为 `ALIGN-006`。

新增与恢复在事务内检查 Plan 的 `monitor_limit`；超限返回 409；到期或无订阅返回 403。

## 5. 行情和个股详情

Bars/Quote 响应除 OHLCV 外，应提供或通过响应头提供：

- `data_source`
- `as_of`
- `is_partial`
- `last_persisted_bar_time`
- `last_live_bar_time`
- `freshness_seconds`
- `degraded` / `degraded_reason`

历史完成 Bar、实时聚合 Bar和指标必须来自同一快照。外部源失败时返回明确降级，不得伪装实时。

## 6. 飞书分享状态

发送和状态查询至少返回：

```json
{
  "test_run_id": "...",
  "message_group_id": "...",
  "card_status": "pending|succeeded|failed",
  "image_status": "pending|succeeded|failed|not_created",
  "overall_status": "pending|succeeded|partial_failed|failed",
  "failed_step": null,
  "error_code": null,
  "error_message": null,
  "image_message_id": null
}
```

提供仅重试图片端点或等价操作；重试不得重复文字。用户只能查询和重试自己的消息组。

## 7. 管理 API

用户启用/禁用、订阅授予/续期/撤销/改套餐、任务触发、投递重试都必须要求 Admin，并写审计日志。不存在真实 API 的前端控件不得保留。

## 8. 兼容与废弃

`/subscription-expired` 为 canonical 前端路由；`/membership-expired` 仅兼容重定向。旧 Membership API 和模型不得作为第二套长期路径。
