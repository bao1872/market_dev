# 系统运行体系 Map

本 Map 只记录已由仓库或只读运行证据核验的当前事实。目标行为见
`docs/prd/80-system-runtime.md`，操作步骤见 `docs/runbooks/development-deployment.md`。

## 本地边界

| 项目 | 当前实现 |
|---|---|
| 代码目录 | `/Users/zhenbao/Desktop/coding/market_dev` |
| 默认分支 | `dev`，跟踪 `origin/dev` |
| Backend | `backend/.venv` + Uvicorn，入口 `backend/app/main.py:app` |
| Frontend | Node.js + Vite，配置入口 `frontend/vite.config.ts` |
| PostgreSQL | 不在本地启动；应用开发可经 SSH Tunnel 连接正式 `bz_stock`，本地测试禁止连接任何数据库 |
| Redis | 不在本地启动；开发连接使用隔离逻辑 DB，测试使用 mock |
| Worker/Scheduler | 本地不得启动正式 Worker、Scheduler、盘后编排或全市场任务 |
| 测试 | `PURE_UNIT_TEST=1`；PostgreSQL 集成测试只在 CI 临时容器运行 |

本地启动和隧道命令以 `docs/runbooks/local-development.md` 为准。本地进程隔离不能被
解释为测试数据隔离；任何连接正式库的写入都是真实业务写入。

## 生产身份

| 项目 | 当前事实 |
|---|---|
| SSH 配置别名 | `panji-prod` |
| 唯一 SSH 执行入口 | `scripts/ops/panji-prod-ssh` |
| 部署前检查 | `scripts/ops/panji-prod-preflight` |
| 服务器仓库 | `/root/web_dev` |
| 运行代码目录 | `/opt/panji-live` |
| 环境配置 | `/etc/market-dev/market.env` |
| Compose | `docker-compose.prod.yml` 与 `docker-compose.live.yml` 始终叠加 |
| 部署状态文件 | `/etc/market-dev/.panji-deploy-state`，保存上一成功部署完整 SHA |

原始 IP 和旧 SSH 别名不是允许的操作入口。生产身份的具体网络值由 SSH 配置和
`panji-prod-preflight` 校验，不在普通命令或报告中重复传播。

## Git 与 CI

- `dev` 是默认开发分支，也是手工 CI 与开发部署的唯一代码来源；
- `.github/workflows/ci.yml` 只接受 `workflow_dispatch`；可选 `base_sha` 用于范围分类，
  为空时使用目标 SHA 的第一父提交；
- CI 是诊断工具，不执行部署，也不是部署前置门禁；
- `main` 未经明确授权不得修改、合并或推送；隔离实验分支不得作为部署来源；
- 当前仓库中只有 `ci.yml` 一个 workflow。

## 部署调用图

```text
scripts/ops/panji-test-deploy <FULL_SHA> [--dry-run]
  -> scripts/ops/panji-prod-preflight
  -> scripts/ops/panji-prod-ssh
  -> /root/web_dev/scripts/deploy/panji-deploy.sh <FULL_SHA> [--dry-run]
  -> docker-compose.prod.yml + docker-compose.live.yml
  -> /opt/panji-live
  -> health / ready / version / mount / scheduler-singleton verification
```

实现边界：

- 本地入口只校验来源、运行 preflight 并调用服务器仓库内的部署实现；
- 服务器实现根据“上一成功部署 SHA 到目标 SHA”的完整差异分类；
- 普通 Backend 代码只同步 Live Mount，不构建镜像；
- 普通 Frontend 代码在服务器生成 `dist` 后同步，不构建镜像；
- 依赖、Dockerfile、系统依赖或必须烘焙的 Nginx 配置变化才构建对应环境镜像；
- 即使构建环境镜像，服务仍通过 prod + live 叠加运行，代码来源仍是 `/opt/panji-live`；
- migration 只在 migration 文件发生变化时执行；部署不自动执行 bootstrap、业务 run、
  publish、withdrawal 或其他业务数据动作；
- PostgreSQL、Redis 和 Umami 不进入普通重启列表；禁止 `down -v`。

成功证据必须同时满足：服务器 repo HEAD、`/opt/panji-live/RUNTIME_SHA` 和版本接口的
`runtime_git_sha` 等于目标完整 SHA，`deployment_mode=live`，健康/就绪探针通过，受影响
容器挂载来源包含 `/opt/panji-live`。状态文件只在这些检查全部通过后更新。

## Compose 服务边界

`docker-compose.prod.yml` 定义 frontend、backend、PostgreSQL、Redis、Umami，以及 bars、
strategy、calendar、monitor、strategy-batch、outbox、delivery、after-close、watchdog、capture
等 Worker。`docker-compose.live.yml` 为应用服务叠加 `/opt/panji-live` 只读运行代码挂载。

有状态服务的数据卷由 Compose 管理。部署脚本不得删除或重建 PostgreSQL/Redis Volume，
不得把测试数据库或测试数据写入生产持久化资源。

## 最近只读运行证据

2026-08-02 的只读核验记录显示，当时服务器 repo 和运行容器仍处于旧镜像构建模式，
容器未挂载 `/opt/panji-live`；同时观察到 `trading-postgres-test` 持久测试容器。两者均不符合
当前合同。此次治理修改没有连接生产，也没有部署、迁移或删除资源，因此不能把它们写成已修复。

后续只有在用户明确授权生产操作后，才能通过 `panji-prod-preflight` 和只读命令重新确认；
持久测试容器的删除还需要单独确认影响范围和数据保护条件。

## 更新触发条件

以下事实变化后必须更新本 Map：本地/远程入口、SSH 身份、Compose 服务、运行目录、
部署调用图、CI 触发方式、数据库/Redis 边界、版本证据或已知生产偏差。
