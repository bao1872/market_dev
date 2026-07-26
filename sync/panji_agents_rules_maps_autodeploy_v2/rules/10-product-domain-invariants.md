# 10 产品和业务不变量

## 定位

盘迹是 A 股研究、全市场特征计算、自选盘中监控、个股状态观察、消息和飞书平台。

不做：

- 自动交易；
- 券商连接；
- 资金管理；
- 收益承诺；
- 普通用户修改生产算法参数。

## 生产策略

仅：

```text
dsa_selector
watchlist_monitor
```

多策略组合不得恢复。

## DSA

- 完整 computable universe；
- skipped 必须有 reason；
- failed 和 partial_failed 不发布；
- 用户只读 published run；
- 前端只筛选已发布结果。

## 自选监控

- active 自选自动进入监控；
- completed 1m bar 触发；
- 到期数据保留、监控停止；
- 幂等和资格复核必须存在。

## 个股详情

- `/stock/:symbol` 唯一普通用户主 K 线；
- `/market` 不显示主 K 线；
- AFC 只描述事实，不输出买卖。

## 飞书

- Platform App only；
- 文字和图片独立；
- partial_failed；
- 允许 image-only retry；
- Webhook 永久禁止。
