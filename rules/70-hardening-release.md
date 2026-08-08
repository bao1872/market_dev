# 70 Hardening 与 Release

本文件**不是 Exploration 默认流程**。

只有 `AGENTS.md` / `rules/README.md` 的 Hardening Trigger 命中后启用。

## 1. Hardening 的目的

Hardening 回答：

- 这个版本是否适合长期稳定运行？
- 数据迁移是否对真实存量安全？
- 所有 mandatory 产品是否闭合？
- 兼容接口是否完整？
- 全面回归是否通过？
- 发布/回滚证据是否足够？

它不负责早期产品假设判断。

## 2. 进入条件

至少明确：

- target exact SHA；
- release scope；
- target DB revision；
- migration risk；
- release acceptance；
- rollback / downgrade boundary；
- 必须关闭的 Deferred Debt。

## 3. Full RTM

仅 Hardening 要求完整：

`PRD requirement → code → unit/contract/integration → runtime → DB/API/frontend evidence`

范围覆盖本次 release 的所有受影响域。

## 4. Full Regression

按 release scope 运行：

- full PURE_UNIT；
- 相关 frontend contract/e2e；
- PG Integration；
- Migration；
- Synthetic E2E；
- 必要外部数据验证；
- release smoke。

Hardening 不是“所有测试永远全跑”；仍按 release impact，但范围明显大于 Exploration。

## 5. ProductReadiness / Closure

如果 release 声称 after-close 产品闭合，则正式验证九节点：

- daily_facts
- board_facts
- stock_core
- dsa_projection
- state_events
- chip
- auction_anchor
- board_aggregation
- review

必须使用正式 ProductReadiness evaluator，不用人工表代替。

### Mandatory

- daily_facts
- board_facts
- stock_core
- board_aggregation
- review

### Required compatibility

- dsa_projection

### Enhancement

- chip
- state_events
- auction_anchor

fully_ready 与 mandatory-ready 必须区分。

## 6. Migration Rehearsal

以下情况通常需要真实数据级 rehearsal / clone：

- destructive / irreversible；
- 新 NOT NULL 对存量有约束；
- unique / FK 可能与存量冲突；
- 大量 backfill / rewrite；
- 长锁风险；
- 无法仅靠 precheck 证明；
- 用户明确要求。

纯 additive、已通过存量 precheck 的小 migration 不因进入 Hardening 就机械要求完整 17GB clone；仍按 `80-deployment-migration.md` 风险分级。

## 7. Exact-SHA Evidence

Release evidence 必须绑定：

- repo SHA；
- runtime SHA；
- DB revision；
- deployment mode；
- verification environment identity；
- test set；
- target date / data evidence。

不同 SHA 的结果不能拼成同一 release PASS。

## 8. Release Decision

只允许基于证据输出：

- READY_TO_RELEASE
- BLOCKED_BY_CODE
- BLOCKED_BY_TEST
- BLOCKED_BY_MIGRATION
- BLOCKED_BY_DATA
- BLOCKED_BY_RUNTIME
- BLOCKED_BY_SECURITY

UNKNOWN 不能写成 READY。

## 9. Deferred Debt Closure

进入正式 beta / release 前，根据触发条件处理 Exploration 积累的债。

不是所有 P2/P3 必须清零；但任何会影响：

- 数据可信度；
- 长期兼容；
- 用户安全；
- 可恢复性；
- 稳定运行；

的债必须在 release decision 前明确处理或接受风险。

## 10. Hardening 完成

Hardening 完成后才可宣称：

- release-grade verified；
- full closure verified；
- migration compatibility verified；
- stable runtime ready。

Exploration PASS 不得直接使用这些表述。
