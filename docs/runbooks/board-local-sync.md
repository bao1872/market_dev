# Runbook：板块/概念数据本地手动同步（BOARD-LOCAL-OWNERSHIP-01）

归属迁移后，**问财网络访问只发生在本地 Mac**；生产服务器只接收已构造好的规范化快照，
并继续由唯一 DB 写 owner `board_sync_service.sync_boards()` 做门禁 + 原子写库。

## 1. 网络 / 写库 owner 划分

```text
本地 Mac
  → 读取本地 cookie（backend/wencai_cookie.json 或 env WENCAI_COOKIE）
  → wencai_board_provider.fetch_board_snapshot()（问财抓取 + 规范化）
  → board_snapshot_transfer.build_envelope()（schema_version=1，payload_sha256）
  → SSH stdin（不发送 cookie）
生产服务器
  → app.cli.board_snapshot_import（校验 schema/hash/contract → 重建 BoardSnapshot）
  → 同一事务内 single-flight advisory lock
  → board_sync_service.sync_boards()（门禁 + 版本/历史 + 原子切换）
  → 提交；失败整体 rollback（保留上一成功快照）
```

盘后任务（after-close）**不再**包含板块/概念同步，也不再读取任何 `BOARD_SYNC_ENABLED` 开关。

## 2. 更新本地 cookie

1. 从浏览器复制问财（iwencai）登录态 cookie。
2. 写入本地 `backend/wencai_cookie.json`（该文件已被 `.gitignore` 忽略，不进版本库）：

```json
{ "cookie": "<粘贴的 cookie 串>" }
```

或临时用环境变量 `WENCAI_COOKIE` 覆盖。

> 本流程**不**把 cookie 复制到生产（不走 `docker cp`、不写命令行、不放进 envelope）。
> envelope 由 `board_snapshot_transfer` 显式拒绝 `cookie` / `authorization` 等敏感字段。

## 3. 运行同步

```bash
# 真正同步：本地抓取 → SSH stdin → 生产原子应用
scripts/ops/panji-board-sync

# 仅本地 dry-run：抓取 + 构造/校验 envelope，打印摘要；不 SSH、不改动生产
scripts/ops/panji-board-sync --dry-run
```

成功输出（本地摘要 + 生产结果）示例：

```text
local_fetch:
  source=wencai
  raw_rows=...
  board_count=...
  industry_count=...
  concept_count=...
  membership_count=...
  payload_sha256=...
status=fetched
status=succeeded
mode=local_manual
source=wencai
effective_date=...
raw_rows=...
industry_count=...
concept_count=...
membership_count=...
resolved=...
unresolved=...
duration_ms=...
error_code=None
```

`effective_date` 由生产 importer 按**上海日历当日**决定，不信任客户端传入。

## 4. 语义与边界

- **无 schedule、无 cron**：只在用户手动执行时运行。
- **无频率限制**：同一天可重复同步；相同快照可安全重放。没有冷却期、没有 stale 阈值、
  没有 once-per-day 限制。
- **并发保护（非频率限制）**：生产 importer 用事务级 advisory lock 做 single-flight；
  若有另一个同步正在运行，本次会 fail-fast（`BOARD_SYNC_BUSY`）。
- **失败**：importer 整体 rollback，生产保留上一成功快照，退出码非零。
- **不自动重建 Market Dashboard**：手动板块同步只更新板块/成分数据；新的成分会在**下一次正常
  盘后** `rebuilding_market_dashboard` 中被消费。若在 dashboard 重建期间同步，既有
  membership commit guard（`LOCK TABLE ... IN SHARE MODE`）会保护投影不被写入混合成员集。

## 5. 管理后台查看状态

`GET /v1/admin/board-sync/status`（admin only，只读）：

- `mode=local_manual`、`source=wencai`、`available`（库中是否有有效板块数据）；
- `last_success_at`（`MAX(MarketBoard.updatedAt) WHERE isActive=true`）；
- `board_count / industry_count / concept_count / membership_count / stock_count`；
- `recent_attempt`（最近一次尝试，Redis 短期诊断；不可用时为 null）。

管理后台 `/admin` 的「板块」tab 展示以上只读状态，并提示在本地执行
`scripts/ops/panji-board-sync`。**没有**服务端"立即同步"按钮，**没有**基于 age 的过期/超时判定。

## 6. 失败排查

| 现象 | 含义 | 处理 |
|---|---|---|
| `local_error_code=CookieMissingError` / `未配置 WENCAI_COOKIE` | 本地无有效 cookie | 按第 2 节更新 `backend/wencai_cookie.json` |
| `error_code=WencaiFetchError` | 本地抓取失败（cookie 过期 / 网络 / 限流） | 更新 cookie 或稍后重试；生产不受影响 |
| `error_code=SnapshotHashMismatchError` | envelope 被篡改/截断 | 重新执行同步 |
| `error_code=SnapshotContractMismatchError` | 本地与生产 contract 版本不一致 | 先同步生产 SHA 与本地一致再重试 |
| `error_code=BOARD_SYNC_BUSY` | 已有一个同步在运行 | 稍后重试（并发保护，非频率限制） |
| `error_code=StagingValidationError` | 快照未过绝对/相对门禁 | 检查问财返回是否异常（raw/行业/概念/关系量级） |
