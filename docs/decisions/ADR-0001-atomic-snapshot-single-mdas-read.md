# ADR-0001: Atomic Chart Snapshot 单 MDAS 读取

- **状态**: accepted
- **日期**: 2026-07-21
- **关联**: CP-16 / SNAP-01 / PRD V2.0 §4.2

## Context（背景）

旧 Atomic Chart Snapshot 实现存在两次市场数据读取：第一次读取 bars 后用于渲染，第二次在指标计算内部再次读取市场数据。这导致：

1. 单次请求产生两次 MDAS 调用，违反 SSOT 与单源原则（AGENTS §七.12）
2. 第二次读取可能返回与第一次不同的数据（特别是实时场景下），导致 `source_bar_hash` 与 `adj_factor_hash` 不一致
3. Redis 缓存层无法保证原子性——bars 帧与 indicators 帧可能来自不同时间点的市场数据
4. 前端无法判断 snapshot 是否为原子一致帧，被迫处理 mismatch 异常

约束（PRD V2.0 §4.2 SNAP-01 + AGENTS §七.13）：
- Atomic Snapshot 必须保证原子一致性（bars + indicators 同源）
- Redis 只缓存最终 Snapshot 响应，不缓存中间结果
- 前端只请求 chart-snapshot，独立的 Bars/Indicators 请求不恢复

## Decision（决策）

Atomic Chart Snapshot 端点（`backend/app/api/chart_snapshot.py`）使用**单次 MDAS 读取**策略：

1. **单次读取**：API 入口调用 `MarketDataAggregationService.get_bars(...)` 一次，返回完整 DataFrame
2. **直接传递 DataFrame**：将 DataFrame 直接传给 `CanonicalComputationService.compute(...)`，禁止 Canonical 内部再次调用 MDAS
3. **CanonicalInput 封装**：使用 `CanonicalInput` 数据类封装 instrument_id / timeframe / bars_df / adjustment_as_of / source_bar_hash / adj_factor_hash，作为指标计算的统一输入
4. **Redis 仅缓存响应**：缓存 key 基于最终 Snapshot 响应的 `source_bar_hash + adj_factor_hash + adjustment_as_of + indicator_view`，缓存值为完整 JSON 响应；禁止缓存中间 DataFrame 或 CanonicalInput
5. **render_frame.matched 验证**：响应中 `render_frame.matched=true` 表示 bars 与 indicators 同源；前端无需 mismatch 处理
6. **测试覆盖**：
   - `test_chart_snapshot.py::test_single_mdas_call`（断言 MDAS 调用计数为 1）
   - `test_chart_snapshot.py::test_hash_consistency`（断言 bars.source_bar_hash == indicators.source_bar_hash）
   - `test_chart_snapshot.py::test_redis_unavailable`（Redis 不可用时仍返回正确响应）
   - `test_chart_snapshot.py::test_partial_bar_handling`（partial bar 不破坏原子性）
   - `test_chart_snapshot.py::test_render_frame_matched`（render_frame.matched 始终为 true）

## Consequences（后果）

- **正面影响**：
  - 单次 MDAS 调用降低数据库负载 ~50%
  - 原子一致性保证，前端不再需要处理 mismatch 异常
  - 缓存策略简化（只缓存最终响应）
  - `source_bar_hash` 与 `adj_factor_hash` 始终一致，便于审计

- **负面影响**：
  - 单次读取返回完整 DataFrame，内存占用略增（约 +5%）
  - 指标计算无法在内部按需重新查询（必须用传入的 bars）

- **风险与缓解**：
  - 风险：传入 bars 数量不足导致指标计算失败
  - 缓解：MDAS 请求参数中显式包含 `warmup_bars` 与 `limit`，保证足够输入
  - 风险：DataFrame 传递跨函数边界增加耦合
  - 缓解：使用 `CanonicalInput` 数据类封装，明确接口契约

- **后续约束**：
  - 写入 AGENTS §七.13「Atomic Chart Snapshot 单 MDAS 读取」硬规则
  - 前端禁止恢复独立 Bars/Indicators 请求（AGENTS §七.13）
  - 任何修改 `chart_snapshot.py` 必须运行 `test_chart_snapshot.py` 全部测试
