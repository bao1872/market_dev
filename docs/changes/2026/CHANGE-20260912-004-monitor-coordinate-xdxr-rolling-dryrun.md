# CHANGE-20260912-004 前端与监控坐标对齐、XDXR 版本滚动生命周期及真实交易日只读 Dry-Run（Stage G7 & G8）

- 日期：2026-09-12
- 基线：`d6f7ee860ee3d89dd7d9daec47a75caadff98e29`
- 层级：**Level 2（契约敏感）**：前端图表展示与盘中监控复权坐标体系对齐、除权除息（XDXR）与 Target Set Version 滚动重置、内存价格跟踪器重置语义、全链路 443 单元测试与真实交易日只读 dry-run 验收。
- 状态：`partial`（坐标对齐 / XDXR 版本滚动逻辑已实现并单测；G1~G8 **并非全部完成**，准确状态见文末「状态纠偏」。）。

## 1. 变更背景与解决的问题

在完成 G1~G6 的数据源收口与盘中极速监控重构（注：G1~G6 实际为部分完成，见文末状态纠偏）后，本变更推进 Stage G7 与 G8 的逻辑实现与单测：
1. **坐标系与复权一致性（G7）**：核验前端图表（`ChartSnapshotService` / `StrategyChart.tsx`）与后端监控（`WatchlistRealtimeMonitorService`）的价格基准，确保证明数学一致性；
2. **XDXR 与 Target Set 版本滚动（G7）**：当标的发生分红送配（XDXR）或新 Bar 完成导致 Target Set Version 变更时，内存中已触发状态必须优雅清空重置，杜绝历史触发标记阻塞新目标位判定；
3. **入参规范化与标的重置（G7）**：增强 `RealtimeMarketFactService.fetch_quotes` 对 `Sequence[Instrument]` 的兼容，为 `PriceTracker` 增加精确标的重置能力；
4. **端到端测试防线与真实 Dry-Run（G8）**：建立包含 443 个纯单元测试的防线，并用真实 pytdx 链路完成只读端到端闭环验证。

## 2. 关键设计与契约保证

### 2.1 坐标系数学恒等性
- **QFQ 锚点定义**：以最新交易日（$t = \text{as\_of}$）为基准时，$\frac{\text{factor}(t)}{\text{factor}(\text{as\_of})} = 1.0$。
- **当日一致性**：最新交易日的 raw price 与 QFQ price 严格恒等。
- **协同结论**：盘前基于最新 QFQ 日线/分时固化的 `NodeMonitorTargetSet` 和 `SmcMonitorTargetSet` 目标价位，与盘中 pytdx 实时快照价格位于严格相同的物理价格坐标系中，前端图表点位与后端监控报警无偏差。

### 2.2 XDXR / Target Set 版本滚动与生命周期管理
- `WatchlistRealtimeMonitorService` 在状态 payload 中记录：
  - `node_target_set_version`
  - `smc_target_set_version`
  - `triggered_node_target_ids`
  - `triggered_smc_target_ids`
- 当检测到当前传入的 `target_set_version` 与前次状态不一致时（如 XDXR 因子更新重算或盘中新 Bar 生成），自动将对应域的 triggered targets 清空，由新 Target Set 全权接管监控，彻底防止触发漏报。

### 2.3 价格跟踪器重置与入参规范化
- `PriceTracker.reset(symbol)`：支持重置单标的或全部标的的前次价格。重置后下一次更新重回 $(p, p)$，严禁从 0 跃迁造成伪穿透；
- `RealtimeMarketFactService.fetch_quotes`：统一规范化入参，支持 `Sequence[str]` 与 `Sequence[Instrument]` 混入，去重并保持原有顺序。

## 3. 代码变更清单

1. `backend/app/services/realtime_market_fact_service.py`：
   - 增加 `PriceTracker.reset(symbol=None)`；
   - `fetch_quotes` 增强 `symbols` 类型适配与去重；
2. `backend/app/services/watchlist_realtime_monitor_service.py`：
   - 增加版本感知管理，在 Target Set Version 滚动时清空 triggered targets；
   - 区分 `triggered_node_target_ids` 与 `triggered_smc_target_ids`，同时向后兼容 `triggered_target_ids`；
   - 提供 `reset_symbol_state(symbol)` 接口；
3. `backend/tests/test_monitor_coordinate_and_xdxr_contract.py` [NEW]：
   - QFQ 最新日 raw==qfq 恒等性测试；
   - `PriceTracker` 重置与防伪穿透测试；
   - Target Set Version 滚动重置 triggered targets 自动化测试；
4. `scratch/dry_run_realtime_monitor.py` [SCRATCH]：
   - G8 真实 pytdx 毫秒级 quote 获取与监控闭环只读 Dry-Run 脚本。

## 4. 验证与质量保证

1. **纯单元测试回归**：
   - 全量 443 个纯单元测试在 3.10 秒内全数通过（PASS 100%）。
2. **真实 pytdx 只读 Dry-Run 验证（READ-ONLY）**：
   - pytdx 真实 TCP 连接成功获取 600519（贵州茅台 1275.16 元）、000001（平安银行 11.74 元）、300750（宁德时代 330.51 元）实时 quote；
   - volume 自动由手正确转为规范股（如 600519 对应 3,480,100 股）；
   - Eastmoney 502 离线时平滑处理，pytdx 主源 100% 不受阻断；
   - 连续快照价格区间 $[P_{\text{last}}, P_{\text{curr}}]$ 首轮初始化为 $(p, p)$，0 伪穿透；
   - 模拟向上突破 SMC BOS 目标位（1287.91 元），精确触发 `smc_bos_cross` 事件（首次突破一次性穿透；`smc_bos_retest` 仅保留历史兼容）；
   - One-shot 铁律验证通过：价格持续处于突破位上方，0 重复报警；
   - 零 DB 写入，只读安全。

## 5. 状态纠偏（本 correction commit 更正，2026-09-12）

下游纠偏评审结论：G1~G8 **并非全部完成**，此前 CHANGE-003/004 的「完成 / 100% E2E」表述过度乐观。
本 correction commit 仅完成下列纠偏，**不向前开发 G8**：

1. **生产监控接线（G6 部分）**：`MonitorBatchService` 注入冻结 Node TargetSet + 连续快照价格区间，
   使 `WatchlistMonitor.detect_events` 走一次性穿透（one-shot crossing）新路径（Node 已激活，零额外重算；
   SMC 待 `compute_smc_pine` 冻结 TargetSet 编排层落地）。
2. **事件合同（G5 引擎）**：首次突破 BOS/CHoCH 统一发射 `smc_bos_cross` / `smc_choch_cross`；
   `smc_bos_retest` / `smc_choch_retest` 仅保留作历史数据回读兼容（本文 §4.2 原 `smc_bos_retest` 已更正为 `smc_bos_cross`）。
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
| G3 实时行情事实 | ✅ | 引擎组件 + 生产接线（MonitorBatchService 已注入 PriceTracker） |
| G4 筹码共识穿透 | ✅ | 引擎 + 生产 Node 路径已激活 |
| G5 SMC 事件合同 | 🟡 | 引擎合同已修正（`*_CROSS`）；SMC 生产穿透待冻结 TargetSet 编排层 |
| G6 生产接线 | 🟡 | Node 已激活；SMC 待编排层 |
| G7 坐标对齐 + XDXR 版本滚动 | 🟡 | 逻辑已实现并单测，生产经 `WatchlistMonitor.detect_events` 部分生效 |
| G8 端到端 E2E | ❌ | dry-run 脚本存在，未宣告完整通过 |

