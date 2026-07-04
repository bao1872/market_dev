# 05 测试、CI 与验收

## 1. 测试数据库

所有数据库集成测试使用 PostgreSQL 测试库和真实 Alembic。禁止 SQLite、aiosqlite、内存数据库、测试手写生产 Schema 和模块级 db_session 覆盖。

## 2. 测试层级

| 层级 | 覆盖 |
|---|---|
| Unit | 纯函数、算法、状态转换 |
| Integration | PostgreSQL、ORM、Service、事务、锁、Worker |
| API | 认证、资格、所有权、响应、错误 |
| Frontend | Adapter、路由、状态、交互 |
| E2E | 用户操作到数据库、消息、飞书、截图 |
| Deployment | Compose、迁移、健康、版本、Worker |

## 3. 关键回归

- 趋势选股：universe、result_count、partial_failed 禁止发布、分页不改变全量；
- 权限订阅：active/expired/no-subscription/disabled/admin/用户 A-B；
- 盘中监控：完成 1m Bar、幂等、投递前资格复核；
- 行情聚合：尾部补齐、partial、degraded、页面/指标/截图同源；
- 飞书：文字图片成功、partial_failed、仅重试图片、不重复文字、所有权；
- 管理任务：真实 API、审计日志、run key、heartbeat、lease、stale recovery、Worker Git SHA。

## 3.1 本轮新增回归

- `BarsCoverageService` 统一 A 股口径，排除指数/ETF，默认使用 `shanghai_business_date`，返回 `coverage`（展示）与 `coverage_raw`（阈值判断）；
- `/admin/after-close-runs/dsa-only`、`bars_scheduler`、系统概览 `WAITING_DSA` 判定等覆盖率门禁使用 `coverage_raw` 原始值；
- `/admin/after-close-runs/dsa-only` 当日无数据时 fallback 到最新交易日，覆盖率不足返回 409；
- `/watchlist/monitor-status` 无 `MonitorState` 或 `payload` 无效时通过 `MonitorSnapshotService` fallback 返回指标，单只失败单行降级；
- 飞书消息时间统一格式化为 Asia/Shanghai，文本中触发时间显示 CST；
- 前端 `mergeRealtimeQuoteIntoBars` 不修改原数组、1d 保留日期语义、intraday 使用 `quote.update_time`。

## 4. CI 门禁

阻断项：

```text
Architecture Rules
Docs Consistency
Test Allowlist
Ruff New Files
Ruff Baseline Regression
Mypy New Files
Mypy Baseline Regression
Alembic Upgrade/Downgrade/Upgrade
PostgreSQL Integration Tests
Frontend Type Check
Frontend Lint
Frontend Build
```

非阻断历史债务展示：

```text
Ruff Full Repository Report
Mypy Full Repository Report
```

禁止通过扩大 ignore、per-file-ignores、noqa、type ignore、exclude 或关闭全仓检查来绕过新增债务。

## 5. 文档一致性

v2 后 docs consistency 应检查 `current/MANIFEST.md`，而不是要求每个 current 文件重复基线头。应用本包时必须同步改脚本和测试。

## 6. 完成标准

一次变更完成必须满足：

```text
代码实现
= 当前设计文档
= 实现地图
= API 和数据契约
= 测试验证
= 部署配置
= CHANGE 记录
```

如果任一层不一致，必须登记到 `current/code-doc-alignment.md`，不能假装完成。
