# AI Onboarding：新对话/新机器快速恢复上下文

> 适用对象：Trae、Codex、ChatGPT、Claude Code 或任何新 agent。  
> 第一原则：先理解当前事实，再提出修改。不要直接按聊天记忆改代码。

## 1. 项目一句话

盘迹（PanJi）是面向 A 股个人投资者的多用户研究平台：盘后全市场特征计算，盘中自选股监控，站内消息和飞书通知，支持管理员运维与任务观察。系统不做自动交易，不接券商账户，不承诺收益。

## 2. 当前必须知道的业务边界

- 普通用户必须是 active member 且有 active subscription 才能使用趋势选股、自选股、个股详情、监控和新通知；
- 管理员无 Plan、无 Subscription，不进入普通会员监控 universe；
- 用户添加 active 自选股后自动进入监控，不再创建 MonitoringPlan；
- 盘中监控只处理已完成 1m Bar，不用未完成 Bar 生成正式事件；
- 趋势选股只读取完整 published run，查询绑定 `published_run_id`；
- 当前不支持多策略组合；
- DSA 和 Node Cluster 参数不可由普通用户修改；
- 飞书唯一接入方式是 `feishu_platform_app`，`feishu_webhook` 已永久删除；
- 手动指定 `target_channel_id` 的通知跳过 `eligible_user_service`，自动通知仍必须过滤资格；
- Capture Token 与普通 Access Token 隔离，不得污染普通登录态。

## 3. 新任务开始前必须读

```text
docs/current/MANIFEST.md
docs/current/00-product-business.md
docs/current/01-system-architecture.md
docs/current/code-doc-alignment.md
docs/maps/backend-module-map.md
docs/maps/frontend-route-map.md
docs/maps/api-route-map.md
```

如果任务涉及 Worker、飞书、截图、任务状态，再读：

```text
docs/current/03-jobs-integrations-operations.md
docs/maps/worker-job-map.md
docs/maps/notification-flow-map.md
docs/maps/deployment-runtime-map.md
```

如果任务涉及数据、API、权限，再读：

```text
docs/current/02-data-api-contracts.md
docs/maps/database-model-map.md
docs/maps/test-coverage-map.md
```

## 4. 修改前必须回答

```text
1. 当前功能是什么；
2. 当前代码入口在哪里；
3. 涉及哪些前端页面、API、Service、Repository、Model、Worker；
4. 相关测试在哪里；
5. current docs 和 maps 是否一致；
6. 修改范围是什么；
7. 明确不修改什么；
8. 是否需要 CHANGE；
9. 验收标准是什么。
```

## 5. 禁止行为

- 不读 docs 就改代码；
- 把旧注释、旧分支、旧 archive 当当前事实源；
- 恢复 Webhook；
- 恢复多策略组合；
- 用前端隐藏代替后端权限；
- 修改算法参数但不发新 StrategyVersion；
- 把部分失败伪装成成功；
- 为通过 CI 扩大 ignore、skip、noqa、type ignore；
- 一次性全盘重构。

## 6. 推荐工作方式

每次只做一个小目标：

```text
读文档 → 建分支 → 写 CHANGE → 最小修改 → 测试 → 更新 current/maps → Draft PR → CI → 人工 Review
```
