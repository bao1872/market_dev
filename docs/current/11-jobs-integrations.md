> 文档状态：CURRENT DESIGN BASELINE  
> 基线日期：2026-07-02  
> 已核对代码基线：`6f5ae2cec6b24dbd1b7bf6f23477f5e6f5096822`（`refactor/access-v2-platform-recovery`）  
> 事实来源：代码库 + 项目负责人截至 2026-07-02 已确认的产品与架构要求  
> 维护要求：任何代码、配置、测试、部署或文档修改都必须同步更新相关当前设计文档，并新增 CHANGE 记录。  
> 注意：该代码基线用于设计核对，不代表已经满足合并 `main` 或生产发布条件。
> 对齐口径：`CURRENT` 表示已确认设计，不等同于代码已完成；代码未实现、未验证或生产表现不一致的内容，必须在 `18-code-doc-alignment.md` 标为 `KNOWN_GAP`。

# 11 后台任务与第三方集成

## 1. Worker

统一入口：`backend/app/worker.py`。服务名以 `docker-compose.prod.yml` 为准。

| WORKER_TYPE | 职责 |
|---|---|
| `bars_scheduler` | 更新行情、聚合和触发盘后链路 |
| `strategy_scheduler` | DSA 兜底调度，不重复已有运行 |
| `calendar_scheduler` | 更新交易日历 |
| `monitor_scheduler` | 对有效会员自选股进行盘中监控 |
| `strategy_batch` | 领取并执行 StrategyRun |
| `outbox` | 扩张消息和投递 |
| `delivery` | 实际渠道投递、重试和最终状态 |
| `after_close_orchestrator` | 领取并执行盘后编排任务 |
| capture service | 生成个股详情图片 |

## 2. 调度语义

- 日历刷新：约 02:00 Asia/Shanghai；
- 盘后行情：交易日约 16:00；
- DSA 兜底：交易日约 18:30；
- 盘中监控：09:30–11:30、13:00–15:00，按配置轮询；
- Outbox/Delivery：短轮询；
- 心跳：按配置持续更新。

精确 Cron 和间隔由代码/环境变量作为机器事实源。

## 3. 任务状态与恢复

重要任务保存 run_key、business_date、scheduled/started/finished、status、heartbeat、lease、instance、计数、错误和 Git SHA。重复触发复用或返回 duplicate；stale 任务按服务规则恢复，禁止直接手改数据库状态。

## 4. 行情集成

Pytdx 提供行情，Mootdx 提供交易日历。统一行情聚合负责历史完成 Bar、尾部补齐和盘中 partial Bar；外部源失败时记录降级。交易日不能通过“是否有 K 线”推断。

## 5. DSA 运行链

`bars_scheduler` 只负责准备行情和创建/复用 queued run；`strategy_batch` 执行计算；`strategy_scheduler` 兜底；完整性门禁通过后发布。缺少任一 Worker 都不能认为盘后链路正常。

## 6. 飞书

用户渠道支持 Webhook 或平台应用，每个用户最多一个 active 渠道。管理员系统通知使用独立受限配置。

文字和图片分开记录 Delivery；Capture、图片上传和图片发送分别可失败。状态必须可查询，支持仅重试图片。

## 7. 截图

Capture Worker 使用短期 Token 访问指定个股详情，等待 `data-render-ready=true`，截取明确区域。截图使用与页面相同的行情聚合快照，保存 as_of 和 source hash。服务未运行或健康失败时，后端不能返回整体成功。

## 8. 可观察性

管理员和运维必须能回答：运行中的 Worker、Git SHA、心跳、next run、当前任务、股票计数、失败阶段、重试状态、发布完整性、文字状态、图片状态和数据新鲜度。
