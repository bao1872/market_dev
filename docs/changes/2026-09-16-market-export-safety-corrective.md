# 决策记录：S2-A-C1 Market Export Safety Corrective（C1a）

- 日期：2026-09-16
- 基线 SHA：`50c32b857786ed047c1257d9048fd234def354da`
- 阶段：C1a-C3（安全导出管线 + admin-only 熔断 + 固定两列出货）
- 后续：C1b（验证通过后再开放普通用户导出），不在本 commit

---

## 1. Incident Mechanism / 事故机制

原有 `/market/export` 端点把分页重型 read model 直接当作全量导出器使用：

```python
result = await get_market_stocks(
    ..., page=1, page_size=MAX_EXPORT_ROWS + 1,   # = 10001
)
```

而 `get_market_stocks` 本是为 50/100 行页面设计的：对每只返回的股票还要批量查询
两根日线、`StockFeatureSnapshot.summary_payload`（完整 JSON，非单列）、把 `first_pyramid`
展平成 99 字段、chip snapshot、board membership、最新 bar 日期，最后组装完整
`MarketStockRow`。再叠加：

```text
rows → data_rows → all_rows → parts[] → sheet_xml(str) → sharedStrings → BytesIO ZIP → bytes
```

生成 Excel 的 `generate_xlsx()` 是**同步 CPU 工作**（XML 拼接 + ZIP 压缩）。

生产 backend 为**单 worker**（`uvicorn app.main:app`，无 `--workers N`）。一次重导出会把
数万行完整 read model 同时驻留内存多副本，并在唯一事件循环上长时间执行同步 CPU 工作，
表现为：点一次“导出 Excel”，整个站点 API 超时、转圈，像快崩了。

测试债：原测试把 `get_market_stocks` 与 `generate_xlsx` 全部 mock 掉，并**显式锁定
`page_size == 10001`**，只验证“语义正确”，从未验证真实 DB/CPU/内存/响应行为——这正是
危险结构能长期存在的原因。

---

## 2. Why the Old Design Failed / 旧设计为何不成立

**“复用 canonical query owner 做导出”方向被判定错误。**

- 列表分页（50/100）与批量导出**共享 read model** 不可行：SQL 条数不随 page_size 增长，
  但单只股票的处理工作量（snapshot/flat/chip/board/bar）恒定；从 50 股变 5000 股，
  每条工作量放大约两个数量级，SQL 条数不变 ≠ 工作量不变。
- `MAX_EXPORT_ROWS = 10000` 被同时当成“业务数量上限”与“一次性加载行数”，是概念混淆：
  它只能约束**业务允许导出的最大行数**，绝不能成为 `page_size` 或一次性 Python
  materialization 数量。新代码硬性要求 `get_market_stocks(... page_size=10001)` 的
  production call count = 0。
- `generate_xlsx` 的全内存多副本 + BytesIO 终态，与“流式、增量、脱离事件循环”目标相悖。

> 语义同源 ≠ read model 同源。
> 列表与导出共享**筛选/排序/scope/FP 字段定义/canonical CoreRun 身份**，但**物理 read model 不同**。

---

## 3. New Export Contract / 新导出合同

新增窄 owner `app/services/market_export_service.py`（modular monolith 内服务，非新框架/
新 worker/新表/migration）：

1. **请求先 fail-fast**：`validate_export_columns` 在执行任何重 DB 查询前校验
   `visible_columns`（非空、列数上限、key 不重复、禁止 action/select 类、仅白名单基础列或
   `fp_*` 字段、`data_type` 合法、`title` 长度上限）。服务端决定 column→source，
   **不接受客户端 `payload_key` 控制读取路径**。
2. **查询语义复用，read model 不复用**：`_assemble_market_query()` 与 `get_market_stocks`
   共享同一套 helper（`_build_search_conditions` / `_parse_sort` / `_build_state_filter` /
   `_build_board_filter_conditions` / `_parse_fp_filter` / `_parse_fp_sort` /
   `_needs_snap_lateral` / `_build_snap_lateral` / `_build_chip_lateral` /
   `_build_display_snapshot_query` / `resolve_current_core_run`），禁止复制第二套规则。
3. **`MarketExportPlan` source planning**：按可见列决定 `needs_price / needs_snapshot /
   needs_boards / needs_chip` 与所需 `fp` keys。只加载真正需要的 source；不需要 chip/board
   的导出不加载它们。补齐历史上 `stock_name/stock_name_op` 漂移（concept/industry/state/
   fp_filter/keyword 同为 AND）。
4. **filtered COUNT 先行**：先 `SELECT COUNT`；`> MAX_EXPORT_ROWS` 立即 422，**绝不先查出
   完整结果集再判上限**。
5. **有界分批**：`EXPORT_BATCH_SIZE = 250`（200~500）。任何一次 export DB fetch ≤ 250 行；
   `offset` 分页 + 稳定 `ORDER BY ... symbol` 副键，无重排/重复/遗漏。
6. **轻量 row**：导出只构造 `dict[column_key → cell]`，不再组装完整 `MarketStockRow`、
   不再把整个 `summary_payload` 在 Python 端展平 99 字段再只取 8 个；`fp` 仅取所需 keys。
7. **低内存 Excel writer** `MarketXlsxWriter`：`TemporaryDirectory` 内逐 batch 增量追加
   worksheet XML（inlineStr，无 sharedStrings 全量驻留）；`finalize` + `zipfile` 写临时
   xlsx；**不持有 `all_rows`、不持有整张 sheet XML string、不持有 BytesIO 终态**。
8. **CPU 工作脱离事件循环**：`writer.add_rows` / `writer.build_zip` 均经 `await
   asyncio.to_thread(...)`。有确定性测试证明 writer 在 worker 线程运行时事件循环 heartbeat
   仍推进。
9. **StreamingResponse**：最终文件分块读取并以 `StreamingResponse` 返回；
   `async generator` 的 `finally` 保证成功/断开/异常均清理临时目录。
10. **资源保护（C1a 最保守）**：全局导出租约（Redis `SET key token NX EX ttl` + Lua
    compare-and-delete，仅持有者可释放，防 ABA）。忙时 HTTP 429。单 worker 生产环境
    当前 `global = 1`，不排队、不 sleep/retry。
11. **临时安全熔断**：C1a 后端 `POST /v1/market/export` 使用 canonical `require_admin`，
    普通用户直接 403。前端 `MarketWorkspacePage` 仅 admin 渲染“导出 Excel”按钮，并加
    `exporting` in-flight 锁防双击/多 tab。

**硬约束（禁止回归）**：`get_market_stocks(page_size=MAX_EXPORT_ROWS+1)` 的 production
call count = 0；任何一次 export DB fetch 行数 ≤ `EXPORT_BATCH_SIZE`；不存在完整
`all_rows` / 整张 sheet XML string / BytesIO 终态 xlsx。

---

## 4. Hard Gates Before Member Reopening / 重新开放普通用户权限的硬门

C1a 是**临时安全熔断**，不是最终产品权限。C1b 验证通过后恢复：

```text
market_data     → 可导出 market
self_selection  → 可导出自己的 watchlist
admin           → 全部可导出
```

在以下门禁达标前，保持 admin-only 熔断（验证在远程 verify 运行时 + 部署后 runtime 压力测试完成）：

- **语义一致性门**：export 与列表使用同一筛选/排序/scope/fp/canonical CoreRun 语义
  （共享 helper，见 §3.2）。
- **资源门（压力测试）**：真实规模（≥5000 行）下确认
  - 数据库单次 fetch 有界（≤ `EXPORT_BATCH_SIZE`）；
  - Python 不持有完整导出集重型 read model；
  - Excel CPU/文件工作脱离唯一事件循环（不阻塞其他 API）；
  - 并发请求不倍增资源压力（租约忙时 429）。
- **指标门**：记录并复核耗时 / 峰值 RSS 或分配 / 输出大小；异常规模下不得出现数百 MB 级
  瞬时占用或事件循环长时间阻塞。
- **正确性门**：导出行集合与筛选条件一致（无重复/遗漏/越权范围）。

### Residual Risk / Follow-up

- 旧 `/strategy-runs/{run_id}/results/export` 仍使用 `MAX_EXPORT_ROWS + 1` +
  `generate_xlsx` 旧批量路径，属于独立后续风险。**本 corrective 不扩大范围修改它**；
  如需统一低内存 writer，作为单独任务评估（零语义风险前提下方可复用新 `MarketXlsxWriter`）。
- 前端 `StrategyDataTable` 保持通用，不引入权限判断（UI policy owner = page，security
  owner = 后端 `require_admin`）。
- 列表读取权限（`market_data`/`self_selection`/`admin` 对 `GET /v1/market/stocks` 的访问）
  未被本变更收紧。

---

## 5. Export Projection Contract (C1a-C3 更新)

**产品合同在 C3 阶段进一步收窄**：Excel 永远只导出两列——`股票名称`（Instrument.name）与
`股票代码`（Instrument.symbol）。这是一次**导出链路减法**，而非在 `visible_columns`
架构上硬限制两列。

> `visible_columns` is removed from the `/market/export` contract.
> The client does not choose export columns.

```text
Export Projection Contract

Market list export intentionally exports only:
1. 股票名称
2. 股票代码

The client does not choose export columns.

Filtering/sorting determines row membership and order only.

The export path must not perform display-field enrichment
for price, board, snapshot, first-pyramid, or chip data.

If a filter/sort requires one of those sources,
the canonical market query owner may use it to determine
membership/order, but it is not loaded again for export projection.
```

### 5.1 本 corrective 实际改动（相对 C1a）

- **请求合同**：`MarketExportRequest.visible_columns` 已删除；服务端不再接受客户端列定义。
  `build_export_plan` 不再做列白名单校验（`validate_export_columns` 仅保留给 legacy
  strategy export），不再计算 `needs_price/needs_snapshot/needs_boards/needs_chip`。
- **服务端固定列**：`market_export_service.MARKET_EXPORT_COLUMNS = (name, symbol)`，
  顺序固定（名称 → 代码）。`MarketXlsxWriter` 始终使用该集合。
- **`MarketExportPlan` 退化为纯 query 语义**：仅 `scope/query/state/industry/concept/
  fp_filter/fp_sort/sort/stock_name/stock_name_op`；不再承担 output source planner。
- **删除导出 enrichment pipeline**：`_cell_value` / `_fetch_prices` / `_fetch_snapshots`
  / `_fetch_chips` 及其 `price_map/flat_map/payload_map/snap_meta/boards_map/
  chip_flat_map` 全部从 market export service 移除（相关 import 一并清理）。
- **`_fetch_batch_rows` 轻量化**：只执行 `_assemble_market_query` 已装配好的
  筛选/排序 query，每次 ≤ `EXPORT_BATCH_SIZE`，仅映射 `name` / `symbol` 两列；
  不随后再查询 bars/snapshot/chip/boards。

### 5.2 明确不变的边界

- **筛选/排序语义完整保留**：`_assemble_market_query` 仍按 `fp_filter/fp_sort/industry/
  concept/state/stock_name/sort` 决定行集合与顺序；若筛选/排序本身需要 snapshot/chip/
  board/bar LATERAL，canonical query owner 仍可使用它们确定 membership/order——**但**
  export projection 不再为“显示字段”做第二轮 enrichment。这是本轮最重要边界。
- **资源安全机制全部保留**：`MAX_EXPORT_ROWS=10000`、`EXPORT_BATCH_SIZE=250`、全局导出租约
  （并发=1，忙时 429）、`prepare_market_export` 先于 `StreamingResponse` 完成所有重工作、
  临时文件增量 writer、`finally` 清理——不因列减少而恢复“一次性 10000 行 / BytesIO 终态 /
  整张 sheet_xml string”。
- **admin-only 熔断保留**：`POST /v1/market/export → require_admin`；普通用户权限恢复属 C1b。
- **前端**：`buildMarketExportRequest` 停止发送 `visible_columns`；`MarketExportColumn`
  类型删除；in-flight 锁（`tryAcquireExportUiLock`/`releaseExportUiLock`）与按钮可见性
  逻辑保持 C1a 不变。

---

## 6. Export Streaming Contract (C4)

C4 进一步把 export 从「filtered COUNT + N 次 OFFSET batch」收敛为「1 次有界 server-side
流式 SELECT」。

**性能合同（架构级，非 runtime 实测峰值）**：

```text
Market export does not use OFFSET pagination.

After canonical query assembly, export data is consumed by
one bounded server-side streaming SELECT with LIMIT 10001.

The API never performs one database SELECT per export batch.
EXPORT_BATCH_SIZE controls cursor partition / Python memory,
not SQL query count.
```

固定区分：

```text
MAX_EXPORT_ROWS    = business cardinality cap（业务允许导出的最大行数）
EXPORT_BATCH_SIZE  = in-memory / cursor partition bound（Python 内存 / cursor 分片上限）
SQL export-row query count = 1
```

实现要点：
- `_build_export_rows_stmt(ctx)`：`ctx.base_stmt.with_only_columns(name, symbol,
  maintain_column_froms=True).limit(MAX_EXPORT_ROWS + 1)`；OFFSET 分页彻底移除。
- `prepare_market_export`：`await db.stream(stmt)` → `async for partition in
  result.partitions(EXPORT_BATCH_SIZE)`；读到第 10001 行立即 422（不再执行完整 count scan）。
- 内存：任何时刻只保留一个 `EXPORT_BATCH_SIZE` 的 partition；Python 不持有完整导出集。
- 资源生命周期（429 忙 / query 异常 / 流异常 / 10001 行超限 / writer 异常）：全部
  `release_lock` + `shutil.rmtree(tmp_dir)` + `await result.close()`，绝不泄漏。

**硬约束（禁止回归）**：
- SQL export-row query count = 1（N+1 次 OFFSET SELECT = 0）。
- 不存在 `offset(` / `batch_index` / `(total + batch - 1) // batch` 之类的分页路径。
- 不执行 filtered COUNT 查询（MAX_EXPORT_ROWS 是 cardinality cap，不是报表统计需求）。
- 内存中最多一个 `EXPORT_BATCH_SIZE` 的 partition。

**验证状态**：PURE 单元测试已证明 query count=1 / scalar=0 / LIMIT=10001 / OFFSET 缺失 /
projection 仅 name+symbol / 5000 行 20 partition / 10001 行 422 即清理。真实 DB 5000/10001
压力验证由远程 verify 运行时（`PANJI_REMOTE_VERIFY_DB_TEST=1`）在该 commit 审计通过后的
受控阶段执行；该阶段的容器 RSS / 峰值内存验收留待后续 admin runtime pressure gate，
本轮不宣称任何 runtime peak-memory 数字。
