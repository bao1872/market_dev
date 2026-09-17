# 核心业务链路与边界 Map

核验状态：部分核验（静态代码链路）
最后核验日期：2026-09-17
核验分支：`dev`
核验基线提交：`8251f0a375a241dbc894255b02c6979098bcc040` + 本 Map 同提交变更
事实所有权：跨域输入、计算 owner、持久化边界、发布指针、API 消费者和失败状态

> 本 Map 只记录已从当前代码确认的边界。运行时性能、真实 PG 语义与远程闭环未在本轮执行。

## 1. 行情链

| 环节 | 当前 owner | 边界 |
|---|---|---|
| Provider I/O | `bars_fetch_worker.py` / `eod_market_snapshot_provider.py` / realtime providers | 只负责外部请求、超时、重试和原始响应校验 |
| Provider 合同 | `core/exchange/contracts.py` | 只定义 Exchange 接口与周期映射；factory façade 和具体 adapter 不反向持有彼此的合同 |
| 周/月纯聚合 | `domain/shared/kline_frequency.py` | 只做 deterministic 日线聚合，不读取、不复权、不持久化、不发布 |
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

权限与订阅边界：`subscription_summary_service.py` 只读解析商业周期与展示摘要，
`effective_access_service.py` 解析 capability/default route；显式 capability 用户不依赖商业状态，
仅明确标记的 legacy-plan fallback 消费商业摘要。

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
- 抽出 `domain/shared/kline_frequency.py`，仓储保留兼容导出；Pytdx、DBExchange 与 MDAS 聚合适配器不再为纯聚合逻辑反向依赖仓储。
- 抽出 `core/exchange/contracts.py`，具体 Exchange 实现依赖无副作用合同，`core.exchange` 只保留兼容导出、factory 与实例缓存。
- 行情仓储/Provider/Exchange 循环依赖 SCC 已清零；结构基线中的 Python SCC 从 3 个降至 2 个。
- 抽出 `subscription_summary_service.py` 作为商业状态/摘要只读 owner；原订阅 service 保留兼容导出，权限解析不再反向依赖订阅写服务。权限订阅 SCC 已清零，结构基线从 2 个 SCC 降至 1 个。
- 抽出 `canonical_view_primitives.py` 与 `canonical_smc_adapter.py`：详情/快照只消费 DTO/投影 helper，结构因子的 SMC fallback 复用同一 compute-and-view owner，不再反向依赖 adapter composition。指标快照 SCC 已清零，结构基线中的 Python SCC 为 0。
- `research/feature_computer.py` 的未来收益、未来最大回撤与突破/破位标签改为固定偏移的整列向量计算；随机 NaN/Inf/非正价格及短序列与冻结循环逐元素 exact parity。这些字段仍属于 label namespace，不进入 causal feature。10 万行本地基准约 `1.14s → 0.010s`（约 112.9×），tracemalloc 峰值约 `7.64MiB → 8.97MiB`（+17.3%）。
- `adjustment_factor_calculator.py` 保留公司行动过滤、前收盘价、反向累计乘法及严格 `event_date > bar_date` 合同；前收盘价改为二分查找，最终 bar 映射改为有界分块向量化。同日重复事件、乱序 bars 与冻结旧循环逐元素 exact parity；10 万 bars / 99 events 本地映射基准约 `0.602s → 0.075s`（约 8.1×），tracemalloc 峰值约 `3.21MiB → 3.40MiB`（+6.0%）。
- 通知 worker 的 Outbox Relay / Delivery 轮询生命周期由 `notification_worker_runtime.py` 持有；`worker.py` 只装配 session、心跳、关闭信号及进程配置，并保留原函数 façade。业务状态机仍分别由 `outbox_relay.py`、`delivery_worker.py` 持有，提交、重试和异常后继续轮询语义未改变。该首批拆分使 `worker.py` 函数内 import 从 65 降至 63；尚未达到阶段性 70% 目标。
- 公开 API、Schema、DB 表、状态值、metadata 字段和 commit 边界未改变。
- 本计划已识别的 Python 循环依赖簇均已清零；后续新增依赖必须继续通过结构基线验证。

## 7. 可重复结构基线

`python tools/complexity_baseline.py` 输出 JSON，覆盖生产/测试 LOC、最大文件、Python SCC 循环依赖和三个核心文件的函数内 import。SQL round-trip、延迟与峰值内存必须由具体业务链 benchmark 采集，不以静态计数冒充运行证据。
