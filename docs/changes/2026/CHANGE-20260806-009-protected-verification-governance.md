# CHANGE-20260806-009 — 受保护治理域与远程验证框架收口

- 日期：2026-08-06
- 类型：governance + verification-infrastructure + docs + quality-gate
- 状态：`verified_code_pending_remote_execution`
- 需求出处：用户明确授权更新整个治理体系，并要求治理规则与绑定代码只能在治理授权下联动修改

## 修改前后

修改前，治理文档与远程验证脚本没有机器可读绑定清单；执行器接受的身份、计划、数据库创建、
Migration round-trip、清理和证据边界仍有缺口，CHANGE-008 还把验证代码排除在治理层之外。

修改后，`rules/PROTECTED_GOVERNANCE_FILES.json` 定义受保护治理变更域；AGENTS、rules、PRD、Map、
唯一 Runbook、治理检查器和合同测试共同约束单一 `panji-verify` 入口、完整 SHA、封闭计划、串行
attempt、维护库建删、一次性 verify-test、有界脱敏证据及 fail-closed cleanup。普通功能任务不得
修改清单中的验证框架，只有用户当轮明确授权治理调整时才能联动修改。

## 影响与文件

影响治理授权、远程验证执行、Compose 隔离、证据与资源清理。新增封闭计划与计划解析器，修正
验证编排、清理器和证据导出器；同步治理规则、系统运行 PRD/Map、部署 Runbook、检查器及测试。
无数据库 Migration，无业务数据写入，无稳定运行部署。

## 验证与状态

本地执行治理检查、文档一致性、目标 Ruff、验证基础设施 pure-unit、治理检查器测试、Python 编译、
Shell 语法和 Compose YAML 解析。远程 PostgreSQL、Migration、Synthetic E2E 和真实资源清理尚未执行，
因此仅为 `verified_code_pending_remote_execution`，不得声称远程闭环。

## Git 与风险

- 分支：`dev`
- Commit：提交后回填
- 风险：远程环境的镜像、网络和数据库维护凭据仍需正式 attempt 验证；失败必须保留有界证据并产生新 SHA 修复。
