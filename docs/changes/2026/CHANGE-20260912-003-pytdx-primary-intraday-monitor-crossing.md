# CHANGE-20260912-003 pytdx 全链路主源与盘中监控极速穿透事件（One-Shot Crossing）架构

- 日期：2026-09-12
- 基线：`ecc37bf71f33a57915ffc1f42e5842f472fa20fa`
- 实施 commit：`28fac2b6`（G1 缺口修复主源回正） + `607df2ed`（G3~G6 事实引擎与极速穿透事件主干）
- 层级：**Level 2（契约敏感）**：行情数据源契约收口（pytdx 唯一主源，外部全部降级为备用源），盘中自选股监控重构为轻量级连续快照目标位穿透与一次性事件（One-shot crossing）。
- 状态：`partial`（引擎与事件合同完成并通过单测；生产接线部分完成。G1~G8 **并非全部完成**，准确状态见文末「状态纠偏」。）。

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
  - 顺势突破（high + bias=1 或 low + bias=-1）触发 `smc_bos_cross` (BOS，首次突破一次性穿透)；
  - 逆势反转（high + bias=-1 或 low + bias=1）触发 `smc_choch_cross` (CHoCH，首次突破一次性穿透)；
  - `smc_bos_retest` / `smc_choch_retest` **仅保留作历史数据回读与旧事件兼容**，不再由首次突破路径发射；
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

## 5. 状态纠偏（本 correction commit 更正，2026-09-12）

下游纠偏评审结论：G1~G8 **并非全部完成**，此前「G1~G8 完成 / G8 100% E2E」表述过度乐观。
本 correction commit 仅完成下列 6 项纠偏，**不向前开发 G8**：

1. **生产监控接线（G6 部分）**：`MonitorBatchService` 注入冻结 Node TargetSet + 连续快照价格区间，
   使 `WatchlistMonitor.detect_events` 走一次性穿透（one-shot crossing）新路径（Node 已激活，零额外重算；
   SMC 待 `compute_smc_pine` 冻结 TargetSet 编排层落地）。
2. **事件合同（G5 引擎）**：首次突破 BOS/CHoCH 统一发射 `smc_bos_cross` / `smc_choch_cross`；
   `smc_bos_retest` / `smc_choch_retest` 仅保留作历史数据回读兼容。
3. **版本/重启价格 bootstrap（G7）**：Target Set Version 变更首帧强制 `(p,p)`，重启窗口期由持久化
   `current_price` 恢复 `P_last` 支持 catch-up 穿透。
4. **缺口修复生产接线（G1）**：`scripts/recover_daily_gaps.py` 注入 pytdx adapter，repair 与一致性门禁以 pytdx 为主源。
5. **CHANGE-003/004 状态更正**：删除「G1~G8 完成 / G8 100% E2E」表述。
6. **G2、G1D、G1E 保持未完成。**

冻结状态（本 commit 后）：

| 阶段 | 状态 | 说明 |
|---|---|---|
| G0 / G1A / G1B-2A / G1B-2B / G1B-3B / XDXR | ✅ | 已完成 |
| G1 缺口修复 | ✅ | 服务可用 + 生产 owner 已注入 pytdx adapter |
| G1C | 🟡 | 部分完成 |
| G1D / G1E / G2 | ❌ | **保持未完成** |
| G3 实时行情事实 | 🟡 | 引擎完成（`RealtimeMarketFactService.fetch_quotes`，80 只/批）；**production batch market-fact 未接** —— `MonitorBatchService` 不调用 `fetch_quotes`，生产现价仍取 `bars_minute["close"].iloc[-1]`，只用到了 `PriceTracker` |
| G4 筹码共识穿透 | 🟡 | crossing engine 完成；production Node 路径已激活（`detect_events` One-shot），但 `WatchlistMonitor.calculate_state()` 仍在跑旧 VN + SMC 重算 |
| G5 SMC 事件合同 | 🟡 | 引擎合同已修正（`*_CROSS`）；SMC 生产穿透待冻结 TargetSet 编排层 |
| G6 生产接线 | 🟡 | Node crossing 已接入，但旧 `calculate_state` / 重算仍存在，**「停止盘中重算」未达成**；SMC 待编排层 |
| G7 坐标对齐 + XDXR 版本滚动 | 🟡 | 生命周期判定已下沉为唯一 owner `resolve_snapshot_price_range`，**生产 `MonitorBatchService` 已生效**（与旁路共用同一规则）；坐标对齐其余项仍待验证 |
| G8 端到端 E2E | ❌ | dry-run 脚本存在，未宣告完整通过 |

### 5.1 G3 / G6 生产性能目标仍未达成（须准确记录）

生产当前仍是**逐标的循环**，不是设计的「一轮自选股 → 一次批量 pytdx quotes → shared Market Fact」：

```
for each instrument:
    拉 1m bar
    拉 Node daily / 15m
    calculate_state()          ← 旧 VN + SMC 重算仍在跑
    detect_events()
```

因此：

```
G3 引擎组件             ✅
G3 production market fact ❌
G4 crossing engine        ✅
G4 production crossing    🟡
G6「停止盘中重算」        ❌
```

性能目标（正常日 = ≈全市场 quote batch + 2 次 sentinel daily + bulk DB write，
而非 5000 × `get_daily_bars`）**尚未达成**。

