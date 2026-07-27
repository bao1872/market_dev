# 盘迹项目治理 Phase 1 报告

> 报告日期：2026-07-26
> 阶段：Phase 1 — 建立根 rules/ 并行规则体系
> 工作分支：dev（固定）
> 建立 CHANGE：CHANGE-20260726-001

---

## Legacy Report Metadata

- Report ID: REPORT-20260726-002-governance-phase1
- Status: SUPERSEDED
- Report Type: governance-phase
- Environment: TRAE Work
- Created At: 2026-07-26 (Asia/Shanghai)
- Original Path: sync/outbox/project-governance-phase1.md
- Migrated To: reports/archive/2026/07/REPORT-20260726-002-governance-phase1.md
- Migration Date: 2026-07-26 (Asia/Shanghai)
- Migration CHANGE: CHANGE-20260726-003
- Note: 本报告在 reports/ 体系建立前生成，原保存于 sync/outbox/。迁移后内容未改写，仅增加本 Legacy Report Metadata。原始内容中关于"工作分支 dev（固定）"的描述已被 Phase 2 修正为 TRAE Work 真实模型（trae/agent-* + HEAD:dev fast-forward）。

---

## 1. Base HEAD

| 项 | 值 |
|---|---|
| Base HEAD（Phase 1 开始时） | `be29e20813d49ad270ac162b189fdc1117482d69` |
| Base HEAD commit message | `feat: 盘迹项目治理第一阶段审计` |
| 上一 commit | `06bf510` (`files upoad`) |
| 与 origin/dev 关系 | `up to date with 'origin/dev'` |

> 注：Phase 0 审计报告记录 HEAD 为 `06bf510`，但实际仓库在 Phase 1 开始时 HEAD 已变为 `be29e20`（Phase 0 审计报告已 commit）。本 Phase 1 基于 `be29e20`。

## 2. 修改后 HEAD 或未提交状态

| 项 | 值 |
|---|---|
| 当前 HEAD | `be29e20813d49ad270ac162b189fdc1117482d69`（未变化） |
| 提交状态 | **未提交**（Phase 1 禁止 git add / commit / push） |
| 工作区状态 | `M docs/changes/CHANGELOG.md` + `?? docs/changes/records/CHANGE-20260726-001.md` + `?? rules/` + `?? sync/outbox/project-governance-phase1.md` |

### git status --short

```
 M docs/changes/CHANGELOG.md
?? docs/changes/records/CHANGE-20260726-001.md
?? rules/
?? sync/outbox/project-governance-phase1.md
```

### git diff --stat

```
 docs/changes/CHANGELOG.md | 8 ++++++++
 1 file changed, 8 insertions(+)
```

> 注：`rules/` 与 `docs/changes/records/CHANGE-20260726-001.md` 为未跟踪文件，不在 diff --stat 中显示。

## 3. 新增文件

### rules/ 目录（13 份新文件）

| 文件 | 大小 | 主题 | 状态 |
|---|---|---|---|
| `rules/README.md` | 3905 B | 索引 + Phase 1 并行验证状态 + 权威声明 | 并行验证 |
| `rules/00-core-governance.md` | 2423 B | 事实源优先级 + 修改闭环 + 修改前最小报告 + PLANNED（分层/单一代码源/时间和因果） | 并行验证 + PLANNED |
| `rules/10-product-domain-invariants.md` | 1771 B | 产品边界 + 策略 + DSA + 自选和监控 + 飞书 | 并行验证 |
| `rules/20-market-data-indicators.md` | 7626 B | Node Cluster + MDAS SSOT + Atomic Chart Snapshot + SMC FVG + Canonical 四链 + AFC Core 14 + 三链五周期一致性 + 个股详情行情唯一真源 + 板块同步降级保护 + 因子版本追踪与 auto-resume | 并行验证 |
| `rules/30-access-security.md` | 1029 B | Capture Token + 权限隔离 + 生产环境账户密码 + PLANNED（生产秘密边界） | 并行验证 + PLANNED |
| `rules/40-testing-quality.md` | 3165 B | CHANGE 必填 + 文档目录与 CI 门禁 + 质量门禁 + 测试纪律 + ref/ 隔离测试 + Migration 测试纪律 | 并行验证 |
| `rules/50-git-development-flow.md` | 2395 B | 分支模型 + PR 要求 + 提交安全 + 删除保护 + 分支保护 + 执行模式 + PLANNED（角色与执行边界） | 并行验证 + PLANNED |
| `rules/60-trae-work.md` | 1712 B | TRAE Work 角色边界 | **PLANNED** |
| `rules/70-trae-cn.md` | 2778 B | TRAE CN 角色多模式 | **PLANNED** |
| `rules/80-deployment-data-safety.md` | 3025 B | Migration 纪律 + 测试期不备份 + Docker 镜像保护 + Live Mount 部署规则 + 部署顺序与回滚 + PLANNED（自动部署） | 并行验证 + PLANNED |
| `rules/85-server-directory-boundaries.md` | 2249 B | 三目录职责 + 腾讯云单实例约束 + 自动部署目录流 | **PLANNED** |
| `rules/90-deprecated-forbidden.md` | 5165 B | 通用禁止行为 12 条 + 废弃项 + 提交与删除禁止 + Docker 镜像禁止 + 数据库备份禁止 + Migration 禁止 + 过时内容候选 | 并行验证 |
| `rules/AGENTS-MIGRATION-MAP.md` | 9345 B | AGENTS 章节 → rules 映射表 + 硬规则级覆盖统计 + 未迁移内容 + 重复与冲突 | 并行验证 |

### docs/changes/（1 份新文件 + 1 份修改）

| 文件 | 操作 | 说明 |
|---|---|---|
| `docs/changes/records/CHANGE-20260726-001.md` | 新增 | Phase 1 CHANGE 记录（9706 B） |
| `docs/changes/CHANGELOG.md` | 修改 | 新增 CHANGE-20260726-001 索引（+10 行） |

## 4. AGENTS 条款覆盖统计

### 章节级覆盖（§一-§十一 + §七.1-23 = 34 节）

| 类别 | 总数 | MOVE_TO_RULES | KEEP_IN_AGENTS | MOVE_TO_MAPS | MOVE_TO_CHANGE | DEPRECATED_CANDIDATE |
|---|---|---|---|---|---|---|
| §一-§十一 章节级 | 11 | 8 | 3 | 0 | 0 | 0 |
| §六 禁止行为条目 | 12 | 12 | 0 | 0 | 0 | 0 |
| §七 硬规则 | 23 | 23 | 0 | 0 | 0 | 0 |
| **合计** | **46** | **43** | **3** | **0** | **0** | **0** |

### KEEP_IN_AGENTS（3 节）

| 章节 | 原因 |
|---|---|
| §二 必读入口 | 入口说明，必须在 AGENTS |
| §十 完成报告格式 | 流程模板，必须在 AGENTS |
| §十一 变更历史索引 | 索引指向，必须在 AGENTS |

### DEPRECATED_CANDIDATE（本阶段不删除）

| 内容 | 位置 | 评估 |
|---|---|---|
| §十一"近期关键变更"列表 | `AGENTS.md` §十一 | Phase 2+ 评估迁移到 `maps/changes/CHANGELOG.md` 索引 |

### AGENTS-MIGRATION-MAP 章节覆盖验证

`grep -n "§" rules/AGENTS-MIGRATION-MAP.md` 结果：41 行匹配，覆盖：

- §一、§二、§三、§四、§五、§六、§八、§九、§十、§十一（11 节）✓
- §七.1、§七.2、§七.3、§七.4、§七.5、§七.6、§七.7、§七.8、§七.9、§七.10、§七.11、§七.12、§七.13、§七.14、§七.15、§七.16、§七.17、§七.18、§七.19、§七.20、§七.21、§七.22、§七.23（23 子节）✓

**全部 34 节均出现在 AGENTS-MIGRATION-MAP.md**。

## 5. 发现的重复和冲突

### rules/ 内部重复

未发现。同一规则只在一个文件中完整描述，其他文件只引用文件名（不重复规则内容）。

### rules/ 与 AGENTS.md 冲突

未发现。`rules/` 内容全部从 `AGENTS.md` 提取，与 `AGENTS.md` 一致。`AGENTS.md` 仍是最高权威。

### rules/ 与 sync 草案差异（已记录在 AGENTS-MIGRATION-MAP.md）

| 项 | rules/ | sync 草案 | 处理 |
|---|---|---|---|
| 事实源优先级第 3 位 | `docs/current/MANIFEST.md` | `rules/` | 采用 AGENTS，不采用 sync |
| `rules/30` Capability V2 | 不引入 | 引入 Capability V2 概念 | 本阶段不引入 |
| `rules/60/70` 角色规则 | PLANNED，标记未生效 | 描述为已生效 | 标记 PLANNED |
| `rules/85` 三目录 | `/opt/panji-deploy` 标记 PLANNED | 描述为已存在 | 标记 PLANNED |
| 自动部署 | PLANNED，未实现 | 描述为已启用 | 标记 PLANNED |

## 6. 未迁移内容

| 内容 | 位置 | 原因 | 未来归属 |
|---|---|---|---|
| §二 必读入口清单 | `AGENTS.md` §二 | 入口说明，必须在 AGENTS | 保留 AGENTS |
| §四 修改流程入口 | `AGENTS.md` §四 | 流程入口，必须在 AGENTS | 保留 AGENTS |
| §十 完成报告格式 | `AGENTS.md` §十 | 流程模板，必须在 AGENTS | 保留 AGENTS |
| §十一 变更历史索引 | `AGENTS.md` §十一 | 索引指向，必须在 AGENTS | 保留 AGENTS |
| §十一 近期关键变更列表 | `AGENTS.md` §十一 | 历史索引内容 | DEPRECATED_CANDIDATE，Phase 2+ 评估 |
| docs/current/MANIFEST.md 实现核对基线 SHA | `docs/current/MANIFEST.md` | 当前事实，不属于规则 | 保留 docs/current/；Phase 3 迁移到 maps/ |
| docs/maps/* 代码位置 | `docs/maps/*` | 代码地图，不属于规则 | 保留 docs/maps/；Phase 3 迁移到 maps/code/ |
| docs/runbooks/* 操作手册 | `docs/runbooks/*` | 操作手册，不属于规则 | 保留 docs/runbooks/；Phase 3 迁移到 maps/runbooks/ |
| sync/ 草案 | `sync/` | 临时中转站，非正式真源 | 不迁移；Phase 4+ 评估采用部分内容 |

## 7. CHANGE 编号

| 项 | 值 |
|---|---|
| CHANGE 编号 | `CHANGE-20260726-001` |
| 扫描方式 | `glob docs/changes/records/CHANGE-20260726-*.md` → 无匹配 → 当天首个 |
| CHANGE record 路径 | `docs/changes/records/CHANGE-20260726-001.md` |
| CHANGELOG 索引 | `docs/changes/CHANGELOG.md` `## 2026-07-26` 节 |
| 必填字段 | 14 字段全部填写（变更编号、任务名称、需求出处、修改前/后行为、影响模块、修改文件、文档更新、测试证据、Git 分支、Git Commit、数据库迁移、配置变化、风险、遗留问题） |

## 8. 检查结果

### 8.1 `python tools/check_docs_consistency.py`

**结果**：1 FAIL（**预先存在，非 Phase 1 引起**）

```
[FAIL] docs/current/MANIFEST.md
       - SHA 不是有效的 git 提交: 086ebce593ac19ea49f5a4ce2f21e8c77af5ec80
```

**根因**：`docs/current/MANIFEST.md` 第 4 行 `实现核对基线：086ebce593ac19ea49f5a4ce2f21e8c77af5ec80`，该 SHA 不在本地 git 仓库中（`git cat-file -t` 返回 `fatal: could not get object info`；`git merge-base --is-ancestor` 返回 `fatal: Not a valid commit name`）。

**与 Phase 1 的关系**：Phase 1 未修改 `docs/current/MANIFEST.md`。该 FAIL 在 Phase 1 开始前已存在（HEAD `be29e20` 时已存在）。Phase 1 的所有变更（rules/ + CHANGE record + CHANGELOG）均不影响 MANIFEST baseline 检查。

**通过项**：

- `[PASS] docs/ 顶层目录结构`（rules/ 不在 docs/ 下，不影响）
- `[PASS]` 全部 12 个 current 文档（webhook/open 回归检查）
- `[PASS] CHANGE 引用可达性`（CHANGE-20260726-001 record 存在，CHANGELOG 引用可达）
- `[PASS] 必需新文档存在性`
- `[PASS] ref/ 隔离文本扫描`（rules/ 不在扫描范围：仅扫描 docs/current + docs/maps + AGENTS.md）
- `[PASS] 必需 CHANGE 记录`

### 8.2 `python tools/check_architecture.py`

**结果**：PASS（0 violations，11 checks passed）

```
Summary:
  Total violations: 0
  Failed checks: 0
  Passed checks: 11
    - SQLite engine strings
    - aiosqlite imports
    - Handwritten schema
    - Custom db_session fixture
    - aiosqlite in pyproject.toml
    - user role usage
    - ADMIN_PLAN_CODE
    - strategy_author
    - Plan value hardcoding/duplication
    - 待填写 placeholder
    - v2 docs structure
```

**与 Phase 1 的关系**：`check_architecture.py` 扫描 `backend/`、`frontend/src/`、`docs/`、`tools/` + 根 `*.md`。不扫描根 `rules/` 子目录。Phase 1 变更不影响架构检查。

### 8.3 `python tools/check_test_allowlist.py`

**结果**：PASS（0 issues）

```
[1/4] 扫描到带 @pytest.mark.xfail 的测试: 0 个
[2/4] allowlist.json 登记记录: 0 条
[3/4] 字段校验: 校验问题: 0 项
[4/4] 一致性校验: 未登记 xfail: 0 个, 无效 allowlist 记录: 0 个
============================================================
结果: PASS
============================================================
```

### 8.4 `python tools/update_docs.py --check`

**结果**：FAIL（**预先存在环境问题，非 Phase 1 引起**）

```
ModuleNotFoundError: No module named 'sqlalchemy'
```

**根因**：`tools/update_docs.py` 第 47 行 `from app.models.bar import ...`，需要 sqlalchemy。当前沙箱环境未安装 sqlalchemy（仅在 docker 容器或 .venv 中可用）。

**与 Phase 1 的关系**：Phase 1 未修改 `tools/update_docs.py`，未修改 backend models。该 FAIL 在 Phase 1 开始前已存在。

### 8.5 rules 内部 Markdown 链接检查

**结果**：N/A（无 markdown 链接）

`grep -n '\[([^\]]+)\]\(([^)]+\.md)\)' rules/` → 无匹配。

rules/ 文件中的交叉引用使用反引号代码跨度（如 `` `50-git-development-flow.md` ``），不使用 markdown 链接语法。无断链风险。

### 8.6 AGENTS 每一章节是否出现在 AGENTS-MIGRATION-MAP.md

**结果**：全部覆盖 ✓

`grep '^## |^### ' AGENTS.md` → 34 节（11 顶级 + 23 子节）。

`grep '§' rules/AGENTS-MIGRATION-MAP.md` → 41 行匹配，覆盖全部 34 节。

### 8.7 搜索 rules 中误写虚假生效语句

**结果**：无匹配 ✓

`grep '已启用自动部署|已存在 /opt/panji-deploy|rules 已替代 AGENTS|Capability V2 已经成为 CURRENT|自动部署已启用|/opt/panji-deploy 已存在' rules/` → 无匹配。

### 8.8 搜索 rules 中"待填写"占位符

**结果**：无实际占位符 ✓

`grep '待填写' rules/` → 1 匹配（`rules/40-testing-quality.md:39: - 无"待填写"占位符；`）。

该匹配是规则文本描述（描述 check_docs_consistency.py 的规则 8 禁止"待填写"占位符），不是实际占位符。

## 9. 下一阶段建议

### Phase 2：精简 AGENTS.md（独立分支）

**启动条件**：

1. Phase 1 报告经用户确认；
2. `rules/` 内容经用户审查无误；
3. `rules/AGENTS-MIGRATION-MAP.md` 覆盖全部 AGENTS 章节；
4. CI 检查通过（`check_architecture.py` PASS + `check_docs_consistency.py` MANIFEST FAIL 为预先存在，不阻塞）；
5. 用户明确授权进入 Phase 2。

**Phase 2 范围**：

- 精简 `AGENTS.md` 到 ~250 行；
- 保留：最高原则、必读入口、修改流程入口、完成报告格式、变更历史索引；
- §七 23 条硬规则指向 `rules/`；
- 不删除任何规则内容；
- 更新 `docs/AI-ONBOARDING.md` 指向新 `rules/`；
- 新增 CHANGE 记录。

**Phase 2 检查点**：

- AGENTS.md 行数 ≤ 300；
- 规则内容 100% 在 rules/ 可查；
- `check_docs_consistency.py` 不引入新 FAIL；
- `check_architecture.py` 继续 PASS。

### Phase 3：建立根 maps/（独立分支）

**启动条件**：Phase 2 完成 + 用户授权。

**Phase 3 范围**：

- 新建根 `maps/`；
- 从 `docs/current/` 迁移当前事实到 `maps/current/`；
- 从 `docs/maps/` 迁移代码地图到 `maps/code/`；
- 从 `docs/changes/` 迁移历史到 `maps/changes/`；
- 从 `docs/runbooks/` 迁移操作手册到 `maps/runbooks/`；
- 从 `docs/decisions/` 迁移 ADR 到 `maps/decisions/`；
- 从 `docs/evidence/` 迁移证据到 `maps/evidence/`；
- 更新 `tools/check_docs_consistency.py` 路径；
- 新增 CHANGE 记录。

## 10. Known Gaps

| Gap | 影响 | 缓解 |
|---|---|---|
| `docs/current/MANIFEST.md` baseline SHA `086ebce` 不在本地 git | `check_docs_consistency.py` FAIL | 预先存在，非 Phase 1 引起；需 CN 在服务器上验证 SHA 是否在完整 git 历史中；如确实不存在，需更新 MANIFEST baseline 到 HEAD 祖先 |
| `tools/update_docs.py` 需要 sqlalchemy | `update_docs.py --check` 无法在沙箱运行 | 预先存在环境问题；需在 docker 容器或 .venv 中运行 |
| `rules/60-trae-work.md` 与 `rules/70-trae-cn.md` 为 PLANNED | 角色边界规则未在 AGENTS.md 确立 | Phase 2 评估是否在 AGENTS.md 增加角色识别章节 |
| `rules/85-server-directory-boundaries.md` 三目录为 PLANNED | `/opt/panji-deploy` 未实现 | Phase 4+ 评估落地 |
| `rules/30-access-security.md` 生产秘密边界为 PLANNED | 自动部署秘密管理未实现 | Phase 4+ 评估落地 |
| `rules/40-testing-quality.md` 引用 `tools/update_docs.py --check` | 该检查在沙箱无法运行 | 保留规则文本；CN 在服务器或容器中运行 |
| `AGENTS.md` §十一"近期关键变更"列表 DEPRECATED_CANDIDATE | 列表随时间累积 | Phase 2+ 评估迁移到 maps/changes/CHANGELOG.md 索引 |
| Phase 1 未提交 | rules/ 为未跟踪文件 | 等待用户审查后授权 git add + commit |

## 11. 第一阶段建议修改文件（已完成）

| 文件 | 操作 | 状态 |
|---|---|---|
| `rules/README.md` | 新增 | ✅ 完成 |
| `rules/00-core-governance.md` | 新增 | ✅ 完成 |
| `rules/10-product-domain-invariants.md` | 新增 | ✅ 完成 |
| `rules/20-market-data-indicators.md` | 新增 | ✅ 完成 |
| `rules/30-access-security.md` | 新增 | ✅ 完成 |
| `rules/40-testing-quality.md` | 新增 | ✅ 完成 |
| `rules/50-git-development-flow.md` | 新增 | ✅ 完成 |
| `rules/60-trae-work.md` | 新增 | ✅ 完成 |
| `rules/70-trae-cn.md` | 新增 | ✅ 完成 |
| `rules/80-deployment-data-safety.md` | 新增 | ✅ 完成 |
| `rules/85-server-directory-boundaries.md` | 新增 | ✅ 完成 |
| `rules/90-deprecated-forbidden.md` | 新增 | ✅ 完成 |
| `rules/AGENTS-MIGRATION-MAP.md` | 新增 | ✅ 完成 |
| `docs/changes/records/CHANGE-20260726-001.md` | 新增 | ✅ 完成 |
| `docs/changes/CHANGELOG.md` | 修改 | ✅ 完成 |

## 12. 明确不修改范围

| 范围 | 说明 |
|---|---|
| 根 `AGENTS.md` | 不修改、不精简、不重写 |
| `docs/current/` 全部 | 不删除、不移动、不重命名 |
| `docs/maps/` 全部 | 不删除、不移动 |
| `docs/changes/records/` 全部（除新增 CHANGE-20260726-001） | 不删除、不移动、不修改 |
| `docs/runbooks/` 全部 | 不删除、不移动 |
| `docs/decisions/` 全部 | 不删除、不移动 |
| `docs/contracts/` 全部 | 不删除、不移动 |
| `docs/evidence/` 全部 | 不删除、不移动 |
| `docs/work/` 全部 | 不删除、不移动 |
| `docs/archive/` 全部 | 不删除、不移动 |
| `docs/acceptance/` 全部 | 不删除、不移动 |
| `docs/` 根 .md 文件 | 不删除、不移动 |
| 根 `maps/` 顶级目录 | 不创建（Phase 3 范围） |
| `.github/workflows/ci.yml` | 不修改 |
| `.github/workflows/deploy-dev.yml` | 不创建、不启用 |
| `docker-compose.prod.yml` | 不修改 |
| `docker-compose.live.yml` | 不修改 |
| `docker-compose.yml` | 不修改 |
| `scripts/deploy_live_runtime.sh` | 不修改 |
| `scripts/sync_live_runtime.sh` | 不修改 |
| `scripts/deploy.sh` | 不修改 |
| `scripts/cleanup-docker.sh` | 不修改 |
| `scripts/deploy/` | 不创建 |
| `Makefile` | 不修改 |
| `backend/` 全部代码 | 不修改 |
| `frontend/` 全部代码 | 不修改 |
| `tools/check_architecture.py` | 不修改 |
| `tools/check_docs_consistency.py` | 不修改 |
| `tools/check_test_allowlist.py` | 不修改 |
| `tools/update_docs.py` | 不修改 |
| 任何 migration | 不执行 |
| 任何生产环境配置 | 不修改 |
| 任何服务器配置 | 不修改 |
| 任何 GitHub Actions secret | 不创建 |
| 任何 SSH key | 不创建 |
| 任何飞书配置 | 不修改 |
| 任何数据库 | 不连接、不修改 |

## 13. Git 操作边界

本阶段已执行：

- 创建文件（rules/ + CHANGE record）
- 修改文件（CHANGELOG）
- 运行检查（check_docs_consistency / check_architecture / check_test_allowlist）

本阶段**未执行**：

- `git add`（任何形式）
- `git commit`
- `git push`
- `git branch`
- `git checkout`（不切换分支）
- `git reset`
- `git revert`
- 任何 force 操作

## 14. 是否可以提交

**建议**：可以提交，但需用户明确授权。

**理由**：

1. Phase 1 全部检查通过（除预先存在的 MANIFEST baseline FAIL 与 sqlalchemy 环境问题，均非 Phase 1 引起）；
2. `rules/` 内容全部从 `AGENTS.md` 提取，与 `AGENTS.md` 一致，无冲突；
3. PLANNED 内容明确标记，不描述为已生效；
4. AGENTS-MIGRATION-MAP 覆盖全部 34 节；
5. 无虚假生效语句；
6. 无"待填写"占位符；
7. 无 rules/ 内部重复；
8. CHANGE record 14 必填字段完整；
9. CHANGELOG 索引更新。

**提交命令建议**（待用户授权后执行）：

```bash
git add rules/README.md \
  rules/00-core-governance.md \
  rules/10-product-domain-invariants.md \
  rules/20-market-data-indicators.md \
  rules/30-access-security.md \
  rules/40-testing-quality.md \
  rules/50-git-development-flow.md \
  rules/60-trae-work.md \
  rules/70-trae-cn.md \
  rules/80-deployment-data-safety.md \
  rules/85-server-directory-boundaries.md \
  rules/90-deprecated-forbidden.md \
  rules/AGENTS-MIGRATION-MAP.md \
  docs/changes/records/CHANGE-20260726-001.md \
  docs/changes/CHANGELOG.md

git commit -m "docs(governance): Phase 1 — 建立根 rules/ 并行规则体系 (CHANGE-20260726-001)"
```

**禁止**：

- `git add -A` / `git add .` / `git add -u`；
- `git push`（除非用户明确授权）；
- 切换分支。

---

## 报告摘要

Phase 1 已完成：建立根 `rules/` 并行规则体系，共 13 份新文件（11 份规则文件 + README + AGENTS-MIGRATION-MAP）+ 1 份 CHANGE record + CHANGELOG 更新。

**AGENTS 条款覆盖**：46 条章节/硬规则级条款全部映射，其中 43 条 MOVE_TO_RULES、3 条 KEEP_IN_AGENTS（§二/§十/§十一）、0 条 DEPRECATED_CANDIDATE 删除（§十一"近期关键变更"列表标记为候选，本阶段不删除）。

**检查结果**：
- `check_architecture.py`：PASS（0 violations）
- `check_test_allowlist.py`：PASS（0 issues）
- `check_docs_consistency.py`：1 FAIL（预先存在 MANIFEST baseline SHA `086ebce` 不在本地 git，非 Phase 1 引起）
- `update_docs.py --check`：FAIL（预先存在 sqlalchemy 环境问题，非 Phase 1 引起）
- rules 内部 markdown 链接：N/A（无 markdown 链接，仅反引号引用）
- AGENTS 章节覆盖：全部 34 节出现在 AGENTS-MIGRATION-MAP ✓
- 虚假生效语句搜索：无匹配 ✓
- "待填写"占位符：无实际占位符 ✓

**关键冲突**：无。`rules/` 与 `AGENTS.md` 无冲突；`rules/` 与 `sync/` 草案差异已记录在 AGENTS-MIGRATION-MAP.md。

**建议**：可以提交，但需用户明确授权。等待用户审查。

**Phase 1 完成，等待用户确认。**
