# Review 恢复与正式发布 Runbook

本 Runbook 描述盘后流水线中 review 阶段失败的恢复、正式发布、幂等重跑、与 after_close 正式链的操作。所有写入必须通过正式 orchestrator API / CLI / 管理后端 API，禁止裸 SQL、临时脚本 `UPDATE`。

对应 PRD：
- `docs/prd/30-after-close.md` §AC-70、§AC-71（盘后 7 步 review 阶段 + 幂等重跑）
- `docs/prd/70-review.md` §RV-25（raw 冷启动 / pointer 日期同步 / bootstrap）

## 前置条件

1. 通过 `scripts/ops/panji-prod-preflight` 校验；
2. 明确 `stock_core` 与 `board_analysis` 正式 pointer 已就绪（通过 `factor_publications` 表 is_published=true 的最新两条 scope=stock_core / board_analysis）；
3. 禁止 force 发布全空数据（coverage < 0.95 或 P/Q/U/C/V raw 全部为 null）。

## 1. after_close review 阶段的正常流

### 1.1 after_close_orchestrator 正式 8 步（已在 `after_close_orchestrator.py 实现）

```
1. refreshing_daily → syncing_boards → checking_coverage
2. computing_features（旧 4 步收敛后）
3. publishing: 切换 stock_core + board_analysis 正式 pointer
4. computing_review: 【新增，本 Runbook 核心】
   4a. get_current_pointer(stock_core) + get_current_pointer(board_analysis)
   4b. review_orchestrator.create_run(trade_date, stock_core_run_id, board_analysis_run_id)
   4c. review_orchestrator.compute_run(review_run_id)
   4d. review_orchestrator.publish_review(review_run_id)
5. watchlist_ready（三项都 succeeded 时标记 completed）
```

### 1.2 review 失败的影响（失败绝不静默写 succeeded）

- 任一步 4b/4c/4d 失败 → orchestrator metadata 写入：
  - `metadata.review_run_id / review_status / review_reason`
  - `SchedulerJobRun.status = 'failed' + error_message = 'review_failed: <具体原因>'`
- **禁止**：`after_close_runs.status = 'succeeded' 但 review pointer 未 publish。

## 2. review 幂等恢复

**触发时机**：盘后主任务在 computing_review 阶段失败；或 4b/4c/4d 任一步失败，但上游 stock_core / board pointer 不需要重跑（只恢复 review）。

### 2.1 Step 1: 只读确认状态

```bash
# 用正式 CLI，禁止 docker exec 里 python -c 临时脚本
docker exec trading-backend python -m scripts.review_cli status --trade-date YYYY-MM-DD
```

输出：
```
stock_core_pointer:   run_id=X status=published trade_date=YYYY-MM-DD coverage=0.98
board_analysis_pointer: run_id=Y status=published trade_date=YYYY-MM-DD coverage=0.97
current_latest_review_run: run_id=Z status=failed reason='<具体原因> created_at
review_publication: is_published=false
```

### 2.2 Step 2: 幂等重跑（上游未变）

若 stock_core_run_id + board_analysis_run_id 与 current_latest_review_run 相同 → 使用同一 review_run_id 重跑：

```bash
# --skip-if-same-input（只重跑当前失败的 compute/publish 步骤；
# 若 compute 已 succeeded，直接 publish 失败 → 只跑 publish）
docker exec trading-backend python -m scripts.review_cli rerun --review-run-id <review_run_id>
```

**幂等保证**：
- 任一阶段已 succeeded 的子步骤跳过（create / compute / publish）不重跑；
- 同一 review_run_id 重复调用返回同一结果。

### 2.3 Step 3: 输入变化（上游 pointer 已切换）→ 创建新 review_run

若上游 stock_core / board pointer 变化（例如 stock_core 重新跑了增量修复后切了新 run_id），必须创建新 review_run：

```bash
docker exec trading-backend python -m scripts.review_cli create-and-run \
  --trade-date YYYY-MM-DD \
  --stock-core-run-id <new_X> \
  --board-analysis-run-id <new_Y> \
  --publish-on-success
```

→ 旧 review_run（run_id=Z 保留为审计（不 DELETE / UPDATE status）。

## 3. Review 发布门禁（publish_review）

### 3.1 发布前 5 项硬条件

在正式切换 review_publications.published_run_id 前必须满足：

```
条件 1: market 范围 coverage >= 0.95
条件 2: market 范围的 P.raw / Q.raw / U.raw 不全为 null（否则 P 若 raw 依赖 fp_segment_change_pct 全空则必须查 §40 MX-63)
条件 3: review_run.status = 'succeeded' （非 running / failed）
条件 4: source_core_run_id 与 source_board_run_id  == 当前正式 pointer run_id
条件 5: 若 review_runs.metadata 中至少有一个 scope 成功 completed
```

违反任一条 → `publish_review()` 直接拒绝，返回 `error='publish_gate_failed: <违反项>`。**禁止 force 发布全空数据。`

### 3.2 冷启动时的发布门禁（历史不足60天）

冷启动场景（累计 normalized 不可用但 raw 已足够）：

```
放宽: normalized / historyPercentile120d 可为 null
必须: raw_ready=true, coverage>=0.95, P.raw 和 Q.raw 存在
发布后: /review 页面 header 显示 "raw baseline only" chip；五阶段正常展示 raw 与 coverage + insufficient_history reason
```

## 4. Review pointer 与 after_close watchlist_ready 标志位回填

### 4.1 review 成功发布后，立即回填

```
after_close_runs.metadata.review_run_id   = published_review_run.id
after_close_runs.metadata.review_status  = published
after_close_runs.status                = succeeded
SchedulerJobRun.status                     = succeeded + payload.review_stage = succeeded
factor_publications (scope='review'): is_published=true, published_run_id=review_run_id
```

→ 上述 5 项必须同一原子事务内完成（或 2-3 个有序事务）。任一步失败 → 整体 rollback + rollback 一致。。

### 4.2 Review pointer 日期同步审计

禁止：review.pointer.trade_date 必须 == stock_core.pointer.trade_date == board.pointer.trade_date。

若发现不一致：
```bash
docker exec trading-backend python -m scripts.review_cli audit-pointer-dates --fix=false
# 若不一致:
  → 审计不一致的 scope 与原因；
  → 把 after_close status 改为 failed（若之前错误地标为 succeeded）
```

## 5. 冷启动 bootstrap（历史观测不足60）

### 5.1 bootstrap 入口（幂等）

```bash
docker exec trading-backend python -m scripts.review_cli bootstrap-history \
  --from-date YYYY-MM-DD \
  --obs-count-target 120 \
  --dry-run true      # 先 dry-run，输出 "将回填 N 天，K 个板块因无历史版本将标记 bootstrap_unavailable
```

### 5.2 point-in-time 约束（严格执行

- 执行前检查以下 point-in-time 三条：

1. 每个历史观测值 必须使用当日有效的 board members（通过 board_versions × 与 publication 与当日 snapshot 有效 board_membership 表 join）；
2. 禁止使用当日 effective 当日 stock_core / board_analysis publication.run_id（禁止使用未来 run_id；
3. 禁止伪造 normalized 值；观测不足 60 → 标记 insufficient_history=true。

违反任一条不符合 → bootstrap 立即停止并输出：
```
output: {skipped_boards_total=12, reason=no_board_version_prior_to_YYYY-MM-DD,
```

## 6. 禁止清单

1. 禁止裸 SQL `UPDATE review_runs SET status='succeeded' WHERE id=?`；
2. 禁止 force 直接写 `review_runs` / factor_publications` 用 INSERT，必须走 `publish_review()`；
3. 禁止临时 one-off 脚本 `python -c "from review_orchestrator_service import publish_review"`（必须用 review_cli 正式入口）；
4. 禁止把 `review_publications` 中的 `is_published=true` 的 published_run_id 指向 `status=failed` 的 run_id；
5. 禁止在盘后流水线中 computing_review 阶段静默写主任务 succeeded；
6. 禁止用当前成员数据回填历史 scope_items；
7. 禁止把 `fp_segment_change_pct 全空 `时填 0 或均值。

## 7. 验收 / 验证步骤

review 恢复 + 发布后，执行以下浏览器验收（手动或 Playwright 自动）：

| 验收项目 | 通过标准 |
|---|---|
| `/review` 顶部 date picker latest trade date  == stock_core.pointer.trade_date | 日期一致，不等于 7/29（若当日为 7/31 及以后） |
| Market Scan 阶段 P/Q/U/C/V 5 维度 有值 至少 3 个维度有 raw value 展示 + coverage chip | 不是整页“不可用” |
| insufficient_history 时 rawValue + reason | 页面 右侧显示 `insufficient_history` chip |
| 5 阶段页面导航：Market → Filter → Board → Stock → Tracking 可切换 | 5 个阶段均可进入，无 4xx/5xx |
| market 范围 + 至少一个行业范围 均可展示 至少 1 个 scope 的 raw | scope 列表为空的行业 ≤ 5% |
| SignalCard：若 normalized 不可用 显示 "raw baseline only" 标签，signal 有 raw 结论 | signal=0 但 raw 不 0 时 给出说明 |
| admin 盘后时间线 computing_review 步骤 存在且 status=completed，duration 正数或 "进行中/未知" | computing_review 步骤存在，非负数 负数不显示 负数耗时 |
