# 50 Git 与开发流程

> 来源：AGENTS.md §九、§七.21、§六.9
> 状态：生效（Phase 2 激活）

## 分支模型（dev-only）

仓库只保留三个长期分支，未经用户明确授权禁止创建任何新分支：

| 分支 | 职责 | 约束 |
|---|---|---|
| `dev` | 唯一默认日常开发分支 | 所有变更默认直接在 `dev` 提交；`dev` 是 CI 与手动部署的唯一来源；push `dev` 只触发 CI，不触发自动部署 |
| `main` | 阶段性稳定锚点 | 未经明确授权不得修改、合并或推送 |
| `experiments` | 明确授权下的隔离实验 | 不得作为生产部署来源；进入 `dev` 前必须明确审查 |

硬约束：

- 未经用户明确授权，禁止创建任何新的本地或远程分支，**包括 backup 分支**；
- 未经用户明确授权，禁止切换工作分支（`git checkout` / `git switch` 到 `dev` 以外的分支）；
- 未经用户明确授权，禁止对任何分支 force push；
- 需要可恢复点时，使用 **checkpoint commit** 而不是新建分支；
- 无法以 fast-forward 方式对齐时必须停止并报告，不得自行 merge、rebase 或覆盖。

## 提交说明要求

每个提交（或一组紧密相关提交的说明）必须能回答：

- 当前系统原来如何运行；
- 本次为什么修改；
- 修改了哪些代码 / docs/maps（`docs/current` 已标记 legacy 只读，不再要求同步）；
- 新增哪个 CHANGE；
- 是否改变 API / 数据模型 / Worker 或第三方集成；
- 测试结果；
- 是否仍有 Known Gap；
- 是否需要生产验证。

## 提交安全

禁止 `git add -A` / `git add .` / `git add -u`；必须精确 `git add <file>`。

不得提交：

- `.vscode/settings.json`；
- `.traeignore`；
- `node_modules/`；
- `.venv/`；
- `__pycache__/`；
- `*.py[cod]`；
- `.mypy_cache/`；
- `.pytest_cache/`；
- `.ruff_cache/`；
- `.coverage`；
- `coverage.xml`；
- `dist/`；
- `build/`；
- `*.log`；
- `*.csv`；
- `*.parquet`。

## 删除保护

未经用户明确授权禁止删除：

- 数据库卷；
- 运行中容器；
- postgres / redis 数据目录；
- node_modules；
- .venv；
- .git；
- 源码；
- 生产数据。

## 分支保护

- 未经明确授权修改、合并或推送 `main`：禁止；
- 未经明确授权创建任何新分支（含 backup 分支）：禁止；
- 未经明确授权切换到 `dev` 以外的工作分支：禁止；
- 对任何分支 force push：未经明确授权禁止；
- 以 `experiments` 作为生产部署来源：禁止；
- 为通过检查削弱 `check_docs_consistency.py`：禁止。

## 执行模式

### 继续执行模式

当任务 checkpoint 匹配时，不重复审计和规划，直接从断点继续。

断线恢复校验仅检查：

- 当前分支必须是 `dev`，并核对 HEAD；
- 未提交文件；
- 冲突标记；
- 编译；
- diff check。

通过后立即继续当前任务。禁止在继续模式下重新规划已完成步骤或全仓审计。

### 前台串行执行

默认前台串行执行测试和检查命令。

- 禁止强制 `nohup` 后台测试；
- 仅当单条命令预计超过 5 分钟且用户明确同意时才可使用后台日志方式；
- 测试组之间必须串行，禁止并行构建或并行测试。

## 角色与执行边界

> 详见 `60-trae-work.md` 与 `70-trae-cn.md`。

- 所有 AI 助手（CodeBuddy / Codex / TRAE Work / TRAE CN）默认直接在当前 `dev` 分支工作与提交；
- 未经明确授权不得创建新分支、不得切换工作分支；
- 完成后使用 `git push origin dev` 以 fast-forward 方式推送；
- 只允许 fast-forward；禁止 force push；
- 若 `origin/dev` 已前进、不是当前 HEAD 祖先，必须停止并报告；
- TRAE CN 保留开发、测试、部署、验收和运维全部能力；
- `dev` 是 CI 与开发部署的唯一来源；push `dev` 只触发 CI（诊断用途），不触发自动部署，也不作为部署前置条件；
- `main` 是阶段性稳定锚点，未经明确授权不得修改、合并或推送。
