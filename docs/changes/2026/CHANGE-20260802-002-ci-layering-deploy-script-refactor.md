# CHANGE-20260802-002 CI 三层重构 + 部署脚本结构重构 + 测试分类收口

| 项 | 值 |
|---|---|
| 日期 | 2026-08-02 |
| 类型 | architecture + ci + ops |
| 影响范围 | CI 工作流 / 部署脚本 / 测试分类 |
| 状态 | 代码完成；Fast CI 待 exact-SHA 验证；Registry 推送 `blocked_registry_auth`；本轮未执行生产部署 |
| 业务代码 | **零改动**（竞价权限、邀请码、bootstrap、Review 业务逻辑均未触碰） |

## 1. 为什么改

### 1.1 部署事故：整段部署逻辑静默未执行

2026-08-02 部署 `73a46ae` 时发生事故：镜像已构建成功，但容器仍运行旧 SHA，
`/health` 返回 200，全程无任何告警。

根因：旧 `scripts/ops/panji-test-deploy` 把约 389 行部署逻辑写在**未加引号的 heredoc**
里，经本地变量展开后用 `bash -s` 从 stdin 执行。该结构有三个致命缺陷：

1. 无法本地 `bash -n` / shellcheck 静态检查；
2. 失败时无行号、无阶段信息，无法定位；
3. 远端脚本内容经 stdin 传入，后续命令若读 stdin 会把剩余脚本吞掉——
   本次事故中整段 §8（`docker compose up -d`）就是这样被跳过的。

事后只能手工 `up -d --force-recreate` 补救。

### 1.2 CI 单体化：与变更无关的全量运行

旧 `ci.yml` 为单体结构，14 个 job 无条件全量运行。纯文档变更也会起 Postgres 容器、
跑完整 3674 条测试、执行前端全套构建。反馈慢，且资源消耗与变更规模完全脱钩。

### 1.3 测试分类缺失：PG 依赖与外部数据依赖混在一起

3674 条测试中，1178 条依赖真实 PostgreSQL、6 条依赖 mootdx 外部行情源，
但三者没有机器可判定的边界。后果：

- 本地 `PURE_UNIT_TEST=1` 运行时，依赖 PG 的测试会以 ImportError / KeyError 形式
  在**收集期**中断，而不是被干净地跳过；
- 外部数据源不可达导致的失败，与真实代码回归无法区分。

## 2. 改了什么

### 2.1 CI 拆为三层

| 层 | 文件 | 触发 | 职责 | 门禁 |
|---|---|---|---|---|
| Fast CI | `.github/workflows/ci.yml` | push dev / PR main | 按变更范围裁剪 | `CI Gate` |
| Release Gate | `.github/workflows/release.yml` | 手动 exact SHA | 全量 + 构建镜像 + manifest | `Release Gate` |
| Nightly | `.github/workflows/nightly.yml` | 每日 03:00 | 全量回归兜底 | `Nightly Summary` |

**Fast CI**：新增 `changes` job（纯 git diff，不依赖第三方 action）输出
`docs/backend/frontend/db/migration/deploy` 六个标志。始终运行架构规则、文档一致性、
测试白名单、治理规则；后端/前端/PG/Migration 检查按对应标志条件执行。
前端合并为单一 job，一次 `npm ci` 后依次跑 tsc/lint/contract/build。

**CI Gate 语义**（关键）：裁剪只能依据 `changes` 输出，不接受人工判断。
- 范围内 job 若 skipped → **失败**；
- 范围外 job 若实际运行且失败 → **失败**（说明 `if` 条件与 `changes` 输出不一致，属 CI 配置错误）。

**Release Gate**：校验 SHA ∈ origin/dev → 全量 PG 集成（断言 skipped=0）+ 关键
Playwright E2E + Migration 完整 base↔head 循环 → 构建 3 个不可变镜像 → 生成
release manifest（完整 SHA、镜像 repo/tag/digest、alembic head、构建时间）→ compose 部署演练。

**Nightly**：全量 pytest + Migration 完整循环 + 全量 Playwright + 前端全量 +
全仓 Ruff/Mypy 报告（观测项，不阻断）+ **external_data 独立分组**（不阻断，单独报告）。

### 2.2 部署脚本结构重构

新增 `scripts/ops/panji-deploy-remote.sh`（受版本控制的真实文件，12 个阶段）：

| 阶段 | 内容 |
|---|---|
| 0 | `flock` 部署互斥锁 |
| 1 | 资源门禁（改动任何状态**之前**） |
| 2 | 校验目标 SHA |
| 3 | checkout |
| 4 | `market.env` 原子更新（临时文件 + `mv`） |
| 5 | 获取镜像：Registry pull，或显式 `--allow-local-build` |
| 6 | Alembic migration（`</dev/null` 防 stdin 吞噬） |
| 7 | **一次性重建全部无状态服务**（`postgres`/`redis` 明确排除） |
| 8 | 逐服务校验镜像 SHA |
| 9 | 健康检查 |
| 10 | 写 manifest + state |
| 11 | 受控清理 |

配 `ERR` trap，失败输出阶段/行号/命令/退出码四项。

`scripts/ops/panji-test-deploy` 由 517 行缩减至 156 行：保留 preflight 与
SHA 祖先校验，传输远程脚本前先 `bash -n` 预检，执行后经公网 `/api/v1/version` 终校验。

**旧 heredoc 已完全删除。按变更文件推断部署范围的逻辑已完全移除**，
Release Gate 的 `deploy-drill` job 用 grep 断言其不会回潮。

### 2.3 测试分类

新增两个 marker，由 `backend/tests/conftest.py` 的 `pytest_collection_modifyitems` 统一判定：

| 类别 | marker | 运行位置 |
|---|---|---|
| PG 集成 | `postgres` | CI 临时容器 |
| 外部数据 | `external_data` | 仅 Nightly 独立分组 |
| 纯单元 | 无 | 所有层 + 本地 |

同时修复 `PURE_UNIT_TEST=1` 下的收集期中断：为 `test_async_engine` /
`TestAsyncSessionLocal` 提供占位实现，真正调用时抛出明确错误而非 ImportError。

新增**漏标检查** `_DB_SUSPECT_PATTERN`：报告"源码含连库调用但未被判定为 postgres"
的嫌疑用例，**只报告不自动补 marker**——自动补标会掩盖分类规则盲区，而暴露盲区正是它的目的。

## 3. 前后关键差异

| 维度 | 改前 | 改后 |
|---|---|---|
| 远程部署逻辑 | 本地 heredoc → `bash -s` stdin | 受版本控制文件 + `bash -n` 预检 |
| 部署失败定位 | 无行号无阶段 | ERR trap 输出阶段/行号/命令/退出码 |
| 部署服务范围 | 按 `git diff` 推断（会静默漏重建） | 一次性重建全部无状态服务 |
| 有状态服务 | 无明确排除 | `postgres`/`redis` 显式排除 |
| 镜像来源 | 服务器构建 | Registry pull 优先，服务器构建降级为显式过渡开关 |
| 纯文档变更的 CI | 起 PG 容器 + 全量 3674 条 | 仅 4 个常驻检查 job |
| PG job 测试范围 | 容器内跑全部 3674 条 | 仅 `-m postgres` 的 1178 条 |
| 本地纯单元 | 2 个模块收集期中断 | 0 error，2496 条干净执行 |
| 外部数据源失败 | 与代码回归混淆 | 独立分组，不阻断 |

## 4. 验证结果

| 项 | 结果 |
|---|---|
| 三个 workflow YAML 语法 | PASS（`yaml.safe_load` 解析并列出 job） |
| CI Gate 判定逻辑 | PASS（本地模拟 5 种 changes 场景，结论均正确） |
| 测试分类对账 | PASS：`postgres=1178 + pure_unit=2496 = 3674`，`external_data=6`，漏标嫌疑=0 |
| marker 过滤自洽 | PASS：4 种 `-m` 组合互补，计数一致 |
| 部署脚本语法 | PASS（`bash -n` 两个脚本） |
| 部署 dry-run | PASS（真实服务器执行全部 12 阶段；发现 15 个服务，13 个无状态重建，`postgres`/`redis` 正确排除） |

**部署 dry-run 的意义**：旧实现中静默跳过的 §8，在新结构下完整执行并输出，
证明 1.1 节的缺陷已修复。

## 5. 阻塞项

### 5.1 `blocked_registry_auth`

GHCR 凭据未配置。实测：生产服务器无 `ghcr.io` 登录态、本机 `gh` CLI 未认证、
`docker pull ghcr.io/...` 返回 401。

处置（未绕过）：
- Release Gate 完整构建镜像并生成 manifest，`pushed=false`、`digest` 字段留空；
- 推送步骤显式标记 `blocked_registry_auth` 并写入 Step Summary；
- **未**伪造 digest、**未**改用 image tar 旁路、**未**回退服务器构建。

部署侧以 `PANJI_ALLOW_LOCAL_BUILD=1` 过渡开关运行。凭据打通后：
`push_images` 默认改 true，并移除部署侧过渡开关。

### 5.2 遗留测试缺陷（本轮未修，属业务测试范畴）

| 用例 | 现象 | 判定 |
|---|---|---|
| `test_no_tushare_references_in_backend` | grep 未排除 `__pycache__`，匹配到 `.pyc` | 本地伪失败；CI 干净 checkout 不受影响 |
| `test_check_minute_freshness_recent` | `datetime.now()` 无时区，与被测代码 `Asia/Shanghai` aware 时间相减产生固定偏移 | 时间依赖缺陷，非外部数据依赖，**不应**标 `external_data` |

两者均为既有缺陷，与本轮改动无关（`git stash` 前后表现一致）。
未借 `external_data` marker 掩盖——该 marker 仅用于真实外部依赖。

## 6. 受影响契约

- `CI Gate` job 名称保持不变，兼容 `rules/40`、`AGENTS.md` 与分支保护配置的既有引用；
- `docker-compose.prod.yml` 未改动，Release Gate 通过 `--env-file` 注入构建参数，
  与生产部署同源；
- 数据库结构未变更，无新增 migration。

## 7. 关联文档

- `rules/40-testing-quality.md` §TQ-90~TQ-93（CI 三层结构、测试分类 marker、显式标注要求、分类基线）
- `rules/80-deployment-data-safety.md` §DS-90~DS-93（远程脚本受版本控制、禁止变更推断、镜像来源、部署互斥）
- `docs/maps/80-system-runtime.md` §14（CI 三层与部署脚本已核验实现）
- `docs/runbooks/production-deployment.md`（部署操作步骤）
