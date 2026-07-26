# 盘迹规则体系（rules/）

> Phase 2 已激活：**rules/ 正式生效**
> 建立 CHANGE：CHANGE-20260726-001（Phase 1 建立并提取）
> 激活 CHANGE：CHANGE-20260726-002（Phase 2 AGENTS 精简 + rules 正式生效）

## 权威声明

- `AGENTS.md` 是项目入口、规则路由器和最高安全边界。
- `rules/` 是详细强制规则的正式位置，已正式生效。
- `AGENTS.md` 与 `rules/` 共同构成项目强制规则体系；当两者出现冲突时，以 `AGENTS.md` 第十节"最高风险禁止项"为最高边界，其余以更具体的一方为准。
- `docs/current/` 仍是当前项目事实源；`docs/maps/` 仍是代码地图源。
- `sync/` 不是正式真源，仅作为临时中转站与结构建议参考输入，不得作为运行时依赖。

## 内容来源

本目录规则按以下优先级提取与维护：

1. 用户当前明确要求；
2. 当前根 `AGENTS.md`（入口与路由器）；
3. 当前 `docs/current/MANIFEST.md` 与 `docs/current/*`；
4. 当前代码、测试与部署合同；
5. `sync/panji_agents_rules_maps_autodeploy_v2/rules/` 草案（结构与建议）。

`sync/` 草案只提供结构与建议，不得覆盖当前真实合同。冲突时采用当前正式合同，并在 `AGENTS-MIGRATION-MAP.md` 与 CHANGE 记录中标注。

## 文件索引

| 文件 | 主题 | 来源（AGENTS.md 章节） | 状态 |
|---|---|---|---|
| `00-core-governance.md` | 事实源优先级、闭环、修改前最小报告 | §一、§三、§四 | 生效 |
| `10-product-domain-invariants.md` | 产品边界、策略、DSA、自选与监控、飞书 | §七.1-4、§七.6 | 生效 |
| `20-market-data-indicators.md` | MDAS、复权、Node Cluster、SMC、AFC、Canonical、ChartSnapshot、板块同步、因子版本 | §七.5、§七.12-19、§七.23 | 生效 |
| `30-access-security.md` | Capture Token、权限隔离、生产秘密 | §七.7、§六.7、§六.10 | 生效 |
| `40-testing-quality.md` | CHANGE 必填、CI 门禁、质量门禁、测试纪律、ref 隔离测试 | §五、§七.20、§八、§六.6、§六.8、§七.8 | 生效 |
| `50-git-development-flow.md` | 分支、PR、提交安全、执行模式、继续执行 | §九、§七.21、§六.9 | 生效 |
| `60-trae-work.md` | TRAE Work 角色边界与自动分支模型 | Phase 2 确立 | 生效 |
| `70-trae-cn.md` | TRAE CN 多模式职责 | Phase 2 确立 | 生效 |
| `80-deployment-data-safety.md` | Migration、不备份、Docker 镜像保护、Live Mount | §七.9-11、§七.22 | 生效 |
| `85-server-directory-boundaries.md` | 三目录职责 | §七.22 + 提议 | 目标合同（PLANNED 部分标记） |
| `90-deprecated-forbidden.md` | 禁止行为清单、废弃项、禁止恢复项 | §六、§七.2、§七.6、§七.8、§七.14、§七.15、§七.18 | 生效 |
| `AGENTS-MIGRATION-MAP.md` | AGENTS 章节 → rules 映射表 | 全章节 | 生效 |

## 状态语义

- **生效**：规则为 CURRENT，必须执行。
- **目标合同**：规则作为目标定义生效，但其中标记 PLANNED 的目录或脚本尚未在服务器建设，不得描述为已存在。
- **PLANNED**：未来阶段提议，尚未实施，不得描述为已生效。

## PLANNED 项（未实施）

以下均为 PLANNED，不得描述为已生效或已存在：

- dev push 自动部署；
- `/opt/panji-deploy` 干净部署目录；
- forced-command SSH；
- GitHub 部署 secrets；
- Capability V2；
- 尚未在腾讯云建设的目录和脚本。

## 使用约束

- 本目录不写具体代码路径、当前数量、当前 SHA 或阶段性进度；这些属于 `docs/maps/` 或 `docs/changes/`。
- 本目录不描述 `sync/` 草案为已生效。
- 本目录不启用 GitHub Actions、不修改 Compose、不修改部署脚本。
- 自动部署、`/opt/panji-deploy`、`panji-deploy` 用户、SSH forced command、Capability V2 当前均为 PLANNED，未实现。

## 与现有文档的关系

- `AGENTS.md`：项目入口、规则路由器、最高安全边界。
- `docs/current/`：当前项目事实源。
- `docs/maps/`：代码地图源。
- `docs/changes/`：历史变更记录源。
- `docs/runbooks/`：操作手册源。
- 根 `maps/`：当前不创建（未来阶段评估）。
