# 策略页与十四任务模型接入计划

2026-10-16，基于用户确认的第三版设计执行。Level 2。代码在 dev 上开发；不修改 PRD、治理、数据库或运行环境，不执行训练、全市场任务、发布或远程刷新。

## 已核验基线与 owner

- `strategy_integration/producer.py`：候选/联合评价快照生产。
- `strategy_candidate_snapshot.py` / `strategy_candidate_shards.py`：候选验证、分片及读取。
- `strategy_research_read_service.py`：联合性能评价，API 不推理。
- `strategy_integration/training.py` / `rolling_windows.py`：训练及 24/6/3 切割。
- `strategy_integration/worker.py` / `release_store.py`：既有任务、审核及发布路径。
- `frontend/src/features/strategy-research/`：四个页面及图表。
- panji_lab `integration/panji_fourteen_v1`：冻结 14 个原生任务与评分规则；原样引入，不依赖研究缓存和 pickle。

## 切片

1. 原样引入包；新增适配器复用冻结 runtime，历史评分按各决策日前缀生成；D0 独立，旧候选被严格新极值替代后不再评分，D3 不延用。
2. 既有生产/读取链新增版本化合同，兼容旧快照；身份含证券、锚点、方向、阶段、层和模型版本；全市场统一模型，不接个人模型。
3. 任务性能从全适用候选样本统计 Precision/Recall/AUC，按阶段与评价域分开；联合评价复用既有路径收益、回撤及事件链；趋势协议缺失显式 unavailable。
4. 全宽 K 线与时间对齐评分轨道；个股增加同版本评价，全市场相同评价组件；保留当日方向/收益斜率和管理员手动训练。
5. 纯单元/合同、TypeScript/build、浏览器合成接口验证；仅在实际执行后记录结果。真实输入口径对账、全市场时延、PG/远程运行不以本地测试替代。

## 关键边界

panji_lab 未声明与本项目 QFQ_ASOF_V1 输入完全一致，正式生产必须通过显式输入合同核验；不能因为可加载便认为可发布。模型原始分数未经校准。L2 研究条件子群不代表实时全候选成绩。联合规则沿用既有阈值门控，不自行引入新融合公式。趋势性能无标签和连续分数，不伪造指标。

已有用户修改包括 canonical_strategy_bars.py、db_export.py 及历史发布脚本，本任务保留，不覆盖。
