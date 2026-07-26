# 50 Git 与开发流程

> 来源：AGENTS.md §九、§七.21、§六.9
> 状态：并行验证

## 分支模型

每个变更使用独立分支：

- `fix/<topic>`
- `feat/<topic>`
- `docs/<topic>`
- `refactor/<topic>`
- `chore/<topic>`
- `experiment/<topic>`

禁止直接改 main。

## PR 要求

PR 必须说明：

- 当前系统原来如何运行；
- 本次为什么修改；
- 修改了哪些代码 / docs/current / docs/maps；
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

- 直接修改 main：禁止；
- force push 已共享分支：禁止；
- 为通过检查削弱 `check_docs_consistency.py`：禁止。

## 执行模式

### 继续执行模式

当任务 checkpoint 匹配时，不重复审计和规划，直接从断点继续。

断线恢复校验仅检查：

- 分支 / HEAD；
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

## 角色与执行边界（PLANNED）

> 提议中，尚未在 `AGENTS.md` 确立。详见 `60-trae-work.md` 与 `70-trae-cn.md`。

- TRAE Work 固定在 dev 分支开发；
- TRAE CN 保留开发、测试、部署、验收和运维全部能力；
- 临时分支只由本地或 TRAE CN 按需使用；
- dev 是日常开发和未来自动部署线；
- main 是阶段性稳定锚点。
