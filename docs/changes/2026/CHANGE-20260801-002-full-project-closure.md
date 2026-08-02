# CHANGE-20260801-002 全项目问题收口候选版本

## 需求出处

《盘迹全项目问题收口总任务书》与用户确认的续作计划，基线 `origin/dev@ff89fea`。

## 修改前后

修改前：Review force/withdrawal、chip 生命周期、第一金字塔消费语义、DSA segment、板块层级、
Review 层级归因与 P/Q/U/C/V、历史 bootstrap、发布门禁、API gateway、行情详情来源和竞价真值/
publication 存在断裂或状态误报。

修改后：

- Review force 仅 provisional；withdrawal 默认 dry-run、限定 pointer 且保留 run/子数据。
- chip job、第一金字塔 canonical 消费、DSA segment 与板块批次/层级身份收口。
- Migration 080–081 建立层级归因 evidence 与 versioned metric observations；Review 使用
  `ReviewMemberFact`、PIT membership、真实日收益、两遍横截面和正式发布门禁。
- 浏览器 API 统一 `/api` gateway + `/v1` endpoint；MCQ、operator、自选和 Review 五阶段 UI 收口。
- Migration 082 建立 `auction_analysis_publications`；竞价按 provider family 验证独立来源，
  只有共识报价可 scan/aggregate/publish，用户 API 只读 pointer。

## 影响模块

Review、after-close/chip、第一金字塔与 DSA、板块 taxonomy、行情与个股详情、筛选器、竞价分析、
API gateway、CI 与治理文档。

## 数据库迁移

- `080_review_hierarchy_attribution_evidence`
- `081_review_metric_observations`
- `082_auction_analysis_publication`

均为前向迁移并保持单一 Alembic head。PostgreSQL upgrade/downgrade/upgrade 只允许在 CI 临时容器验证。

## 验证证据

- Review/行情/竞价目标 pure-unit 已通过；竞价目标集 295 项通过。
- 前端合同 507 项通过；竞价合同 11 项通过；build 通过；lint 0 error（历史 warnings 保留）。
- 本轮新增/修改竞价目标文件 Ruff 与 Mypy 通过；Alembic 单一 head 为 082。
- PG Integration、Migration 往返、Architecture/Docs/Governance、完整 backend、E2E 与同一最终 SHA
  CI 尚需远端确定结论，未标记 verified。

## 生产与外部状态

- 未部署生产、未启动本地 Worker、未连接本地/正式数据库、未执行 production withdrawal。
- 当前只有 mootdx/pytdx 同一通达信供应链，竞价生产状态为
  `blocked_external_auction_truth_source`；不得以不同服务器伪装双源。
- 只有 PG Integration、迁移与发布安全通过后，才可按任务书限定条件运行 withdrawal dry-run/apply。

## 回滚

- 代码按聚焦提交逆序回滚；旧 run 与历史数据不原地修改。
- Migration 082 可降到 081，只移除竞价 analysis pointer 表；080/081 的生产回滚需先评估数据影响。
- withdrawal 只撤销唯一正式 Review pointer，不删除 run；若条件不完全匹配则不得 apply。

## 分支与提交

分支：`codex/panji-full-closure-20260801`。

逻辑提交从 `[P0] Close Review publication safety and withdrawal` 到
`[AUCTION] Complete auction analysis closure`；最终文档/CI 提交在本 Change 所属提交记录。

## 遗留问题

- 同一最终 SHA 的完整 CI 与 PG Integration。
- 真实第二独立竞价供应商及正式交易日 E2E。
- 生产 withdrawal dry-run/apply、部署和 dev 快进均未执行。
