# Bars Golden Test Data

此目录存放行情数据的黄金测试数据。

## 预期文件

- `sample_daily_bars.json`: 日线行情样本
  - 包含 open, high, low, close, volume 字段
  - DatetimeIndex 格式

- `sample_15min_bars.json`: 15分钟行情样本
  - 用于 Volume Profile 计算

- `sample_1min_bars.json`: 1分钟行情样本
  - 用于盘中监控事件检测
  - 需验证 floor 对齐到整分钟

- `sample_qfq_bars.json`: 前复权行情样本
  - 验证 adjustment="qfq" 参数生效

## 数据来源

通过以下命令从测试环境导出：
```bash
python -m app.repositories.bar_repository  # 从 DB 查询导出
```

## 注意事项

- 数据必须来自真实落盘样本，禁止伪造行情
- 1m bar 数据需包含完整的时间戳（验证去重逻辑）
- 前复权数据需与不复权数据对比验证复权因子
