# 核心业务链路与边界 Map

核验状态：部分核验（静态代码链路）
最后核验日期：2026-09-17
核验分支：`dev`
核验提交：`854ff313a1a0092131f9c057e84298c60e645da2`
事实所有权：跨域输入、计算 owner、持久化边界、发布指针、API 消费者和失败状态

> 本 Map 只记录已从当前代码确认的边界。运行时性能、真实 PG 语义与远程闭环未在本轮执行。

## 1. 行情链

| 环节 | 当前 owner | 边界 |
|---|---|---|
| Provider I/O | `eod_market_snapshot_provider.py` / realtime providers | 只负责外部请求、超时、重试和原始响应校验 |
| 标准化与复权 | `MarketDataAggregationService` | 业务读 bars 的 SSOT；qfq 只在出口应用一次 |
| 持久化 | `bar_repository.py` 及明确的批量写入 service | 原始 bar 落库，不隐藏 provider 网络调用 |
| 质量门禁 | `market_data_quality_service.py` / factor audit | 区分缺数据、因子过期与基础设施失败 |

失败状态必须保留 provider / preparation / persistence 分类，不得静默切源。

## 2. Core / DSA 链

`Canonical daily bars → canonical indicators/structure → DSA artifact → selection composition → StrategyRun/Result → publication`

- 计算 owner：`canonical_computation_service.py` 与 `strategy/selectors/dsa_selector.py`。
- DSA 在 Core 内计算一次；`dsa_projection_service.py` 只消费已计算 artifact。
- 持久化：`strategy_result_repository.py` / Core artifact repositories。
- 正式结果：published run/pointer，`partial_failed` 不得当作完整发布。
- API 消费：strategy run/result、stock context 及 review preparation 链。

## 3. Review 链

`published/lineage-locked Core → observation preparation → canonical scope observation → metric/filter/attribution → ReviewRun → review publication → review API`

- 纯计算 owner：`app/domain/review/`。
- 编排 owner：`review_orchestrator_service.py`，不得重新定义 observation 或 publication 条件。
- 发布 owner：`review_publication_service.py`。
- 当前 Core lineage 以 `source_core_run_id` 显式锁定；withdrawn pointer 不得复用历史 published run。
- API 消费：`api/review.py`、`api/admin_review.py` 及 board-analysis 读模型。

## 4. 监控通知链

`active watchlists 去重 → released monitor versions → day/15m/1m bars → state/events → recipients → outbox/delivery`

- 批处理 owner：`monitor_batch_service.py`。
- 策略语义 owner：`strategy/monitors/` 与 `strategy/events/detectors/`。
- 持久化：monitor state/evaluation/event repositories/services；冷却后再落库。
- 收件人与投递：`event_recipient_service.py` → outbox → `delivery_worker.py`。
- 终态必须区分 deferred / delivered / failed，不得以已入队冒充已投递。

## 5. 盘后控制链

`SchedulerJobRun → after-close DAG → worker lease/fencing → Core → Review → History → readiness`

- 编排步骤 owner：`after_close_orchestrator.py`。
- 状态/metadata/event 持久化边界：`after_close_run_contract.py`。
- Worker 只做入口派发、租约和生命周期协调。
- Readiness 唯一 owner：`ProductReadinessService.collect_states()` 与 `evaluate_closure()`。
- 必须保留 queued/running/partial/failed/interrupted/cancelled 及 step summary 的差异。

## 6. 本轮边界变更

- 抽出 `after_close_run_contract.py`，使 DSA recovery 不再反向 import 整个 orchestrator。
- 抽出 `domain/shared/bar_identity.py` 作为 bar 时间序列化与 `source_bar_hash` 唯一 owner；`chart_bars_service` 保留原导出兼容，MDAS 不再反向依赖 chart adapter。
- `source_bar_hash` 的逐行 `iterrows()` 改为同质数值矩阵行视图；10 万行本地微基准约 `1.73s → 0.49s`，golden hash 与 NaN/Inf 边界 parity 保持一致。
- 抽出 `outbox_writer.py` 作为事务内 Outbox 写入 owner，内测申请的 event/DTO 收口到 `beta_application_notification_contract.py`；生产者不再依赖 relay，relay 不再依赖 notifier。
- 公开 API、Schema、DB 表、状态值、metadata 字段和 commit 边界未改变。
- 其余循环依赖簇仍待后续 slice 处理，不在本 Map 中写为已完成。

## 7. 可重复结构基线

`python tools/complexity_baseline.py` 输出 JSON，覆盖生产/测试 LOC、最大文件、Python SCC 循环依赖和三个核心文件的函数内 import。SQL round-trip、延迟与峰值内存必须由具体业务链 benchmark 采集，不以静态计数冒充运行证据。
