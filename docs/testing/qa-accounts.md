# Canonical QA Accounts

## qa-normal

Purpose:
普通用户长期回归账号。用于行情、自选、普通用户权限与 UI 测试。

Email:
qa-normal@panji.test

Role:
member

Admin:
false

Capabilities:
- self_selection: active
- market_data: active
- research_replay: inactive

Expected watchlist:
3 active instruments (000001 / 300750 / 600519)

Expected routes:
- /market
- /market?scope=watchlist
- /stock/:symbol for authorized scope
- /settings

Secret:
密码不进入 Git。
dev runtime 凭据位置：
/etc/market-dev/qa-test-account.env

Usage rule:
以后普通用户权限/行情/自选相关人工和 runtime 验证，
默认优先使用 qa-normal，
不要临时拿 Owner/admin 账号替代。

禁止：
- 把 qa-normal 提升为 admin
- 在业务测试中删除该账号
- 随意改其 capability
- 清空其全部 watchlist
- 将密码/token 写入仓库

如测试需要改变权限，
应创建一次性测试用户，不污染 qa-normal 基线。

## qa-normal verification（PANJI-REVIEW-V2 部署后人工验收，2026-09-21）

已在部署运行时实网验证（RUNTIME_SHA=17887efb08561801983617b13da2e357add72570）：

- `/v1/auth/login` 登录：OK
- `/v1/me`：status=active，roles=[member]，is_admin=false
- `/v1/me/access`：subscription_active=true（observe_20，到期 2027-09-15）
- capabilities：self_selection=active，market_data=active，research_replay 不存在（inactive）
- 会员 + market_data 权限 smoke：`/v1/market-dashboard/market`、`/v1/market-dashboard/scopes`、`/v1/market-dashboard/rankings` → 200

## Admin QA 账号

PANJI-REVIEW-V2 部署验证期间**未创建**专用管理员 QA 账号。

原因：卡住的盘后任务 `fcf954da-…` 已自行进入终态 `partial_success`（active after_close 计数 = 0），
因此部署无需通过 admin API 执行 `reconcile`/`cancel` 来解阻塞。授权密钥文件
`/etc/market-dev/qa-test-account.env` 中不存在管理员凭据（仅含 `PANJI_QA_NORMAL_EMAIL` /
`PANJI_QA_NORMAL_PASSWORD`）。

如未来需要管理员 QA 账号做 admin API 回归，建立方式（唯一合规路径）：

- 禁止在 production 使用 `tools/create_test_accounts.py`（仅测试环境）。
- 禁止用 `backend/app/scripts/bootstrap_admin.py` 创建第二名管理员
  （仅当系统中零管理员时可用；production 已有管理员）。
- 禁止用 `JWT_SECRET` 手工签发 JWT。
- 必须由已有管理员通过正式业务 API 发起：
  1. `POST /v1/admin/invite-codes`（一次性邀请码）
  2. `POST /v1/auth/register`（创建用户）
  3. `POST /v1/admin/users/{user_id}/change-role` 传 `{"role":"admin"}`
- 凭据仅写入 `/etc/market-dev/qa-test-account.env`（新增 `PANJI_QA_ADMIN_EMAIL` /
  `PANJI_QA_ADMIN_PASSWORD`），`chmod 600`。
- 待授权管理员确认的候选现有管理员身份：`test-admin@market.dev`
  （定义见 `tools/create_test_accounts.py`）。

Secret 位置（非秘密事实）：
dev runtime 凭据位置：`/etc/market-dev/qa-test-account.env`
（当前仅含 qa-normal 密钥。）
