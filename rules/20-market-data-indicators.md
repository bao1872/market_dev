# 20 行情与指标

> 来源：AGENTS.md §七.5、§七.12-19、§七.23

## Node Cluster 固定契约

- `1d=250 根日线`；
- `15m=250*16=4000 根`；
- `1m=2 根已完成 Bar`。

图表显示数量、指标输出数量、Node 内部输入数量必须分离。

**禁止修改 250/4000/2 固定参数**。

禁止飞书舞台 90 bar 展示参数进入任何指标计算逻辑。

## MDAS 唯一行情读取出口（SSOT）

`MarketDataAggregationService.get_bars` 是后端唯一行情读取出口。

业务/API/indicator/SMC/strategy_batch/feature_snapshot/structural_factor/temporal_feature/monitor/capture/chart_bars 全部经 MDAS。

禁止业务层直接调用 `bar_repository` 的私有 `_query_*` / `_get_adj_factor_df` / `apply_adj_factor*` 或旧 `bar_repository.get_bars`。

### 复权口径

- 原始 bar 始终保持不复权落库；
- qfq 只在 MDAS 出口统一应用一次；
- 不信任 bar 自带 `adj_factor` 列；
- `adjustment_as_of` point-in-time 截断：`qfq_price = raw_price × factor(bar_date) / factor(as_of)`；
- as_of 之后的除权事件不得泄漏到历史回算中。

### 盘后顺序门禁

原始日线刷新 → 公司行为/factor 重建成功 → 覆盖率门禁/DSA → snapshot 发布。

因子未完成时不得创建 DSA 或发布 snapshot。

### count-aware 回补

MDAS 必须实现 count-aware 回补：

- daily required_count=250；
- 15m required_count=4000；
- completed_only=True；
- include_realtime=False；
- adj=qfq；
- 统一 adjustment_as_of。

实际返回少于 required_count 时自动向前扩展，直到达到 required_count、到达真实上市历史起点或安全边界。

必须返回 `history_exhausted: bool` 区分"DB 历史不足"与"系统未取满"。

## Atomic Chart Snapshot 单 MDAS 读取

- Atomic Snapshot 必须使用单次 MDAS 读取，直接将 DataFrame/CanonicalInput 传递给指标计算；
- **禁止在单次请求中进行第二次市场数据读取**；
- Redis 仅缓存最终 Snapshot 响应；
- 前端只请求 chart-snapshot；
- 独立的 Bars/Indicators 请求不恢复。

### quote 唯一真源

ChartSnapshot 是个股详情页 quote 的唯一真源。`BarAggregationResult.latest_daily_quote` 字段在单次 MDAS 读取内派生当日行情事实：

- 1d/1w/1mo：从聚合前的 `daily_df`（已合并今日 partial daily + qfq）取末根日线 OHLC；
- 1m/15m/1h：从已加载的目标周期 `bars_df_full`（limit 截断前）按最新交易日聚合 open/high/low/close/volume/amount；
- **禁止为 quote 增加第二次 Pytdx/Repository/MDAS 行情读取**；
- `fetch_today_daily_bars` / `_query_daily_bars` 不得用于 quote 派生；
- `latest_daily_quote` 缺失时 `quote=null` 且 `freshness_state=unavailable`；
- 禁止从 1w/1mo page_df 派生日行情兜底；
- 所有周期返回 `current/open/high/low/prev_close/change_pct/volume/amount`，业务语义不随展示周期变化；
- 前端不得恢复 `useRealtimeQuote` 或独立 `/quote` 请求。

## SMC FVG 完全排除

Fair Value Gap 不计算、不返回、不缓存、不渲染，也不暴露 FVG 开关。

- 生产计算路径不包含 FVG 函数或状态；
- 输出结构中不存在 FVG 相关键、事件或 box；
- FVG 验收为输出级别断言。

### SMC 严格 time-key

SMC 渲染必须使用严格 time-key 匹配：

- `strictTimeKey=true` 时 time 缺失 → `missing_time` + skip；
- time 匹配失败 → `match_failed` + skip；
- **禁止 index fallback**；
- events 和 EQH/EQL 使用 OR 逻辑（anchor/confirmed 任一匹配即渲染，两者都缺失才 skip）；
- 详情链和 Capture（90-bar 舞台）共用同一 SMC 坐标映射核心，只允许 font/lineWidth/lane 差异。

## 飞书 Capture 固定组合图合同（CHANGE-20260728-010）

旧"每张截图只渲染一个指标视图"规则（advice.md v6）已被新组合视图合同取代。

- 飞书截图固定使用组合视图 `FEISHU_CAPTURE_VIEW='structure_node'`（结构 + 筹码共识）。
- 图层固定：`node + smc + volume`，`boll=false`；其余 `trend/macd/sqzmom/breakout=false`。
- 后端 `/capture/stocks/{id}/snapshot` 强制 `include_smc=True`，始终返回 Node 数据 + SMC DTO 结构。
- combined Ready = `nodeReady && smcContractReady`：Node 数据完整 + SMC DTO 结构存在（SMC 数组允许为空，无事件时不阻塞）。
- 旧 `node_cluster`/`bollinger`/`smc` 三套独立 preset 标记 `_legacy`，仅供历史 URL 参数回读兼容；新业务不再写入。
- 事件类型 → 监控事件类别映射用于文字与统计归类，不再决定截图图层：
  - 结构（EVENT_CATEGORY_STRUCTURE）：SMC BOS/CHoCH/EQH/EQL/OB first touch
  - 筹码共识（EVENT_CATEGORY_NODE_CONSENSUS）：node_cluster_touch

## Canonical 四链统一调度

详情/盘后/盘中/Capture 四条调用链必须通过 `CanonicalComputationService` 调度已注册算法。

- 禁止生产模块直接 `import` kernel 绕过注册表；
- 四链只能做适配（节奏/去重/TTL/截图），基础指标值必须来自同一 Kernel；
- 相同输入（instrument + timeframe + as_of + source_bar_hash + adj_factor_hash）必须得到相同 `result_hash`（5 维度确定性）。

## AFC Core 14 不可改

Atomic Fact Contract V1 的 Core 14 项不可修改。

- 产品观察扩展不进入 `core`/`auxiliary`/`availability`；
- 不影响 14/14 统计；
- worker 持久化链保持不变；
- schema_version bump 保证旧快照不可见。

## 三链五周期一致性

详情链 `/stock/:symbol` 切换 1d/15m/1h/1w/1mo 时：

- Node Cluster `profile_hash` / `daily_source_hash` / `bars_15m_source_hash` 必须完全一致；
- 图表 bars frame hash 允许不同；
- Atomic Facts 中的"筹码共识价"与详情页 Node Cluster 必须消费同一个 Canonical 结果；
- `node_cluster_engine.compute_node_cluster_profile` 是唯一入口，三链同核。

## 个股详情行情唯一真源

个股详情页行情唯一真源为 `/api/v1/instruments/{id}/chart-snapshot`。

- **禁止详情页同时调用 `/quote` 和 `/chart-snapshot`**；
- 禁止恢复前端 `useRealtimeQuote` 或 `mergeRealtimeQuoteIntoBars()`。

### quote 派生

- quote 从同一 snapshot 的 `latest_daily_quote` 派生，保证 `as_of` 一致；
- 所有周期（1d/15m/1h/1w/1mo）返回完整 OHLC + prev_close + change_pct + volume + amount。

### K 线实时

- 交易时段内 `include_realtime=true` 返回 partial bar（`data_source=hybrid`、`is_partial=true`、`last_live_bar_time` 非空）；
- 收盘后不得伪装实时。

### 盘后边界

- 盘后 MFCS 回归必须使用 `include_realtime=False`；
- 不得产生新增日线查询；
- `latest_daily_quote` 从已有 `daily_df` / `bars_df_full` 派生。

### 数据周期合同

- 1d = DB 日线 + Pytdx 日线；
- 15m = DB 15m + Pytdx 原生 15m；
- 1h = DB 60m + Pytdx 原生 60m；
- 1w = 合并日线 → 周线；
- 1mo = 合并日线 → 月线；
- 禁止 1m → 15m / 1m → 60m / 1m → 1d 聚合。

### 来源区分

- `market` / `watchlist` / `direct` 必须显式区分；
- market/watchlist 双列布局（`200px minmax(0,1fr)`）；
- direct 单列布局（`minmax(0,1fr)`）。

### 来源列表稳定性

- symbol 切换只更新 active 行和右侧详情；
- 禁止页面级 loading 卸载/清空来源列表。

### 来源列表可见性（missing_origin invalid）

- symbol 切换只更新 active 行和右侧详情；
- 禁止页面级 loading 卸载/清空来源列表；
- 缺 originScope 时显示 missing_origin invalid 占位，不静默单列；
- 只有显式 direct 才使用单列。

## 板块同步降级保护

pywencai（`wencai_board_provider`）为唯一板块分类源。

- `/market/boards` 只读数据库 + Redis 状态；
- 不在用户 API 请求链访问问财；
- 后端镜像构建文件必须安装 `nodejs`；
- 盘后 worker 唯一同步入口是 `after_close_orchestrator` 的 `syncing_boards` 步骤；
- `BOARD_SYNC_ENABLED` 默认 `false`；
- `restart_from="daily_ready"` 跳过日线刷新，从 DSA 阶段重算（仍执行板块同步，由 `syncing_boards` 步骤控制）；
- 不得增加 akshare、代理、IP 绕过、东方财富混用或新常驻 worker。

## 因子版本追踪与 auto-resume

- 成功因子重建后必须调用 `stamp_factor_reconciliation_version` 写入 `factor_algorithm_version` / `factor_reconciliation_version` / `factor_reconciled_at`；
- 盘后流程通过 `find_stale_version_instruments` 识别版本过期的影响集；
- `after_close_orchestrator` 任务支持 auto-resume：`interrupted` → `resume_queued`（`attempt_no` 递增，max=3）；
- `lease_epoch` fencing 防止旧 worker 写入；
- `last_completed_step` 支持断点恢复。

## 第一金字塔历史 SSOT 与核心/筹码解耦（CHANGE-20260729-003）

### 历史点时安全

- `compute_first_pyramid_history` 必须一次计算所有指标 series，禁止循环 N 次调用 snapshot；
- 历史 DSA 用 `lookback=None`（完整历史，不截断）；当前 snapshot 的 250-bar 合同保持不变；
- `rope_dir1_pct` 必须段内 expanding 计数，禁止完整 group 统计回填过去（未来数据泄漏）；
- 截断后重算前 N 行必须与全量结果前 N 行一致（前缀不变性）。

### anchor/confirmed/event 分离

- SMC OB 生命周期输出为不可变的 `OB_CREATED` / `OB_ENTERED` / `OB_MITIGATED` 三状态，单次触发不可变；
- `OB_ENTERED` 仅在 OB 确认后、mitigation 前，由前一 bar 与区域无重叠到当前 bar high-low 首次重叠触发；
- 保留 anchor/confirmed/event、bias、structure_level 和上下界；删除"活跃 OB = OB_ENTRY"派生；
- `emit_timeline=True` 输出逐 bar state_timeline（swing_bias / internal_bias / active_*_ob_count），默认 False。

### review core 不得依赖 Node/15m

- 盘后 review core 关键路径使用 `compute_first_pyramid_core_snapshot`，禁止 Node Cluster 和 15m Node 输入；
- `compute_review_core_for_trade_date` 为 daily-core only 路径，不得用单周期 VP 伪装筹码；
- core 的 version/parameterHash/inputHash 排除 Node 参数；
- 现有非盘后调用（`compute_feature_snapshot_for_date`）保持兼容，不受影响。

### chip 不得阻塞发布

- 核心发布成功即标记主 run succeeded 并可复盘；
- 发布后只创建独立 `after_close_chip_consensus` job，不 await、不加入主 run 成功门禁；
- chip 任务可失败/部分成功/单独重试，绝不反改主 run 或重算 core；
- chip 使用独立 version/hash/run 关联；
- chip 持久化 migration 为下一阶段唯一 blocker，禁止用 Redis 冒充持久化、禁止未经验证新增 migration。
