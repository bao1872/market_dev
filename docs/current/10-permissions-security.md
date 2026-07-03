> 文档状态：CURRENT DESIGN BASELINE  
> 设计基线日期：2026-07-03  
> 设计确认截止日期：2026-07-03  
> 实现核对基线：ddca659b8c9d64b6a414da0b4bbd6f80f704aef1  
> 实现核对分支：main  
> 最近一致性检查日期：2026-07-03  
> 事实来源：代码库 + 项目负责人截至 2026-07-02 已确认的产品与架构要求  
> 维护要求：任何代码、配置、测试、部署或文档修改都必须同步更新相关当前设计文档，并新增 CHANGE 记录。  
> 注意：该代码基线用于设计核对，不代表已经满足合并 `main` 或生产发布条件。
> 对齐口径：`CURRENT` 表示已确认设计，不等同于代码已完成；代码未实现、未验证或生产表现不一致的内容，必须在 `18-code-doc-alignment.md` 标为 `KNOWN_GAP`。

# 10 权限与安全设计

## 1. 角色

系统基础角色只有：

- `admin`：管理权限，无 Plan、无 Subscription；
- `member`：普通用户，核心业务能力由有效 Subscription 和 Plan features 决定。

不得恢复 `user` 或 `strategy_author` 角色。

## 2. 认证和 Token

- Access Token 只访问普通业务 API；
- Refresh Token 只用于刷新；
- Capture Token 只访问指定截图场景，不能访问普通 API；
- Capture Token 使用独立存储 key 和请求客户端，短期有效，权限最小化；
- Capture Token 不覆盖、不污染普通 Access Token；
- disabled 用户拒绝登录和访问。

## 3. Subscription 资格

有效资格由 `access_control_service.py` 统一计算。资格链：

```text
JWT → AccessContext → User → Role → Subscription → Feature → Quota → Ownership
```

- JWT 只携带 user_id 与 token 类型；
- `AccessContext` 由 `access_control_service.py` 统一计算，聚合 User.status、roles、Subscription 有效期、Plan features 和 quotas；
- Feature 检查（如 `trend_selection`）在 active subscription 之后进行；
- 资源所有权（自选股、消息、渠道等）由 JWT user_id 隔离；
- 管理员角色绕过 subscription 检查，但仍受 admin RBAC 约束。

到期或无订阅用户仍可登录和续期，但核心业务 API 必须 403；不能只在前端重定向。

## 4. 私有资源所有权

自选股、消息、渠道、备忘录、分享状态和用户配置全部按 JWT user_id 隔离。管理员跨用户操作只能使用明确 Admin API，并写审计日志，不能复用普通 API 绕过所有权。

## 5. Worker 资格

Monitor、Recipient、Outbox 和 Delivery 统一要求 active member + active subscription + 有效时间。投递前再次检查。到期后不删除历史数据，但不生成新业务输出。

## 6. Secret

数据库密码、JWT、飞书 Platform App 凭据（app_id/app_secret）、签名 Secret 和第三方凭据只进入受限环境文件或 Secret 管理系统：

- 不提交 Git；
- 不写进文档示例；
- 不完整打印日志；
- API 只返回掩码和是否已配置；
- 发现已提交凭据后先轮换，再评估历史清理。

## 7. 管理审计

`access_audit_logs` 记录 actor、action、target、before/after、request_id、ip_hash 和 created_at。普通用户不能写入、修改或删除审计日志。

## 8. 安全验收

每次权限修改至少验证 active、expired、no-subscription、disabled、admin、用户 A/B 所有权、Capture Token 隔离、Secret 脱敏和 Worker 资格。

## 9. Capture Token 隔离（advice.md 第十节硬规则）

Capture Token 是截图 worker 专用短期 JWT，与普通用户 Access Token 严格隔离。

### 9.1 隔离规则

- Capture Token 只能访问 `/api/v1/capture/*` 端点；
- 普通访问 token（type=access）不能访问 Capture API（`get_capture_token_payload` 拒绝 `type != "capture"`）；
- Capture Token 不能访问普通 API（`get_current_user` 拒绝 `type != "access"`）；
- Capture Token 不覆盖、不污染普通 Access Token（前端使用独立 storage key 与 captureClient）；
- Capture Token 短期有效，权限最小化（默认 TTL 由 `settings.jwt_capture_ttl_seconds` 控制）。

### 9.2 Capture Token 校验项

`get_capture_token_payload`（位于 `backend/app/core/deps.py`）依次校验，任一失败返回 401：

- token 可解码（签名 + exp 有效）；
- `payload.type == "capture"`；
- `payload.scope == "stock_detail_capture"`；
- 必需声明：`user_id`、`instrument_id`、`event_id`。

支持两种传入方式（任一即可）：

1. `Authorization: Bearer <token>`；
2. query 参数 `token=<token>`（前端 `/capture/stock/:symbol?...&token=...` 场景）。

### 9.3 path 与 token 一致性校验

`/api/v1/capture/stocks/{instrument_id}/snapshot` 端点在依赖校验通过后，额外校验：

- path 参数 `instrument_id` 必须与 token 中的 `instrument_id` 一致，否则返回 403（防越权）。

### 9.4 实现位置

| 职责 | 代码位置 |
|---|---|
| Capture Token 生成 | `backend/app/core/security.py: create_capture_token` |
| Capture Token 校验依赖 | `backend/app/core/deps.py: get_capture_token_payload` |
| 普通用户认证拒绝 capture token | `backend/app/core/deps.py: get_current_user`（校验 `type == "access"`） |
| Capture API 端点 | `backend/app/api/capture.py: GET /api/v1/capture/stocks/{instrument_id}/snapshot` |
| scope 常量 | `backend/app/core/deps.py: CAPTURE_SCOPE_STOCK_DETAIL = "stock_detail_capture"` |

### 9.5 向后兼容

`create_capture_token` 的 `scope`、`instrument_id`、`user_id` 为关键字参数（可选）：

- 旧调用方（`monitor_batch_service` 等不传新参数）仍可工作，生成无 scope 的 legacy capture token；
- stock_detail 链路必须传 `scope=stock_detail_capture` 与 `instrument_id`，由 `get_capture_token_payload` 校验。
