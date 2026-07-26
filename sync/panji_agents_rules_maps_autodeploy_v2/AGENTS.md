# PanJi Agent Entry V5

> 适用：盘迹 `market_dev`  
> 文档体系：`AGENTS.md + rules/ + maps/`  
> 当前阶段：调试、开发、内部测试  
> 默认运行线：`dev → GitHub Actions → 腾讯云`

## 1. 最高原则

1. 用户当前明确要求优先。
2. GitHub 是应用代码唯一交接源。
3. 腾讯云数据库和行情数据是核心资产，普通开发部署不得删除或重建。
4. 代码、规则、地图和运行环境冲突时，先报告，不得自行选择一个版本冒充事实。
5. 完成声明必须有对应环境和证据。
6. 当前阶段优先缩短反馈环路，不机械套用正式生产审批流程。

## 2. 角色识别

开始任务先执行：

```bash
echo "${PANJI_EXECUTION_ROLE:-UNSET}"
```

合法值：

```text
trae_work_development
trae_cn_fullstack
```

### TRAE Work：`trae_work_development`

主要负责：

- 需求理解；
- 代码和测试；
- Work Preview；
- Git 提交和 push；
- 文档与交接。

不得：

- 连接腾讯云；
- 获取真实数据库或 SSH 凭据；
- 操作真实 migration；
- 声称完成腾讯云真实验收。

必读：

```text
rules/00-core-governance.md
rules/10-product-domain-invariants.md
rules/20-market-data-indicators.md
rules/40-testing-quality.md
rules/50-git-development-flow.md
rules/60-trae-work.md
maps/MANIFEST.md
maps/current/INDEX.md
```

### TRAE CN：`trae_cn_fullstack`

TRAE CN 是腾讯云完整工程环境，按任务选择模式：

```text
开发
测试
自动部署观察
手动部署
真实验收
运维排障
紧急修复
```

它不是“只能部署”的受限 IDE。

必读：

```text
rules/00-core-governance.md
rules/30-access-security.md
rules/40-testing-quality.md
rules/50-git-development-flow.md
rules/70-trae-cn.md
rules/80-auto-deployment-data-safety.md
maps/MANIFEST.md
maps/current/09-development-deployment-workflow.md
```

### 角色未知

角色未设置时，只允许只读检查，不修改、不提交、不部署、不操作数据。

## 3. 项目事实入口

所有任务先读：

```text
rules/README.md
maps/MANIFEST.md
maps/current/INDEX.md
maps/restore/RESTORE-CHECKLIST.md
maps/work/ACTIVE.md
```

再根据任务读取：

| 任务 | 地图 |
|---|---|
| 产品和业务 | `maps/current/00-product-business.md` |
| 架构 | `maps/current/01-system-architecture.md` |
| 数据、API、权限 | `maps/current/02-data-api-access.md` |
| Worker、盘后、飞书 | `maps/current/03-jobs-integrations-operations.md` |
| 前端 | `maps/current/04-frontend-ux.md` |
| 测试 | `maps/current/05-testing-acceptance.md` |
| 研究 | `maps/current/06-research-feature-matrix.md` |
| AFC | `maps/current/07-atomic-fact-contract-v1.md` |
| 指标 | `maps/current/08-indicator-contracts.md` |
| 开发和自动部署 | `maps/current/09-development-deployment-workflow.md` |
| 代码位置 | `maps/code/*` |
| 操作步骤 | `maps/runbooks/*` |

## 4. 分支规则

长期分支只有：

```text
dev
main
```

### `dev`

- 默认日常开发分支；
- push 后自动部署腾讯云；
- TRAE Work 和 TRAE CN 都可以按任务需要在 dev 开发；
- push 前必须确保提交可运行；
- 检测到 migration 时自动部署应暂停。

### `main`

- 阶段性稳定代码；
- 代码回滚锚点；
- 不要求同时运行第二套服务；
- 不自动跟随每次 dev push；
- 一个阶段确认稳定后才把 dev 合并到 main。

### 临时分支

仅在以下情况按需使用：

- 跨多天；
- 大范围重构；
- 当前 dev 必须保持可运行；
- 权限、数据库、部署系统等高风险工作；
- 用户明确要求 PR 审查。

禁止把“每个小改动都建分支”当成固定仪式。

## 5. 服务器目录职责

```text
/root/web_dev
```

- TRAE CN 开发和测试工作区；
- 可以有未提交修改；
- 不作为自动部署来源。

```text
/opt/panji-deploy
```

- 自动部署专用；
- 必须保持 clean；
- 只检出 GitHub 明确 SHA；
- 禁止日常手改。

```text
/opt/panji-live
```

- 当前运行代码与前端 dist；
- 保存 `RUNTIME_SHA`；
- 由部署脚本更新。

## 6. 修改前最小报告

```text
目标
角色
当前分支 / Base / HEAD
使用 dev 还是临时分支
已读规则和地图
涉及代码入口
是否会 push 并自动部署
是否涉及 migration / 依赖 / Compose / 环境变量
测试计划
明确不修改范围
```

## 7. 绝对禁止

- 删除数据库、核心行情数据或 PostgreSQL/Redis volume；
- `docker compose down -v`；
- `docker image prune -a`；
- 删除受保护的 `node:20-alpine`；
- 将真实秘密写入 Git；
- force push `dev` 或 `main`；
- 修改已发布 migration；
- 用服务器未提交代码形成第二套事实；
- 为通过检查削弱测试或架构门禁；
- 把 Work Preview 当腾讯云 E2E；
- 把临时分支 WIP 写成 CURRENT；
- 自动部署在 migration 未审核时继续执行。

## 8. 完成闭环

```text
读规则和地图
→ 修改
→ 测试
→ 更新 CHANGE 和地图
→ 精确暂存
→ 提交
→ push
→ dev 自动部署（如适用）
→ 腾讯云真实验收（如需要）
→ 记录 evidence
```

## 9. 完成报告

```text
角色
分支 / Base / Target SHA
修改文件
测试
push 状态
Actions 状态
腾讯云 runtime SHA
部署模式
migration
真实验收
Known Gap / BLOCKED
回滚点
```
