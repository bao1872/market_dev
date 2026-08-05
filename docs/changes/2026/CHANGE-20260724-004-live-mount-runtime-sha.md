# CHANGE-20260724-004 — Live Mount：运行时 SHA 与部署模式

- 日期：2026-07-24
- 范围：`backend/app/api/health.py` 的 `/v1/version` 端点；部署脚本 `panji-deploy.sh` / `panji-verify-deploy.sh` / `panji-deploy.test.sh`；`docker-compose.verify.yml`
- 前序：无（与 Phase8A 集成同期引入，提交 `086ebce`）

---

## 0. 背景

V2.1 验证栈采用 **Live Mount** 策略：复用既有正式镜像作依赖底座，只读挂载已 checkout 的代码目录（`/root/web_dev_verify/backend/app`），并在容器启动时注入 `RUNTIME_SHA` 文件。镜像本身不再重新构建（`docker compose up -d` 不带 `--build`）。

为让运行中的容器能暴露「实际挂载运行的代码 SHA」，而不是仅暴露镜像构建时注入的 `GIT_SHA`，`/v1/version` 端点新增运行时 SHA 字段。

---

## 1. `/v1/version` 端点变更

`backend/app/api/health.py` 的 `version()` 端点：

- 优先读取 `/app/RUNTIME_SHA`（Live Mount 同步的运行时 SHA），缺失时回退到镜像环境变量 `GIT_SHA`。
- 新增返回字段：
  - `runtime_git_sha`：运行代码的实际 SHA（Live Mount 注入，或回退镜像 SHA）
  - `image_git_sha`：镜像构建时注入的 `GIT_SHA`
  - `deployment_mode`：`"live"`（存在 RUNTIME_SHA）/ `"image"`（仅镜像）/ `"native-development"`
- 兼容旧字段 `git_sha` 仍返回 `runtime_git_sha`。

---

## 2. 部署脚本约定

- `panji-verify-deploy.sh`：在精确检出验证 SHA 后生成 `RUNTIME_SHA` 文件内容（完整 40 位 SHA），挂载进容器 `/app/RUNTIME_SHA`；`GET /v1/version` 取其 `runtime_git_sha` + `deployment_mode=="live"` 作为部署校验探针（**非** `/v1/version.runtime_git_sha` 路径端点，该路径不存在）。
- `docker-compose.verify.yml`：无 `build:` 段，依赖底座镜像 + 只读代码挂载 + `RUNTIME_SHA` 挂载。

---

## 3. 验证结论

- 代码事实：`runtime_git_sha` / `image_git_sha` / `deployment_mode` 三字段在 `health.py` 真实返回，且全部部署脚本引用 `RUNTIME_SHA`（已 grep 确认）。
- 本 CHANGE 记录为后置补登：代码注释自 `086ebce` 起即以 `[CHANGE-20260724-004]` 标注，但记录文件此前缺失；本文件补齐以满足文档一致性门禁的 CHANGE 引用可达性要求。
- `db_tested=false` / `deployed=false`（本记录仅描述已落地代码行为，不含本轮验证栈部署）。
