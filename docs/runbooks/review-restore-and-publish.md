# Review 恢复、发布与撤销 Runbook

本 Runbook 只描述仓库当前存在的正式入口。禁止裸 SQL、`python -c`、临时脚本或直接修改
`market_review_runs` / `factor_publications`。

对应 PRD：`../prd/70-review.md`。

## 前置条件

1. 生产操作先运行 `scripts/ops/panji-prod-preflight`；本地不得连接数据库或启动 Worker。
2. 当日正式 `stock_core` 与 `market_aggregation` pointer 已发布且日期一致。
3. Migration 080–081 已在 CI 临时 PostgreSQL 完成 upgrade/downgrade/upgrade 与 Integration 验证。
4. Review run 的 algorithm version、source run、scope 配置与输入 hash 均已核对。

## 1. 正常计算与恢复

正式盘后入口为 `after_close_orchestrator` 的 `computing_review` 阶段；管理员入口为
`/api/v1/admin/review/runs`、`/runs/{id}/resume`、`/runs/{id}/publish` 和 `/runs/{id}/status`。

- 同一输入的失败/中断 run 通过 resume 恢复，已完成 item 不重复计算。
- 上游 core/board pointer 改变时必须创建新 Review run，旧 run 保留审计。
- 不得原地修改已发布旧 run；新 run 通过门禁后原子切换 `factor_publications`。
- failed run item 或 failed signal 必须修复根因，不得把状态直接改成 succeeded。

## 2. Bootstrap

`review_bootstrap_service.bootstrap_history(..., dry_run=True)` 默认只读，按
market/index/style/industry/concept 分开处理。

- 历史事实来自当日第一金字塔 history state/events、历史日线和 PIT universe/membership。
- 缺少 PIT membership 时写 `bootstrap_unavailable`，禁止使用当前成员。
- 相同 input hash + membership version 幂等写 `market_review_metric_observations`。
- 未提供正式 CLI/API 前，不得在生产通过临时 Python 调用 service；由 after-close/admin 正式入口接入后再执行。

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
