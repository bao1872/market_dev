# Task 15: 补合并 970 只 ETF/基金的周线/月线数据

## 目标
对 970 只有日线但无周线/月线的 ETF/基金，从日线合并生成周线/月线数据，最终使日线/周线/月线的 instrument 数量一致（6166 只）。

## 关键约束
- DB 连接串: postgresql+psycopg://bz:es123456@127.0.0.1:5432/bz_stock
- 使用 AsyncSessionLocal + refresh_weekly_bars/refresh_monthly_bars（从 DB 日线合并，不涉及 pytdx）
- 工作目录: /root/web_dev/backend
- 脚本目录: /root/web_dev/backend/scripts/
- 使用 .venv/bin/python 执行
- 先小批量验证 3 只 ETF，确认正确后再批量处理
- tqdm 进度条底部固定

## Phases

### Phase 1: SubTask 15.1 - 查询差集（in_progress）
- 编写脚本查询有日线但无周线/月线的 instrument_id 列表
- 预期约 970 只
- 打印数量和前 10 个示例（symbol + instrument_id）
- 状态: in_progress

### Phase 2: SubTask 15.2 - 小批量验证 3 只 ETF
- 先取差集前 3 只 ETF，调用 refresh_weekly_bars + refresh_monthly_bars
- 验证合并结果正确（行数合理、数据完整）
- 状态: pending

### Phase 3: SubTask 15.2 - 批量处理 970 只
- 串行处理，tqdm 进度条
- 记录成功/失败统计
- 状态: pending

### Phase 4: SubTask 15.3 - 验证一致性
- 查询 bars_daily/weekly/monthly 的 distinct instrument_id 数量
- 预期三者一致（6166 只）
- 状态: pending

## Errors Encountered
| Error | Attempt | Resolution |
|-------|---------|------------|
| (none yet) | | |
