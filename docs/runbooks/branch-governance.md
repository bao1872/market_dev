# 分支治理 Runbook

本 Runbook 描述盘迹仓库的长期分支策略、分支清理流程、archive 标签规则和远程服务器清理注意事项。

## 1. 长期分支策略

仓库只保留以下三个长期分支：

| 分支 | 职责 | 进入方式 | 是否可 force-push |
|---|---|---|---|
| `main` | 阶段性稳定锚点，对应远程生产运行 | 未经明确授权不得修改、合并或推送 | 否 |
| `dev` | 唯一默认日常开发分支，CI 与手动部署的唯一来源 | 所有变更直接在本地 `dev` 提交 | 否 |
| `experiment` | 明确授权下的隔离实验 | 仅在明确授权时使用；不得作为生产部署来源 | 仅在明确授权时 |

本地、`origin` 和远程服务器最终仅保留 `main`/`dev`/`experiment`。

未经用户明确授权，禁止创建任何新分支（含 backup 分支）、禁止从 `dev` 切换工作分支、
禁止对任何分支 force push。需要可恢复点时使用 checkpoint commit 替代新建分支。
详见 `rules/50-git-development-flow.md`。

## 2. 分支清理流程

### 2.1 准备

1. 确保本地当前分支为 `dev` 且工作区干净。
2. 执行一次 `git fetch --all --prune`。
3. 记录当前资源基线（可选，参见 Phase 4 检查点模板）。

### 2.2 列出分支

```bash
git branch -a
git ls-remote --heads origin
```

服务器需额外检查：

```bash
ssh panji-prod 'cd /root/web_dev && git branch -a && git status --short'
```

### 2.3 分析每个非保留分支

对每个非 `main`/`dev`/`experiment` 分支记录：

- tip SHA；
- upstream；
- 是否已合并到 `main`/`dev`/`experiment`；
- patch 等价性：`git cherry <target> <branch>` 输出无 `+` 即等价；
- 是否被 worktree 检出。

### 2.4 已合并或 patch 等价分支

直接删除本地和 origin 分支：

```bash
git branch -d <branch>
git push origin --delete <branch>
```

### 2.5 有唯一提交的分支

1. **密钥扫描**：使用 `git log -p <branch>` 或 `git secrets --scan` 检查是否包含密码、私钥、完整 URL。
2. 创建 annotated tag：

```bash
git tag -a archive/<规范化分支名>-YYYYMMDD -m "Archive <origin/branch> before cleanup"
```

规范化规则：将 `/` 替换为 `-`，例如 `origin/feat/portal-replacement-v1` → `feat-portal-replacement-v1`。

3. push tag 并用 `ls-remote` 验证：

```bash
git push origin archive/<name>-YYYYMMDD
git ls-remote --tags origin 'archive/<name>-YYYYMMDD'
```

4. 验证成功后删除分支：

```bash
git branch -D <branch>
git push origin --delete <branch>
```

## 3. 远程服务器清理

远程 `/root/web_dev` 当前必须满足以下条件才能继续：

- 工作区无未提交修改；
- 无 env、数据库、上传数据、未知大文件或冲突；
- 无仅存在于服务器且未归档的源码。

若存在脏工作区：

1. 只列出文件名、类型、大小和 tracked 状态，不查看敏感内容。
2. 比较修改是否已存在于 `origin/dev` 或 `origin/experiment`。
3. 生成/日志/缓存文件仅在明确可重建且非数据时删除。
4. 唯一源码只有通过密钥检查并安全提交到 `experiment` 后才能清理。
5. 出现数据风险时停止服务器清理，向团队报告阻塞。

清理完成后，服务器必须切到 `main` 并确保工作区干净：

```bash
ssh panji-prod 'cd /root/web_dev && git checkout main && git status --short'
```

> 禁止在服务器执行 `git reset --hard`。

## 4. 最终验证

- 本地分支：`git branch` 只显示 `main`、`dev`、`experiment`。
- origin 分支：`git ls-remote --heads origin` 只显示 `main`、`dev`。
- 服务器分支：`ssh panji-prod 'cd /root/web_dev && git branch -a'` 只显示 `main`、`dev`、`experiment`（`experiment` 可为本地实验分支）。
- 服务器工作区干净。
- `dev` 可 fast-forward 推送到 origin：

```bash
git merge-base --is-ancestor origin/dev dev && git push origin dev
```

- `experiment` 可推送到 origin（如需要）：

```bash
git push origin experiment
```

- 不得推送 `main`。

## 5. 归档标签管理

- 标签格式：`archive/<规范化分支名>-YYYYMMDD`。
- annotated tag 必须包含说明文字。
- 删除分支前必须确认 tag 已推送且 `ls-remote` 可见。
- 若标签已存在，使用带前缀的唯一名称，如 `archive/server-<name>-YYYYMMDD`。

## 6. 更新相关文档

分支策略发生实质变化时：

- 更新 `docs/prd/80-system-runtime.md` 的长期分支策略条款；
- 更新 `docs/maps/80-system-runtime.md` 的实际分支结果；
- 创建或更新 `docs/runbooks/branch-governance.md`；
- 创建 `docs/changes/2026/CHANGE-YYYYMMDD-NNN-branch-governance.md` 记录删除、归档标签和阻塞。

## 7. 禁止事项

- 不得删除含唯一提交或未提交工作的分支，除非已安全转入 `experiment` 或保存为 archive 标签。
- 不得对 `main` 执行 force-push 或直接推送。
- 不得在服务器执行 `git reset --hard` 或清理持久化数据。
- 不得将生产 env、数据库文件或上传数据移入仓库。
