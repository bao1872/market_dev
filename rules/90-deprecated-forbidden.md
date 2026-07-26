# 90 废弃与禁止

> 来源：AGENTS.md §六（12 条禁止行为）+ §七.2、§七.6、§七.8、§七.14、§七.15、§七.18
> 状态：并行验证

## 通用禁止行为（AGENTS §六）

1. 未读 AI-ONBOARDING 和 MANIFEST 就修改：禁止；
2. 根据旧 `docs/current/00-18` 或 archive 修改当前系统：禁止；
3. 根据旧聊天记忆覆盖 current：禁止；
4. 只改代码不改文档 / 只改 current 不改 CHANGE / 改代码结构不更新 maps：禁止；
5. 复制旧实现形成第二条路径 / 在前端重新实现后端业务规则：禁止；
6. 删除测试以适配错误实现 / 修改 API 不检查前端调用 / 修改数据模型不检查 migration：禁止；
7. 修改 Worker 不检查幂等、心跳、重试 / 修改权限不检查用户隔离：禁止；
8. 把 Mock E2E 说成真实生产 E2E / 把 OPEN 问题写成最终结论 / 把临时实验写成永久规则：禁止；
9. 直接修改 main / force push 已共享分支 / 为通过检查削弱 `check_docs_consistency.py`：禁止；
10. 未经许可修改生产环境账户密码：禁止；
11. 生产代码 / 测试 / 工具 / 构建脚本在运行时 `import` / `open` / `read` / `glob` `ref/` 目录：禁止；
12. `git add -A` / `git add .` / `git add -u` 批量暂存：禁止（必须精确 `git add <file>`）。

## 废弃项（禁止恢复）

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
- 未经用户明确授权禁止删除：数据库卷、运行中容器、postgres / redis 数据目录、node_modules、.venv、.git、源码、生产数据。

## Docker 镜像禁止（AGENTS §七.11）

- 禁止主动删除 `node:20-alpine`；
- 禁止 `docker image prune -a`；
- 除非明确升级 Node 版本或镜像损坏，否则不要删除 `node:20-alpine`。

## 数据库备份禁止（AGENTS §七.10）

- 测试期部署默认不备份数据库；
- 除非用户明确说"先备份数据库"，否则禁止 `pg_dump` / 大体积备份；
- 禁止写入 `/root/backups` 或 `/root/web_dev/backups`。

## Migration 禁止（AGENTS §七.9）

- 禁止修改已发布历史 migration；
- 只允许新增前向 migration；
- 修改 migration 必须有 upgrade / downgrade / upgrade 验证。

## 过时内容候选（DEPRECATED_CANDIDATE）

> 本阶段不删除。Phase 2+ 评估是否从 AGENTS.md 移除。

- `AGENTS.md` §十一 变更历史索引中的"近期关键变更"列表：随时间累积，可考虑迁移到 `maps/changes/CHANGELOG.md` 索引；
- `AGENTS.md` §七.20 中 `tools/check_docs_consistency.py` 规则清单：可考虑迁移到 `40-testing-quality.md` 详细描述（本 rules 已含概要）。
