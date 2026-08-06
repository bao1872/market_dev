# CHANGE-20260806-007 — 同 SHA、自包含远程验证合同

日期：2026-08-06
类型：governance + verification-contract + quality-gate
状态：`governance_implemented_code_pending`

## 1. 目标

将远程 PG 验证从临时命令组合收敛为同一 SHA、自包含、纯 synthetic、机器可验证的正式合同，避免旧镜像 Migration、临时测试环境、Seed 依赖和跨 SHA 证据污染。

## 2. 新合同

- 基础 PG tests 必须自行创建完整 fixture，不依赖 Seed、顺序、历史数据或业务库。
- 正式 PG 证据只来自目标 SHA 的一次性 `verify-test` 服务。
- target/repo/runtime SHA、验证库和 Alembic revision 必须同时核对。
- Migration 必须使用 target SHA checkout，并执行 upgrade/downgrade/upgrade round-trip。
- 标准验证和 Seed 100% synthetic，验证栈不得连接或读取 `bz_stock`。
- Seed 只创建 raw facts/prerequisite，不直接写最终 publication/readiness 或固定结果。
- 失败停止后续 gate，清理后回本地修复并形成新 SHA；禁止远程 patch 和跨 SHA 累计证据。
- verification plane 身份与 Live Mount 代码挂载方式分离表达。

## 3. 当前实现差距

审计时确认：

- `docker-compose.verify.yml` 尚无一次性 `verify-test` 服务；
- `scripts/verify/seed_v21_verify_data.py` 仍从 `bz_stock` 只读复制；
- 部分 PG 测试声明依赖预先运行 Seed；
- 正式 Migration runner、自包含 fixtures 和统一证据模板仍需代码实现与远程验证。

因此本 Change 只表示治理合同生效，不表示远程验证能力已经闭环。

## 4. 修改范围

- `rules/40-testing-quality.md`
- `rules/80-deployment-data-safety.md`
- `rules/90-deprecated-forbidden.md`
- `tools/check_governance_rules.py`
- `tools/tests/test_check_governance_rules.py`

本次不修改 PRD、Maps 或 Runbooks。Runbook 只能在机器能力实现、真实远程跑通、用户验收并明确授权后更新。
