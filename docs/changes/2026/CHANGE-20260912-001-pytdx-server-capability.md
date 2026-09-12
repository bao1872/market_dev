# CHANGE-20260912-001 pytdx server capability 感知与 daily/quote 单位契约

- 日期：2026-09-12
- 基线：`3e5b449b`（`feat(market-data): recover multi-day daily gaps and add replay gate`）
- 层级：**Level 2（契约敏感）**：只读网络取证 + 生产 server 选择逻辑修复（无 migration、无数据写入）
- 状态：代码已提交；G1B-2A 的两个子合同（daily 单位 / quote 单位）均由 direct 对照证明

## 1. 原故障（production 事实）

旧 `PYTDX_SERVERS`（10 台硬编码池）经 2026-09-12 只读逐台取证：

| 状态 | 台数 | 证据 |
|---|---|---|
| TCP 建连失败 | **8** | `TdxConnectionError: connection timeout error`（6）/ `other errors`（2） |
| 仅 xdxr 可用 | **2** | 可连接，但 `get_security_bars` 函数级 `TdxFunctionCallError` |
| bars 可用 | **0** | — |

后果：`PytdxAdapter` 无论如何轮转都拿不到 historical daily bars —— `max_retries=3` 的每一次
attempt 必然落在仅有的 2 台可连服务器（二者 bars 均已废）。同时同一批服务器的
`get_xdxr_info` 仍可用（R3 replay：5196/5196），造成「XDXR 正常 / historical daily 全失败」
的分裂现象。注意：`30/30 PytdxSourceError` **只能**证明「当前调用路径失败」，
不能推断「全池逐台 bars 失败」——本 Change 之前的表述已按此纠正。

## 2. 证据（只读、可重复）

- `backend/scripts/pytdx_server_sweep.py`：18 台 union（本项目 10 + chanlun-pro 当前离线池 10，
  去重后 18）逐台 connect + SH/SZ bars + SH/SZ xdxr，**每台 2 轮**，subprocess 硬超时 8s，
  connect timeout 1.5s，记录 DNS resolved IP：
  - `our_pool`：connect_ok=**2/10**，stable_bars=**0/10**，xdxr=2/10
  - `chanlun_pool`：connect_ok=**10/10**，stable_bars=**4/10**，xdxr=10/10
- `backend/scripts/verify_pytdx_daily_contract.py`（**production Adapter 路径**，4 台 × 40 只）：
  - `get_daily_bars`：**160/160** fetch_ok / exact-date；OHLC `bad=0`、`max_abs_diff=0`；`amount ratio ≈ 1`
  - `get_security_quotes`：**160/160** returned；`price_bad=0`、`price_max_abs=0`

结论：**既不是 pytdx 接口整体失效，也不是 retry 策略缺陷**，而是候选池腐化 +
operation capability 未分离。

## 3. 数据语义结论（冻结）

| API | 单位 | canonical 换算 |
|---|---|---|
| `PytdxAdapter.get_daily_bars().volume` | **股（shares）** | **×1（禁止 ×100）** |
| `get_security_quotes().vol` | **手（lots）** | **×100** |

- daily：4 台独立 server × 40 只，`pytdx daily volume / canonical DB volume` median = **1.0**
  （min `0.99999993` / max `1.00000005`，偏差为 pytdx `vol` 的 float 表示精度）。
  09-09（修复前既有行，来源不同）与 09-11（THS 修复行）**两条独立 DB 来源同时成立**
  （例：600519 09-09 `3,222,611` = `3,222,611.00`）。
- quote：**同一 server、同一 symbol**，`quote.vol / daily.volume` median = **0.00999998937**
  （min `0.0099989839` / max `0.0100000000`）→ 手；`amount ratio = 1.0`；
  `servertime ≈ 15:30:2x`（周五收盘快照）。
- **两个 endpoint 是独立 contract**：不得由 daily 推断 quote，也不得把 quote 的 ×100 施加到 daily。
  `pytdx_eod_snapshot_provider.py` 中 quote 侧 `raw_volume` 的 `UNVERIFIED` 标记应据此更新。

## 4. 根因

1. **静态 server pool 长期未维护**：旧池 10 台中 8 台 TCP 已不可达、2 台 bars 已废。
2. **operation capability 未分离**：把「server 是否健康」当作单一布尔，导致
   `get_security_bars` 的函数级失败在选择层无法与可用的 `get_xdxr_info` 区分。

## 5. 修复（本 Change 落地）

- 候选池按 2026-09-12 取证重建：**4 台 bars+xdxr+quote**（其中 3 台为 hostname 形式域名）
  + **6 台 xdxr-only**；8 台不可达旧 IP 移出候选池。
- 新增 `PytdxServerCapability`（`bars` / `xdxr` / `quote`；**`quote=None` 表示未验证，不得当作 False**）
  与 `_OPERATION_CAPABILITY` 映射（不改 public API 签名）。
- `_call_with_reconnect` / `_connect_excluding` 按 operation 的 capability 选择 eligible server；
  **capability 隔离**：某 operation 的 source failure 只冷却该 capability，
  bars 失败不污染 xdxr / quote。
- 运行时健康为 **process-local + TTL**（默认 30 分钟）冷却，非永久黑名单，不持久化 Redis/DB；
  connect 失败同样进入短期冷却，避免每个请求重复等 timeout。
- **hostname 保持 hostname**（实测 DNS 每次解析结果不同，3 次均变），不写死当次 resolved IP。
- 可重复取证工具化：`pytdx_server_sweep.py`（capability sweep）与
  `verify_pytdx_daily_contract.py`（daily + quote 单位 direct 对照）。

## 6. 未做 / 边界

- 未做 Redis/DB 持久化 best-IP；未做 per-symbol 全池 sweep（筛选复杂度 O(server count)，
  仅在启动 / TTL 到期 / 人工诊断时执行）。
- 未改 `PYTDX_SERVERS` 之外的 retry 语义；未接 G1B-2B。
- `test_eod_external_ab.py` 的 5 个 `external_data` 失败为**基线既有**
  （`git stash` 对照结果一致），与本 Change 无关：东财出口被封 + 该测试自身旧的单位假设。

## 7. 证据文件

- `.tmp_r4/sweep_results.json`、`.tmp_r4/sweep.log`
- `.tmp_r4/r4_contract.json`（Phase A–E）
- `.tmp_r4/xcheck_units.py` 输出（09-09 / 09-11 跨来源原始数值对照）
