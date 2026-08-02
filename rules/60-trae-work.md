# 60 TRAE Work 角色边界与 dev 提交约束

> 来源：AGENTS.md §八 + §九 分支模型（2026-08-02 收口为 dev-only）+ §七.21 提交安全 + §六 禁止行为
> 状态：生效（Phase 2 激活）

## 角色定义

TRAE Work 是日常开发执行角色，**默认直接在 `dev` 分支工作与提交**。

历史上 TRAE Work 使用系统生成的 `trae/agent-*` 内部分支；该分支模型自 2026-08-02 起废弃，
不得再创建 `trae/agent-*` 或任何其他新分支。详见 `90-deprecated-forbidden.md`。

## 提交模型（核心，dev-only）

- `dev` 是唯一默认日常开发分支，也是 CI 与手动部署的唯一来源；
- 开始任务时必须 `git fetch origin dev`；
- 必须确认当前分支为 `dev`，且 `origin/dev` 是当前 HEAD 的祖先：
  `git merge-base --is-ancestor origin/dev HEAD` 退出码为 0；
- 只有祖先检查通过才允许继续；
- 若 `origin/dev` 已前进、不是当前 HEAD 祖先，必须停止并报告，不得自行 merge、rebase 或覆盖；
- 完成后使用 `git push origin dev` 以 fast-forward 方式推送；
- **只允许 fast-forward；禁止 force push**；
- 未经用户明确授权禁止创建任何新分支（含 backup 分支）、禁止切换工作分支；
- 需要可恢复点时使用 checkpoint commit，而不是新建分支；
- 禁止 `git add -A` / `git add .` / `git add -u`；必须精确 `git add <file>`。

## 必须做

- 直接在 `dev` 分支开发；
- 遵循 `50-git-development-flow.md` 分支与提交安全；
- 修改前输出修改前最小报告（见 `00-core-governance.md`）；
- 修改后运行质量门禁（见 `40-testing-quality.md`）；
- 修改后新增 CHANGE 记录并更新 CHANGELOG；
- 修改后更新 `docs/maps`（`docs/current` 已标记 legacy 只读，不再要求同步）；
- 推送前确认当前分支为 `dev`，且 `git merge-base --is-ancestor origin/dev HEAD` 退出码为 0。

## 报告与对话输出（2026-07-29 收口）

> 详见 `rules/40-testing-quality.md`。
> 硬规则：禁止新建未经用户确认的报告/治理目录；TRAE 完整过程只在对话输出，不写入仓库；
> 普通Bug由Git历史记录，只有重要行为变化才写一个CHANGE。历史 `reports/` 目录已删除。

## 禁止做

- 不切换分支（`git checkout` / `git switch` 到 `dev` 以外的分支）；
- 不创建任何新分支，包括 `trae/agent-*` 与 backup 分支；
- 不直接修改 main；
- 不接触生产服务器；
- 不连接生产数据库；
- 不连接飞书生产账户；
- 不执行 migration；
- 不执行部署脚本；
- 不修改 Compose / 部署脚本 / 服务器配置；
- 不启用 GitHub Actions；
- 不删除数据库卷 / 运行中容器 / 源码 / 生产数据；
- 未经明确授权不对任何分支 force push；
- 不批量 `git add`。

## 与 CN 的边界

- Work 完成开发并推送 `dev` 后，由 CN 决定是否部署；
- Work 不直接触发部署；
- Work 不接触 `/opt/panji-live` 运行目录；
- Work 不接触 `/opt/panji-deploy` 干净部署目录（PLANNED，当前未实现）。

## dev push 行为（当前）

当前 `git push origin dev` 只触发 CI 质量门禁，不触发自动部署。

自动部署为 PLANNED，未实现。详见 `80-deployment-data-safety.md` 与 `70-trae-cn.md`。
