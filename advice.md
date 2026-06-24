## 总体判断

这版前端的主链已经基本恢复：

* 选股页能读取已发布批次和结果；
* 自选股关系能够正确显示；
* 测试选股策略已不再出现在普通选股页；
* 监控组合入口已经删除；
* 最新代码为 `5330a56364c506781e1e2da38741ef0c9c0f30e9`。

执行记录显示，它已经重新构建前端、部署静态文件、重启后端和 `trading-scheduler.service`，并确认 `/health/ready` 和 `/version` 可访问。

但是目前还不能下结论说“后端定时任务正常”。**截图里已经暴露出至少一个明显异常：一个 scheduled 选股任务在 queued 状态停了约 16 小时。** 而系统概览中 Worker 和 Scheduler 都是 `unknown`，说明当前后台页面没有真正观测到任务进程。

---

# 一、截图里已经能确认的问题

## 1. 选股页股票名称仍然丢失

选股策略页中部分股票显示：

```text
-
a17c39be
```

即名称为空、代码退化成 UUID 前缀。

原因在前端：

```typescript
function toRow(r: StrategyResult) {
  return {
    resultId: r.id,
    instrumentId: r.instrument_id,
    payload: r.payload,
  }
}
```

它丢掉了后端已经返回的：

```text
instrument_symbol
instrument_name
instrument_market
```

然后 `getStockDisplay()` 只去 `payload` 里找股票信息，找不到就显示 UUID。

但前端类型本身已经声明了这些顶层字段。

### 修改

```typescript
interface ScreenerRow {
  resultId: string
  instrumentId: string
  symbol: string
  name: string
  market: string
  payload: Record<string, unknown>
  [key: string]: unknown
}

function toRow(r: StrategyResult): ScreenerRow {
  return {
    resultId: r.id,
    instrumentId: r.instrument_id,
    symbol: r.instrument_symbol ?? '-',
    name: r.instrument_name ?? '-',
    market: r.instrument_market ?? '',
    payload: r.payload,
  }
}
```

`getStockDisplay()` 优先读取 `row.symbol/name/market`，不要再依赖 payload。

---

## 2. 09:05 显示“已收盘”是错误状态

截图时间是 09:05，导航栏正确显示“A股盘前”，但自选股全部显示“已收盘”。

后台目前只有：

```text
交易时间内 → 正常状态
交易时间外 → MARKET_CLOSED
```

所以盘前、午休、盘后、非交易日全部被合并为 `MARKET_CLOSED`。

前端再把它直接翻译成“已收盘”。

09:05 没有指标本身是正常的，因为监控从 09:30 才开始；**错的是状态文案。**

应拆成：

```text
PRE_MARKET          盘前等待开市
TRADING             交易中
LUNCH_BREAK         午间休市
AFTER_MARKET        已收盘
NON_TRADING_DAY     非交易日
WAITING_FIRST_RUN   等待首次计算
SUCCEEDED           已计算
FAILED              计算失败
STALE               数据延迟
```

盘前有自选但从未计算过时，应显示：

```text
盘前等待开市
```

而不是“已收盘”。

---

## 3. 监控失败状态实际上读不到

聚合接口当前从 `MonitorState.payload` 中读取：

```python
evaluation_status = payload.get("evaluation_status")
error_code = payload.get("evaluation_error") or payload.get("error_code")
```

但监控算法写入 `MonitorState` 时只存：

```python
payload=curr_state.state
```

Evaluation 的 `FAILED / PENDING / DEAD / retry_count` 存在单独的 `monitor_evaluations` 表中，并不会自动进入状态 payload。

因此当前前端很难真正显示：

* 计算失败；
* 正在重试；
* 已超过最大重试；
* Worker 崩溃后恢复中。

### 修改

`GET /watchlist/monitor-status` 应同时查询：

1. 最新 `MonitorState`；
2. 每只股票最新一条 `MonitorEvaluation`。

建议使用窗口函数：

```sql
ROW_NUMBER() OVER (
  PARTITION BY instrument_id
  ORDER BY source_bar_time DESC
)
```

返回：

```json
{
  "monitor_status": "FAILED",
  "evaluation_status": "FAILED",
  "retry_count": 2,
  "error_code": "...",
  "source_bar_time": "...",
  "metrics": {}
}
```

---

## 4. “测试监控策略”仍然存在

策略目录截图里还有：

```text
测试监控策略
test_monitor_...
Active
已发布
```

这不是普通前端残留，而是**数据库里的测试策略仍被标记为生产、可见、已发布**。

启动 Seed 目前只自动归档：

```text
bb_monitor
volume_node_monitor
```

不会清理任意 `test_monitor_*`。

先查询准确 key：

```sql
SELECT
    sd.id,
    sd.strategy_key,
    sd.display_name,
    sd.environment,
    sd.is_user_visible,
    sd.is_scheduled,
    sv.id AS version_id,
    sv.version,
    sv.status
FROM strategy_definitions sd
LEFT JOIN strategy_versions sv
    ON sv.strategy_definition_id = sd.id
ORDER BY sd.strategy_key, sv.version;
```

确认测试策略后：

```sql
UPDATE strategy_definitions
SET environment = 'test',
    is_user_visible = false,
    is_scheduled = false
WHERE strategy_key = '<准确的 test_monitor key>';

UPDATE strategy_versions
SET status = 'archived'
WHERE strategy_definition_id = (
    SELECT id
    FROM strategy_definitions
    WHERE strategy_key = '<准确的 test_monitor key>'
);
```

不要按中文名称模糊删除。

---

## 5. 通知设置页仍有死代码和假保存

截图中还存在：

```text
Node 监控策略消息
Node 单策略过程消息
```

代码里对应的状态仍是：

```typescript
monitorNode
nodeSingle
```

而且“保存设置”现在只是：

```typescript
toast.show('已保存', '个人设置已保存')
```

没有任何后端请求，刷新页面后设置会丢失。

这是典型的假功能。

处理方式二选一：

* 暂时删除“用户通知规则”和“策略消息订阅”两张卡；
* 或真正增加 `GET/PUT /me/notification-preferences`。

订阅项不应该再按 Node 单策略划分，建议只保留：

```text
选股结果发布通知
盘中监控事件通知
站内消息
飞书通知
静默时段
通知冷却时间
```

---

## 6. 策略目录还有假“灰度发布”

管理员策略目录中的灰度操作目前只更新本地状态并弹 Toast，没有后端发布流量控制。

没有真正灰度基础设施前，应删除或禁用：

```text
灰度发布
流量比例
回滚
```

否则管理员会以为操作已经生效。

---

# 二、后端定时任务代码到底是什么状态

代码里确实存在三条核心任务。

## 1. 盘后行情拉取

计划时间：

```text
每天 16:00
```

处理：

```text
日线
15分钟
60分钟
```

交易日历判断后，串行更新全市场。

行情服务明确说明：

* pytdx 串行；
* 预计约 1.8 小时；
* 1 分钟线不由盘后任务更新；
* 1 分钟线在盘中监控时按需获取。

## 2. 盘后选股

计划时间：

```text
每天 18:00
```

它只负责创建 `queued` 的 StrategyRun，真正计算由 Strategy Batch Worker 消费。

## 3. 盘中监控

运行时间：

```text
09:30–11:30
13:00–15:00
```

理论上每 30 秒发起一轮。

生产 Compose 里目前用一个进程：

```text
WORKER_TYPE=all
```

把行情、选股、监控、Outbox、通知投递都放在一起运行。

---

# 三、目前不能说定时任务正常

## 1. queued 任务停了 16 小时

截图中的 Job Run：

```text
status = queued
耗时 = 16h35m
```

这是明显异常。

至少有两种可能：

1. Strategy Batch Worker 根本没有启动；
2. Worker 启动了，但该协程崩溃、卡死或被其他计算阻塞。

另外，代码在创建 queued run 时就把：

```python
started_at = datetime.now(UTC)
```

写入了数据库。

因此页面会把“排队等待时间”也当成运行耗时，但即使排除这个显示问题，**queued 一夜仍表示没有被消费。**

正确字段应是：

```text
created_at / queued_at
started_at
finished_at
```

只有 Batch Worker 成功领取任务后才写 `started_at`。

---

## 2. Batch Worker 没有恢复 running 任务

Batch Worker 只查：

```python
StrategyRun.status == "queued"
```

如果任务已经改成 `running` 后 Worker 崩溃，重启后它不会再被处理，会永久卡在 running。

需要给 StrategyRun 增加：

```text
lease_expires_at
heartbeat_at
worker_id
attempt_count
next_retry_at
```

并增加过期恢复。

---

## 3. 16:00 和 18:00 只是固定时间，不是真正依赖关系

行情更新预计约 1.8 小时，但 DSA 在 18:00 固定启动。

这存在竞态：

```text
16:00 行情开始
18:00 行情还没完成
18:00 选股尝试创建任务
覆盖率不足
本次选股直接跳过
当天不再重试
```

虽然 DSA 有 90% 行情覆盖率门禁，能防止用残缺数据计算，但目前门禁失败后只记录日志，没有“行情完成后自动重试”。

最合理的逻辑应是：

```text
日线行情阶段成功
→ 写入 bars_refresh_completed
→ 校验覆盖率
→ 自动创建 DSA run
```

18:00 或 18:30 只作为兜底检查，不是主触发方式。

---

## 4. 全部 Worker 在一个进程里风险很大

`WORKER_TYPE=all` 会同时运行：

* 全市场行情；
* DSA 计算；
* 盘中监控；
* Outbox；
* 通知投递；
* 日历更新。

如果 DSA 或 pytdx 串行任务占用大量 CPU、线程或数据库连接，盘中监控和通知可能被拖慢。

生产环境建议拆成：

```text
trading-bars-scheduler
trading-strategy-scheduler
trading-strategy-batch
trading-monitor
trading-outbox
trading-delivery
```

每个独立服务、独立日志、独立重启。

---

## 5. 系统概览现在无法证明 Worker 正常

后台 API 中：

```python
worker_health = "unknown"
scheduler_health = "unknown"
queue_backlog = 0
notification_delivery_rate = 0.0
recent_anomalies = []
```

仍然是固定返回值。

所以截图中的 `unknown` 不是 Worker 一定故障，而是**系统根本没有 Worker 心跳数据源**。

此外 `/health/ready` 目前只检查：

* 策略资产；
* 策略 Seed。

它不检查 Worker 或 Scheduler。

因此：

```text
/health/ready = ready
```

只能证明 API 可以启动，不能证明定时任务在运行。

---

# 四、盘中监控还有两个实现缺口

## 1. 心跳函数写了但没有真正调用

代码已经增加：

```python
update_heartbeat()
recover_stale_evaluations()
```

但在 `execute_monitor_cycle()` 和 Worker 启动流程中，没有看到明确调用恢复函数；单只股票计算过程中也没有周期调用 heartbeat。

这意味着：

* 字段有了；
* 方法有了；
* 但恢复机制没有真正接进执行主链。

所谓崩溃恢复测试中，前两个测试还只是手工复制状态判断逻辑，并没有真正执行 `_process_instrument_evaluation()`。

### 修改

Monitor Worker 启动时：

```python
async with AsyncSessionLocal() as db:
    recovered = await service.recover_stale_evaluations(db)
    await db.commit()
```

长计算过程应定时：

```python
await service.update_heartbeat(db, evaluation_id)
await db.flush()
```

更好的实现是后台 heartbeat task，而不是只调用一次。

## 2. “每 30 秒”并不是真正固定 30 秒

当前流程是：

```text
执行完整一轮所有股票
→ sleep 30 秒
```

所以实际周期是：

```text
整轮计算耗时 + 30 秒
```

如果以后监控 500 只股票、一轮需要 4 分钟，那么每只股票约 4 分 30 秒才更新一次。

需要改为：

* 固定调度 tick；
* 队列化单股票 evaluation；
* 多个受控消费者并行；
* pytdx 访问限流。

---

# 五、你现在怎么检查定时任务

我无法直接读取你的服务器 systemd 日志，所以需要在服务器执行以下命令。

## 第一步：确认到底启动了什么 Worker

```bash
sudo systemctl status trading-scheduler.service --no-pager -l
sudo systemctl cat trading-scheduler.service

sudo systemctl show trading-scheduler.service \
  -p MainPID \
  -p ExecStart \
  -p Environment \
  -p WorkingDirectory
```

检查进程环境：

```bash
PID=$(systemctl show trading-scheduler.service -p MainPID --value)

sudo tr '\0' '\n' < /proc/$PID/environ \
  | grep -E 'WORKER_TYPE|DATABASE_URL|REDIS_URL|TZ'
```

必须确认：

```text
WORKER_TYPE=all
```

或者明确看到各类独立服务。

## 第二步：检查所有子任务是否启动

```bash
sudo journalctl -u trading-scheduler.service \
  --since "today 00:00" \
  --no-pager \
  | grep -E \
  "Worker 启动|Bars Scheduler|Strategy Scheduler|Strategy Batch|Monitor Scheduler|Calendar Scheduler|Outbox|Delivery"
```

至少应看到：

```text
Strategy Batch Worker 启动
Bars Scheduler Worker 启动
Strategy Scheduler Worker 启动
Monitor Scheduler Worker 启动
```

缺任何一个，对应链路就没有运行。

## 第三步：检查盘后行情

```bash
sudo journalctl -u trading-scheduler.service \
  --since "today 15:45" \
  --no-pager \
  | grep -E \
  "开始行情刷新|每日增量更新|定时任务完成|period_counts|拉取失败|行情刷新.*异常"
```

数据库检查：

```sql
SELECT
    trade_date,
    COUNT(*) AS rows,
    COUNT(DISTINCT instrument_id) AS instruments
FROM bars_daily
WHERE trade_date >= CURRENT_DATE - 7
GROUP BY trade_date
ORDER BY trade_date DESC;

SELECT COUNT(*)
FROM instruments
WHERE status = 'active';
```

当天 `COUNT(DISTINCT instrument_id) / active 股票数` 应达到至少 90%。

再检查日内周期：

```sql
SELECT MAX(trade_time), COUNT(DISTINCT instrument_id)
FROM bars_15min;

SELECT MAX(trade_time), COUNT(DISTINCT instrument_id)
FROM bars_60min;
```

## 第四步：检查选股

```sql
SELECT
    id,
    run_type,
    trade_date,
    status,
    started_at,
    finished_at,
    total_instruments,
    succeeded_count,
    failed_count,
    skipped_count,
    published_at
FROM strategy_runs
ORDER BY started_at DESC
LIMIT 20;
```

查卡死任务：

```sql
SELECT
    id,
    status,
    started_at,
    NOW() - started_at AS age,
    total_instruments,
    succeeded_count,
    failed_count
FROM strategy_runs
WHERE status IN ('queued', 'running')
ORDER BY started_at;
```

查某个 run 的明细：

```sql
SELECT status, COUNT(*)
FROM strategy_run_items
WHERE run_id = '<run_id>'
GROUP BY status;
```

健康链应是：

```text
16:00 行情开始
→ 行情完成
→ DSA queued
→ running
→ completed
→ published
```

不能停在 queued。

## 第五步：检查盘中监控

交易时段执行：

```sql
SELECT
    status,
    COUNT(*) AS count,
    MAX(source_bar_time) AS latest_bar,
    MAX(calculated_at) AS latest_calculated,
    MAX(heartbeat_at) AS latest_heartbeat
FROM monitor_evaluations
WHERE source_bar_time::date = CURRENT_DATE
GROUP BY status;

SELECT
    COUNT(DISTINCT instrument_id),
    MAX(updated_at)
FROM monitor_states;

SELECT COUNT(DISTINCT instrument_id)
FROM user_watchlist_items
WHERE active = true;

SELECT COUNT(*)
FROM monitor_evaluations
WHERE status = 'PENDING'
  AND lease_expires_at < NOW();
```

在 09:30 以后：

* `MAX(source_bar_time)` 应接近当前完成分钟；
* `MAX(updated_at)` 不应落后几分钟以上；
* `SUCCEEDED` 应持续增长；
* 过期 PENDING 不应长期堆积；
* 去重计算股票数应接近全用户自选股去重数。

---

# 六、建议修改顺序

### P0：先把任务运行链修通

1. 查明 queued 16 小时任务为什么没有被消费；
2. 确认 Strategy Batch Worker 是否启动；
3. 修复 queued/running 任务 lease 和恢复；
4. 将行情完成改成 DSA 的真实触发条件；
5. 增加 Worker/Scheduler 心跳表和管理 API。

### P1：修复前端剩余错误

1. 修复选股页 symbol/name/market 映射；
2. 拆分盘前、午休、盘后状态；
3. Watchlist 聚合接口关联 MonitorEvaluation；
4. 清理测试监控策略；
5. 删除 Node 通知订阅和本地假保存；
6. 删除假灰度发布；
7. AdminJobs 不再把 watchlist_monitor 当成 StrategyRun。

### P2：清理死代码

1. 删除已下线组合模型的 legacy 文件；
2. 删除未调用的 Hook、类型、组件；
3. 把 `if __name__ == "__main__"` 自测迁移成 pytest；
4. 删除永远返回固定值的占位接口字段；
5. 增加 CI：后端测试、前端类型检查、构建、数据库迁移检查。

下面这版可以直接交给 Trae。

# 下一轮任务：定时任务闭环、运行可观察性与死代码清理

代码基线：

5330a56364c506781e1e2da38741ef0c9c0f30e9

本轮不要继续增加产品功能，重点修复定时任务真正可运行、可恢复、可观察，以及删除假功能和旧语义。

## 一、立即诊断线上任务

必须先提供：

1. systemctl status trading-scheduler.service
2. systemctl cat trading-scheduler.service
3. systemctl show 中的 MainPID、ExecStart、Environment、WorkingDirectory
4. 运行进程的 WORKER_TYPE、TZ、DATABASE_URL
5. 当天完整 scheduler 日志
6. strategy_runs 中 queued/running 任务
7. strategy_run_items 状态分布
8. 当天 bars_daily 覆盖率
9. 当天 monitor_evaluations 状态分布

重点解释截图中 queued 任务持续约 16 小时的原因。

## 二、拆分生产 Worker

不要继续使用一个 WORKER_TYPE=all 进程承载所有任务。

拆为：

* trading-bars-scheduler
* trading-strategy-scheduler
* trading-strategy-batch
* trading-monitor
* trading-outbox
* trading-delivery
* trading-calendar-scheduler

将 systemd unit 或 Docker Compose 配置提交到 Git，禁止服务器存在仓库外不可追踪的启动配置。

## 三、建立任务状态表

新增 scheduler_job_runs：

* id
* job_name
* business_date
* scheduled_at
* started_at
* finished_at
* status
* heartbeat_at
* lease_expires_at
* total_count
* succeeded_count
* failed_count
* progress
* error_code
* error_message
* metadata

新增 worker_heartbeats：

* worker_name
* instance_id
* started_at
* heartbeat_at
* status
* current_job_id
* build_sha

管理员首页必须从这两张表返回真实 worker_health 和 scheduler_health。

/health/ready 不应只检查 API Seed；新增 /health/workers 或在管理员接口中返回各 Worker 心跳。

## 四、盘后行情与选股依赖关系

当前 16:00 行情和 18:00 选股是两个独立固定时间任务，必须改为：

1. 行情任务先完成日线阶段；
2. 持久化任务结果和覆盖率；
3. 日线覆盖率达到阈值后自动创建 dsa_selector run；
4. 18:30 只作为兜底补偿；
5. 行情失败或未完成时自动重试，不得当天永久跳过。

盘后日线优先于 15m/60m，不能逐只股票交叉执行三个周期，导致日线覆盖迟迟不能完成。

建议顺序：

全市场日线
→ 校验覆盖率
→ 触发 DSA
→ 全市场 15m
→ 全市场 60m

## 五、StrategyRun 恢复机制

新增：

* queued_at
* started_at 只在 Worker 领取后赋值
* heartbeat_at
* lease_expires_at
* worker_id
* attempt_count
* next_retry_at
* error_code

Worker 启动时：

* 恢复 lease 过期的 running；
* 重新领取 stale queued；
* 超过最大次数标记 failed；
* 不允许 queued 任务无限期存在。

修改幂等键，至少包含：

strategy_key + trade_date + run_type

否则同一天已有 scheduled run 后无法创建合法 replay/manual run。

## 六、盘中监控修复

1. Worker 启动时实际调用 recover_stale_evaluations。
2. 单股票长计算期间实际调用 update_heartbeat。
3. 不要以“整轮完成后 sleep 30 秒”实现固定周期。
4. 将股票 Evaluation 放入队列，由受控并发消费者处理。
5. 保留 pytdx 限流。
6. Watchlist monitor-status 必须 JOIN 最新 MonitorEvaluation。
7. 不得从 MonitorState payload 推断 FAILED/PENDING/DEAD。
8. 增加 PRE_MARKET、LUNCH_BREAK、AFTER_MARKET、NON_TRADING_DAY 状态。

## 七、修复页面剩余问题

1. ScreenerPage 使用 StrategyResult 顶层的：

   * instrument_symbol
   * instrument_name
   * instrument_market
2. 不再显示 UUID 前缀作为股票代码。
3. 清理数据库中的 test_monitor 策略：

   * environment=test
   * is_user_visible=false
   * is_scheduled=false
   * 版本 archived
4. SettingsPage 删除：

   * Node 监控策略消息
   * Node 单策略过程消息
5. 通知规则未接后端前整卡禁用或删除，不得点击保存后只弹成功 Toast。
6. AdminStrategiesPage 的灰度发布没有后端支持，必须禁用或删除。
7. AdminJobsPage 只把 selector 的 StrategyRun 作为 Job Run。
8. watchlist_monitor 应通过 MonitorEvaluation/MonitorCycle 展示，不应调用 StrategyRun 接口。

## 八、构建版本

前端和后端构建时必须注入：

* GIT_SHA
* BUILD_TIME
* VITE_GIT_SHA
* VITE_BUILD_TIME

禁止线上继续显示：

* frontend SHA = dev
* backend SHA = unknown
* Worker = unknown
* Scheduler = unknown

## 九、死代码清理

执行并提交结果：

git grep -n "SelectionPlan"
git grep -n "MonitoringPlan"
git grep -n "Node Monitor"
git grep -n "nodeSingle"
git grep -n "monitorNode"
git grep -n "TODO:"
git grep -n "[LEGACY]"

对结果分类：

* 正式运行代码：修改；
* 已下线路由和模型：删除；
* 迁移历史：保留；
* 注释和文档：更新；
* 临时占位 UI：删除或禁用。

## 十、完整测试

必须运行：

* 后端全部 pytest
* Evaluation Worker 崩溃恢复集成测试
* StrategyRun Worker 崩溃恢复集成测试
* 行情完成触发 DSA 的集成测试
* 前端 tsc
* 前端 production build
* 关键页面 E2E
* Alembic 从空库升级到 head
* Alembic 从现有生产 revision 升级到 head

最终必须提供：

1. Git SHA
2. systemd 或 Compose 服务配置
3. Worker 启动日志
4. 16:00 行情任务日志
5. DSA 自动触发日志
6. 盘中监控日志
7. 数据库任务状态截图
8. 后端测试退出码
9. 前端测试和构建退出码
10. 修复后的页面截图

不能只回复“代码已修改”或“测试通过”。
