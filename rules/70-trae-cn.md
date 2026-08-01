# 70 TRAE CN 角色多模式

> 来源：AGENTS.md §九（TRAE CN 能力边界）+ §七.10-11 部署数据安全 + §七.22 Live Mount
> 状态：生效（Phase 2 激活）

## 角色定义

TRAE CN 是开发、测试、部署、验收和运维全能力执行角色。

## 一轮闭环规则（CHANGE-20260729-009 收口）

- 用户在单轮指令中明确给出"完整闭环"目标和"阶段间不得询问继续"的约束时，所有阶段视为同一发布目标的子步骤，不得在阶段之间停下询问"是否继续"。
- 硬阻塞仅限：可能破坏现有生产数据且无法通过代码/只读审计消除风险；权限或必要密钥确实缺失；修复后目标测试第二次仍失败；Mac 可用磁盘<12GiB 或 `memory_pressure` 红色；服务器可用磁盘<20GiB 或 MemAvailable<2.5GiB。某一外部事项阻塞时记录证据并继续完成其他事项，不得整体停止。

### macOS 内存规则（2026-07-31 补充）

- 本机为 MacBook Pro，**禁止按 Linux 方式使用"可用内存低于固定 GiB"或"Swap 超过固定百分比"判定 RED**。Swap 只记录，不作为阻塞依据。
- 开始、重任务前后、结束记录 `memory_pressure`、`memory_pressure -Q`、`vm_stat`、`sysctl vm.swapusage`、Top 20 RSS 进程、`df -h /` 及 `.git/.pytest_cache/.ruff_cache/.mypy_cache/frontend/dist/tmp` 大小。
- `memory_pressure` 绿色且稳定时允许单个目标测试、tsc 或 Vite；黄色时不启动全量测试/build/Docker，只做目标检查；红色时停止新重任务，结束本轮无用子进程并清理本轮临时文件。
- `tsc --noEmit` 和目标合同测试在黄色时不得跳过；Vite build 可交 CI。
- 禁止并行重任务、子智能体、npm install、依赖重装、git gc/stash/worktree、广域缓存清理、Docker volume 或基础镜像删除。
- 使用 `-p no:cacheprovider`；只清理本轮可重建缓存；非源码磁盘净增长必须 ≤ 0。
- Compact/上下文压缩发生后，只读取 `/tmp/trae_release_closure_ledger.md`（或同等位置的执行账本）继续工作，**禁止**重新 git status、重读 instruction、重复扫描已完成审计。账本必须每完成一步立即更新状态、命令、结果和测试次数。
- Compact/子代理恢复后禁止重新发现生产服务器入口：必须读取账本 + `docs/maps/80-system-runtime.md` §2 权威参数，并通过 `scripts/ops/panji-prod-preflight` 校验后继续；禁止猜测 SSH 别名（如 `panji-server`/`55-server`），禁止读取 `~/.ssh/config` 重新选择 Host。详见 `rules/80-deployment-data-safety.md` "生产服务器 SSH SSOT"。
- 已确认事实直接复用：代码级审计或 SQL 查询已确认的结论（数据版本一致性、字段空值根因、参数门槛定义等）在账本记录后不再重复验证。
- 测试必须进入正式测试文件（`backend/tests/` 或 `frontend/src/**/__tests__/`），禁止仅依赖临时 `python -c` 或 `node -e` 断言作为验收。
- 每个测试集最多 2 次。第一次失败只修相关问题再复跑一次，不得无限重跑。
- 最终只输出一次完整报告，禁止输出阶段性总结、选项或"是否继续"。页面和生产未验收前不得在 ledger 或对话输出中写 COMPLETED。
- 一轮闭环视为一个目标，不是多个独立子目标；不得套用"每轮只做一个子目标"的停止方式。

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

## 2026-08-01 收口（配合 CHANGE-20260801-001）

### TRAE-90 一轮闭环禁止的错误姿势

本轮四个用户问题（第一金字塔空值审计、详情同源、删除DSA列、复盘闭环+时间线负耗时）必须真实通过证据支持，以下为禁止姿势：

1. **不得中途报告"已完成"**：一轮闭环完成或被权限阻塞时只输出一次最终报告；不得逐文件/逐子任务写阶段性"成功"；
2. **不得把未验证的结论写成事实**：四问题通过前，不得在对话输出/ledger/CHANGE 中写 `DONE` / `CANARY_PASSED` / `COMPLETED` / `SUCCESS`；
3. **不得自动清理历史Canary证据**：旧 Canary namespace / run 数据保留用于对比，本轮不得 DELETE 或 PURGE 已存在的 canary 数据；
4. **不得创建本地测试库**：local Backend 必须连接正式 `bz_stock` 数据源，隔离边界为进程而非数据复制；本地测试只能纯单元/mock，不得连接正式库；
5. **不得本地启动 scheduler/盘后编排**：本机只允许启动 Backend、Frontend、Capture 和 SSH Tunnel，不得启动 Scheduler / 正式 Worker / 全市场盘后任务；
6. **不得并行重任务**：同一时刻只有一个 `pytest`、`tsc`、`vite build`、`docker build` 在运行；禁止并发子智能体、并行 Playwright 与 pytest；
7. **禁止扩大缓存基线 / 删除共享缓存**：pytest `-p no:cacheprovider`，不得删除 node_modules/.cache、.pytest_cache、.git 缓存目录；禁止 `git gc`、`git stash`、`git worktree add`；
8. **禁止 `scp / docker cp / 手工vi源码`部署**：部署只允许通过正式 `panji-test-deploy` 入口；
9. **禁止把局部Canary当整体成功**：TQ-83 同 `rules/40-testing-quality.md`；
10. **禁止把局部测试通过当CI全绿**：本地 33/33 node contract 通过 ≠ GitHub Actions CI Gate 通过；PG 集成必须 CI 真实跑且 0 skipped。

### TRAE-91 一轮闭环执行顺序（默认）

未获得用户明确变更执行顺序前，TRAE CN 默认按以下顺序执行：

```
Step 1: 事实审计（分支、SHA、工作区、SSH只读查询生产publication/run状态）
Step 2: 第一金字塔空值审计（SQL只读，6天×99字段×三层）
Step 3: 删除旧DSA-only列 + 后端无效链 deprecate
Step 4: 详情同源（左栏改用 /market/stocks + MCQ 规范查询）
Step 5: 复盘闭环（after_close 接入 computing_review 阶段+冷启动展示raw）
Step 6: 时间线负耗时修复（attempt隔离+时区统一+warnings字段）
Step 7: 测试与CI：Ruff/Mypy → tsc → 前端合同 → Playwright → Push dev → 监控 CI 全绿
Step 8: 部署：panji-test-deploy 正式入口（CI 全绿后）
Step 9: 浏览器验收：/market 筛选→详情左栏；列设置+导出；/review 五阶段；admin 时间线
Step 10: 更新PRD/Maps/Rules/Runbooks + 写唯一CHANGE + 输出最终一次性报告
```

若某一步骤遇到权限/外部系统不可用阻塞 → 跳过并记录，继续执行后续不依赖它的步骤；不得整体停下。

### TRAE-92 资源账本必须包含的条目

ledger 文件固定路径：`/tmp/trae_market_review_closure.md`

开始、每步重任务（pytest / tsc / build / Playwright / deploy）前后、结束必须记录：

```
- memory_pressure
- memory_pressure -Q
- vm_stat
- sysctl vm.swapusage
- Top 20 RSS (ps -axo pid,rss,comm | sort -k2 -rn | head -20)
- df -h /
- du -shc .git/ .pytest_cache/ .ruff_cache/ .mypy_cache/ frontend/dist node_modules tmp 2>/dev/null | tail -1
```

Compact/上下文压缩后 **只** 读取 ledger 继续；不得重新 git status 扫描全仓、不得重读 instruction.md、不得重新扫描已完成审计。

### TRAE-93 最终报告的状态值

只允许以下状态值之一：

| 状态 | 含义 |
|---|---|
| `CLOSURE_PASSED` | 四用户问题全部通过真实数据 + 浏览器验收 + CI 全绿 + 部署SHA一致 |
| `PARTIAL` | 1~3个用户问题通过，其余有明确根因/已修复但验收证据不足/遗留问题清单明确 |
| `BLOCKED` | 遇到不可绕过的外部权限/凭据/系统阻塞（如 GitHub token 缺失无法监控 CI，SSH key 缺失无法部署等） |

**禁止**使用：`DONE` / `CANARY_PASSED` / `SUCCESS` / `COMPLETED` / `OK` / `PASSED` 作为最终状态（除非四问题全部通过证据后写 `CLOSURE_PASSED`）。
- 自动部署不读取数据库秘密。

详见 `80-deployment-data-safety.md`。

## 闭环恢复与成功判定硬约束（2026-07-30 收口）

> 适用于一轮闭环、Compact 恢复和成功判定的硬约束。与上文"一轮闭环规则"叠加生效，冲突时以更严格一方为准。

1. **禁止临时生产脚本代替永久代码修复**：禁止用 `/tmp` Python、裸 SQL、`docker cp`、stdin 注入等临时生产脚本替代正式代码修复。发现闭环缺口必须走"代码修复 + 正式测试（`backend/tests/` 或 `frontend/src/**/__tests__/`）"，不得用临时脚本补生产状态。已在生产用临时脚本补过的状态必须回滚或转正后才能视为闭环。
2. **Compact 后只读 `/tmp/trae_panji_closure_ledger.md` 恢复上下文**：Compact（上下文压缩）发生后，只读取 `/tmp/trae_panji_closure_ledger.md` 恢复上下文，不得重新 `git status`、重读 instruction、重复扫描已确认内容或重新发现生产服务器入口（SSH 入口见 `rules/80-deployment-data-safety.md` "生产服务器 SSH SSOT"）。账本每完成一步立即更新状态、命令、结果和测试次数。该规则与上文"一轮闭环规则"中 `/tmp/trae_release_closure_ledger.md` 互斥：按当前正在执行的闭环目标读取对应账本，禁止交叉读取。
3. **成功判定三要素**：成功判定必须同时具备 pointer、版本、真实数据证据：
   - **pointer**：`factor_publications` 中对应 kind（`stock_core` / `market_aggregation` / `review` / `history_cross_section`）的 pointer 已切换至目标 run，且 `data_run_id` 指向本轮 run；
   - **版本**：repo HEAD（GIT_SHA）、`algorithm_version`、image tag、container env `GIT_SHA` 一致，`/version` runtime SHA 等于 main HEAD；
   - **真实数据证据**：DB 查询（pointer 行、`stock_feature_snapshot_run_items.status` 分布）或日志（worker heartbeat、publish 事件）证明发布已生效。
   - 禁止凭"页面能打开"或"`/health` 返回 200"单独判成功；这两者只能作为辅助证据，不能替代 pointer + 版本 + 数据证据三要素。

## 页面验收要求（2026-07-30 补充）

> 一轮闭环规则与 ledger 恢复规则见上文"一轮闭环规则"章节，此处仅补充页面验收要求。

- 涉及前端页面的任务，验收时必须真实登录浏览器完成 URL、页面、Console 和 Network 三类证据记录，不得以 IDE 截图或静态代码审查代替行为核验。
- **URL 验收**：记录目标路由实际访问 URL（含 query 参数），确认 hydration 后不被默认值覆盖，前进/后退能正确恢复状态。
- **Console 验收**：记录浏览器 Console 是否存在 error / warning，异常必须定位根因或明确标注为已知无关警告。
- **Network 验收**：记录关键 API 请求的状态码与响应摘要（200/4xx/5xx），不得仅凭页面渲染成功推断 API 正常。
- 页面和生产未验收前，不得在 ledger 或对话输出中写 COMPLETED。

## CI 监控硬规则（2026-07-31 补充，[CHANGE-20260731-002]）

> `git push` 不是任务完成。每次 Push 后必须监控该精确 SHA 直到 Workflow 终态。

1. **Push 不是完成**：`git push origin dev` 成功不等于 CI 通过，必须持续监控。
2. **精确 SHA 监控**：每次 Push 后必须监控该精确 commit SHA 直到 Workflow 运行至终态（success/failure）；不得用前一次 push 的 SHA 代替。
3. **查询优先级**：
   - GitHub 连接器（plugin 中的 GitHub app）
   - 已认证 `gh` CLI（`gh run list --branch dev --json status,conclusion,headSha,databaseId`）
   - 公开 GitHub REST API（`https://api.github.com/repos/{owner}/{repo}/actions/runs?head_sha={sha}`，无需认证）
4. **`gh` 未认证不能成为停止监控理由**：必须降级到公开 REST API 读取 Run 和 Job 状态；公开 API 有速率限制但仍可读取。
5. **轮询频率**：每 30–60 秒轮询一次；CI 远程监控是轻量 HTTP 操作，不受 macOS 黄色 Memory Pressure 和"本地测试最多两次"限制。
6. **失败后修复**：
   - 获取失败 Job/Step 的真实日志（优先 `gh run view <run_id> --log`，降级到公开 API 下载 logs）；
   - 按真实日志修复代码，不得凭猜测修改；
   - Push 新 SHA 后重新监控新 SHA 的 CI 终态。
7. **PG Job 被 skipped 视为 CI 失败**：禁止写"PG未确认"后结束；`postgres-integration-tests` 为 skipped 时必须排查前置依赖并修复。
8. **无法下载日志时**：仍必须报告已知失败 Job 名称和状态，并运行其准确本地等价命令（如 `PURE_UNIT_TEST=1 pytest tests/test_xxx.py`），不得把已知 failure 写成"CI不可见"。
9. **最终报告必须列出全部 Job 状态和 CI Gate 结果**：
   - 列出每个 Job 的 name 和 result（success/failure/skipped/cancelled）；
   - 列出 CI Gate 的最终 conclusion（success/failure）；
   - CI Gate=success 是部署前置条件，任何单个 Job 通过不能代替 CI Gate。
10. **CI Gate 拓扑**（[CHANGE-20260731-002]）：
    - `postgres-integration-tests` 独立运行，不再依赖 Ruff/Mypy；
    - `alembic-cycle` 独立运行；
    - `CI Gate` 是唯一阻断 Job，`if: always()`，needs 所有阻断 Job；
    - `Ruff Full Repository Report` 和 `Mypy Full Repository Report` 为报告型任务，不纳入 CI Gate；
    - 部署只认最终 SHA 的 `CI Gate=success`。
