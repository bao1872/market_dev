# Trae 指令：应用 docs v2 候选包

> 任务：把 v2 文档结构作为 Draft PR 应用到仓库。  
> 注意：本指令供后续使用；不要直接在 main 覆盖。

## 1. 分支

```bash
cd /root/web_dev
git fetch origin
git switch main
git pull --ff-only origin main
git switch -c docs/restructure-v2-system-map
```

确认 main 包含：

```text
40dd2287f0962910d2e272c468b3e5054abddaaf
```

## 2. 允许修改

```text
docs/**
tools/check_docs_consistency.py
tools/tests/test_check_docs_consistency.py
docs/changes/CHANGELOG.md
```

## 3. 禁止修改

```text
backend/app/**
frontend/src/**
backend/alembic/**
docker-compose.prod.yml
scripts/deploy.sh
任何业务代码
任何数据库 migration
任何算法文件
```

## 4. 执行步骤

1. 将旧 `docs/current/*.md` 移动到 `docs/archive/current-v1-20260703/`；
2. 写入 v2 候选包中的 `docs/` 文件；
3. 保留现有 `docs/changes/records/` 历史，不删除历史 CHANGE；
4. 修改 `tools/check_docs_consistency.py`：改为检查 `docs/current/MANIFEST.md` 的唯一基线，而不是每个 current 文档重复头；
5. 修改测试，覆盖：MANIFEST 缺失、非法 SHA、非祖先、关键文件缺失、Webhook 回归、OPEN 回归、本地链接、待填写；
6. 新增 CHANGE 记录；
7. 更新 CHANGELOG；
8. 运行验证。

## 5. 验证命令

```bash
python tools/check_docs_consistency.py
python tools/check_architecture.py
python tools/check_test_allowlist.py
python -m pytest tools/tests/test_check_docs_consistency.py -q
```

如未修改业务代码，不需要跑全量后端测试；但 PR CI 必须全绿。

## 6. PR

创建 Draft PR：

```text
base: main
head: docs/restructure-v2-system-map
title: docs: restructure current docs and add implementation maps
```

PR 描述必须说明：

```text
1. 仅改 docs 和 docs consistency；
2. 不改业务代码；
3. 旧 00-18 已归档；
4. current docs 压缩为 6 个核心文件 + manifest + alignment + open decisions；
5. 新增 maps 作为系统还原地图；
6. 新增 AI onboarding / restore checklist；
7. 修改 check_docs_consistency 以适配 v2；
8. 验证结果。
```

不得自动 merge，不得 deploy。
