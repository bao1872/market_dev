# AGENTS.md 条款迁移映射表

> Phase 1 并行验证状态：**生效中（并行）**
> 建立 CHANGE：CHANGE-20260726-001
> 当前根 `AGENTS.md` 仍是最高正式权威；本表只描述映射关系，不修改 `AGENTS.md`。

## 标记语义

- `KEEP_IN_AGENTS`：入口与流程说明，保留在 `AGENTS.md`；
- `MOVE_TO_RULES`：长期强制规则，已映射到 `rules/`；
- `MOVE_TO_MAPS`：当前事实 / 代码位置，未来迁移到 `maps/`（Phase 3）；
- `MOVE_TO_CHANGE`：历史变更记录，已在 `docs/changes/`；
- `DEPRECATED_CANDIDATE`：过时候选，本阶段不删除，Phase 2+ 评估。

## 章节级映射

| 原 AGENTS 章节 | 核心内容 | Phase 1 归属 | 目标文件 | 是否已完整覆盖 | 备注 |
|---|---|---|---|---|---|
| §一 最高原则 | 修改闭环 + 六者对齐 | MOVE_TO_RULES | `rules/00-core-governance.md` | 是 | 闭环与六者对齐已完整迁移 |
| §二 必读入口 | AI-ONBOARDING / MANIFEST / RESTORE-CHECKLIST / AGENTS 必读 + docs 顶级目录白名单 | KEEP_IN_AGENTS + MOVE_TO_RULES | `AGENTS.md` §二 + `rules/40-testing-quality.md`（CI 门禁部分） | 是 | 入口保留在 AGENTS；docs 顶级目录白名单由 `check_docs_consistency.py` 规则 11 强制，rules/40 引用 |
| §三 事实源优先级 | 10 级优先级 | MOVE_TO_RULES | `rules/00-core-governance.md` | 是 | 完整迁移；sync 草案提议将 `rules/` 提到第 3 位，本阶段不采用 |
| §四 修改流程 | 动手前 10 项输出 | MOVE_TO_RULES + KEEP_IN_AGENTS | `rules/00-core-governance.md`（修改前最小报告）+ `AGENTS.md` §四（流程入口） | 是 | 原则迁移到 rules/00；流程入口保留 AGENTS |
| §五 CHANGE 规则 | 必填 14 字段 + check_docs_consistency 规则 12 | MOVE_TO_RULES | `rules/40-testing-quality.md` | 是 | 完整迁移 |
| §六 禁止行为 | 12 条禁止 | MOVE_TO_RULES | `rules/90-deprecated-forbidden.md` | 是 | 12 条完整迁移到 §通用禁止行为 |
| §七.1 产品边界 | 不做自动交易等 | MOVE_TO_RULES | `rules/10-product-domain-invariants.md` | 是 | 完整迁移 |
| §七.2 策略规则 | dsa_selector + watchlist_monitor；多策略组合废弃 | MOVE_TO_RULES | `rules/10-product-domain-invariants.md` + `rules/90-deprecated-forbidden.md` | 是 | 策略规则迁 10；废弃项迁 90 |
| §七.3 DSA 规则 | computable universe + partial_failed 不发布 | MOVE_TO_RULES | `rules/10-product-domain-invariants.md` | 是 | 完整迁移 |
| §七.4 自选和监控 | 自动进入监控 + 到期保留 | MOVE_TO_RULES | `rules/10-product-domain-invariants.md` | 是 | 完整迁移 |
| §七.5 Node Cluster 固定契约 | 250/4000/2 + 90 bar 隔离 | MOVE_TO_RULES | `rules/20-market-data-indicators.md` | 是 | 完整迁移 |
| §七.6 飞书 | Platform App only + 盘中监控触发口径 | MOVE_TO_RULES | `rules/10-product-domain-invariants.md` + `rules/90-deprecated-forbidden.md` | 是 | 接入规则迁 10；禁止恢复项迁 90 |
| §七.7 Capture Token | 仅 Capture API | MOVE_TO_RULES | `rules/30-access-security.md` | 是 | 完整迁移 |
| §七.8 ref/ 隔离 | 禁止运行时 import ref/ | MOVE_TO_RULES | `rules/90-deprecated-forbidden.md` + `rules/40-testing-quality.md` | 是 | 禁止项迁 90；测试纪律迁 40 |
| §七.9 Migration | 不修改已发布 + upgrade/downgrade/upgrade | MOVE_TO_RULES | `rules/80-deployment-data-safety.md` + `rules/90-deprecated-forbidden.md` | 是 | 部署安全迁 80；禁止项迁 90 |
| §七.10 测试期不备份 | 禁止 pg_dump | MOVE_TO_RULES | `rules/80-deployment-data-safety.md` + `rules/90-deprecated-forbidden.md` | 是 | 部署安全迁 80；禁止项迁 90 |
| §七.11 Docker 镜像保护 | node:20-alpine 受保护 | MOVE_TO_RULES | `rules/80-deployment-data-safety.md` + `rules/90-deprecated-forbidden.md` | 是 | 部署安全迁 80；禁止项迁 90 |
| §七.12 MDAS SSOT | MDAS 唯一行情出口 + 复权口径 + count-aware 回补 | MOVE_TO_RULES | `rules/20-market-data-indicators.md` | 是 | 完整迁移 |
| §七.13 Atomic Chart Snapshot | 单 MDAS 读取 + quote 唯一真源 | MOVE_TO_RULES | `rules/20-market-data-indicators.md` | 是 | 完整迁移 |
| §七.14 SMC FVG 排除 + 严格 time-key | FVG 完全排除 + 禁止 index fallback | MOVE_TO_RULES | `rules/20-market-data-indicators.md` + `rules/90-deprecated-forbidden.md` | 是 | 计算规则迁 20；禁止项迁 90 |
| §七.15 Canonical 四链统一调度 | 禁止绕过 Registry | MOVE_TO_RULES | `rules/20-market-data-indicators.md` + `rules/90-deprecated-forbidden.md` | 是 | 计算规则迁 20；禁止项迁 90 |
| §七.16 AFC Core 14 不可改 | 14 项不可修改 | MOVE_TO_RULES | `rules/20-market-data-indicators.md` | 是 | 完整迁移 |
| §七.17 三链五周期一致性 | profile_hash 一致 | MOVE_TO_RULES | `rules/20-market-data-indicators.md` | 是 | 完整迁移 |
| §七.18 个股详情行情唯一真源 | ChartSnapshot 唯一 + 禁止双源 | MOVE_TO_RULES | `rules/20-market-data-indicators.md` + `rules/90-deprecated-forbidden.md` | 是 | 计算规则迁 20；禁止项迁 90 |
| §七.19 板块同步降级保护 | pywencai 唯一源 | MOVE_TO_RULES | `rules/20-market-data-indicators.md` + `rules/90-deprecated-forbidden.md` | 是 | 计算规则迁 20；禁止项迁 90 |
| §七.20 文档目录与 CI 门禁 | check_docs_consistency.py 规则 | MOVE_TO_RULES | `rules/40-testing-quality.md` | 是 | 完整迁移 |
| §七.21 提交安全与执行模式 | 精确 git add + 前台串行 + 继续执行 | MOVE_TO_RULES | `rules/50-git-development-flow.md` | 是 | 完整迁移 |
| §七.22 Live Mount 部署规则 | 固定运行目录 + 只读挂载 + 同步脚本 + 部署脚本 + 版本端点 | MOVE_TO_RULES | `rules/80-deployment-data-safety.md` + `rules/85-server-directory-boundaries.md` | 是 | 部署规则迁 80；运行目录迁 85 |
| §七.23 因子版本追踪与 auto-resume | stamp_factor_reconciliation_version + lease_epoch fencing | MOVE_TO_RULES | `rules/20-market-data-indicators.md` | 是 | 完整迁移 |
| §八 质量门禁 | Ruff/Mypy/Docs/Arch/Allow/Sync + 前端测试 | MOVE_TO_RULES | `rules/40-testing-quality.md` | 是 | 完整迁移 |
| §九 分支与 PR | 独立分支 + PR 说明 | MOVE_TO_RULES | `rules/50-git-development-flow.md` | 是 | 完整迁移 |
| §十 完成报告格式 | 5 节报告 | KEEP_IN_AGENTS | `AGENTS.md` §十 | 是 | 流程模板保留 AGENTS |
| §十一 变更历史索引 | CHANGELOG 指向 + 近期关键变更列表 | KEEP_IN_AGENTS + MOVE_TO_CHANGE | `AGENTS.md` §十一 + `docs/changes/CHANGELOG.md` | 是 | 索引保留 AGENTS；历史在 docs/changes/；"近期关键变更"列表 DEPRECATED_CANDIDATE |

## 硬规则级覆盖统计

| 类别 | 总数 | MOVE_TO_RULES | KEEP_IN_AGENTS | MOVE_TO_MAPS | MOVE_TO_CHANGE | DEPRECATED_CANDIDATE |
|---|---|---|---|---|---|---|
| §一-§十一 章节级 | 11 | 8 | 3 | 0 | 0 | 0 |
| §六 禁止行为条目 | 12 | 12 | 0 | 0 | 0 | 0 |
| §七 硬规则 | 23 | 23 | 0 | 0 | 0 | 0 |
| **合计** | **46** | **43** | **3** | **0** | **0** | **0** |

> 备注：§十一 中"近期关键变更"列表被标记为 DEPRECATED_CANDIDATE，但不计入硬规则总数（属于索引内容）；本阶段不删除。

## 未迁移内容

本阶段以下内容未迁移到 `rules/`，保留在原位置：

| 内容 | 位置 | 原因 | 未来归属 |
|---|---|---|---|
| §二 必读入口清单 | `AGENTS.md` §二 | 入口说明，必须在 AGENTS | 保留 AGENTS |
| §四 修改流程入口 | `AGENTS.md` §四 | 流程入口，必须在 AGENTS | 保留 AGENTS |
| §十 完成报告格式 | `AGENTS.md` §十 | 流程模板，必须在 AGENTS | 保留 AGENTS |
| §十一 变更历史索引 | `AGENTS.md` §十一 | 索引指向，必须在 AGENTS | 保留 AGENTS |
| §十一 近期关键变更列表 | `AGENTS.md` §十一 | 历史索引内容 | DEPRECATED_CANDIDATE，Phase 2+ 评估迁移到 `maps/changes/CHANGELOG.md` 索引 |
| docs/current/MANIFEST.md 实现核对基线 SHA | `docs/current/MANIFEST.md` | 当前事实，不属于规则 | 保留 docs/current/；Phase 3 迁移到 maps/ |
| docs/maps/* 代码位置 | `docs/maps/*` | 代码地图，不属于规则 | 保留 docs/maps/；Phase 3 迁移到 maps/code/ |
| docs/runbooks/* 操作手册 | `docs/runbooks/*` | 操作手册，不属于规则 | 保留 docs/runbooks/；Phase 3 迁移到 maps/runbooks/ |
| sync/ 草案 | `sync/` | 临时中转站，非正式真源 | 不迁移；Phase 4+ 评估采用部分内容 |

## 重复与冲突

本阶段未发现 `rules/` 内部规则重复（同一规则只在一个文件中完整描述，其他文件只引用）。

`rules/` 与 `AGENTS.md` 无冲突：`rules/` 内容全部从 `AGENTS.md` 提取，`AGENTS.md` 仍是最高权威。

`rules/` 与 `sync/` 草案差异：

| 项 | rules/ | sync 草案 | 处理 |
|---|---|---|---|
| 事实源优先级第 3 位 | `docs/current/MANIFEST.md` | `rules/` | 采用 AGENTS，不采用 sync |
| `rules/30` Capability V2 | 不引入 | 引入 Capability V2 概念 | 本阶段不引入 |
| `rules/60/70` 角色规则 | PLANNED，标记未生效 | 描述为已生效 | 标记 PLANNED |
| `rules/85` 三目录 | `/opt/panji-deploy` 标记 PLANNED | 描述为已存在 | 标记 PLANNED |
| 自动部署 | PLANNED，未实现 | 描述为已启用 | 标记 PLANNED |
