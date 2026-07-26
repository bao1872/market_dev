# 数据、API 和权限

## 数据域

- users / roles / subscriptions / invite_codes；
- instruments / calendar / bars / company actions；
- strategy definitions / runs / results；
- watchlist / monitor / events；
- feature snapshots / AFC；
- boards；
- messages / outbox / deliveries / capture；
- scheduler jobs / heartbeats；
- research matrix。

partial bar 不写 completed 表。

## 权限

当前 main 仍以 active subscription 为普通会员核心资格。

Capability V2：

```text
watchlist_management
market_screening
review_management
```

仍属于 WIP，合并前不覆盖 CURRENT。
