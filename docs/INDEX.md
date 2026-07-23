# docs/ 权威层级入口

本文件是 `docs/` 目录的权威入口，描述各子目录职责与阅读顺序。
维护者：架构收敛 PRD V2.0 §7.1。最后更新：CP-14。

## 目录层级

```text
AGENTS.md                 仅稳定护栏（项目开发与文档一致性规则）
docs/INDEX.md             本文件 — 权威层级入口
docs/contracts/           机器可执行合同（6 份，CP-14 新增）
docs/current/             当前生产行为（9 份必需文件 + MANIFEST）
docs/decisions/           架构决策记录（ADR-*.md）
docs/runbooks/            操作手册（飞书/盘后/复权/部署）
docs/acceptance/          DoD 与验收矩阵
docs/maps/                调用链与代码位置（8 份必需 map）
docs/changes/             变更历史（CHANGELOG + CHANGE-TEMPLATE）
docs/work/                当前 PRD 与阶段计划
docs/evidence/            生产验收证据（绑定最终 merge SHA）
docs/archive/             归档历史文档
```

## 阅读顺序（新人 onboarding）

1. `AGENTS.md` — 项目规则与护栏
2. `docs/current/00-product-business.md` — 产品与业务
3. `docs/current/01-system-architecture.md` — 系统架构
4. `docs/maps/backend-module-map.md` — 后端模块地图
5. `docs/contracts/` — 机器可执行合同（6 份）
6. `docs/current/02-data-api-contracts.md` — 数据与 API 合同
7. `docs/current/05-testing-acceptance.md` — 测试与验收
8. `docs/runbooks/` — 操作手册

## 机器可执行合同（docs/contracts/）

PRD V2.0 §7.2 要求的 6 份合同文件：

| 文件 | 用途 | 对应代码 |
|------|------|----------|
| `node-cluster-input.yaml` | Node Cluster 输入合同（250/4000/2 completed qfq） | `_load_node_cluster_inputs` |
| `chart-frame.schema.json` | 图表 display frame 契约 | `build_display_frame` |
| `smc-events.schema.json` | SMC 事件输出 schema | `compute_smc_adapter` |
| `detail-entry-context.schema.json` | 详情入口上下文 schema | `DetailEntryContext` |
| `message-group.schema.json` | 飞书消息分组 schema | `MessageGroup` |
| `after-close-recovery.schema.json` | 盘后恢复与 lease fencing schema | `AfterCloseRecovery` |

## CI 门禁映射（docs/acceptance/ci-gates.md）

PRD V2.0 §7.3 的 8 条 CI 门禁规则及其执行测试见 `docs/acceptance/ci-gates.md`。

## 变更流程

修改 `docs/contracts/` 或 `docs/current/` 时，必须在 `docs/changes/` 创建 CHANGE 记录。
CI 门禁 `check_v2_docs_structure` 验证必需文件存在性。
