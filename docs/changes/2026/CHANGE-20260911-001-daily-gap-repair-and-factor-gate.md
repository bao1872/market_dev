# CHANGE-20260911-001 日线缺口修复与因子源门禁

- 日期：2026-09-11
- 基线：`d542d10e`（`fix(market): harden eod refresh and daily-only after-close`）
- 层级：**Level 2（契约敏感）** + 生产数据修复（Level 3，已获本任务显式授权）
- 状态：代码已提交；生产 2026-09-10 已修复并核验

## 1. 契约变更（有意为之，需下游确认）

### 1.1 `bars_daily.volume` 单位是「股」，不是「手」

- **事实**：canonical `bars_daily.volume` 单位为**股**。实测 600519 2026-09-09：
  DB = `3_222_611` 股；东方财富 `f5` = `32_226` 手；同花顺 = `3_222_611` 股。
- **缺陷**：`eod_market_snapshot_provider` 原先把东财 `f5`（手）直接入库，
  使全市场成交量缩小 100 倍。
- **修复**：新增 `SHARES_PER_LOT = 100`；`_parse_volume()` 对 `f5` ×100 转股。
- **影响**：仅影响**本次改动后新写入**的快照行；历史行未回填（见 §5 遗留）。
- **同花顺不受影响**：`ths_raw_daily_provider` 原始返回即为股，不缩放。

### 1.2 Eastmoney 兜底可关闭

`repair_market_wide_daily_gap(..., use_eastmoney_fallback=False)`。

- 原因：东财对生产出口 IP 硬封期间，每次兜底要跑满 3 轮 × 多主机重试（单只 10~20s）。
  5293 只标的里只要有几百只掉进兜底，整轮修复从分钟级退化到小时级（实测 4 分钟只进 1 块）。
- 默认仍为 `True`（保持原语义），仅由操作员在确认东财不可用时显式关闭。

## 2. 行为变更

| 变更点 | 位置 | 说明 |
|---|---|---|
| 快照单主机完整分页 | `eod_market_snapshot_provider` | `_fetch_full_snapshot_from_host()` 要求分页期间 `total` 恒定；漂移即整 host 作废，外层再换 host |
| 市场水位门禁 | 同上 | `validate_snapshot_market_watermark()`：全市场 `f124` 水位 ≥ 15:00 才接受当日快照 |
| 保留完整 `f124` | 同上 | `EodSnapshotRow.updated_at` 存完整时间戳，`trade_date` 改为派生 property |
| 停牌股复活 | `eod_daily_refresh_service` | `sync_instruments_from_eod_snapshot()` 对状态非 active 且当日行有效者复活 |
| 有效性唯一属主 | 同上 | `is_valid_snapshot_daily_row(row, trade_date)` 统一，落库与复活共用 |
| `period_counts["d"]` 修正 | `bars_scheduler_service` | 快照路径此前不累加，现 `+= upserted` |
| 延迟创建 ProcessPool | 同上 | 仅 `_run_parallel_period` 真正需要时创建；snapshot-only 盘后 `pool_creations=0` |
| `periods` 参数校验 | 同上 | 未知周期或空元组 → `ValueError`，禁止「静默 no-op 全计成功」 |
| 日线连续性硬门禁 | 同上 | `_scan_daily_continuity_gate()` 抛 `DailyContinuityBlockedError`，阻断 `_run_post_daily_phase` |
| 因子源健康门禁 | 同上 | `probe_factor_provider()` 快失败（前 3 server、`connect_timeout=1.0`、`max_retries=1`）；不可用 → `FactorSourceUnavailableError`，禁止 5000+ 次逐股 xdxr 重试 |
| 公司行为探测不再吞异常 | `adjustment_factor_service` | `detect_company_action_change` 抛 `CorporateActionProviderError`，不再返回 `None`（旧实现使「源挂了」与「无公司行为」不可区分） |
| pytdx 连接超时可配 | `core/pytdx_adapter` | `PytdxAdapter(connect_timeout=...)`，探测用 1.0s |

## 3. 新增数据源：`ths_raw_daily_provider`

- 端点：`https://d.10jqka.com.cn/v6/line/hs_{symbol}/{adjust}/{name}.js`
- `00`=不复权（**必需**；`01` 是前复权，混进 raw 会污染 canonical）
- 字段序：`date, open, high, low, close, volume, amount, ...`
  —— 与东财 `date, open, close, high, low` **不同**，不可复用东财解析函数
- 单位：`volume`=股、`amount`=元，均不缩放
- 北交所代码同样用 `hs_` 前缀（不是 `bj_`）

### 源选型实测（2026-09-11 生产出口）

| 源 | 结论 |
|---|---|
| 东财 `push2his` | **IP 级硬封**（`RemoteProtocolError`，含 `41.`/`1.` 分片与 `push2delay`）。SOCKS 换出口后仍不通 |
| 同花顺 `d.10jqka.com.cn` | 可用；直连被限流后经 SOCKS 出口 27.7 req/s、成功率 98.7% |
| 腾讯 `fqkline` | 可用但**无成交额**，不能单独落 canonical；可作 OHLC 交叉校验源 |
| 网易 `chddata` | 恒 502 |
| 新浪 | 可用但**无成交额** |
| pytdx `get_security_bars` | 2/10 server 可连但 `get_security_bars` 返回 `None`，日线不可用 |
| 雪球 | 需 `xq_a_token`（浏览器会话），不可用 |

## 4. 生产修复结果：2026-09-10

前置门禁（全通过）：

- **Gate A**（保真，走生产 `compare_db_vs_source_for_date`）：2026-09-09 抽样 200 只，
  `fetch_ok=200`，OHLC 逐位相等 200/200，VOL 200/200（max_abs=4 股），AMT 坏点 0 → `validate_consistency` 通过。
- **Gate B**（目标日，30 只预飞）：同花顺 `/00/` 09-10 覆盖 30/30，
  与腾讯 raw 日线 OHLC 交叉 30/30 逐位一致，OHLC 自洽 0 违规。

修复（走生产 `repair_market_wide_daily_gap`，`on_conflict_do_nothing`，分 4 轮收敛）：

| 轮次 | missing | fetched/inserted | still_missing | coverage |
|---|---|---|---|---|
| 1 | 5293 | 4983 | 129 | 0.9756 |
| 2 | 129 | 108 | 21 | 0.9960 |
| 3–5 | 21 → 2 → 0 | 2 / 0 / 0 | 19 | 0.9964 |

最终核验（2026-09-11 16:44）：

```
09-09: total=5286  traded(volume>0)=5276  suspended(volume=0)=10
09-10: total=5274  traded=5274           suspended=0
09-11: total=0                            ← 指纹未变，未写入

09-10 结构非法行 = 0；volume=0 行 = 0
09-09 指纹 count=5286 sum_close=149107.5500 sum_vol=111315030621.00 sum_amt=1835540762578.00（未变）
09-11 指纹 count=0（未变）
```

**对「09-09 有真实成交」的 5276 只，仅 2 只 09-10 无 bar**：
`301390 经纬股份`、`603159 上海亚虹`。同花顺与腾讯**双源一致**判定其最后交易日为 09-09 → **09-10 停牌**，非漏补。

剩余 17 只缺失的构成（均非回归）：

- 12 只停牌股（`000016` `*ST康佳A`、`002731`、`002870`、`002998`、`301139`、`600825`、`600929`、`605577`、`688291`、`688432` 等）：
  库中 09-04 起即为 `volume=0` 平盘占位行，09-10 本就不该有成交 bar。
- 6 只退市股（`000004 国华退`、`002808 恒久退`、`002898 赛隆退`、`300029 天龙退`、`600193 退市创兴`、`605081 退市太和`）：
  最后 bar 在 7 月。
- 1 只 `920305 *ST云创`：库中**从未**有 bar（含 09-09），历史性覆盖缺口，同花顺亦返回空数据。

副作用：09-10 没有写入停牌占位行，因此 `total` (5274) 低于 09-09 (5286)。
这是修复源只提供真实成交 bar 的必然结果，与「未修复」可区分：按 `volume>0` 口径 5274 / 5276 = **99.96%**。

## 5. 遗留（未做，需授权）

1. **历史 volume 单位未回填**：§1.1 只影响改动后新写入的行。若历史 `bars_daily.volume`
   存在按「手」写入的批次，需要单独的回填迁移（Level 3）。
   实测 09-09 存量与同花顺股数逐位相等，说明**近期存量是正确的股**，但不等于全部历史。
2. **同花顺 `last.js` 存在 CDN 陈旧窗口**：实测 `601212` 在第一次修复时 `last.js` 末根为 09-09
   （判定停牌），约 40 分钟后刷新出 09-10 与 09-11。修复流程靠**多轮重跑**收敛，
   单轮不能保证 100%；建议把「多轮收敛」写进运维 runbook。
3. **同花顺对生产出口 IP 会限流**：直连持续请求后 502 率从 ~5% 升到 >50%。
   本轮改用已授权的 SSH 主机做 SOCKS 出口（`ssh -D`）后恢复 27.7 req/s。
   `repair_market_wide_daily_gap` 已支持注入 `client`，但**出口配置尚未产品化**。
4. `test_after_close_phase0_control_flow.py` 的 12 个失败为**基线既有**（`git stash` 验证），
   根因是 `_validate_core_ready` 与测试 `_FakeSession` 的契约漂移，与本变更无关。

## 6. 证据文件

- 门禁与修复日志：`.tmp_r3d/repair2.log`、`.tmp_r3d/repair3.log`
- 修复结果 JSON：`.tmp_r3d/repair_result.json`
- 核验脚本：`.tmp_r3d/final_verify.py`、`.tmp_r3d/residual.py`
- 源选型实测：`.tmp_r3d/altall.py`、`.tmp_r3d/tune2.py`、`.tmp_r3d/preflight.py`
