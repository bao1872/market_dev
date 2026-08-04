# 90 废弃与禁止

> 来源：AGENTS.md 基础安全边界与项目废弃合同

## 通用禁止行为（AGENTS §六）

1. 未读 AI-ONBOARDING 和 MANIFEST 就修改：禁止；
2. 根据旧 `docs/current/00-18` 或 archive 修改当前系统：禁止；
3. 根据旧聊天记忆覆盖 current：禁止；
4. 只改代码不改文档 / 只改 current 不改 CHANGE / 改代码结构不更新 maps：禁止；
5. 复制旧实现形成第二条路径 / 在前端重新实现后端业务规则：禁止；
6. 删除测试以适配错误实现 / 修改 API 不检查前端调用 / 修改数据模型不检查 migration：禁止；
7. 修改 Worker 不检查幂等、心跳、重试 / 修改权限不检查用户隔离：禁止；
8. 把 Mock E2E 说成真实生产 E2E / 把 OPEN 问题写成最终结论 / 把临时实验写成永久规则：禁止；
9. 未经明确授权修改 / 合并 / 推送 main、对任何分支 force push、创建新分支（含 backup 分支）、切换到 `dev` 以外的工作分支、为通过检查削弱 `check_docs_consistency.py`：禁止；
10. 未经许可修改生产环境账户密码：禁止；
11. 生产代码 / 测试 / 工具 / 构建脚本在运行时 `import` / `open` / `read` / `glob` `ref/` 目录：禁止；
12. `git add -A` / `git add .` / `git add -u` 批量暂存：禁止（必须精确 `git add <file>`）。

## 废弃项（禁止恢复）

### 多分支工作流与工具自动内部分支（2026-08-02 收口）

每个变更使用独立分支（`fix/` `feat/` `docs/` `refactor/` `chore/` `experiment/` 前缀）
与由编辑器/Agent 自动创建的内部工作分支（如 `trae/agent-*`）模型均已废弃。

- 仓库只保留 `main` / `dev` / `experiments` 三个长期分支；
- 所有执行主体默认直接在 `dev` 提交，需要可恢复点时使用 checkpoint commit；
- 不得从旧代码或旧文档恢复上述分支前缀约定；
- 详见 `50-git-development-flow.md`。

### 工具专属角色规则（2026-08-02 收口，CHANGE-20260802-005）

按 IDE / Agent / 模型 / 客户端划分的角色规则已废弃并删除
（原 `rules/60-trae-work.md`、`rules/70-trae-cn.md`）。

禁止恢复的设计：

- 按工具命名的角色定义与能力矩阵；
- 工具运行模式表（开发 / 测试 / 观察 / 部署 / 排障 / 紧急修复模式）；
- 一轮闭环模式与固定十步执行顺序；
- 固定 ledger 文件路径；
- 工具专属最终状态值（如 `CLOSURE_PASSED` / `PARTIAL` / `BLOCKED` 作为强制枚举）；
- 把某次具体业务任务的步骤写成长期规则。

替代合同：所有 IDE、编码助手和自动化 Agent 遵守同一套仓库规则，
治理按实际操作定义。有长期价值的部分已归入
`40-testing-quality.md`、`50-git-development-flow.md`、`80-deployment-data-safety.md`。
旧事实由 Git 历史与 `docs/changes/` 保存。

### 多策略组合（AGENTS §七.2）

多策略组合已废弃。

- 不得从旧代码或旧文档恢复；
- 当前生产只保留 `dsa_selector` 与 `watchlist_monitor`。

### feishu_webhook（AGENTS §七.6）

`feishu_webhook` / `FEISHU_WEBHOOK` 已废弃。

- 禁止恢复 `feishu_webhook` / `FEISHU_WEBHOOK`；
- 禁止独立管理员飞书 App；
- 禁止独立管理员接收人配置；
- 唯一接入方式：`feishu_platform_app`。

### ref/ 运行依赖（AGENTS §七.8）

`ref/` 目录已从运行依赖中隔离。

- 禁止生产代码 / 测试 / 工具 / 构建脚本在运行时 `import` / `open` / `read` / `glob` `ref/` 目录下任何文件；
- 禁止把 `ref/` 文件称为"真源"、"合同"、"fixture 生成器"或"运行依赖"；
- 应称为"参考源（人工阅读）"；
- SMC Pine parity 测试禁止从 DB 重新取 bar 或依赖 `ref/` 导出脚本。

### ref/sync git 跟踪禁止

`ref/` 与 `sync/` 不得进入 Git 仓库（所有活跃分支）。

- `ref/` 是本地参考资料（第三方项目 + 实验归档），保留本地实体但停止 Git 跟踪；
- `sync/` 是废弃临时中转站，从 Git 与本地同时删除；
- `.gitignore` 必须包含 `/ref/` 与 `/sync/` 两条根锚定规则；
- CI 必须显式检查 `git ls-files ref sync` 输出为空（`.github/workflows/ci.yml` governance-rules job）；
- `backend/tests/test_ref_isolation.py` 必须守护 `git ls-files ref/` 与 `git ls-files sync/` 均为空；
- 不得用 `git add -f` 强制添加 `ref/` 或 `sync/` 下任何文件；
- 不得把 `ref/` 描述为正式模块、正式实现入口或正式数据源；
- 例外：`archive/*` 标签中的历史提交不重写，仍保留旧版跟踪记录（只读历史）。

### SMC FVG（AGENTS §七.14）

Fair Value Gap 已完全排除。

- 禁止计算、返回、缓存、渲染 FVG；
- 禁止暴露 FVG 开关；
- 禁止生产计算路径包含 FVG 函数或状态；
- 禁止输出结构中存在 FVG 相关键、事件或 box。

### Canonical 绕过（AGENTS §七.15）

禁止生产模块直接 `import` kernel 绕过注册表。

- 详情 / 盘后 / 盘中 / Capture 四条调用链必须通过 `CanonicalComputationService` 调度已注册算法；
- 禁止四链直接调用 kernel；
- 禁止四链重算基础指标值（只能做适配：节奏 / 去重 / TTL / 截图）。

### 个股详情行情双源（AGENTS §七.18）

个股详情页行情双源已废弃。

- 禁止详情页同时调用 `/quote` 和 `/chart-snapshot`；
- 禁止恢复前端 `useRealtimeQuote`；
- 禁止恢复 `mergeRealtimeQuoteIntoBars()`；
- 禁止为 quote 增加第二次 Pytdx / Repository / MDAS 行情读取；
- 禁止从 1w / 1mo page_df 派生日行情兜底；
- 禁止 1m → 15m / 1m → 60m / 1m → 1d 聚合。

### 板块同步替代源（AGENTS §七.19）

板块同步替代数据源已废弃。

- 禁止增加 akshare；
- 禁止代理、IP 绕过；
- 禁止东方财富混用；
- 禁止新常驻 worker；
- pywencai 是唯一板块分类源。

## 提交与删除禁止（AGENTS §七.21）

- `git add -A` / `git add .` / `git add -u`：禁止；
- 不得提交：`.vscode/settings.json`、`.traeignore`、`node_modules/`、`.venv/`、`__pycache__/`、`*.py[cod]`、`.mypy_cache/`、`.pytest_cache/`、`.ruff_cache/`、`.coverage`、`coverage.xml`、`dist/`、`build/`、`*.log`、`*.csv`、`*.parquet`；
- 未经用户明确授权禁止删除：数据库卷、运行中容器、postgres / redis 数据目录、node_modules、.venv、.git、源码、共享开发业务数据。

## Docker 镜像禁止（AGENTS §七.11）

- 禁止主动删除 `node:20-alpine`；
- 禁止 `docker image prune -a`；
- 除非明确升级 Node 版本或镜像损坏，否则不要删除 `node:20-alpine`。

## 清理边界正向路径（2026-08-04 收口）

本节只回答「不能通用 prune，那到底该怎么清理」——把禁止项落回可安全执行的正向路径。

- 无用**镜像**：禁止通用 `docker image prune -a`；应走 `80-deployment-data-safety.md` DS-105 的旧 SHA 完整组精确回收（保留当前 / 上一成功 / rollback / 基础镜像）；
- 无用**容器**：禁止通用 `container prune`；应走 DS-106 只读盘点，满足全部前置条件且当轮授权后定向删除，Volume 永不随容器删除；
- **构建缓存 / 悬挂镜像**：仅在本轮实际构建镜像时允许 `docker builder prune -f` / `docker image prune -f`（见 DS-105 / 80 部署后回收）；
- **测试库**：禁止创建 / 复用任何独立、临时、CI 测试数据库或测试专用容器；唯一允许的两种测试模式见 `40-testing-quality.md` TQ-100（`PURE_UNIT_TEST=1` 与 `PANJI_SHARED_DEV_DB_TEST=1`）。此路径已永久废弃，禁止恢复。

## 数据库备份禁止（AGENTS §七.10）

- 测试期部署默认不备份数据库；
- 除非用户明确说"先备份数据库"，否则禁止 `pg_dump` / 大体积备份；
- 禁止写入 `/root/backups` 或 `/root/web_dev/backups`。

## Migration 禁止（AGENTS §七.9）

- 禁止修改已发布历史 migration；
- 只允许新增前向 migration；
- 修改 migration 必须有 upgrade / downgrade / upgrade 验证。

## 部署阶段与发布流程禁止（2026-08-02 开发治理收口）

盘迹当前只关心**开发阶段**，禁止定义或保留其他阶段的工作流程。以下流程当前与盘迹开发阶段无关，有效治理文档中不得描述、保留或改名为 deferred 后继续保留：

- **Release Gate**（`.github/workflows/release.yml` 的 `Release Gate` job / `release-gate` 门禁）；
- **GHCR / Registry / 镜像仓库推送**（如 `ghcr.io` push、registry digest）；
- **Release Manifest / immutable image release / formal release candidate**；
- **服务器只 pull 不 build**（Registry 凭据打通前的过渡开关）；
- **Fast CI / CI Gate 作为部署强制门禁**；
- **多阶段 delivery phase / 未来正式发布流程**；
- 新增 `development` / `runtime` / `formal_release` 等阶段状态机。

上述工作流文件已删除，禁止恢复。任何当前部署行为以
`rules/80-deployment-data-safety.md`、`docs/maps/80-system-runtime.md` 和
`docs/runbooks/development-deployment.md` 为唯一权威。
