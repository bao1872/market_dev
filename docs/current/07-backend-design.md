> 文档状态：CURRENT DESIGN BASELINE  
> 基线日期：2026-07-02  
> 已核对代码基线：`6f5ae2cec6b24dbd1b7bf6f23477f5e6f5096822`（`refactor/access-v2-platform-recovery`）  
> 事实来源：代码库 + 项目负责人截至 2026-07-02 已确认的产品与架构要求  
> 维护要求：任何代码、配置、测试、部署或文档修改都必须同步更新相关当前设计文档，并新增 CHANGE 记录。  
> 注意：该代码基线用于设计核对，不代表已经满足合并 `main` 或生产发布条件。
> 对齐口径：`CURRENT` 表示已确认设计，不等同于代码已完成；代码未实现、未验证或生产表现不一致的内容，必须在 `18-code-doc-alignment.md` 标为 `KNOWN_GAP`。

# 07 后端设计

## 1. 分层职责

- API：认证、权限依赖、参数校验、响应和事务入口；
- Service：业务规则、资格、状态转换、幂等、审计和编排；
- Repository：数据访问和批量写入；
- Strategy Runtime：统一行情输入、计算和输出契约；
- Worker：领取任务、心跳、租约、重试和恢复；
- Adapter：Pytdx、Mootdx、飞书和截图浏览器。

API、Worker 和前端都不得各自复制订阅资格、套餐额度、发布门禁和行情新鲜度规则。

## 2. 权限服务

> 实现核对：AccessContext 已建立，但趋势前两个结果端点与 Watchlist 路由仍未全部接入有效订阅依赖；详见 `ALIGN-006`、`ALIGN-007`。

`access_control_service.py` 产生 AccessContext，包含用户状态、角色、订阅状态、Plan、features 和 limits。核心业务 API同时要求有效订阅和所需 feature；仅检查 feature 不能替代有效订阅判断。

`eligible_user_service.py` 是 Monitor、Recipient、Outbox 和 Delivery 的用户资格唯一入口。管理员不自动进入普通会员监控 Universe。

## 3. 套餐与订阅

套餐定义来自 `plans` 表，业务查询通过 `plan_service.py`。Subscription 保存当前 plan_code、有效期、状态和不可为空的权益快照。管理员无 Subscription。

## 4. 行情聚合

> 实现核对：以下是已确认目标设计；`6f5ae2c` 的图表路径仍存在“DB 非空即不补尾部”和绕过实时合并的问题，详见 `ALIGN-009`。

统一行情服务负责：

1. 查询数据库完成 Bar；
2. 判断应有的最后完成时间；
3. 补齐数据库尾部；
4. 盘中拉取最新 1m；
5. 聚合当前 partial 周期；
6. 复权、去重和排序；
7. 返回新鲜度元数据。

Bars API、Indicator Service、Stock Detail、Monitor Snapshot 和 Feishu Capture 必须复用该服务。

## 5. 策略批处理

> 实现核对：当前仍存在 100ms 单股预算和允许 `partial_failed` 进入发布判断的问题，详见 `ALIGN-004`、`ALIGN-005`。

StrategyBatch：

- 领取 queued run 并设置租约；
- 构建 total/computable universe；
- 每个 computable 股票写入 StrategyRunItem 和 StrategyResult；
- failed 与 skipped 分开；
- 更新心跳和计数；
- 仅在严格完整性门禁通过时发布；
- 超时和预算失败不等于未命中。

## 6. 消息与飞书

> 实现核对：生产已出现只收到文字未收到图片，图片失败的可查询状态和独立重试尚未闭环，详见 `ALIGN-010`、`ALIGN-015`。

手动分享和业务事件复用 NotificationMessage、Outbox、CaptureJob 和 MessageDelivery。文字和图片分别落状态；错误不能吞掉；支持按 message group 查询和仅重试失败图片。

## 7. 事务与幂等

- 邀请码兑换、订阅变更、watchlist 额度、事件和 Outbox 在明确事务边界内执行；
- 任务使用 run key、唯一约束、advisory lock、heartbeat 和 lease；
- 重试创建新 attempt 或复用业务幂等键，不覆盖历史失败记录；
- 管理员敏感操作写 `access_audit_logs` before/after 数据。

## 8. 错误处理

错误必须保留业务上下文、失败阶段、error_code、request_id、run_id、user_id（脱敏）和 instrument。禁止捕获后返回假成功，禁止只记录日志而丢失可查询状态。
