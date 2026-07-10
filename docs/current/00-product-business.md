# 00 产品与业务规则

## 1. 产品定位

盘迹（PanJi）是面向 A 股个人投资者的多用户研究、选股、监控和通知平台。核心路径：

```text
发现机会 → 验证机会 → 持续追踪
```

系统负责行情准备、全市场特征计算、发布批次、自选股盘中监控、事件消息、个股研究、飞书投递和任务可观察性。

系统不负责自动下单、不连接券商账户、不管理资金、不保证收益，也不把单一指标包装成确定性买卖信号。

## 2. 用户和权限

| 用户类型 | 当前能力 |
|---|---|
| 访客 | 查看门户、提交内测申请、登录/注册 |
| 有效会员 | 趋势选股、自选股、个股详情、消息中心、设置、飞书通知 |
| 到期或无订阅会员 | 登录、账户/套餐/续期、历史消息只读；核心功能 403 |
| 管理员 | 用户、邀请码、订阅、策略、任务、投递、审计管理；无 Plan/Subscription |
| Worker | 行情、计算、监控、事件、Outbox、Delivery、Capture、日历和运维任务 |

普通会员核心资格：

```text
User.status = active
具有 member 角色
Subscription.status = active
starts_at <= now < expires_at
```

管理员不进入普通会员监控 universe，不受普通会员额度限制，但必须使用 Admin API 并写审计日志。

## 3. 核心业务规则

### 趋势选股

- DSA selector 对全部 computable universe 生成特征；
- 计算阶段不能按方向、强弱、matched、用户筛选删除股票；
- 不可计算股票必须是 skipped 并有 reason code；
- 失败、超时、数据库异常属于 failed；
- `partial_failed` 不允许自动发布；
- 用户查询绑定不可变 `published_run_id`；
- 前端筛选、排序、分页只作用于已发布结果；
- 当前不支持多策略组合。

### 自选与监控

- 有效会员添加 active 自选后自动进入监控；
- 不创建 MonitoringPlan；
- 到期后自选数据保留，但不读取、不监控、不产生新消息；
- 续期后恢复；
- 盘中监控只处理已完成 1m Bar；`source_bar_time` 必须来自最新已完成 1m bar（剔除最后一根可能未完成的 bar），禁止用日线/15m/partial daily 作为监控触发口径；
- 同一策略版本、股票、源 Bar 只评估一次。

### 个股详情

- K 线、指标和截图共享同一行情快照；
- 响应必须带 `as_of`、`data_source`、`is_partial`、降级状态；
- DSA 与 Node Cluster 图层可开关；
- 页面不能把陈旧数据库数据显示成“实时”。

### 消息与飞书

- 文字和图片是同一 message group 下的独立投递；
- 文字成功、图片失败时整体为 `partial_failed`；
- 失败必须记录 `failed_step`、`error_code`、`error_message`；
- 允许仅重试图片，不能重复发送文字；
- 飞书渠道唯一方式为 `feishu_platform_app`；
- `feishu_webhook` 已永久删除，禁止恢复；
- 手动指定 `target_channel_id` 的用户主动通知跳过 `eligible_user_service`，自动通知仍过滤资格。
- 飞书盘中截图业务默认展示 1d（日线）：实时性由 Capture Snapshot `1d + include_realtime=True` 的 partial daily 合成保证；15m 不是默认飞书图周期（15m 仅作为 Capture API 能力或策略明确声明的辅助上下文）。

## 4. 策略与指标

生产策略只保留：

| 策略 | 用途 |
|---|---|
| `dsa_selector` | 盘后全市场特征计算与趋势选股 |
| `watchlist_monitor` | 盘中有效会员自选股事件判断 |

生产算法参数不可由普通用户修改。算法变更必须发布新 StrategyVersion，不覆盖已发布版本。

Node Cluster 输入契约：

```text
1d: 250 根已完成 qfq 日线
15m: 4000 根已完成 qfq（250 × 16）
1m: 最近 2 根已完成 Bar
```

## 5. 已废弃能力

- 多策略组合：DEPRECATED；未来如恢复，必须作为独立实验重新设计；
- Webhook 飞书渠道：DEPRECATED；已永久删除；
- 旧 Membership 业务模型：不作为运行时事实源；
- `/membership-expired`：仅兼容重定向到 `/subscription-expired`。
