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

# 04 当前业务流程

## 1. 注册、登录、到期与续期

```text
邀请码校验
→ 创建 active 用户和 member 角色
→ 从 plans 读取套餐
→ 创建 Subscription 权益快照
→ 生成 Access/Refresh Token
→ 调用 /me/access 决定路由
```

有效订阅进入业务页面。到期或无订阅用户进入 `/subscription-expired`，仅保留账户、套餐、续期、账户安全和历史消息只读。续期成功后刷新 AccessContext，恢复原自选和核心功能。

失败处理：邀请码无效、已用或套餐不存在时事务回滚；并发兑换只允许一次成功。

## 2. 盘后行情与趋势特征

```text
交易日收盘
→ bars_scheduler 更新日线/15m/1h 并检查覆盖率
→ 创建或复用 queued DSA StrategyRun
→ strategy_batch 领取
→ 构建 total universe 和 computable universe
→ 对每个 computable 股票计算并写入特征结果
→ 记录允许 skipped 和真实 failed
→ 完整性门禁
→ 仅 completed 且 computable 覆盖 100% 时发布
→ 趋势选股读取最新 published_run_id
```

`strategy_scheduler` 在约定时间做兜底，不重复创建已有 queued/running/completed/published 运行。`partial_failed` 保留用于诊断，不自动发布。

## 3. 趋势选股查询

```text
有效会员打开 /screener
→ 获取最新 published run
→ 以 run_id 查询结果
→ 后端按白名单筛选、排序、分页
→ 前端展示 source_total、filtered_total 和批次完整性
```

页面筛选不触发计算，不改变 Universe。到期或无订阅用户后端返回 403。

## 4. 自选股与盘中监控

```text
有效会员添加自选
→ 事务内检查额度并保存 active 记录
→ eligible_user_service 过滤有效会员
→ 对股票去重
→ monitor_scheduler 在交易时段轮询
→ 获取最新两根已完成 1m Bar
→ 按策略版本+股票+源Bar幂等评估
→ 生成 StrategyEvent、Recipients 和 Outbox
→ 投递前再次检查用户资格
```

到期后自选数据保留但不读取、不监控、不生成新消息；续期后自动恢复。

## 5. 个股详情行情

```text
进入 /stock/:symbol
→ 查询历史已完成 Bar
→ 判断数据库应完成到哪个交易时间
→ 补齐数据库尾部缺失
→ 交易时段拉取最新 1m
→ 聚合当前 15m/1h/1d/1w/1mo partial Bar
→ 合并、去重、复权
→ 指标和页面共用同一行情快照
→ 返回 as_of/data_source/is_partial/degraded
```

外部源失败时降级到数据库并明确标识。飞书截图必须使用同一快照。

## 6. 消息、截图和飞书

```text
业务事件或手动分享
→ 创建文字 NotificationMessage + Outbox
→ 创建/领取 CaptureJob
→ 截图成功后创建图片 NotificationMessage + Outbox
→ Outbox Relay 扩张具体 Delivery
→ Delivery Worker 发送文字和图片
→ 汇总 card_status/image_status/overall_status
```

文字和图片独立重试。图片失败不撤销已成功文字，但整体必须标为 `partial_failed`，并支持仅重试图片。

## 7. 管理后台

管理员操作用户状态、订阅、邀请码、策略、任务和投递时：

```text
Admin API 权限校验
→ 读取 before_data
→ 执行业务事务
→ 写 access_audit_logs
→ 返回真实结果
→ 前端刷新服务器数据
```

没有后端实现的按钮不得显示成功。
