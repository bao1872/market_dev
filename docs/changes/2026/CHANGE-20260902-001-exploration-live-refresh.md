# CHANGE-20260902-001 — Exploration claim-driven evidence 与 Live Refresh

## 背景

Exploration 的 modified-scope 测试路由已经存在，但默认 DoD、runtime 路由和部署脚本组合后，
source-only 业务修改仍可能自动升级为完整 runtime closure 与容器 recreate。Live Mount 下，
这增加了与改动风险不相称的等待和运行扰动。

## 决策

- completion evidence 由本轮 claim 决定，不默认展开 DB → API → frontend 全链；
- full suite 必须声明具体 `FULL_TEST_TRIGGER`；日常审查默认为 change-scope review，复用同一
  diff/SHA 的可核验证据，不默认二次审计、部署或 full regression；
- source-only Code Sync / Live Refresh 继承代码改动等级，不因 restart 单独升级 Level 3；
- frontend source-only：build dist → sync，不 restart Nginx；
- backend API-only：sync → `docker compose restart backend`；
- shared/worker/影响不确定 backend：sync → refresh 相关 Python services，不确定时保守全量；
- environment image、container recreate、Compose、Migration、数据操作、Release 继续走 Level 3。

## 安全边界

本变更不放松 PIT/future leakage、canonical owner、lineage/identity、false-green、`bz_stock`
写入、Migration、destructive operation、secret/permission、publish/backfill/full-market 等边界。
所有远程动作仍经唯一正式入口、绑定 `origin/dev` exact SHA、保留 rollback owner，并执行
claim 所需 smoke。

## 实现与验证状态

- governance/docs：已实现；
- deploy fast path 与分类合同：已实现；
- 本地 modified-scope contract：governance checker PASS；governance/deploy Python contracts
  47 passed；deploy shell structure contracts 130 passed；exact SHA `8ff747c9` 完整 deploy
  dry-run contracts 110 passed；
- remote Live Refresh / stable deployment：未执行，本任务只要求 push `dev`。
