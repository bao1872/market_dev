# 90 废弃与永久禁止

本文件只保存“不要恢复”的稳定边界，不承载日常流程。

## 1. 通用禁止

禁止：

- 用旧聊天/archive 覆盖当前 PRD/代码；
- 复制旧实现形成第二业务 owner；
- 前端重新实现后端核心业务；
- 删除/削弱测试以适配错误实现；
- API 改动不检查前端消费者；
- Schema 改动不检查 Migration；
- Worker 改动不检查幂等/重试/fencing；
- Mock/Synthetic 冒充真实市场业务结果；
- OPEN / UNVERIFIED 冒充 PASS；
- 临时实验冒充稳定产品合同；
- force push；
- 未授权创建/切换开发分支；
- `git add -A` / `git add .` / `git add -u`；
- 未授权写 `bz_stock`；
- 未授权 destructive 操作。

## 2. 已废弃：多分支 Agent 工作流

禁止恢复：

- 每个任务自动创建 `feat/*` / `fix/*` / `agent/*`；
- IDE 自动 backup branch；
- 工具专属 worktree 作为默认流程。

当前默认开发分支为 `dev`。

## 3. 已废弃：工具专属治理

禁止恢复：

- 工具专属示例（如 TRAE/Codex/IDE A/IDE B）各自不同权限规则：禁止恢复；
- 工具专属十步流程；
- 工具专属成功枚举；
- 按模型名称定义治理能力。

所有执行主体按实际操作与授权受同一规则约束。

## 4. 已废弃：每轮默认重型闭环

在 `PROJECT_STAGE=EXPLORATION` 下，以下做法已不再是普通 iteration 默认要求：

- 每轮 full RTM；
- 每轮 full PURE_UNIT；
- 每轮 full Synthetic Closure；
- 每轮九节点 fully_ready；
- 每轮 production clone；
- 每轮 migration production rehearsal；
- 每轮全域 Maps/Runbooks 同步；
- 每轮 release certification。

这些能力没有被删除；它们只在 `70-hardening-release.md` 或真实风险触发时启用。

不得用历史规则把普通 Hypothesis Slice 自动升级为 Release Audit。

## 5. 多策略组合

当前已废弃的多策略组合不得从历史代码/文档恢复。

当前正式策略边界由 `docs/prd/` 定义（量化模型见 `docs/prd/20-quant-model.md`）。

## 6. feishu_webhook

禁止恢复：

- `feishu_webhook`
- `FEISHU_WEBHOOK`
- 独立管理员 webhook 路径

当前使用 `feishu_platform_app`。

## 7. ref/ 与 sync/

`ref/`：

- 仅人工参考；
- 不得运行时读取；
- 不得成为正式 fixture 生成器或业务依赖。

`sync/`：

- 不得恢复为正式中转真源。

禁止用 `git add -f` 强制跟踪被禁止目录。

## 8. SMC FVG

禁止：

- 计算 FVG；
- 输出 FVG；
- 缓存 FVG；
- 渲染 FVG；
- 增加 FVG 开关。

## 9. Canonical 绕过

禁止：

- 业务链直接调用 kernel 形成第二 compute owner；
- 详情/盘后/盘中/Capture 各自重算同一基础指标；
- DSA projection 独立重算 Core DSA。

## 10. 个股详情行情双源

禁止恢复：

- `/quote` + `/chart-snapshot` 双真源；
- `useRealtimeQuote`；
- `mergeRealtimeQuoteIntoBars()`；
- 为 quote 增加第二次行情读取；
- 用 1m 聚合冒充正式 15m/60m/1d。

## 11. 板块替代源

当前板块正式来源是 pywencai。

禁止静默增加：

- akshare；
- 代理/IP 绕过；
- 东方财富混用；
- 为板块同步增加新常驻 worker。

如产品明确决定更换数据源，必须先修改 PRD/业务合同，而不是偷偷 fallback。

## 12. 不安全部署

禁止：

- `scp` 单文件热修；
- `docker cp` 注入源码；
- SSH 进运行容器 vi/sed 改源码；
- 临时 `PYTHONPATH`；
- 另一 SHA 的 venv/app/tests 生成当前 SHA 证据；
- 直接在宿主 shell 拼接未登记正式验证流程并称为正式证据。

## 13. 不安全清理

禁止：

- `docker system prune -a`
- `docker image prune -a`
- `docker volume prune`
- 删除数据库 Volume；
- `FLUSHALL` 共享 Redis；
- 模糊匹配删除来源不明资源。

## 14. 测试数据库回退

禁止恢复：

- 本地 `bz_stock_test`；
- CI 临时 Postgres service；
- 连接 `bz_stock` 的 pytest；
- 未显式验证 DB identity 的 PG test。

真实 PG test 只走正式远程验证库。

## 15. 文档强制同步旧规则

以下旧做法不得恢复为 Exploration 通用硬门：

- “改代码必须同时改所有 Maps”；
- “每个 Bug 必须写 Change”；
- “用户未做视觉验收前 IDE 不完成技术验收”；
- “页面可见即可跳过 unit tests”。

正确边界见 `00-core-governance.md`、`40-testing-quality.md`、`60-runtime-frontend-acceptance.md`。
