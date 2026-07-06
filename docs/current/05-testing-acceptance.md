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
- SQZMOM_LB：
  - 后端算法单元测试覆盖 Pine 等价（`dev = multKC * stdev`、`linreg offset=0`、`nz(val[1])` 颜色逻辑、数据不足不 500）；
  - `indicator_service` 在 `compute_all_indicators` 中注入 `sqzmom_lb` 图层与序列；
  - 前端 contract test 覆盖：开关默认关闭、renderer 已注册、独立 pane 分配、API 缺失不崩溃、前端不重新计算指标。
- 结构状态因子：
  - 后端 `test_atr_utils.py` 覆盖 ATR SSOT 与 Pine RMA 等价（首根 TR、RMA seed、数据不足、空输入、返回类型）；
  - 后端 `test_structural_factor_service.py` 覆盖 5 组因子（DSA 段/Swing/成本节点/动量波动/成交参与）+ 异常隔离 + meta 结构 + 无未来函数；
  - 后端 `test_structural_factors_api.py` 覆盖 API 路由（合法请求、非法 timeframe/adj、不存在 instrument、meta 结构）；
  - 前端 `structural-state-panel.test.ts` contract test 覆盖：React 组件存在、使用 `useStructuralFactors` hook、双周期 tabs、5 张卡片、null 占位、降级提示、API 失败处理、前端不重新计算因子。
- 结构状态因子 V1.8：
  - 后端 `test_structural_factor_service.py` 新增 V1.8 测试：`test_v18_dual_period_difference`（构造不同 1d/15m bars，断言字段结构相同但数值不同）、`test_v18_no_future_function_confirmed_pivots`（修改最后一根 bar 不影响已确认 swing pivot）、`test_volatility_v18_sqz_on_off`（sqz_on/sqz_off 互斥）、Relation `primary_dir`/`trend_alignment` 测试、DSA 单 segment/双 segment 段收益与段间对比测试、Swing position/retracement 测试、Node degraded 测试、SQZMOM abs percentile 测试；
  - 前端 `structural-state-panel.test.ts` 新增 V1.8 字段存在性断言：`v18Keys`（33 项含 distance_to_bb_upper_atr/sqz_on/current_dsa_segment_dir/current_vs_prev_volume_ratio 等）+ `v18RelationKeys`（7 项含 primary_dir/secondary_dir/trend_alignment 等），并断言已移除 `momentum_alignment` 引用。
- 时序特征 V1（Temporal Features V1）：
  - 后端 `test_temporal_feature_service.py` 覆盖：daily_context 9 字段结构、duration percentile 公式、sqzmom/volume change since segment start（point-in-time）、m15 swing anchor 选择规则（bsl<bsh→anchor=low / bsh<=bsl→anchor=high）、m15_position_change_since_swing_anchor 手算验证、anchor 处 volume_percentile/bb_bandwidth_percentile 修改后不变（point-in-time）、m15 position/sqzmom/bb_bandwidth/volume change since anchor、derived_relation 只由 daily + m15 派生、alignment direction 4 种情况、intensity mean(abs)、数据不足 null + warmup_notes、单字段失败异常隔离、无未来函数、组级异常隔离（mock m15_response 抛异常 → 整体 200 + m15_response 全 null + degraded_reasons）；
  - 后端 `test_temporal_features_api.py` 覆盖 API 路由（合法请求、非法 timeframe/adj、`as_of != "latest"` 返回 400、不存在 instrument 200 + degraded、meta 结构）；
  - 前端 `structural-state-toggle.test.ts` contract test 覆盖：面板默认隐藏、开关按钮存在、localStorage 持久化、`hideStructuralState=1` 强制隐藏、`capture=1` 强制隐藏、`capture=feishu` 强制隐藏、强制隐藏时禁用 toggle、toggle 按钮在 `tv-chart-column` 内部（以 `position: relative` 为定位上下文）；
  - 验收：所有 anchor 取值必须 point-in-time，不得使用未来 bar；15m 不使用 DSA 位置类字段作为核心输入；V1 只支持 as_of=latest；任一组（daily/m15/derived）异常不得导致整体 API 500。

## 3.1 本轮新增回归

- `BarsCoverageService` 统一 A 股口径，排除指数/ETF，默认使用 `shanghai_business_date`，返回 `coverage`（展示）与 `coverage_raw`（阈值判断）；
- `/admin/after-close-runs/dsa-only`、`bars_scheduler`、系统概览 `WAITING_DSA` 判定等覆盖率门禁使用 `coverage_raw` 原始值；
- `/admin/after-close-runs/dsa-only` 当日无数据时 fallback 到最新交易日，覆盖率不足返回 409；
- `/watchlist/monitor-status` 无 `MonitorState` 或 `payload` 无效时通过 `MonitorSnapshotService` fallback 返回指标，单只失败单行降级；
- 飞书消息时间统一格式化为 Asia/Shanghai，文本中触发时间显示 CST；
- 前端 `mergeRealtimeQuoteIntoBars` 不修改原数组、1d 保留日期语义、intraday 使用 `quote.update_time`。
- admin monitor 资格：
  - `test_monitor_eligible.py` 覆盖 `filter_monitor_eligible_recipients`/`is_user_eligible_for_monitor`：active admin 放行、active member + 有效 subscription 放行、disabled admin 排除、无订阅普通用户排除；
  - `monitor_batch_service`/`event_recipient_service`/`outbox_relay` 三处统一使用监控资格过滤，outbox relay 端到端一致性测试验证 MessageDelivery 生成数量符合预期。
- 实时行情可信化：
  - `test_quote_trustworthy.py` 覆盖交易时段 pytdx 成功（`source=pytdx`、`is_realtime=true`、`degraded=false`）、交易时段 pytdx 失败降级（`source=daily_fallback`、`degraded=true`）、非交易时段 fallback（`degraded=false`）、无数据 404、Redis 缓存命中不走 pytdx；
  - `scripts/verify_quote_trustworthy.py` 本地 ASGI 端到端验证三个场景并输出 curl 示例；
  - 前端 `chart.test.ts` 覆盖不可信 quote 不合并入 K 线、1d 日期语义、intraday 使用 `quote.update_time`。
- monitor 投递与 live bar 后续修复：
  - `test_delivery_worker_monitor_eligible.py` 覆盖 `delivery_worker` 对 `monitor_event` 使用 `is_user_eligible_for_monitor`：active admin 放行、active member + 有效 subscription 放行、disabled admin 排除、无订阅普通用户排除；
  - `test_monitor_batch_live_minute.py` 覆盖 `monitor_batch_service.execute_monitor_cycle` 使用 `include_realtime=True` 拉取 1m、剔除最后一根未完成 bar、记录 `last_minute_bar_time`/`last_minute_data_source`；
  - `test_market_data_aggregation_partial_daily.py` 覆盖交易时段 1d 合成 partial daily bar（`data_source=hybrid`、`is_partial=true`、`last_live_bar_time` 非空）、非交易时段不合成；
  - `test_quote_timezone.py` 覆盖 `/quote` 返回 `update_time` 带 `+08:00`、UTC 字符串被修正为 `+08:00`。

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
