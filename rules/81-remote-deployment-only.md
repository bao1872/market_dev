# 81 远程部署唯一性

本规则是 `rules/80-deployment-data-safety.md` 的部署位置专项补充，适用于所有 IDE、编码助手、自动化 Agent 和人工操作。

## 1. 唯一部署位置

盘迹所有部署只能发生在远程运行服务器 `panji-prod`。

“部署”包括但不限于：

- 生成或安装实际运行所使用的前端 `dist`；
- 同步代码到 `/opt/panji-live`；
- 构建或切换运行镜像；
- 执行 Alembic Migration；
- 执行 `docker compose up`、重启或重建应用容器；
- 写入 `RUNTIME_SHA` 或部署状态文件；
- 执行部署后的 health、ready、version、mount、Scheduler 和资源验收；
- 执行用于最终用户验收的远程运行实例更新。

上述动作必须由远程服务器仓库中的 `scripts/deploy/panji-deploy.sh` 完成。禁止在本地机器承担任何部署实现。

## 2. 本地机器的职责

本地机器只允许承担以下职责：

- 编辑代码和文档；
- 运行纯单元测试、静态检查、前端测试和本地构建验证；
- 提交并推送精确 SHA 到 `origin/dev`；
- 作为控制端运行 `scripts/ops/panji-test-deploy <FULL_SHA> [--dry-run]`；
- 通过 `panji-prod-preflight` 和 `panji-prod-ssh` 发起受控远程操作。

本地启动 Backend、Frontend 或 Capture 仅属于开发预览或调试，不属于部署，不得作为远程运行验收、浏览器最终验收或 `deployment_passed=true` 的证据。

本地执行 `scripts/ops/panji-test-deploy` 只是发起远程部署控制流程，不得称为“本地部署”。

## 3. 前端构建边界

- 本地 `npm`/Vite build 只用于代码质量验证；
- 实际部署使用的 `frontend/dist` 必须由远程服务器在检出目标 SHA 后生成并同步；
- 禁止把本地生成的 `dist` 通过 `scp`、`rsync`、压缩包或其他方式作为远程部署产物；
- 本地 build 通过不能替代远程 build、远程运行和浏览器验收。

## 4. Migration 与数据操作边界

- Migration 只能在远程服务器的受控部署流程中执行；
- 禁止把本地连接共享数据库后执行 `alembic upgrade` 称为部署；
- 本地不得连接共享业务数据库运行 pytest；获得明确授权的只读业务调试也不构成测试或部署完成；
- stock_core、chip、Review、bootstrap、publish、pointer、withdrawal 等业务数据动作不是代码部署，必须单独授权并在远程运行环境执行。

## 4.1 远程验证与稳定运行部署分离

- 远程验证栈（`panji-verify` project、验证数据库 `bz_stock_verify_<sha>`、验证前端、验证 Worker）是独立验证环境，不等同于稳定运行部署；但它只能在 `panji-prod` 由正式脚本创建与运行，本地只负责发起控制命令和 SSH Tunnel。
- 验证前端**必须**在服务器检出目标 SHA 后构建，本地 Vite 页面不能作为验收证据。
- 验证栈**不得**替代正式运行栈；正式运行栈（业务库 `bz_stock`、正式 Nginx 公网入口）不受影响、不被复用。
- 验证通过后才允许**同一 SHA** 部署正式运行栈；未通过前不得把验证库数据或验证容器当作正式资产。
- 验证授权不自动包含正式运行栈部署授权，也不包含任何 `bz_stock` 数据操作授权。
- 现有规则已要求所有最终验收必须发生在远程运行环境，本方向不变；验证栈是远程运行环境内的隔离实例，不是新环境类别。

## 5. 唯一调用链

```text
本地控制端：scripts/ops/panji-test-deploy <FULL_SHA> [--dry-run]
  -> scripts/ops/panji-prod-preflight
  -> scripts/ops/panji-prod-ssh
  -> 远程服务器：/root/web_dev/scripts/deploy/panji-deploy.sh
  -> 远程 Compose / Migration / Live Mount / restart / verification
```

本地控制端脚本必须保持“瘦客户端”：不得包含 Docker Compose、前端部署构建、Migration、Live Mount同步、容器重启或部署验收实现。

## 6. 禁止行为

禁止：

- 在本地运行盘迹应用 Docker Compose 并将其称为部署；
- 在本地构建业务镜像作为远程部署的正式镜像产物；
- 使用本地 `frontend/dist` 覆盖远程运行目录；
- 在本地执行 Migration 作为部署步骤；
- 把本地 Vite/Uvicorn 页面作为最终前端验收环境；
- 在计划、报告、PRD、Map、Runbook或代码注释中使用“本地部署”描述盘迹当前流程；
- 把“从本地发起远程部署”简写成“部署在本地”。

## 7. 术语合同

后续统一使用：

- **本地开发**：代码修改、本地测试、本地预览；
- **本地控制端**：发起SSH受控远程操作的终端；
- **远程部署**：在 `panji-prod` 上执行构建、同步、Migration、重启与验收；
- **远程运行环境**：用户最终进行前端和业务验收的唯一环境。

任何文档中的“本地入口”只能解释为“本地控制端入口”，不能解释为本地部署目标。

## 8. 完成判定

只有以下条件全部成立，才可报告部署完成：

- 目标完整 SHA 已推送到 `origin/dev`；
- 远程服务器仓库、`RUNTIME_SHA` 和版本接口均为目标 SHA；
- 远程 health/ready/mount/Scheduler/资源检查通过；
- 需要的远程前端构建和服务重启已完成；
- PostgreSQL、Redis、Umami和持久卷未被非必要重建或删除。

本地测试、本地build、本地页面可访问或本地控制命令返回成功，均不能单独作为部署成功证据。
