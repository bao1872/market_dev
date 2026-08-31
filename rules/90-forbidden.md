# 90 永久禁止项

本文件只定义当前生效的永久禁止项，不保存历史治理流程或兼容入口。

## 1. 通用禁止

禁止：

- 用聊天记录或归档覆盖当前 PRD/代码；
- 复制实现形成第二业务 owner；
- 前端重新实现后端核心业务；
- 删除或削弱测试以适配错误实现；
- API 改动不检查前端消费者；
- Schema 改动不检查 Migration；
- Worker 改动不检查幂等、重试和 fencing；
- Mock/Synthetic 冒充真实市场业务结果；
- OPEN / UNVERIFIED 冒充 PASS；
- 临时实验冒充稳定产品合同；
- force push；
- 未授权创建或切换开发分支；
- `git add -A` / `git add .` / `git add -u`；
- 未授权写 `bz_stock`；
- 未授权 destructive 操作。

## 2. Git 与执行主体

禁止：

- 每个任务自动创建 `feat/*` / `fix/*` / `agent/*`；
- 自动创建 backup branch；
- 把 worktree 作为默认开发流程；
- 按 IDE、Agent、模型或客户端定义不同权限、流程或成功状态。

当前默认开发分支为 `dev`。所有执行主体按实际操作与当轮授权遵守同一规则。

## 3. Exploration 范围

禁止把普通 Level 1 任务自动扩张为 full RTM、full PURE_UNIT、full Synthetic
Closure、production clone、Migration production rehearsal、全域文档同步或 release
certification。需要更强验证时必须由实际风险、Level 2/3 路由或明确用户要求触发。

## 4. 外部通知

禁止：

- `feishu_webhook`；
- `FEISHU_WEBHOOK`；
- 独立管理员 webhook 路径。

正式通知 owner 为 `feishu_platform_app`。

## 5. ref/ 与 sync/

`ref/` 仅供人工参考，不得被运行时读取，也不得成为 fixture 生成器或业务依赖。
`sync/` 不得作为正式中转真源。禁止用 `git add -f` 强制跟踪这些目录。

## 6. SMC FVG

禁止计算、输出、缓存、渲染 FVG，也禁止增加 FVG 开关。

## 7. Canonical 绕过

禁止：

- 业务链直接调用 kernel 形成第二 compute owner；
- 详情、盘后、盘中或 Capture 各自重算同一基础指标；
- DSA projection 独立重算 Core DSA。

## 8. 个股详情行情双源

禁止：

- `/quote` + `/chart-snapshot` 双真源；
- `useRealtimeQuote`；
- `mergeRealtimeQuoteIntoBars()`；
- 为 quote 增加第二次行情读取；
- 用 1m 聚合冒充正式 15m/60m/1d。

## 9. 板块替代源

当前板块正式来源是 pywencai。禁止静默增加 akshare、代理/IP 绕过、东方财富
混用或常驻同步 worker。更换数据源必须先修改 PRD/业务合同。

## 10. 不安全部署

禁止：

- `scp` 单文件热修；
- `docker cp` 注入源码；
- SSH 进入运行容器修改源码；
- 临时 `PYTHONPATH`；
- 使用另一 SHA 的 venv/app/tests 生成当前 SHA 证据；
- 在宿主 shell 拼接未登记流程并称为正式证据。

## 11. 不安全清理

禁止：

- `docker system prune -a`；
- `docker image prune -a`；
- `docker volume prune`；
- 删除数据库 Volume；
- `FLUSHALL` 共享 Redis；
- 模糊匹配删除来源不明资源。

## 12. 测试数据库

禁止：

- 本地 `bz_stock_test`；
- CI 临时 PostgreSQL service；
- SQLite 或 aiosqlite；
- 连接 `bz_stock` 的 pytest；
- 未显式验证数据库身份的 PG test。

真实 PG test 只走正式远程验证库。

## 13. 文档与验收

禁止：

- 改代码时无条件同步所有 Maps；
- 为每个 Bug 强制写 Change；
- 用户未做视觉验收前拒绝完成工程技术验收；
- 以页面可见为由跳过 unit tests。

正确边界见 `00-core-governance.md`、`40-testing-quality.md` 和
`60-runtime-frontend-acceptance.md`。
