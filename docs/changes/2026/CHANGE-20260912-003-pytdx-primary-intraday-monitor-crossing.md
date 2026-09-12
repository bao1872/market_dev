# CHANGE-20260912-003 pytdx 全链路主源与盘中监控极速穿透事件（One-Shot Crossing）架构

- 日期：2026-09-12
- 基线：`ecc37bf71f33a57915ffc1f42e5842f472fa20fa`
- 实施 commit：`28fac2b6`（G1 缺口修复主源回正） + `607df2ed`（G3~G6 事实引擎与极速穿透事件主干）
- 层级：**Level 2（契约敏感）**：行情数据源契约收口（pytdx 唯一主源，外部全部降级为备用源），盘中自选股监控重构为轻量级连续快照目标位穿透与一次性事件（One-shot crossing）。
- 状态：`verified_code`（纯单元测试 370 passed，无回归；零破坏性 DB 写入）。

## 1. 变更背景与核心动因

1. **行情数据源优先级收口**：
   - 系统早期因 pytdx 服务器连接不稳定，应急引入了同花顺不复权（THS）用于全市场缺口修复，并引入了东财（Eastmoney）进行盘中全市场轮询。
   - 在 CHANGE-20260912-001 重建 4 台高可用 pytdx 服务器池后，明确全系统行情铁律：**pytdx 为唯一主源；同花顺与东财全部退为备用源（明确失败才 fallback）**。
2. **盘中监控架构革新**：
   - 旧有盘中监控在每一轮轮询时为每只标的抓取数千根历史 K 线、逐股重算筹码分布（Volume Profile）与复杂 SMC 结构，不仅消耗大量网络和计算资源，更因 episode 状态机与反复 retest 带来报警风暴与延迟；
   - 用户明确判定规则：“盘中监控的逻辑是获取所有股票的实时快照，对比这一次与上一次快照的价格差异是否穿越目标价位，比如筹码共识区，SMC 的 BOS、CHoCH 价位（这里就是一次性的，穿越了那就是过了），已经进入订单块”。

## 2. 详细设计与关键契约

### 2.1 G1: 全市场日线缺口修复主源回正 (`daily_gap_repair_service.py`)
- `_fetch_t_bar` 增加 `adapter` 参数：SH/SZ 标的优先调用 `adapter.get_daily_bars(exact_date)`；
- 失败时顺畅降级至同花顺 `fetch_ths_raw_daily`，最后兜底 Eastmoney；北交所（BJ）pytdx 原生不支持，天然直通同花顺备用源；
- 修复门禁 `validate_consistency` 与报告支持 `source="pytdx"`；新增 `compare_db_vs_pytdx_for_date`。

### 2.2 G3: 盘中实时行情事实引擎 (`realtime_market_fact_service.py`)
- **唯一主源**：`PytdxAdapter.get_security_quotes_with_provenance`，单批 80 只标的批量抓取，TCP 耗时仅数十毫秒；
- **数量归一化**：pytdx quote `vol`（手）自动乘以 100 转换为规范 `volume`（股）；
- **平滑降级**：pytdx 耗尽重试或异常时，自动受控降级至 Eastmoney `push2.eastmoney.com` 实时快照；BJ 标的自动路由至东财备用；
- **价格区间追踪器 (`PriceTracker`)**：在内存中维护每个标的的前次快照价格 $P_{\text{last}}$ 与本次价格 $P_{\text{curr}}$，提供确定性的离散区间 $[P_{\text{last}}, P_{\text{curr}}]$。

### 2.3 G4 / G5: 一次性穿透判定与事件生成 (`monitor_crossing_service.py`)
- **筹码共识区穿透 (G4)**：
  - 消费 `NodeMonitorTargetSet`；
  - 向上穿透：$P_{\text{last}} < \text{target\_price} \le P_{\text{curr}}$；
  - 向下穿透：$P_{\text{last}} > \text{target\_price} \ge P_{\text{curr}}$；
  - 触发后发射 `EVENT_TYPE_NODE_CLUSTER_TOUCH`；
- **SMC 实时事件 (G5)**：
  - 消费 `SmcMonitorTargetSet`；
  - 顺势突破（high + bias=1 或 low + bias=-1）触发 `smc_bos_retest` (BOS)；
  - 逆势反转（high + bias=-1 或 low + bias=1）触发 `smc_choch_retest` (CHoCH)；
  - 订单块触碰：价格落入或进入 $[OB_{\text{low}}, OB_{\text{high}}]$ 触发 `smc_order_block_first_touch`；
  - 彻底去除 EQH/EQL 触发与复杂 episode retest。
- **一次性铁律 (One-Shot Lifecycle)**：
  - 单个 `target_id` 一旦触发，立即记录至 `triggered_target_ids`，在同一 Target Set Version 生命期内绝不重复触发，杜绝行情震荡期的重复轰炸。

### 2.4 G6: 主干接线与轻量化监控服务 (`watchlist_realtime_monitor_service.py` & `watchlist_monitor.py`)
- `WatchlistMonitor.detect_events` 优先消费 `context.node_target_set` 与 `context.smc_target_set`，零额外指标重算；
- `WatchlistRealtimeMonitorService` 实现单轮全量自选股监控的极速批量编排，单轮百只标的判定用时仅数十毫秒。

## 3. 代码变更清单

1. `backend/app/services/daily_gap_repair_service.py`
2. `backend/tests/test_daily_gap_repair_service.py`
3. `backend/app/services/realtime_market_fact_service.py` [NEW]
4. `backend/tests/test_realtime_market_fact_service.py` [NEW]
5. `backend/app/services/monitor_crossing_service.py` [NEW]
6. `backend/tests/test_monitor_crossing_service.py` [NEW]
7. `backend/app/strategy/runtime.py`
8. `backend/app/strategy/monitors/watchlist_monitor.py`
9. `backend/app/services/watchlist_realtime_monitor_service.py` [NEW]
10. `backend/tests/test_watchlist_realtime_monitor_service.py` [NEW]

## 4. 验证与质量保证

- **单元测试覆盖**：
  - `test_daily_gap_repair_service.py`: 42 passed
  - `test_realtime_market_fact_service.py`: 4 passed
  - `test_monitor_crossing_service.py`: 3 passed
  - `test_watchlist_realtime_monitor_service.py`: 2 passed
  - 关联模块回归（EOD 调度、SMC、自选股等）全量 370 个纯单元测试在 1.45 秒内全数通过（PASS 100%）。
- **代码规范**：Ruff linting 全数通过，Git diff whitespace 检查 clean。
