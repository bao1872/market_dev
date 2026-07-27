# 10 产品域不变量

> 来源：AGENTS.md §七.1-4、§七.6
> 状态：并行验证

## 产品边界

盘迹是 A 股研究、全市场特征计算、自选股盘中监控和消息投递平台。

不做：

- 自动交易；
- 券商账户连接；
- 资金管理；
- 收益承诺；
- 单一指标买卖信号；
- 普通用户修改生产算法参数。

## 策略规则

当前生产只保留 `dsa_selector` 与 `watchlist_monitor`。

多策略组合已废弃，不得从旧代码或旧文档恢复。

## DSA 规则

- DSA 对全市场 computable universe 计算特征；
- 不得在计算阶段按方向、强弱、matched、用户筛选提前删除股票；
- 发布必须满足严格完整性门禁；
- `partial_failed` 不得发布。

## 自选和监控

- 有效会员添加自选后自动进入盘中监控；
- 不创建 MonitoringPlan；
- 到期用户保留历史数据，但不能读取、修改、监控或产生新投递。

## 飞书接入

唯一接入方式：`feishu_platform_app`。

禁止恢复：

- `feishu_webhook` / `FEISHU_WEBHOOK`；
- 独立管理员飞书 App；
- 独立管理员接收人配置。

管理员内测申请通知必须复用管理员用户自己的 active `feishu_platform_app` NotificationChannel。

### 盘中监控触发口径

- 盘中监控触发只依赖**最新已完成 1m bar**；
- `source_bar_time` 来自最新已完成 1m bar，剔除最后一根可能未完成的 bar；
- 飞书盘中截图业务默认 `timeframe=1d`，实时性由 Capture Snapshot `1d + include_realtime=True` 的 partial daily 合成保证；
- 修截图/清晰度/缓存不得改变 `watchlist_monitor` 事件计算口径；
- `monitor_batch_service` 计算输入 `bars_daily` / `bars_15min` 必须 `include_realtime=False`。
