# CHANGE-20260806-001 开发、测试、部署边界与盘后 15m 合同校准

- 变更编号：CHANGE-20260806-001
- 任务名称：开发、测试、部署三平面治理与盘后 15m 依赖校准
- 需求出处：用户确认采用本地开发、远程隔离验证、远程稳定运行设计，并确认筹码共识需要盘后 15m 数据
- 修改前行为：治理允许本地经 SSH 隧道连接 `bz_stock` 运行目标 pytest；远程验证与稳定运行部署术语混用；盘后文档只强调 core 移除 15m 阻塞，容易被误读为盘后无需更新 15m
- 修改后行为：测试合同收敛为 `PURE_UNIT_TEST=1` 与远程 `PANJI_REMOTE_VERIFY_DB_TEST=1`；远程验证授权与稳定运行部署授权分离；stock_core/review core 不等待 15m，但独立 chip job 必须使用目标交易日已刷新并通过 readiness 的 15m 数据
- 影响模块：治理规则、系统运行 PRD/Map、盘后 PRD/Map、开发与恢复 Runbook
- 修改文件：`AGENTS.md`、`rules/20-market-data-indicators.md`、`rules/40-testing-quality.md`、`rules/81-remote-deployment-only.md`、`rules/90-deprecated-forbidden.md`、相关 PRD/Maps/Runbooks
- 文档更新：已更新上述正式文档；未新增治理目录
- 测试证据：`tools/check_docs_consistency.py` 通过；`tools/check_governance_rules.py` 通过；`git diff --check` 通过
- Git 分支：`dev`
- Git Commit：未提交
- 数据库迁移：无
- 配置变化：无
- 风险：15m 刷新依赖外部行情源，刷新失败会按结构化 skipped 处理；真实 PG 与外部源行为尚未在远程验证栈核验
- 遗留问题：在远程验证库完成 PG 集成和带受控行情适配器的验证后再部署稳定运行栈
- 状态：`implemented_local`；`shared_test_code_removal=implemented`；`chip_15m_refresh_gate=implemented`；`remote_verified=false`；`deployed=false`
