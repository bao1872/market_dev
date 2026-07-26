# sync/ 临时中转站

> 状态：临时中转站，非正式真源

## 定位

`sync/` 是本地、TRAE Work、TRAE CN 之间的临时中转站，用于：

- 跨会话传递任务材料；
- 草案与结构建议（如 `panji_agents_rules_maps_autodeploy_v2/`）。

`sync/` 不再保存长期报告。长期报告统一写入 `reports/`（详见 `reports/README.md`）。

## 边界

- `sync/` **不是正式真源**；
- 正式代码和文档不得运行时依赖 `sync/`；
- `sync/` 不被 `backend/`、`frontend/`、`scripts/`、Compose、`AGENTS.md`、`rules/`、`docs/current/`、`docs/maps/` 作为运行时真源引用；
- 禁止在 `sync/` 保存密码、Token、SSH 私钥、数据库连接和数据备份；
- **不再创建 `sync/outbox/*.md` 报告文件**（已于 2026-07-26 迁移到 `reports/archive/2026/07/` 并删除 `sync/outbox/`）。

## 历史报告迁移

原 `sync/outbox/project-governance-audit.md` 与 `sync/outbox/project-governance-phase1.md` 已于 2026-07-26（CHANGE-20260726-003）迁移至：

- `reports/archive/2026/07/REPORT-20260726-001-governance-audit.md`
- `reports/archive/2026/07/REPORT-20260726-002-governance-phase1.md`

`sync/outbox/` 已删除。后续长期报告统一写入 `reports/current/`，详见 `reports/README.md`。

## 清理

中转材料完成迁移后应清理或归档到 `docs/changes/` 或 `docs/archive/`。`sync/` 不作为长期存储。
