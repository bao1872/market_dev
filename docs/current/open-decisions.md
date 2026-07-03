# Open Decisions：未决问题

这里只保存尚未作出最终决定的问题。已确认规则必须移入 current 文件，已关闭过程放入 CHANGE。

## OPEN-PRODUCT-001 商业模式与价格

当前使用邀请码和实验套餐。尚未决定长期收费、价格、套餐数量和公开注册策略。

## OPEN-SELECT-001 历史批次与回补入口

当前普通用户只查看最新 published run。尚未决定历史批次是管理员专用、会员可见还是完全隐藏，以及是否开放手动回补入口。

## OPEN-STRATEGY-001 未来事件引擎

当前不支持策略组合。未来可能探索多指标原子状态、L1/L2/L3 事件、策略表或机器学习，但必须独立实验，不得恢复旧组合页面。

## OPEN-MONITOR-001 新监控算法

未来可能加入 MACD、成交量 Z-score、DSA VWAP、筹码峰等离散状态。尚未决定扩展单一 monitor 还是拆分独立策略。

## OPEN-DATA-001 实时行情运行阈值

统一聚合行为已确认，仍需通过压测决定 Redis TTL、外部源重试次数、可接受 freshness、并发合并和降级告警阈值。

## OPEN-ARCH-001 实验环境

尚未决定在不复制大型行情数据库的前提下，采用共享只读行情、独立 Schema、独立结果表、Feature Flag 或独立 Worker 的具体组合。

## OPEN-NOTIFY-001 图片存储与生产 E2E

Webhook 与 Platform App 的长期选择已决定：Platform App only。仍未决的是图片存储方式、用户配置复杂度、真实生产 E2E 验证口径，以及 partial_failed/仅重试图片的生产验收流程。

## OPEN-PORTAL-001 门户定价与转化

内测套餐卡、价格、申请入口和注册关系仍可调整。
