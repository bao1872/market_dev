# Review 恢复、发布与撤销 Runbook

本 Runbook 只描述仓库当前存在的正式入口。禁止裸 SQL、`python -c`、临时脚本或直接修改
`market_review_runs` / `factor_publications`。

对应 PRD：`../prd/70-review.md`。

## 前置条件

1. 生产操作先运行 `scripts/ops/panji-prod-preflight`；本地不得连接数据库或启动 Worker。
2. 当日正式 `stock_core` 与 `market_aggregation` pointer 已发布且日期一致。
3. Migration 079–082 已在共享开发库目标测试模式（`PANJI_SHARED_DEV_DB_TEST=1`，经 SSH 隧道连 `bz_stock`）完成 upgrade/downgrade/upgrade 与 Integration 验证
   （清单与风险见 §6；见 `rules/40-testing-quality.md` TQ-100）。
4. Review run 的 algorithm version、source run、scope 配置与输入 hash 均已核对。

## 1. 正常计算与恢复

正式盘后入口为 `after_close_orchestrator` 的 `computing_review` 阶段；管理员入口为
`/api/v1/admin/review/runs`、`/runs/{id}/resume`、`/runs/{id}/publish` 和 `/runs/{id}/status`。

- 同一输入的失败/中断 run 通过 resume 恢复，已完成 item 不重复计算。
- 上游 core/board pointer 改变时必须创建新 Review run，旧 run 保留审计。
- 不得原地修改已发布旧 run；新 run 通过门禁后原子切换 `factor_publications`。
- failed run item 或 failed signal 必须修复根因，不得把状态直接改成 succeeded。

## 2. Bootstrap

历史回填按 market/index/style/industry/concept 分开处理，**只有两个正式入口**：
admin API 与 CLI。禁止在生产用临时 Python 调用 service。

### 2.1 语义约束

- 历史事实来自当日第一金字塔 history state/events、历史日线和 PIT universe/membership。
- 缺少 PIT membership 时写 `bootstrap_unavailable`，禁止使用当前成员。
- 相同 input hash + membership version 幂等写 `market_review_metric_observations`。
- `dry_run` 默认 True，且 dry-run 路径**零业务写入**：不创建 run、不写 metadata_json、
  不写 observations、不切 pointer；`operator/reason/input_hash` 只在响应与日志中返回，
  仅 apply 才落库。
- `end_date` 留空时解析为**最近一个完整 A 股交易日**（查 `trading_calendar`），
  不使用自然日 today。
- `algorithm_version` 必须与 `BOOTSTRAP_ALGORITHM_VERSION` 一致，否则拒绝执行。

### 2.2 admin API（异步，生产首选）

120 交易日 × 全 scope 的回填耗时远超 HTTP 超时，因此提交与执行分离：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/admin/review/bootstrap` | 提交任务，返回 **202 + job_run_id**，不同步执行 |
| GET | `/api/v1/admin/review/bootstrap/{job_run_id}` | 查询进度：全局 summary + 分页明细 |
| POST | `/api/v1/admin/review/bootstrap/{job_run_id}/resume` | 中断/失败任务重新入队 |

- 提交端点只创建 `status=queued` 的 `SchedulerJobRun`；真正计算由
  `review_bootstrap` Worker 通过 `FOR UPDATE SKIP LOCKED` + lease fencing 领取。
- `operator` / `reason` 必填（审计）。同输入范围已有活跃任务时复用并返回
  `is_new=false`（HTTP 200）；dry-run 与 apply 使用不同 run_key，互不幂等抵消。
- status 返回 `summary`（`succeeded` / `skipped` / `unavailable` / `failed` 四类计数
  与 `reason_codes`）+ 按 `(trade_date, scope_type, scope_key)` 的**分页**明细
  （`offset` / `limit`），不一次性返回上万行。

先 dry-run 核对统计，确认无误后再以 `dry_run=false` 提交同一范围：

```bash
curl -X POST "$API/api/v1/admin/review/bootstrap" \
  -H "Authorization: Bearer $ADMIN_TOKEN" -H 'Content-Type: application/json' \
  -d '{"days_back":120,"operator":"<操作者>","reason":"<原因>","dry_run":true}'

curl "$API/api/v1/admin/review/bootstrap/<job_run_id>?offset=0&limit=100" \
  -H "Authorization: Bearer $ADMIN_TOKEN"
```

### 2.3 CLI（可同步等待，用于受控窗口）

```bash
python -m scripts.review_bootstrap_cli \
  --operator '<操作者>' --reason '<原因>' --days-back 120
```

缺省为 dry-run；确认统计后追加 `--no-dry-run` 才写入。CLI 同步执行并打印四类计数摘要。

## 3. 正式发布门禁

`review_publication_service.publish_review` 必须同时满足：

1. run 已完成且非 canary/provisional；force 请求只保留 provisional，永不写 pointer；
2. source core/board run 与当日正式 pointer 完全一致；
3. algorithm version 为当前版本，配置要求的 scope types/keys 完整；
4. coverage、run items、signals 和 component readiness 达到门禁；
5. publication date 与 core/board pointer date 一致。

普通 `/api/v1/review/*` 只读取正式 `factor_publications(publication_kind=market_review)`；
provisional 仅允许 admin 通过显式 run_id/include_partial 查看。

## 4. 撤销错误 pointer

唯一正式入口：

```bash
python -m app.scripts.withdraw_review_publication \
  --trade-date 2026-07-31 \
  --expected-run-id <完整-run-uuid> \
  --expected-publication-id <dry-run-返回的-pointer-uuid> \
  --reason '<原因>' \
  --operator '<操作者>' \
  --idempotency-key '<唯一幂等键>'
```

缺省为 dry-run。只有 dry-run 同时匹配任务书限定的日期、kind、scope、run_id 和唯一 pointer，
并且 PG Integration、migration 与发布安全验证均通过后，才可在同一命令追加 `--apply`。

撤销只删除唯一 pointer，保留 run、scope、signal、attribution、instrument、observation 和 audit metadata。
重复执行返回 already-withdrawn；任何预期值不匹配立即停止。

## 5. 验证

- 普通 `/review/dates`、`/review/latest` 和 overview/scopes/signals 只返回正式 pointer。
- admin 显式 run_id 可读取 provisional 或已撤销 run。
- 五阶段分别展示无信号、无追踪、历史不足、字段缺失和 API 错误。
- Evidence Drawer 显示 field source、denominator、weight mode、coverage、algorithm/membership version 和 readiness。
- 同一最终 SHA 的 Review pure-unit、PostgreSQL Integration、Migration、Frontend Contract 和 E2E 全绿后才标记 verified。

## 6. Migration 清单（079–082）

本清单基于仓库 `backend/alembic/versions/` 实际文件生成。链条首尾相接，
**当前单一 head 为 `082_auction_analysis_publication`，无分叉**：

```
078_review_filter_family_d
  └─ 079_board_hierarchy_batch_identity
       └─ 080_review_hierarchy_attribution_evidence
            └─ 081_review_metric_observations
                 └─ 082_auction_analysis_publication   ← head
```

| 版本 | 新建表 | 改既有表 | 数据回填 | downgrade | 破坏性 | 锁风险 |
|---|---|---|---|---|---|---|
| 079 board_hierarchy_batch_identity | 5 | `market_boards`(+8 列)、`board_analysis_snapshots`(+4 列) | **6 条** | 结构完整，**数据不可逆** | **是** | **高** |
| 080 review_hierarchy_attribution_evidence | 0 | 3 张表 +12 可空列 | 无 | 完整对称 | 否 | 低 |
| 081 review_metric_observations | 1（`market_review_metric_observations`） | 0 | 无 | 完整对称 | 否 | 无 |
| 082 auction_analysis_publication | 1（`auction_analysis_publications`） | 0 | 无 | 完整对称 | 否 | 无 |

### 6.1 079 是唯一需要停机窗口 / 灰度演练的迁移

三个阻断点，部署前必须逐项预检：

1. **`board_analysis_snapshots.board_analysis_run_id` 的 `SET NOT NULL`**
   （079 第 227 行）无 server_default，依赖前置 UPDATE 100% 覆盖。
   回填 SQL 执行后必须验证
   `SELECT count(*) FROM board_analysis_snapshots WHERE board_analysis_run_id IS NULL` 为 0，
   否则整个迁移失败。
2. **唯一约束语义收窄**：drop `uq_board_analysis_snapshots_date_board_ver`
   (trade_date, board_id, algorithm_version)，改建
   `uq_board_analysis_snapshots_run_board` (board_analysis_run_id, board_id)。
   需预检新组合在存量数据中无重复。
3. **`factor_publications.data_run_id` 指针重写无逆向脚本**
   （条件 `publication_kind='market_aggregation'`）。`alembic downgrade` 回滚后该字段
   将指向已被删除的 `board_analysis_runs.id`，成为悬空指针。
   **079 的可靠回滚依赖用户另行明确授权的备份或快照，不得依赖 `alembic downgrade`。
   当前测试期部署默认不备份（rules/80），是否备份属用户每次独立决策，不把备份作为默认前置条件。**

其余锁风险：079 全部 `create_index` 均未使用 CONCURRENTLY（Alembic 事务内无法使用），
8 次 `ADD COLUMN NOT NULL DEFAULT` 持 ACCESS EXCLUSIVE 锁，
并含 4 条 INSERT…SELECT + 2 条全表 UPDATE，全部在单个 Alembic 事务内执行。

### 6.2 未确认项（部署前需现场核实）

- 相关表（`market_boards`、`board_analysis_snapshots`、`factor_publications`、
  `market_review_signal_attributions`）的**生产实际数据量级**未确认；
  上表「大表 / 锁风险」判断基于 DDL 形态而非真实行数。
- 目标库 **PostgreSQL 具体版本**未确认，直接影响 079 的 8 个
  `ADD COLUMN NOT NULL DEFAULT` 是否触发全表重写（PG 11 起可免重写）。
- 079 中硬编码 `DATE '2026-08-01'` 作为回填 `effective_from`；
  若实际部署晚于该日期，其「最早可信有效期」语义是否仍成立需业务侧确认。
- `alembic/env.py` 中 `target_metadata = None`（迁移全部手写、不支持 autogenerate），
  无法通过 autogenerate diff 交叉验证 migration 与 ORM 模型一致性。
