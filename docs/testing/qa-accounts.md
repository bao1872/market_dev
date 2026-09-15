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
