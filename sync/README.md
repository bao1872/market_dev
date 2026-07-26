# sync/ 临时中转站

> 状态：临时中转站，非正式真源

## 定位

`sync/` 是本地、TRAE Work、TRAE CN 之间的临时中转站，用于：

- 跨会话传递任务材料；
- 草案与结构建议（如 `panji_agents_rules_maps_autodeploy_v2/`）；
- 阶段性报告（如 `outbox/`）。

## 边界

- `sync/` **不是正式真源**；
- 正式代码和文档不得运行时依赖 `sync/`；
- `sync/` 不被 `backend/`、`frontend/`、`scripts/`、Compose、`AGENTS.md`、`rules/`、`docs/current/`、`docs/maps/` 作为运行时真源引用；
- 禁止在 `sync/` 保存密码、Token、SSH 私钥、数据库连接和数据备份；
- 默认报告直接在 TRAE 对话中输出完整纯文本；
- 除非用户明确要求，不再创建 `sync/outbox/*.md` 报告文件。

## 历史报告

已经提交的历史报告（如 `outbox/project-governance-audit.md`、`outbox/project-governance-phase1.md`）保留作为历史记录，本轮不重写。

## 清理

中转材料完成迁移后应清理或归档到 `docs/changes/` 或 `docs/archive/`。`sync/` 不作为长期存储。
