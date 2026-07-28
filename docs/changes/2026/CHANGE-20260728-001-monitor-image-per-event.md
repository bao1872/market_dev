# CHANGE-20260728-001：盘中结构事件图片按事件独立截图（修复"有文字无图片"）

状态：代码+单元测试+DB 集成测试已通过；真实盘中运行未核验
日期：2026-07-28
类型：bugfix
对应 PRD：`docs/prd/50-watchlist-intraday.md` WI-12（信息定位）
对应 Map：`docs/maps/50-watchlist-intraday.md` §4

## 1. 变更摘要

修复盘中结构事件通知"有文字但缺图片"的 P0 问题。根因：图片链路按 instrument 只取 `events[0]`，
导致同股票多个结构事件只有第一个生成图片；幂等键按 `user+instrument+分钟`，同分钟多事件互相去重。

修复后：
1. 截图粒度改为每个唯一 `(instrument_id, event_id)`，每事件独立解析 `indicator_view`、构建 `focus_event`、
   生成 capture token、文件名/cache key/capture_run_id 和 CaptureJob。
2. 同一事件多用户只截图一次，随后为每个有资格用户创建 image Outbox；文本仍按用户合并一条。
3. 单事件截图失败只记录该 CaptureJob，不阻塞文本及其他事件图片。
4. 图片消息与 delivery 幂等键包含 `event_id+indicator_view`；同事件重试不重复，不同事件即使同一分钟也不互相去重。
5. 未知事件类型显式记录 `UNSUPPORTED_INDICATOR_VIEW` 并跳过，不再回退 `node_cluster` 生成错误图片。

## 2. 根因分析

修复前 `monitor_batch_service._send_chart_images_via_outbox` 流程：

```text
for inst_id, events in instrument_events.items():
    event = events[0]                          # ← 只取首事件
    indicator_view = map_event_type(event.event_type)  # ← 只解析一次
    # 生成 1 个 CaptureJob、1 次 capture 请求、1 个文件名
    capture_run_id = f"monitor-{inst_id}"      # ← 不含 event_id
    # 幂等键 = user + instrument + 分钟          # ← 同分钟多事件互相吞掉
```

问题：
- 一股票同时触发 5 类 SMC 事件（BOS/CHoCH/EQH/EQL/OB）时，仅首事件生成图片，其余 4 事件只有文字。
- 同分钟内多个事件共享幂等键，后到的事件图片被去重吞掉。
- 未知事件类型回退 `node_cluster`，生成与事件无关的错误图片。

## 3. 变化内容

### 3.1 `backend/app/constants/indicator_view.py`

新增：
- `UNSUPPORTED_INDICATOR_VIEW` 常量（错误码）
- `is_supported_event_type(event_type, payload)` 函数：检查事件类型是否有已映射的 indicator_view；
  payload 显式指定 indicator_view 时视为已支持；未映射返回 False，应跳过而非回退 node_cluster。

### 3.2 `backend/app/services/monitor_batch_service.py`

`_send_chart_images_via_outbox` 改为 per-event 遍历：

| 维度 | 修复前 | 修复后 |
|---|---|---|
| 遍历粒度 | per instrument（`events[0]`） | per event（`for event in events`） |
| capture 请求 | 每股票 1 次 | 每事件 1 次 |
| CaptureJob | 每股票 1 个 | 每事件 1 个 |
| capture_run_id | `monitor-{inst_id}` | `monitor-{inst_id}-{event.id}-{indicator_view}` |
| 输出文件名 | 不含 event_id | 含 `event.id` 与 `indicator_view` |
| 图片 Outbox | 每用户 1 个 | 每用户每事件 1 个 |
| 幂等键 | user+instrument+分钟 | user+instrument+event_id+indicator_view |
| 失败隔离 | 无（首事件失败阻塞全部） | 单事件失败只记 CaptureJob，不阻塞其他 |
| 未知事件类型 | 回退 node_cluster | 记录 UNSUPPORTED_INDICATOR_VIEW 并跳过 |
| 多用户截图 | 每用户独立截图 | 同事件截图 1 次，多用户共享 Outbox |

### 3.3 事件类型 → indicator_view 映射（已核验）

| 事件类型 | indicator_view |
|---|---|
| BOS | smc |
| CHoCH | smc |
| EQH | smc |
| EQL | smc |
| OB first touch | smc |
| 未知类型 | UNSUPPORTED_INDICATOR_VIEW（跳过） |

## 4. 测试

`backend/tests/test_monitor_batch_capture_image.py::TestMonitorBatchCapturePerEvent`（6 tests，DB 集成，事务回滚隔离）：

1. 一股票 5 类结构事件 → 5 次 capture 请求、5 个 CaptureJob、5 个 image Outbox
2. 两用户 → 每事件截图 1 次、每用户各有 image Outbox
3. 一个截图失败 → 文字和其他 4 图继续
4. 同事件重试无重复（NotificationMessage 幂等）
5. 同分钟两个事件均发送（幂等键含 event_id，不被分钟级去重吞掉）
6. 未映射事件类型显式失败，不调用 capture worker、不生成 node 图

## 5. 验证

- pytest：6/6 通过（DB 集成测试，事务回滚隔离）
- 测试覆盖：5 类 SMC 事件、多用户、失败隔离、幂等、未知类型
- 真实盘中运行：未核验（需盘中触发结构事件观察实际截图与消息）

## 6. 兼容性

- 文本通知链路不变（仍按用户合并一条）
- CaptureJob 模型字段不变（仅填充值变化：indicator_view 从 None/固定值变为按事件映射）
- 旧幂等键格式不再使用，不影响历史已发送消息

## 7. 未完成项（延后）

- 真实盘中运行验证（需等待盘中结构事件触发）
- 性能验证（一股票多事件并发截图的资源占用）
