# CHANGE-20260817-001 — 竞价 120-bar 历史回补落库 PostgreSQL

- **日期**：2026-08-17
- **类型**：新能力（历史数据回补落库）/ 行为变化（回补 runner 新增 DB 写入）
- **阶段**：EXPLORATION（Correctness Gates 生效）
- **关联**：CHANGE-20260816-003（回补 runner / member-fact JSON）；迁移 077_auction_analysis
- **状态**：implemented_unconfirmed（代码+单测完成，PG 验证与远程实写待用户授权执行）

---

## 1. 需求与决策（用户明确选择）

回补目标：120 个官方 A 股交易日（截至 FROZEN `as_of=2026-08-14`，非自然日）的
09:25 竞价快照，**回补进 PostgreSQL**，成为系统可查询历史行情。

用户澄清的三项决策：
- **落点**：PostgreSQL（`auction_final_quotes` 表），非仅本地 JSON。
- **运行环境**：远程 `panji-prod`（SSH tunnel + 远程脚本），符合全市场任务边界。
- **as_of**：保持 FROZEN `2026-08-14`，不滚动。

---

## 2. 实现方案（与计划一致，附一处契约修正）

### 2.1 落库目标（复用，无新 migration）
- 表 `auction_final_quotes`（迁移 077 已建，含唯一约束
  `(trade_date, instrument_id, source, capture_run_id)` 与索引
  `ix_auction_final_quotes_inst_date`）。
- 父表 `auction_quote_capture_runs`（唯一约束 `(trade_date, source, test_namespace)`）。
- **不新建 migration**，不改动现有 API/frontend/Review/Scope。

### 2.2 新增 writer 服务
`backend/app/services/historical_auction_backfill_writer.py`：
- `get_or_create_historical_capture_run`：每交易日 1 个历史 `CaptureRun`，
  `source="historical_backfill"` + `test_namespace="historical_backfill"`，与 live 隔离。
- `write_bar_quotes`：chunk 小事务幂等 upsert（`pg_insert` ON CONFLICT DO UPDATE，
  利用现有唯一约束），返回 `{written, skipped, failed}`。
- `project_row_to_fact`：只读 runner `_project_member_row` 输出投影为
  `MemberFactProjection`（不重新计算竞价算法）。
- `finalize_historical_capture_run`：bar 完成后更新 run 状态/覆盖率（truthful degraded）。

### 2.3 runner 接线
`experiments/pytdx_auction_history/full_market_member_fact_backfill.py`：
- `_run_bar_partition` 新增可选 `db_writer` / `db_chunk_size` 参数（默认 None → 纯 JSON 行为不变）。
- production chunk 循环与注入路径：每 chunk/每 instrument 投影并经 `write_bar_quotes` 落库；
  RUN_ERROR 行不写库。
- manifest / data_quality 增加 `db_written_rows / db_skipped_rows / db_failed_rows`。
- `run_backfill` / `_run_backfill_impl` / `_bars_loop` 透传 `db_chunk_size=500`。

### 2.4 契约修正（相对原计划）
原计划假设"历史回补 ok 行自然被 scan 的 20 日窗口消费"。**代码核实后此为错误假设**：
`auction_scan_service.load_history_final_quotes` 与 `load_final_quotes_for_scan` 均硬编码
`source="verified_consensus"` + `test_namespace="production"`，历史回补用隔离 source
**不会被现有 scan 自动消费**。

修正决策（blast radius 最小、不污染 live truth）：
- 历史回补使用独立 `historical_backfill` source/namespace，与 live 唯一键不冲突。
- 该 run 仅作 `auction_final_quotes.capture_run_id` 外键 owner，不参与实时 truth/consensus/发布指针。
- **scan 消费侧扩展（让历史窗口包含 backfill 数据）属单独一轮，不在本轮范围**，需在用户授权后扩展
  `load_history_final_quotes` 的 namespace 参数或新增历史查询函数。

### 2.5 pywencai 竞价侧代码清理（阶段 7，审查 ref/竞价.md §3）

数据源合同冻结为 pytdx 后（PRD V3.1 §0.0-A），清理不再使用的 pywencai 竞价侧代码：

删除（`experiments/pytdx_auction_history/`）：
- `auction_wencai_backfill.py`（git rm，已跟踪）
- `explore_wencai_auction_probe.py` / `explore_wencai_auction_local.py` / `explore_wencai_raw.py`（未跟踪）
- `wencai_cookie.txt`（敏感凭据）

保留板块链（与竞价无关）：`wencai_client.py` / `wencai_board_provider.py` /
`board_facts_service.py` / `board_sync_service.py` / pywencai 依赖。

`.gitignore` 补防：新增 `experiments/**/wencai_cookie.txt`（审查 §3 指出原仅覆盖
`backend/wencai_cookie.json`）。

---

## 3. 性能与资源控制（针对历史死机）

| 控制项 | 机制 |
|---|---|
| 连接数 | runner 已约束 PytdxAdapter 单长连接（1 实例/进程，无 per-bar 重连爆炸） |
| 源数据量 | kernel 只抓 09:25:00~09:25:59 定向窗口，不扫全天逐笔 |
| 内存 | runner stream append member_fact JSON；writer 复用 in-memory 投影，不二次落 RAM |
| **DB 写入** | **每 bar ~5000 股票分 chunk（默认 500/批）小事务 upsert，禁止单事务 60 万行**（历史死机主因之一） |
| 幂等/断点 | `(instrument_id, trade_date, source, capture_run_id)` 唯一约束 → 重跑安全；进程中途退出后 resume 重跑整 bar，DB 幂等 upsert 补缺失行 |
| 失败隔离 | 单 instrument upsert 失败记 reason_codes + 该 bar db_failed_rows，不中断整 bar |
| 远程护栏 | 通过 `scripts/ops/panji-prod-ssh` 远程执行（不在本地跑全市场，符合 AGENTS.md §8）；建议远程用 `ulimit -v` / systemd `--memory` 限制，进程超内存 OOM-kill 而非拖垮整机 |

---

## 4. 测试

### 4.1 单测（已通过，PURE_UNIT_TEST=1）
`backend/tests/test_historical_auction_backfill_writer.py`（10 项：5 passed / 5 PG-skip）：
- 纯逻辑：`project_row_to_fact` 字段映射、`quality_status` 映射（zero_volume/error/invalid_volume）、
  未知 quality 归一化为 error。
- PG：`get_or_create` 幂等、upsert 幂等（重跑值更新行数不增）、skip None、resume 补写收敛、隔离于 live namespace。

### 4.2 PG 集成测试（已编写，待 PANJI_REMOTE_VERIFY_DB_TEST=1 运行）
`backend/tests/test_historical_auction_backfill_pg.py`（3 项，PURE 下 skip）：
- runner 接线落库：DB 行数 == JSON 行数（2 bars × 3 inst = 6），manifest 含 db 计数。
- resume：先 1 bar 后全量 → 行数收敛不重复（4 = 2×2）。
- 隔离性：backfill 数据不出现在 verified_consensus/production 查询中。

### 4.3 验证结论（待远程执行后填写）
- [ ] PG 集成测试 3 项 PASS（PANJI_REMOTE_VERIFY_DB_TEST=1）
- [ ] 远程 panji-prod 实写：120 bars × 全市场 A 股落库完成
- [ ] 行数核对：DB `auction_final_quotes WHERE source='historical_backfill'` ≈ 120 × eligible
- [ ] 对账：每 bar `db_written_rows == member_rows_written`（RUN_ERROR 除外）
- [ ] 远程进程 peak RSS 监控 < 内存上限，无 OOM
- [x] 阶段 7 pywencai 竞价侧清理：5 文件删除 + `.gitignore` 补防；板块链单测 `test_wencai_board_provider.py` 70 passed

---

## 5. 远程运行步骤（待用户授权执行）

**前置**：写真实业务库属不可逆操作，须用户显式授权。以下步骤不自动执行。

1. **本地提交候选实现**（dev 分支 checkpoint commit）。
2. **推送 + 远程 preflight**：
   ```bash
   scripts/ops/panji-prod-preflight
   ```
3. **上传回补脚本与 writer 到 panji-prod**（writer 已是 backend 包内模块，随部署同步；
   experiments 脚本需 `docker cp` 或 scp 到远程 `/app/scripts` 镜像层，或远程容器内执行）。
4. **远程执行**（ulimit 护栏 + nohup 后台，日志落文件）：
   ```bash
   scripts/ops/panji-prod-ssh
   # 远程容器内：
   cd /app && ulimit -v <MEM_LIMIT_KB>   # 内存上限，防 OOM 拖垮整机
   nohup .venv/bin/python experiments/pytdx_auction_history/full_market_member_fact_backfill.py \
       --mode live --as-of 2026-08-14 \
       > /var/log/auction_backfill_120bar.log 2>&1 &
   ```
5. **进度监控**（每 bar 打印 peak RSS / pytdx 请求数 / db 写入速率）：
   ```bash
   tail -f /var/log/auction_backfill_120bar.log
   # 或看 runner 自带 progress.json / partition_manifest.json
   ```
6. **完成核对**（远程 PG）：
   ```bash
   docker exec trading-postgres psql -U bz -d bz_stock -c \
     "SELECT count(*) FROM auction_final_quotes WHERE source='historical_backfill';"
   ```
7. **对账**：对比 `output/member_fact_120bar/2026-08-14/` 的 JSON 行数与 DB 行数。

---

## 6. 风险与开放问题

- **scan 消费**：本轮回补数据进入 `historical_backfill` namespace，**现有 scan 不消费**。
  若需纳入历史竞价额中位数/分位窗口，需单独一轮扩展 scan 读取（用户授权后）。
- **trade_date 口径**：严格用官方日历 `previous_trading_dates(as_of,120)`，禁止自然日；
  禁止 future leakage（只写 <= as_of 的 bar）。
- **qfq 复权**：Lane B 投影沿用 kernel 既有 qfq 逻辑，pit_gap=None 走 fail-close，不 fallback。
- **blast radius**：不改动 Scope/Review/frontend/API；auction_scan_service 现有读取不受影响
  （其只读 verified_consensus/production，与 backfill 数据无交集）。

---

## 7. 文件清单

新增：
- `backend/app/services/historical_auction_backfill_writer.py`
- `backend/tests/test_historical_auction_backfill_writer.py`
- `backend/tests/test_historical_auction_backfill_pg.py`

修改：
- `experiments/pytdx_auction_history/full_market_member_fact_backfill.py`
  （`_run_bar_partition` / `_bars_loop` / `run_backfill` / `_run_backfill_impl` 接线 + manifest DB 计数）

删除（阶段 7 pywencai 竞价侧清理）：
- `experiments/pytdx_auction_history/auction_wencai_backfill.py`（已跟踪，git rm）
- `experiments/pytdx_auction_history/explore_wencai_auction_probe.py`
- `experiments/pytdx_auction_history/explore_wencai_auction_local.py`
- `experiments/pytdx_auction_history/explore_wencai_raw.py`
- `experiments/pytdx_auction_history/wencai_cookie.txt`

修改（阶段 7）：
- `.gitignore`（新增 `experiments/**/wencai_cookie.txt`）
