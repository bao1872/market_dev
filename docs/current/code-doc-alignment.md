# Code / Docs / Production Alignment

> 本文件只记录“当前确认设计已经明确，但实现、测试、部署或生产表现尚未一致”的问题。历史经过进入 `changes/`。

## 当前 KNOWN_GAP

| ID | 领域 | 当前证据 | 目标 | 优先级 |
|---|---|---|---|---|
| ALIGN-010 | 飞书图文 E2E | 生产已有图文成功记录，但 partial_failed、仅重试图片、失败状态生产 E2E 尚未系统验证 | 独立 card/image 状态、partial_failed、仅重试图片、真实 E2E | P1 |
| ALIGN-012 | 管理页面 E2E | AdminJobsPage 与部分管理 API 已存在，但用户启停、订阅变更、任务与审计生产操作未完整验收 | 所有管理按钮真实 API、审计完整、生产 E2E 通过 | P1 |
| ALIGN-015 | 服务健康与业务能力 | CORE_ONLY 不包含 capture/outbox/delivery；服务不全会造成业务部分可用 | 部署能力与业务功能匹配；服务不可用时不假成功 | P1 |
| ALIGN-021 | Ruff/Mypy 历史债务 | 全仓 Ruff/Mypy Full Report 仍有历史债务，非阻断展示 | 独立债务分支清零，再改为完全阻断 | P2 |

## CLOSED 摘要

| ID | 摘要 |
|---|---|
| ALIGN-004/005/019/020 | DSA 发布门禁、预算、partial_failed 发布、数量语义已收口 |
| ALIGN-006/007/008 | Watchlist、趋势 API、Worker 资格已接入统一权限/资格路径 |
| ALIGN-009 | 行情聚合与尾部补齐相关路径已收口 |
| ALIGN-011/018 | Capture Token 与 Capture Snapshot 链路已实现并测试 |
| ALIGN-016 | Node Cluster 15m 输入已修正为 4000 |
| ALIGN-017 | 飞书 Platform App only，Webhook 永久删除 |
| ALIGN-022 | `target_channel_id` 手动通知跳过资格过滤，自动通知仍过滤，已补隔离测试 |
| ALIGN-023 | worker-watchdog 生产服务已部署(67105c2)，38 条 stale running 已自动清理为 stopped，stale running=0 |
| ALIGN-024 | docs v2 结构已通过 PR #5 合并落库（cafbdc4），旧 00-18 归档，check 已适配 |

## 关闭要求

每项关闭必须有：代码 commit、测试、CI 或生产验证证据、CHANGE 记录。关闭后只保留摘要，详细历史归入 CHANGE。
