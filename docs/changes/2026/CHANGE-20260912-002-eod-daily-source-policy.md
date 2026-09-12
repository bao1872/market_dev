# CHANGE-20260912-002 正常盘后日线多源编排（pytdx 主源）与 Universe Discovery 解耦

- 日期：2026-09-12
- 基线：`501b6eebe557ea67a603e2f4aa1074e1cd7df3ca`
- 层级：**Level 2（契约敏感）**：盘后日线行情主源编排切至 pytdx，Universe Discovery 与价格主源解耦，彻底删除 `refresh_raw_daily_only`
- 状态：`verified_code`（纯单元测试 188 passed，Section 17 失败矩阵 A~H 全覆盖，2026-09-11 真实只读 dry-run 验证通过，无 migration/未写 DB）

## 1. 变更背景与架构演进

旧架构（Eastmoney 单一快照源）：
- Eastmoney full-market snapshot 同时承担三种职责：
  1. Universe discovery（新股发现、停复牌状态、股票名称更新）；
  2. SH/SZ/BJ 全市场日线价格与成交量行情；
  3. 若 Eastmoney 失败，直接阻断或回退全量逐股历史日线爬取。
- 存在冗余入口 `refresh_raw_daily_only()`：自己重复 `can_use_same_day_eod_snapshot`、交易日、连续性门禁，与主调度编排形成双入口分叉。

新架构（G1B-2B 裁决落地）：
- **职责解耦与单一归属**：
  - **Eastmoney 全市场快照**：每个交易日盘后拉取 **恰好 1 次**（分页流），仅负责：
    ① universe discovery / name / status / BJ 标的发现；
    ② BJ 当日日线（pytdx 不支持 BJ）；
    ③ pytdx 失败时的现成内存 fallback 缓存（绝不重复网络拉取 Eastmoney）。
  - **pytdx 为 SH/SZ 日线主源**：批量 quote（`ceil(N/80)` 批次）+ 2 个 exact-date daily sentinel 校验 + 经 `to_canonical_eod_rows` 转换（volume ×100 手转股）。
  - **Fallback 与降级**：若 pytdx 整体失败或 sentinel 校验不符，直接使用内存中已拉取的 Eastmoney 缓存兜底；若 pytdx 部分股票缺失，缺失的 SH/SZ 标的由 Eastmoney 补充；
  - **独立容灾**：Eastmoney discovery 失败时记录 `universe_discovery_status="failed"`，使用已有 DB active universe 继续执行 SH/SZ pytdx，不阻断主源；若 pytdx 与 Eastmoney 均失败，抛出 `SnapshotProviderError` 交由 legacy 逐股回补；
  - **Previous Close 契约**：`_refresh_daily_from_market_snapshot()` 返回的 `eod_previous_close_by_symbol` 映射中的数值严格来自本轮实际被选为 canonical row 的数据源（pytdx 优先来自 pytdx，fallback 来自 Eastmoney）；
  - **冗余入口清理**：彻底删除 `refresh_raw_daily_only()`，全局零 caller。

## 2. 核心性能原则

在正常交易日盘后，全市场网络与 DB 成本保持为：
- Eastmoney 全市场 snapshot：**恰好 1 次** 分页流；
- pytdx SH/SZ quote：**约 70 批**（`ceil(SH+SZ / 80)`）；
- pytdx exact-date daily sentinel：**恰好 2 次**（1 SH + 1 SZ）；
- 标的发现同步 + raw daily 批量 upsert：**O(1) bulk DB 操作**；
- 绝无 5000 次逐股 daily K 循环，绝无一个 run 内二次网络拉取 Eastmoney。

## 3. 代码变更清单

1. `backend/app/services/bars_scheduler_service.py`:
   - `BatchResult` 增加可观测性指标：`daily_primary_source`, `daily_pytdx_rows`, `daily_eastmoney_rows`, `daily_bj_rows`, `universe_discovery_status`；
   - 彻底删除 `refresh_raw_daily_only()`；
   - 新增 `_merge_daily_snapshot_rows(trade_date, id_by_symbol, pytdx_rows, em_rows)` 纯函数：保证 SH/SZ pytdx > EM fallback 优先级，BJ 必来自 EM；
   - 新增 `_fetch_discovery_snapshot()`：调用 Eastmoney snapshot，单个 run 仅调用 1 次；
   - 新增 `_fetch_pytdx_primary_eod()`：调用 pytdx fetch + verify + canonical convert；
   - 重构 `_refresh_daily_from_market_snapshot()`：实现解耦编排、状态记录、单次批量 upsert 及真实所选源 previous_close 证据映射。
2. `backend/tests/test_eod_daily_refresh_service.py`:
   - 删除 `refresh_raw_daily_only` 废弃测试组（原 Section 14）；
   - 更新既有 snapshot 计数测试以适配 pytdx 主源编排。
3. `backend/tests/test_eod_daily_source_policy.py`:
   - 新增针对 G1B-2B 的独立多源策略测试文件，覆盖 Section 17 失败矩阵（Case A~H）、性能断言（EM fetch==1, sentinel==2, no per-symbol daily loop）、BatchResult 观测字段、previous_close 一致性、`refresh_raw_daily_only` 删除断言。

## 4. 失败矩阵与策略规范

| 场景 | 条件 | 行为 | 最终 source 标记 |
|---|---|---|---|
| Case A | EM ✅, pytdx ✅ | SH/SZ→pytdx, BJ→EM；EM fetch=1, sentinel=2 | `daily_primary_source="pytdx"` |
| Case B | EM ✅, pytdx ❌ | SH/SZ/BJ→内存 cached EM；绝不重复 fetch EM | `daily_primary_source="eastmoney"` |
| Case C | EM ✅, pytdx partial | pytdx 覆盖的用 pytdx，缺失的用 EM fallback，BJ 用 EM | `daily_primary_source="mixed"` |
| Case D | EM ❌, pytdx ✅ | 使用 DB 已有 universe，SH/SZ→pytdx；不阻断 pytdx | `universe_discovery_status="failed"` |
| Case E | EM ❌, pytdx ❌ | 快速路径失败，抛 `SnapshotProviderError`，外层走 legacy | — |
| Case F | EM ✅, sentinel mismatch | pytdx 数据不被信任，降级到内存 cached EM | `daily_primary_source="eastmoney"` |
| Case G | Precedence | 同一 SH/SZ 标的，pytdx 覆盖 EM；prev_close 随实际选中的行源 | — |
| Case H | BJ policy | BJ 标的绝不出现在 pytdx 请求列表，BJ 行必来自 EM | `daily_bj_rows` 准确记录 |

## 5. 验证证据

1. **纯单元测试（PURE_UNIT_TEST=1）**：
   - `test_eod_daily_source_policy.py`: 10 passed
   - `test_eod_daily_refresh_service.py`: 83 passed
   - `test_pytdx_eod_snapshot_provider.py`: 28 passed
   - `test_pytdx_capability_selection.py`: 9 passed
   - `test_bars_scheduler_factor_audit.py`: 12 passed
   - `test_xdxr_refresh_planner_wiring.py`: 46 passed
   - 总计：188 passed, 12 skipped in 1.94s
2. **相关 daily 回归测试**：
   - `test_daily_gap_recovery_service.py`, `test_daily_gap_repair_service.py`: 86 passed
3. **真实只读 Dry-Run（2026-09-11 交易日）**：
   - Eastmoney discovery: 1 次网络请求，成功拉取 5913 行；
   - pytdx SH/SZ quotes: 批量获取成功，同连接 provenance 成立；
   - sentinel verification: 恰好 2 次 daily 调用（1 SH + 1 SZ），2026-09-11 exact-date 校验通过；
   - canonical 转换: volume ×100 手转股正确；
   - 多源合并: pytdx 优先、BJ 来自 Eastmoney、0 DB 写入。
4. **代码质量**：
   - `ruff check`: PASS
   - `git diff --check`: PASS
