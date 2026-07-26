# 盘迹规则体系（rules/）

> Phase 1 并行验证状态：**生效中（并行）**
> 建立 CHANGE：CHANGE-20260726-001

## 权威声明

- **当前根 `AGENTS.md` 仍具有最高正式权威**。
- **`rules/` 尚未替代 `AGENTS.md`**。
- 当前 `docs/current/` 仍是当前事实源；`docs/maps/` 仍是代码地图源。
- `sync/` 不是正式真源，仅作为本目录的结构与建议参考输入。
- 本目录与 `AGENTS.md` 并行存在，用于按主题拆分长期强制规则，便于检索与未来迁移。
- 当 `rules/` 与 `AGENTS.md` 出现冲突时，以 `AGENTS.md` 为准；本阶段不修改 `AGENTS.md`。

## 内容来源

本目录规则按以下优先级提取：

1. 用户当前明确要求；
2. 当前根 `AGENTS.md`（v3，含 §一-§十一与 §七 23 条硬规则）；
3. 当前 `docs/current/MANIFEST.md` 与 `docs/current/*`；
4. 当前代码、测试与部署合同；
5. `sync/panji_agents_rules_maps_autodeploy_v2/rules/` 草案（结构与建议）。

`sync/` 草案只提供结构与建议，不得覆盖当前真实合同。冲突时采用当前正式合同，并在 `AGENTS-MIGRATION-MAP.md` 与 CHANGE 记录中标注。

## 文件索引

| 文件 | 主题 | 来源（AGENTS.md 章节） | 状态 |
|---|---|---|---|
| `00-core-governance.md` | 事实源优先级、闭环、分层、单一代码源 | §一、§三、§四 + 提议 | 并行验证 |
| `10-product-domain-invariants.md` | 产品边界、策略、DSA、自选与监控、飞书 | §七.1-4、§七.6 | 并行验证 |
| `20-market-data-indicators.md` | MDAS、复权、Node Cluster、SMC、AFC、Canonical、ChartSnapshot、板块同步、因子版本 | §七.5、§七.12-19、§七.23 | 并行验证 |
| `30-access-security.md` | Capture Token、权限隔离、生产秘密 | §七.7、§六.7、§六.10 | 并行验证 |
| `40-testing-quality.md` | CHANGE 必填、CI 门禁、质量门禁、测试纪律、ref 隔离测试 | §五、§七.20、§八、§六.6、§六.8、§七.8 | 并行验证 |
| `50-git-development-flow.md` | 分支、PR、提交安全、执行模式、继续执行 | §九、§七.21、§六.9 | 并行验证 |
| `60-trae-work.md` | TRAE Work 角色边界（提议中） | 提议 | **PLANNED** |
| `70-trae-cn.md` | TRAE CN 角色多模式（提议中） | 提议 | **PLANNED** |
| `80-deployment-data-safety.md` | Migration、不备份、Docker 镜像保护、Live Mount | §七.9-11、§七.22 | 并行验证 |
| `85-server-directory-boundaries.md` | 三目录职责（提议中） | §七.22 + 提议 | **PLANNED** |
| `90-deprecated-forbidden.md` | 禁止行为清单、废弃项、禁止恢复项 | §六、§七.2、§七.6、§七.8、§七.14、§七.15、§七.18 | 并行验证 |
| `AGENTS-MIGRATION-MAP.md` | AGENTS 章节 → rules 映射表 | 全章节 | 并行验证 |

## 状态语义

- **并行验证**：规则内容从当前 `AGENTS.md` 真实提取，与 `AGENTS.md` 一致；`AGENTS.md` 仍是最高权威。
- **PLANNED**：规则为未来阶段提议，尚未在 `AGENTS.md` 中确立；本阶段不强制执行，不描述为已生效。

## 使用约束

- 本目录不写具体代码路径、当前数量、当前 SHA 或阶段性进度；这些属于 `maps/` 或 `CHANGE`。
- 本目录不描述 `sync/` 草案为已生效。
- 本目录不启用 GitHub Actions、不修改 Compose、不修改部署脚本。
- 自动部署、`/opt/panji-deploy`、`panji-deploy` 用户、SSH forced command、Capability V2 当前均为 PLANNED，未实现。

## 与现有文档的关系

- `AGENTS.md`：本阶段不修改，仍是最高正式规则入口。
- `docs/current/`：仍是当前事实源，本目录不替代。
- `docs/maps/`：仍是代码地图源，本目录不替代。
- `docs/changes/`：仍是历史变更记录源。
- `docs/runbooks/`：仍是操作手册源。
- 根 `maps/`：本阶段不创建（Phase 3 范围）。
