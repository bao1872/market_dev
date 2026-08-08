# 50 Git 与开发流程

## 1. 分支模型

仓库长期分支：

| 分支 | 职责 |
|---|---|
| `dev` | 默认日常开发与远程开发部署来源 |
| `main` | 阶段性稳定锚点，未经授权不得修改 |
| `experiments` | 用户明确授权的隔离实验，不作为远程开发部署来源 |

未经用户明确授权：

- 禁止新建分支，包括 backup branch；
- 禁止切换到 `dev` 以外工作分支；
- 禁止 force push；
- 禁止 merge/rebase 覆盖共享历史。

需要恢复点使用 checkpoint commit。

## 2. 开始任务

代码任务开始时：

1. `git fetch origin dev`
2. 确认当前分支 `dev`
3. 确认本地与 `origin/dev` 的祖先关系
4. 检查工作树是否存在用户已有未提交修改

若无法安全 fast-forward / 对齐，STOP 并报告。

## 3. 修改范围

Exploration 默认只修改当前 hypothesis slice 需要的文件。

禁止：

- 顺手大重构；
- 修与当前 slice 无关的 P2/P3；
- 为未来可能需求增加 framework；
- 无关格式化导致巨大 diff。

## 4. 暂存

禁止：

- `git add -A`
- `git add .`
- `git add -u`

必须精确暂存目标文件。

## 5. 提交

每个提交应聚焦一个明确问题或一组紧密相关修改。

提交说明至少能回答：

- Why；
- What；
- 对应 PRD / Hypothesis；
- Tests；
- 是否有 Migration / Config；
- Known Gap / Deferred Debt。

普通 Exploration 小改不要求写 release-grade 报告。

## 6. Push

代码完成、必要测试通过后：

`git push origin dev`

只允许 fast-forward。

Push `dev` 本身不自动等同于：

- CI 通过；
- Runtime 已部署；
- 用户验收；
- Release。

## 7. Checkpoint

长任务可以在自然阶段建立 checkpoint commit。

checkpoint 用于：

- 防止本地工作丢失；
- 清晰恢复；
- 区分阶段。

禁止为了 checkpoint 新建分支。

## 8. Continue Mode

如果用户粘贴 IDE 上一阶段结果并要求继续：

- 核对当前 SHA/branch；
- 核对未提交 diff；
- 确认上次已完成步骤证据仍有效；
- 从断点继续。

禁止每次重新做全仓审计或重新规划已完成工作。

## 9. Plan-Scoped Authorization

用户批准包含以下内容的执行计划：

- 目标；
- 允许操作；
- 数据库/部署目标；
- 停止条件；

则计划内动作无需每个子步骤重复索要授权。

以下仍必须重新询问：

- 新增对 `bz_stock` 的写操作；
- 正式运行栈部署超出原计划；
- 删除计划外数据/资源；
- destructive / irreversible 行为；
- 发现数据损坏。

## 10. 临时产物

仅当本轮确实创建临时产物时，任务结束前：

- 清理明确属于本轮、用途已结束的临时文件/验证资源；
- 不删除来源不明或历史资产；
- 不删除业务数据、Volume、依赖目录。

如果本轮没有创建临时资源，不要求输出形式化“四清单”。

## 11. 禁止提交

默认不得提交：

- `.venv/`
- `node_modules/`
- `__pycache__/`
- `.pytest_cache/`
- `.mypy_cache/`
- `.ruff_cache/`
- `.coverage`
- `coverage.xml`
- `dist/`
- `build/`
- `*.log`
- 临时 `*.csv`
- 临时 `*.parquet`
- IDE 私有设置
- 真实秘密

正式 fixtures / 明确业务资产例外必须位于正式目录并符合仓库合同。
