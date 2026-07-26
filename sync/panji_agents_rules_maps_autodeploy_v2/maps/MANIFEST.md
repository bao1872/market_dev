# PanJi Knowledge Manifest

> 状态：CURRENT INDEX  
> 阶段：调试、开发、内部测试  
> 默认运行分支：dev  
> 稳定锚点：main  
> 自动部署：dev push

## 真源

| 事实 | 文件 |
|---|---|
| 产品 | `current/00-product-business.md` |
| 架构 | `current/01-system-architecture.md` |
| 数据/API/权限 | `current/02-data-api-access.md` |
| Worker/飞书/运维 | `current/03-jobs-integrations-operations.md` |
| 前端 | `current/04-frontend-ux.md` |
| 测试 | `current/05-testing-acceptance.md` |
| 研究 | `current/06-research-feature-matrix.md` |
| AFC | `current/07-atomic-fact-contract-v1.md` |
| 指标 | `current/08-indicator-contracts.md` |
| 开发与部署 | `current/09-development-deployment-workflow.md` |
| 代码入口 | `code/*` |
| 进行中任务 | `work/ACTIVE.md` |
| 操作 | `runbooks/*` |
| 证据 | `evidence/*` |

## 当前核心事实

- 一套应用、一套 PostgreSQL/Redis；
- dev 是持续开发和自动部署线；
- main 是阶段稳定锚点；
- Work 与 CN 都可以开发；
- CN 保留完整服务器能力；
- `/root/web_dev` 开发；
- `/opt/panji-deploy` 自动部署；
- `/opt/panji-live` 运行；
- migration 是唯一固定手动门禁；
- 数据库不归普通应用部署管理。
