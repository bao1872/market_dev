# 70 TRAE CN 角色多模式

> 来源：AGENTS.md §九（TRAE CN 能力边界）+ §七.10-11 部署数据安全 + §七.22 Live Mount
> 状态：生效（Phase 2 激活）

## 角色定义

TRAE CN 是开发、测试、部署、验收和运维全能力执行角色。

## 执行模式

CN 可按需切换以下模式：

| 模式 | 范围 | 边界 |
|---|---|---|
| 开发模式 | dev 分支开发 + 质量门禁 + CHANGE | 与 Work 一致 |
| 测试模式 | 运行定向测试 / 回归测试 / 合同测试 | 不部署到生产 |
| 观察模式 | 只读生产验证（`/version`、`/health`、日志查询、DB 只读查询） | 不修改任何配置 |
| 手动部署模式 | 调用部署脚本 / Live Mount 同步 / 镜像构建 | 必须用户明确授权 |
| 排障模式 | 日志分析 / Capture 缓存清理 / 飞书投递重发 | 不修改业务代码 |
| 紧急修复模式 | hotfix 分支 + 快速验证 + 部署 | 必须事后补 CHANGE + 文档对齐 |

## 必须做

- 部署前完成《待部署报告》所有验收项；
- 部署按 `backend → frontend → worker` 顺序，禁止并行；
- 镜像必须打 SHA 标签，便于回滚；
- 保留当前 + 1 rollback 镜像；
- 部署后验证 `/version` 与 `/health`；
- 部署后记录 evidence；
- migration 保持手动门禁；
- 任何不可逆 migration 必须在 PR 描述中明确标注并提供 downgrade 步骤。

## 报告与对话输出（2026-07-29 收口）

> 详见 `rules/40-testing-quality.md`。
> 硬规则：禁止新建未经用户确认的报告/治理目录；TRAE 完整过程只在对话输出，不写入仓库；
> 普通Bug由Git历史记录，只有重要行为变化才写一个CHANGE。历史 `reports/` 目录已删除。

## 禁止做

- 不在用户 API 请求链访问问财（板块同步降级保护）；
- 不增加 akshare、代理、IP 绕过、东方财富混用或新常驻 worker；
- 不删除 `node:20-alpine` 基础镜像；
- 不 `docker image prune -a`；
- 不 `pg_dump` 大体积备份（除非用户明确说"先备份数据库"）；
- 不写入 `/root/backups` 或 `/root/web_dev/backups`；
- 不修改已发布历史 migration；
- 不绕过 `check_docs_consistency.py`；
- 不 force push 已共享分支；
- 不批量 `git add`。

## 目录职责

> 详见 `85-server-directory-boundaries.md`。

- 开发目录：`/root/web_dev`；
- 自动部署干净目录：`/opt/panji-deploy`（PLANNED，当前未实现）；
- 运行目录：`/opt/panji-live`。

## 自动部署（PLANNED）

> 提议中，当前未实现。

- dev push 自动部署为 PLANNED；
- 当前 dev push 只触发 CI 质量门禁；
- 自动部署需要：`panji-deploy` 服务器用户 + SSH forced command + GitHub Environment + 部署锁 + 变更分类；
- 自动部署不自动回滚 migration；
- 自动部署不读取数据库秘密。

详见 `80-deployment-data-safety.md`。
