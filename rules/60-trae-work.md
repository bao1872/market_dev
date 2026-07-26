# 60 TRAE Work 角色边界

> 来源：提议（基于 AGENTS.md §九 分支模型 + §七.21 提交安全 + §六 禁止行为）
> 状态：**PLANNED**（尚未在 `AGENTS.md` 确立）

> Phase 1 注：本文件为未来阶段提议。当前 `AGENTS.md` 未明确区分 TRAE Work 与 TRAE CN 角色。本阶段不强制执行，不描述为已生效。

## 角色定义

TRAE Work 是日常开发执行角色，固定在 dev 分支。

## 必须做

- 在 dev 分支开发；
- 遵循 `50-git-development-flow.md` 分支与提交安全；
- 修改前输出修改前最小报告（见 `00-core-governance.md`）；
- 修改后运行质量门禁（见 `40-testing-quality.md`）；
- 修改后新增 CHANGE 记录并更新 CHANGELOG；
- 修改后更新 `docs/current` 与 `docs/maps` 保持六者对齐。

## 禁止做

- 不切换到 main 分支；
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

- Work 完成开发并提交 dev 后，由 CN 决定是否部署；
- Work 不直接触发部署；
- Work 不接触 `/opt/panji-live` 运行目录（PLANNED，当前未实现）；
- Work 不接触 `/opt/panji-deploy` 干净部署目录（PLANNED，当前未实现）。

## dev push 行为（当前）

当前 dev push 只触发 CI 质量门禁，不触发自动部署。

自动部署为 PLANNED，未实现。详见 `80-deployment-data-safety.md` 与 `70-trae-cn.md`。
