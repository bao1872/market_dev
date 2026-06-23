# Monitoring Golden Test Data

此目录存放监控策略（布林带/成交量节点）的黄金测试数据。

## 预期文件

- `sample_bb_state.json`: 布林带监控状态样本
  - 包含 bb_upper, bb_mid, bb_lower, current_price, prev_close 等字段

- `sample_bb_event.json`: 布林带穿越事件样本
  - 包含 bb_upper_touch/bb_mid_touch/bb_lower_touch 事件

- `sample_vn_state.json`: 成交量节点监控状态样本
  - 包含 current_price, upper_node, lower_node, position_0_1 等字段

- `sample_vn_event.json`: 成交量节点穿越事件样本
  - 包含 node_cluster_touch 事件

- `sample_monitor_cycle_result.json`: 单轮监控执行结果样本
  - 包含 total_instruments, total_events_written 等统计

## 数据来源

通过以下命令从测试环境导出：
```bash
python -m app.services.monitor_batch_service  # 先运行一次监控周期
# 然后从 monitor_states / strategy_events 表导出 JSON
```

## 注意事项

- 数据必须来自真实落盘样本，禁止伪造行情
- 事件数据需包含 dedupe_key 和 bar_time_key 字段（验证去重逻辑）
