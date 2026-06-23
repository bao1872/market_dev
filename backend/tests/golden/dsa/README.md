# DSA Golden Test Data

此目录存放 DSA（方向稳定性选股）策略的黄金测试数据。

## 预期文件

- `sample_dsa_result.json`: 单只股票的 DSA 计算结果样本
  - 包含 dsa_dir_bars, vwap_ret_avg, offset_mean 等指标
  - 来源：真实策略运行落盘数据

- `sample_dsa_batch_result.json`: 批量 DSA 计算结果样本
  - 包含多只股票的选股结果
  - 用于验证批量计算流程

## 数据来源

通过以下命令从生产/测试环境导出：
```bash
python -m app.services.strategy_batch_service  # 先运行一次批量计算
# 然后从 strategy_results 表导出 JSON
```

## 注意事项

- 数据必须来自真实落盘样本，禁止伪造行情
- 文件格式为 JSON，需包含完整的 metrics 字段
