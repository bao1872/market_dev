# 60 TRAE Work 角色边界与分支模型

> 来源：AGENTS.md §八（TRAE Work 分支模型）+ §九 分支模型 + §七.21 提交安全 + §六 禁止行为
> 状态：生效（Phase 2 激活）

## 角色定义

TRAE Work 是日常开发执行角色，使用系统生成的 `trae/agent-*` 内部分支工作。

**TRAE Work 不固定直接工作在 dev 分支**，**不允许切换分支**。这是 TRAE Work 的正常机制。

## 分支模型（核心）

- `origin/dev` 是统一开发基线；
- TRAE Work 在系统生成的 `trae/agent-*` 内部分支中工作；
- 开始任务时必须 `git fetch origin dev`；
- 必须确认 `origin/dev` 是当前 HEAD 的祖先：`git merge-base --is-ancestor origin/dev HEAD` 退出码为 0；
- 只有祖先检查通过才允许继续；
- 若 `origin/dev` 已前进、不是当前 HEAD 祖先，必须停止并报告，不得自行 merge、rebase 或覆盖；
- 完成后使用 `git push origin HEAD:dev` 将当前 HEAD 以 fast-forward 方式推送到远程 dev；
- **只允许 fast-forward；禁止 force push**；
- 禁止 `git add -A` / `git add .` / `git add -u`；必须精确 `git add <file>`。

## 必须做

- 在 `trae/agent-*` 内部分支开发；
- 遵循 `50-git-development-flow.md` 分支与提交安全；
- 修改前输出修改前最小报告（见 `00-core-governance.md`）；
- 修改后运行质量门禁（见 `40-testing-quality.md`）；
- 修改后新增 CHANGE 记录并更新 CHANGELOG；
- 修改后更新 `docs/current` 与 `docs/maps` 保持六者对齐；
- 推送前确认 `git merge-base --is-ancestor origin/dev HEAD` 退出码为 0。

## 禁止做

- 不切换分支（`git checkout` / `git switch` 到其他分支）；
- 不直接修改 main；
- 不接触生产服务器；
- 不连接生产数据库；
- 不连接飞书生产账户；
- 不执行 migration；
- 不执行部署脚本；
- 不修改 Compose / 部署脚本 / 服务器配置；
- 不启用 GitHub Actions；
- 不删除数据库卷 / 运行中容器 / 源码 / 生产数据；
- 不 force push 已共享分支；
- 不批量 `git add`。

## 与 CN 的边界

- Work 完成开发并推送 `HEAD:dev` 后，由 CN 决定是否部署；
- Work 不直接触发部署；
- Work 不接触 `/opt/panji-live` 运行目录；
- Work 不接触 `/opt/panji-deploy` 干净部署目录（PLANNED，当前未实现）。

## dev push 行为（当前）

当前 `git push origin HEAD:dev` 只触发 CI 质量门禁，不触发自动部署。

自动部署为 PLANNED，未实现。详见 `80-deployment-data-safety.md` 与 `70-trae-cn.md`。
