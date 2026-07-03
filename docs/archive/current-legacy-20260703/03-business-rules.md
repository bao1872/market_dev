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

# 03 当前业务规则

## 1. 账户、角色与订阅

### BR-AUTH-001 身份

- 系统只保留 `admin` 和 `member` 两个基础角色；
- 用户身份由 JWT 注入，客户端不能指定 `user_id`；
- `status != active` 的用户不能登录或调用受保护 API；
- 管理员无 Plan、无 Subscription，不受普通会员额度限制。

### BR-SUB-001 有效订阅

普通会员只有同时满足以下条件才具有核心业务资格：

```text
User.status = active
具有 member 角色
Subscription.status = active
starts_at <= 当前时间 < expires_at
```

资格链：JWT → AccessContext → User → Role → Subscription → Feature → Quota → Ownership。

### BR-SUB-002 到期冻结

到期或无 Subscription 的普通会员：

允许动作：
- 登录；
- 读取 `/me/access`；
- 读取 `/plans` 套餐列表；
- 使用邀请码续期；
- 访问 `/subscription-expired` 续期引导页；
- 维护账户与安全设置；
- 只读读取历史消息、历史事件、历史订阅记录。

禁止动作：
- 趋势选股相关 API；
- 自选股读/写（`GET/POST/DELETE /watchlist*`）；
- 个股详情与相关行情/指标；
- 新建或恢复监控；
- 新建事件、站内消息、Outbox、Delivery 和飞书投递。

原自选股、历史消息、历史事件和订阅记录保留。续期后自动恢复，不补发冻结期间未生成的盘中事件。

### BR-SUB-003 套餐与降级

套餐定义以 `plans` 表为机器事实源，通过 `plan_service.py` 查询。降级后已有自选超过新额度时不删除数据，但禁止继续新增；重新激活已删除记录也视为新增并检查额度。

## 2. 趋势选股

### BR-SELECT-001 全市场特征计算

DSA selector 对全部 computable universe 生成特征。方向、强弱、`matched`、页面筛选和用户偏好不能改变计算 Universe，也不能决定是否写入 StrategyResult。

### BR-SELECT-002 允许跳过与失败

- 行情不足、停牌或明确不受支持的数据条件可以 skipped，但必须有 reason code；
- 超时、异常、数据库失败和预算失败属于 failed，不得当作 skipped 或未命中；
- 每个 computable 股票必须有一条 StrategyResult。

### BR-SELECT-003 发布门禁

自动发布必须同时满足：

- run 状态为 `completed`；
- `failed_count = 0`；
- `result_count = succeeded_count`；
- `succeeded_count + skipped_count = total_instruments`；
- skipped 全部属于允许原因并有 reason code；
- computable universe 结果覆盖率为 100%。

`partial_failed` 不得自动发布。用户查询始终绑定不可变 `published_run_id`。

### BR-SELECT-004 计算与查询分离

前端筛选、排序、关键词和分页只作用于已发布结果，不能触发算法重算或改变发布内容。

## 3. 自选与盘中监控

### BR-WATCH-001 自动监控

有效会员添加 active 自选后自动进入监控，不创建 MonitoringPlan。

### BR-WATCH-002 资格和额度

所有 watchlist 读写和监控状态 API 都要求有效订阅；新增或恢复在事务内检查 `monitor_limit`。管理员仅在管理或验证场景豁免，不自动进入普通会员监控 universe。

### BR-MONITOR-001 完成 Bar

盘中监控只使用最新两根已完成 1m Bar。未完成 Bar 不触发正式事件。

### BR-MONITOR-002 幂等

同一策略版本、股票、源 Bar 只评估一次；事件、收件人、Outbox 和投递必须有稳定幂等键。

### BR-MONITOR-003 用户资格复核

监控 Universe、事件收件人扩张、Outbox 和 Delivery 都必须复用统一 eligible-user 规则；投递前再次检查资格，避免排队期间订阅失效。

## 4. 行情与个股详情

### BR-DATA-001 统一聚合

行情消费者必须使用同一聚合语义：数据库历史完成 Bar + 缺失尾部补齐 + 交易时段最新 1m 聚合 partial Bar。页面、指标和飞书截图不得各自形成第二套行情路径。

### BR-DATA-002 新鲜度和降级

响应必须给出 `as_of`、`data_source`、`is_partial`、`freshness_seconds` 和降级原因。外部源失败时可以返回数据库数据，但不得伪装为最新。

## 5. 消息与飞书

### BR-MESSAGE-001 同事务和所有权

业务事件、站内消息和 Outbox 的关键写入必须保持事务一致；用户只能操作自己的渠道和消息。

### BR-FEISHU-001 文字与图片

文字与图片是同一 message group 下的独立投递。状态至少区分 pending、succeeded、failed 和 not_created。

### BR-FEISHU-002 部分失败

文字成功、图片失败时整体状态为 `partial_failed`，明确 `failed_step` 和 `error_code`，并允许只重试图片；重试图片不得重复发送文字。

## 6. 变更与发布

### BR-GOV-001 分支和文档

任何修改使用独立分支，不直接修改 `main`。提交必须同时包含相关当前设计文档、CHANGE、测试和一致性检查。旧分支、旧注释和归档文档不能覆盖当前事实源。
