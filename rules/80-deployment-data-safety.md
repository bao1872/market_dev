# 80 部署与数据安全

> 来源：AGENTS.md §七.9-11、§七.22
> 状态：生效（Phase 2 激活）

## Migration 纪律

- 不得修改已发布历史 migration；
- 只允许新增前向 migration；
- 修改 migration 必须有 upgrade / downgrade / upgrade 验证。

## 测试期部署不备份数据库

测试期部署默认不备份数据库。

除非用户明确说"先备份数据库"，否则禁止：

- `pg_dump` / 大体积备份；
- 写入 `/root/backups` 或 `/root/web_dev/backups`。

当前物理机磁盘紧张，优先节省硬盘。

## Docker 镜像保护

`node:20-alpine` 是受保护基础镜像，拉取很慢。

禁止：

- 主动删除 `node:20-alpine`；
- `docker image prune -a`。

除非明确升级 Node 版本或镜像损坏，否则不要删除 `node:20-alpine`。

普通清理只允许：

- `docker builder prune -f`；
- `docker image prune -f`；
- `docker container prune -f`。

## Live Mount 部署规则

Live Mount 部署通过只读 bind mount 将运行时代码挂载到容器，实现代码热更新而无需重建镜像。

### 固定运行目录

`/opt/panji-live/{backend/app, backend/alembic, backend/alembic.ini, frontend/dist, RUNTIME_SHA}`

### 叠加配置

- `docker-compose.prod.yml` + `docker-compose.live.yml`；
- 不修改 prod 配置。

### 挂载权限

- 所有挂载为只读 (`:ro`)；
- backend + 所有 Python worker + capture worker 挂载 app / alembic / alembic.ini / RUNTIME_SHA；
- frontend 挂载 dist（保留 capture_static 嵌嵌挂载）。

### 同步脚本

- Live Mount 同步脚本使用 `rsync --delete`；
- 只复制运行必需文件（排除 .git / docs / tests / node_modules / 缓存）；
- 同步期间先停止应用容器。

### 部署脚本

- Live Mount 部署脚本编排完整流程：前端构建 → 同步 → config 校验 → alembic → force-recreate。

### 适用范围

- 纯 Python / 前端代码变更用 Live Mount；
- 依赖 / Dockerfile / 基础镜像变化必须重建镜像。

### 版本端点

`/version` 返回：

- `runtime_git_sha`（RUNTIME_SHA 文件）；
- `image_git_sha`（GIT_SHA 环境变量）；
- `deployment_mode`（live / image）。

验证部署时 `runtime_git_sha` 必须等于 main HEAD。

## 部署顺序与回滚

- 部署按 `backend → frontend → worker` 顺序，禁止并行；
- 镜像必须打 SHA 标签，便于回滚；
- 保留当前 + 1 rollback 镜像；
- 不可逆 migration 必须在 PR 描述中明确标注并提供 downgrade 步骤；
- migration 不自动回滚（自动部署 PLANNED 阶段同样不自动回滚 migration）。

## 自动部署（PLANNED）

> 提议中，当前未实现。详见 `70-trae-cn.md`。

- dev push 自动部署为 PLANNED；
- 当前 dev push 只触发 CI 质量门禁；
- 自动部署需要：`panji-deploy` 服务器用户 + SSH forced command + GitHub Environment + 部署锁 + 变更分类；
- 自动部署不自动回滚 migration；
- 自动部署不读取数据库秘密；
- 自动部署只部署 GitHub commit。
