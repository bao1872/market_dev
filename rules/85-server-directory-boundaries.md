# 85 服务器目录边界

> 来源：AGENTS.md §七.22（Live Mount 运行目录）+ 提议（三目录职责）
> 状态：**PLANNED**（三目录职责提议中；`/opt/panji-live` 部分已在 `AGENTS.md` §七.22 确立）

> Phase 1 注：本文件为未来阶段提议。当前 `AGENTS.md` §七.22 仅明确 `/opt/panji-live` 运行目录；`/root/web_dev` 开发目录与 `/opt/panji-deploy` 干净部署目录为提议，尚未在 `AGENTS.md` 确立。

## 三目录职责（PLANNED）

| 目录 | 职责 | 状态 |
|---|---|---|
| `/root/web_dev` | CN 开发目录，git 仓库，分支开发与本地测试 | 当前使用 |
| `/opt/panji-deploy` | 自动部署干净目录，detached checkout，只读部署源 | **PLANNED，当前未实现** |
| `/opt/panji-live` | 运行目录，Live Mount 挂载源 | 当前使用 |

## /root/web_dev

- CN 开发目录；
- git 仓库，包含完整分支与历史；
- 用于分支开发、本地测试、镜像构建；
- 不作为运行目录；
- 不作为自动部署的只读源。

## /opt/panji-deploy（PLANNED）

> 提议中，当前未实现。

- 自动部署干净目录；
- detached checkout，只保留目标 commit；
- 不包含未提交文件、缓存、node_modules、.venv；
- 由自动部署脚本从 GitHub 同步；
- 不产生业务代码；
- 不作为运行目录。

## /opt/panji-live

- 运行目录；
- Live Mount 挂载源；
- 包含 backend/app、backend/alembic、backend/alembic.ini、frontend/dist、RUNTIME_SHA；
- 由 Live Mount 同步脚本从 `/root/web_dev` 或 `/opt/panji-deploy` 同步；
- 所有挂载为只读 (`:ro`)；
- 不产生业务代码。

## 腾讯云单实例约束（PLANNED）

> 提议中，尚未在 `AGENTS.md` 确立。

- 腾讯云只运行一套应用和一套核心数据库；
- 不并行运行 main 与 dev 两套服务；
- main 是阶段性稳定锚点，不是并行运行环境。

## 自动部署目录流（PLANNED）

> 提议中，当前未实现。

```
GitHub commit
    ↓ 自动部署脚本（SSH forced command）
/opt/panji-deploy（detached checkout）
    ↓ Live Mount 同步脚本（rsync --delete）
/opt/panji-live（运行目录）
    ↓ docker compose live.yml 只读挂载
容器内运行时
```
