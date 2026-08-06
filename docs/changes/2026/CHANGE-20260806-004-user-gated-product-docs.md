# CHANGE-20260806-004 — PRD、Maps 与 Runbooks 用户授权门

日期：2026-08-06
类型：governance + workflow + quality-gate
状态：`implemented_local_pending_user_acceptance`

## 1. 目标

将产品决策、候选代码实现和验收后的项目记忆分成独立阶段，防止 Agent 因代码变化自动改写需求、把候选实现写成当前事实，或把未经真实执行的步骤写进 Runbook。

## 2. 新合同

- PRD 只有用户在当前任务主动发起 PRD 更新时才允许修改。
- Maps 只有用户明确要求，或在候选实现验收后明确确认同步时才允许修改。
- Runbooks 只有用户明确要求，或在真实操作验收后明确确认同步时才允许修改。
- 代码实现阶段默认只修改代码、测试、Migration、配置和唯一相关 Change。
- 重要候选实现尚未获用户验收时，Change 使用 pending acceptance 状态。
- 计划授权、“完成闭环”和笼统“更新文档”都不能隐式越过上述文档门。

## 3. 修改范围

- `AGENTS.md`
- `rules/README.md`
- `rules/00-core-governance.md`
- `rules/40-testing-quality.md`
- `rules/50-git-development-flow.md`
- `tools/check_governance_rules.py`
- `tools/tests/test_check_governance_rules.py`

本次未修改 PRD、Maps 或 Runbooks。

## 4. 验证

- Governance checker
- Governance regression tests，包括 PRD、Maps、Runbooks 和计划授权门缺失回归
- Docs consistency
- `git diff --check`
