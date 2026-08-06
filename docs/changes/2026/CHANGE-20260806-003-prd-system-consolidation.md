# CHANGE-20260806-003 — PRD 体系职责与盘后闭环校准

日期：2026-08-06
类型：product-contract + docs
状态：`prd_confirmed`；实现差距已写入 Maps，未修改代码、未部署、未写入数据

## 1. 原因

项目需求曾分散在领域 PRD、跨域总纲、任务附件和 `ref/` 参考材料中，导致总纲覆盖领域细则、实现进度混入 PRD，以及“stock core 不等待 15m”被误读为“盘后不需要更新 15m”。本次从职责所有权和可验证数据流出发，重新建立唯一正式需求体系。

## 2. 变更

- `docs/prd/README.md` 补全 PRD31、行情质量和竞价领域，明确 `ref/`、附件、任务书和外部项目只提供建议，采纳后必须进入唯一所属 PRD。
- PRD31 收敛为跨域总纲，只拥有 canonical、run identity、readiness、publication lineage 和 closure；算法、公式、交互、编排及运行环境仍由领域 PRD 拥有。
- PRD10 确认 `1d`、`1h`、`15m` 为独立正式资产；`15m` 是筹码共识必要输入，不能因 core 不等待而停更。
- PRD20 固定 stock core 与 chip 异步增强边界，并要求 DSA 选股投影消费同一 canonical DSA 结果。
- PRD30 修正本地完整链路与运行安全边界的冲突，并将 chip 调整为运行级有界 15m refresh 后批量读取。
- PRD70、PRD75、PRD80 明确各自领域所有权，删除总纲覆盖领域合同及实现进度式表述。

## 3. 已知实现差距

- chip 当前仍按股票逐个刷新 15m，尚未实现运行级 refresh phase 与 MDAS 批读。
- closure 当前缺少 `mandatory_ready_enhancing`，mandatory ready 但增强仍运行时可能过早显示 degraded。
- `1h` 的 provider、刷新生命周期、完整性和消费方尚未完成端到端核验。

这些差距只记录在 Maps，不在 PRD 中伪装为已实现。

## 4. 影响与验证

本次只修改 PRD、Maps 和 Change 索引，不修改治理文件、业务代码、Migration、运行配置或数据。验证范围为文档结构、内部链接、条款引用、治理检查器和 Git diff 范围。
