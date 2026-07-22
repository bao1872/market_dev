# 盘迹项目开发与文档一致性规则 v3

适用项目：`market_dev` / 盘迹 PanJi
核心目标：防止 AI/Trae 在新对话、新机器、新分支中误解当前系统，防止已确认业务逻辑被旧代码、旧文档或旧记忆还原。

> 完整变更历史见 `docs/changes/CHANGELOG.md` 与 `docs/changes/records/CHANGE-*.md`。

---

## 一、最高原则

任何修改必须形成闭环：读取文档入口 → 理解系统地图 → 核对真实代码 → 建立 CHANGE → 明确修改/不修改范围 → 修改代码/文档/测试 → 运行一致性检查 → PR → 人工 Review 后合并。

完成标准（六者对齐）：代码实现 = 当前设计文档 = 系统地图 = API/数据契约 = 测试验证 = 部署配置。六者缺一不可。

---

## 二、必读入口

任何 Trae/Codex/ChatGPT 任务开始前必须先读取：`docs/AI-ONBOARDING.md`、`docs/current/MANIFEST.md`、`docs/RESTORE-CHECKLIST.md`、`AGENTS.md`（本文件）。

`docs/` 顶级目录只允许：`current/` `maps/` `changes/` `archive/` `contracts/` `decisions/` `runbooks/` `acceptance/` `evidence/` `work/`（`docs/` 根 `.md` 文件不受限）。

---

## 三、事实源优先级

冲突时判断顺序（前者覆盖后者）：1.用户当前明确要求 → 2.当前 main 代码 → 3.docs/current/MANIFEST.md → 4.docs/current/*.md → 5.docs/maps/*.md → 6.最新 docs/changes/records/*.md → 7.测试与 CI 结果 → 8.生产只读验证结果 → 9.archive 历史文档 → 10.旧聊天记忆。archive 和旧聊天不能覆盖 current。

---

## 四、修改流程

Trae 动手前必须输出：任务目标 / 分支和 base commit / 已读 docs/current 与 docs/maps / 当前代码入口（前端/API/Service/Repository/Worker）/ 涉及数据表 / 测试覆盖规则 / 文档与代码是否一致 / 本次准备修改什么 / 明确不修改什么 / 预计更新哪些 docs/current 与 docs/maps / 预计新增哪个 CHANGE。发现冲突先列出，不得直接编码。

---

## 五、CHANGE 规则

每次修改必须新增 `docs/changes/records/CHANGE-YYYYMMDD-NNN.md` 并更新 `docs/changes/CHANGELOG.md`。CHANGE 必填字段：变更编号、任务名称、需求出处、修改前/后行为、影响模块、修改文件、文档更新、测试证据、Git 分支、Git Commit、数据库迁移、配置变化、风险、遗留问题。不存在"小改不用 CHANGE"。`tools/check_docs_consistency.py` 规则 12 强制校验 CHANGE 引用可达性。

---

## 六、禁止行为

```
1.  未读 AI-ONBOARDING 和 MANIFEST 就修改；
2.  根据旧 docs/current/00-18 或 archive 修改当前系统；
3.  根据旧聊天记忆覆盖 current；
4.  只改代码不改文档 / 只改 current 不改 CHANGE / 改代码结构不更新 maps；
5.  复制旧实现形成第二条路径 / 在前端重新实现后端业务规则；
6.  删除测试以适配错误实现 / 修改 API 不检查前端调用 / 修改数据模型不检查 migration；
7.  修改 Worker 不检查幂等、心跳、重试 / 修改权限不检查用户隔离；
8.  把 Mock E2E 说成真实生产 E2E / 把 OPEN 问题写成最终结论 / 把临时实验写成永久规则；
9.  直接修改 main / force push 已共享分支 / 为通过检查削弱 check_docs_consistency.py；
10. 未经许可修改生产环境账户密码；
11. 生产代码/测试/工具/构建脚本在运行时 import/open/read/glob `ref/` 目录（详见 §七.8）；
12. `git add -A` / `git add .` / `git add -u` 批量暂存（必须精确 `git add <file>`）。
```

---

## 七、盘迹硬规则

### 1. 产品边界
盘迹是 A 股研究、全市场特征计算、自选股盘中监控和消息投递平台。不做：自动交易、券商账户连接、资金管理、收益承诺、单一指标买卖信号、普通用户修改生产算法参数。

### 2. 策略规则
当前生产只保留 `dsa_selector` 与 `watchlist_monitor`。多策略组合已废弃，不得从旧代码或旧文档恢复。

### 3. DSA 规则
DSA 对全市场 computable universe 计算特征；不得在计算阶段按方向、强弱、matched、用户筛选提前删除股票。发布必须满足严格完整性门禁；`partial_failed` 不得发布。

### 4. 自选和监控
有效会员添加自选后自动进入盘中监控；不创建 MonitoringPlan。到期用户保留历史数据，但不能读取、修改、监控或产生新投递。

### 5. Node Cluster 固定契约
`1d=250 根日线`、`15m=250*16=4000 根`、`1m=2 根已完成 Bar`。图表显示数量、指标输出数量、Node 内部输入数量必须分离。**禁止修改 250/4000/2 固定参数**；禁止飞书舞台 90 bar 展示参数进入任何指标计算逻辑（CHANGE-20260720-001）。

### 6. 飞书
唯一接入方式：`feishu_platform_app`。禁止恢复 `feishu_webhook` / `FEISHU_WEBHOOK` / 独立管理员飞书 App / 独立管理员接收人配置。管理员内测申请通知必须复用管理员用户自己的 active `feishu_platform_app` NotificationChannel。

盘中监控触发只依赖**最新已完成 1m bar**（`source_bar_time` 来自最新已完成 1m bar，剔除最后一根可能未完成的 bar）；飞书盘中截图业务默认 `timeframe=1d`，实时性由 Capture Snapshot `1d + include_realtime=True` 的 partial daily 合成保证；修截图/清晰度/缓存不得改变 `watchlist_monitor` 事件计算口径（`monitor_batch_service` 计算输入 `bars_daily` / `bars_15min` 必须 `include_realtime=False`）。

### 7. Capture Token
Capture Token 只能访问 Capture API；不能访问普通用户 API；不能污染普通 Access Token。

### 8. ref/ 彻底隔离
`ref/` 目录下所有文件（含 `ref/smc_user_source.pine`、`ref/smc_user_export.pine`、`ref/smc.py`、`ref/盘迹品牌视觉资产包_v1.0/`）仅供人工阅读参考，**禁止作为运行依赖**。生产代码、测试、工具、构建脚本在运行时不得 `import`/`open`/`read`/`glob` `ref/` 目录下任何文件（CHANGE-20260718-004）。

`AGENTS.md` / `docs/current/*.md` / `docs/maps/*.md` 不得把 `ref/` 文件称为"真源"、"合同"、"fixture 生成器"或"运行依赖"；应称为"参考源（人工阅读）"。算法真源必须是生产代码（如 `smc_pine_core.py`、`node_cluster_engine.py`、`indicator_contract.py`、`indicator_semantics.py`）。SMC Pine parity 测试只读取 `backend/tests/fixtures/smc_pine/*.csv`，**禁止从 DB 重新取 bar** 或依赖 `ref/` 导出脚本。

### 9. Migration
不得修改已发布历史 migration；只允许新增前向 migration；修改 migration 必须有 upgrade/downgrade/upgrade 验证。

### 10. 测试期部署不备份数据库
测试期部署默认不备份数据库；除非用户明确说"先备份数据库"，否则禁止 `pg_dump`/大体积备份，禁止写入 `/root/backups` 或 `/root/web_dev/backups`。当前物理机磁盘紧张，优先节省硬盘。

### 11. Docker 镜像保护
`node:20-alpine` 是受保护基础镜像，拉取很慢。禁止主动删除 `node:20-alpine`；禁止 `docker image prune -a`；除非明确升级 Node 版本或镜像损坏，否则不要删除 `node:20-alpine`。普通清理只允许 `docker builder prune -f`、`docker image prune -f`、`docker container prune -f`。

### 12. MDAS 唯一行情读取出口（SSOT）
`MarketDataAggregationService.get_bars` 是后端唯一行情读取出口。业务/API/indicator/SMC/strategy_batch/feature_snapshot/structural_factor/temporal_feature/monitor/capture/chart_bars 全部经 MDAS；禁止业务层直接调用 `bar_repository` 的私有 `_query_*`/`_get_adj_factor_df`/`apply_adj_factor*` 或旧 `bar_repository.get_bars`（CHANGE-20260717-002）。

原始 bar 始终保持不复权落库；qfq 只在 MDAS 出口统一应用一次；不信任 bar 自带 `adj_factor` 列。`adjustment_as_of` point-in-time 截断：`qfq_price = raw_price × factor(bar_date) / factor(as_of)`，as_of 之后的除权事件不得泄漏到历史回算中。盘后顺序门禁：原始日线刷新 → 公司行为/factor 重建成功 → 覆盖率门禁/DSA → snapshot 发布。因子未完成时不得创建 DSA 或发布 snapshot。

MDAS 必须实现 count-aware 回补：daily required_count=250、15m required_count=4000、completed_only=True、include_realtime=False、adj=qfq、统一 adjustment_as_of；实际返回少于 required_count 时自动向前扩展，直到达到 required_count、到达真实上市历史起点或安全边界；必须返回 `history_exhausted: bool` 区分"DB 历史不足"与"系统未取满"（CP-V3-A2）。

### 13. Atomic Chart Snapshot 单 MDAS 读取
Atomic Snapshot 必须使用单次 MDAS 读取，直接将 DataFrame/CanonicalInput 传递给指标计算；**禁止在单次请求中进行第二次市场数据读取**；Redis 仅缓存最终 Snapshot 响应（CP-16）。前端只请求 chart-snapshot；独立的 Bars/Indicators 请求不恢复。

### 14. SMC FVG 完全排除 + 严格 time-key
Fair Value Gap 不计算、不返回、不缓存、不渲染，也不暴露 FVG 开关；生产计算路径不包含 FVG 函数或状态；输出结构中不存在 FVG 相关键、事件或 box。FVG 验收为输出级别断言（CHANGE-20260715-001 ~ 002）。

SMC 渲染必须使用严格 time-key 匹配：`strictTimeKey=true` 时 time 缺失→`missing_time`+skip，time 匹配失败→`match_failed`+skip，**禁止 index fallback**；events 和 EQH/EQL 使用 OR 逻辑（anchor/confirmed 任一匹配即渲染，两者都缺失才 skip）；详情链和 Capture（90-bar 舞台）共用同一 SMC 坐标映射核心，只允许 font/lineWidth/lane 差异（CP-V3-C2）。

### 15. Canonical 四链统一调度
详情/盘后/盘中/Capture 四条调用链必须通过 `CanonicalComputationService`（`backend/app/services/canonical_computation_service.py`）调度已注册算法；禁止生产模块直接 `import` kernel 绕过注册表；四链只能做适配（节奏/去重/TTL/截图），基础指标值必须来自同一 Kernel；相同输入（instrument + timeframe + as_of + source_bar_hash + adj_factor_hash）必须得到相同 `result_hash`（5 维度确定性）（CHANGE-20260718-006）。

### 16. AFC Core 14 不可改
Atomic Fact Contract V1 的 Core 14 项不可修改；产品观察扩展不进入 `core`/`auxiliary`/`availability`，不影响 14/14 统计；worker 持久化链保持不变；schema_version bump 保证旧快照不可见（CHANGE-20260716-005 / 006）。

### 17. 三链五周期一致性
详情链 `/stock/:symbol` 切换 1d/15m/1h/1w/1mo 时，Node Cluster `profile_hash`/`daily_source_hash`/`bars_15m_source_hash` 必须完全一致（图表 bars frame hash 允许不同）；Atomic Facts 中的"筹码共识价"与详情页 Node Cluster 必须消费同一个 Canonical 结果（`node_cluster_engine.compute_node_cluster_profile` 唯一入口，三链同核）（CHANGE-20260721-001）。

### 18. 个股详情 K 线实时契约
`/quote` 实时只代表顶部行情卡片实时，不等价于 K 线实时。交易时段内，`/bars?timeframe=1d&include_realtime=true` 必须返回今日 partial daily bar（`data_source=hybrid`、`is_partial=true`、`last_live_bar_time` 非空、最后一根 bar 日期为今日、close 来自最新已完成 1m bar）。收盘后或非交易时段不得伪装实时（`is_partial=false`、1d 最后一根应为完整日线、quote 可为 `daily_fallback`）。Exchange→MDAS→ChartSnapshot→Canvas 是唯一 K 线链；**禁止前端 `mergeRealtimeQuoteIntoBars()` 或任何 quote→bar 兜底**（CP-V3-D2 修正）。

### 19. 板块同步降级保护（pywencai 唯一数据源）
pywencai（`wencai_board_provider.py`）为唯一板块分类源；`/market/boards` 只读数据库 + Redis 状态，不在用户 API 请求链访问问财；`backend/Dockerfile` 必须安装 `nodejs`；盘后 worker 唯一同步入口是 `after_close_orchestrator.py` 的 `syncing_boards` 步骤；`BOARD_SYNC_ENABLED` 默认 `false`；`mode=dsa_only` 跳过该步骤。不得增加 akshare、代理、IP 绕过、东方财富混用或新常驻 worker（CHANGE-20260713-006 / PR #77）。

### 20. 文档目录与 CI 门禁
`tools/check_docs_consistency.py` 必须通过；规则包括：MANIFEST 存在且含实现核对基线（40 位 SHA 且为 HEAD 祖先）、baseline 必须在 HEAD 的最近 50 个 commit 内、docs/current/*.md 与 docs/maps/*.md 存在、本地 Markdown 链接有效、无"待填写"占位符、feishu_webhook 不得回退为当前方案、open-decisions 不得把 Webhook vs Platform App 写回 OPEN、CHANGE 引用必须可达、ref/ 隔离文本扫描。CI 必须失败若代码 SHA 变化后未同步 current/contracts/CHANGE/MANIFEST baseline。

### 21. 提交安全
禁止 `git add -A` / `git add .` / `git add -u`；必须精确 `git add <file>`。不得提交：`.vscode/settings.json`、`.traeignore`、`node_modules/`、`.venv/`、`__pycache__/`、`*.py[cod]`、`.mypy_cache/`、`.pytest_cache/`、`.ruff_cache/`、`.coverage`、`coverage.xml`、`dist/`、`build/`、`*.log`、`*.csv`、`*.parquet`。长命令（mypy 冷启动、大批量测试）必须用后台日志方式：`nohup bash -lc '<command>' > /tmp/<name>.log 2>&1 &`。未经用户明确授权禁止删除：数据库卷、运行中容器、postgres/redis 数据目录、node_modules、.venv、.git、源码、生产数据。

### 22. 因子版本追踪与 auto-resume
成功因子重建后必须调用 `stamp_factor_reconciliation_version` 写入 `factor_algorithm_version`/`factor_reconciliation_version`/`factor_reconciled_at`；盘后流程通过 `find_stale_version_instruments` 识别版本过期的影响集。`after_close_orchestrator` 任务支持 auto-resume：`interrupted` → `resume_queued`（`attempt_no` 递增，max=3），`lease_epoch` fencing 防止旧 worker 写入，`last_completed_step` 支持断点恢复（CP-V3-D）。

---

## 八、质量门禁

```
Ruff   新增/修改 Python 文件零错误；历史债务由 tools/quality_baselines/ruff.json 管控
Mypy   新增 backend/app Python 生产文件零错误；历史债务由 tools/quality_baselines/mypy.json 管控
Docs   python tools/check_docs_consistency.py
Arch   python tools/check_architecture.py
Allow  python tools/check_test_allowlist.py
Sync   python tools/update_docs.py --check
```

禁止通过全局 ignore、批量 noqa、扩大 exclude、批量 `type: ignore` 或关闭检查掩盖新增问题。前端：`tsc --noEmit`、`npm run lint`、`npm run build`、`npm run test:contract`、`npm run test:e2e`。

---

## 九、分支与 PR

每个变更使用独立分支：`fix/<topic>` `feat/<topic>` `docs/<topic>` `refactor/<topic>` `chore/<topic>` `experiment/<topic>`。禁止直接改 main。PR 必须说明：当前系统原来如何运行、本次为什么修改、修改了哪些代码/docs/current/docs/maps、新增哪个 CHANGE、是否改变 API/数据模型/Worker 或第三方集成、测试结果、是否仍有 Known Gap、是否需要生产验证。

---

## 十、完成报告格式

当前分支 / Base Commit / Head Commit；一、修改前理解（产品行为/系统地图/代码入口/文档依据/冲突）；二、实际修改（代码/docs/current/docs/maps/docs/changes/tools/测试）；三、一致性检查（current/maps/CHANGE/CHANGELOG/archive 是否更新）；四、验证（执行命令/测试结果/CI 状态）；五、剩余问题（Known Gap/OPEN/需要生产验证）。

---

## 十一、变更历史索引

完整变更历史见 `docs/changes/CHANGELOG.md`（按日期顺序的简短摘要）与 `docs/changes/records/CHANGE-YYYYMMDD-NNN.md`（每条变更的完整记录）。任何对历史变更的疑问必须查阅对应 record，不得凭旧聊天记忆或 archive 推断。

近期关键变更（仅列编号，详见 records）：

- CHANGE-20260713-005 ~ 010：行情列表 DSA SSOT、品牌视觉 V1.0、行业/概念筛选、市值与 Excel 导出
- CHANGE-20260715-001 ~ 006：SMC 智能资金指标、Pine parity core、MiniKline viewport 重写
- CHANGE-20260717-002：MDAS SSOT 与复权唯一出口
- CHANGE-20260718-002 ~ 006：docs 顶层目录规范、Docker 构建性能、ref/ 隔离、全算法族 SSOT
- CHANGE-20260720-001：日线 SMC 盘中监控、Canonical 四链 re-export 接入
- CHANGE-20260721-001 ~ 002：FR-11 缓存精确失效、nodeAvailability 5 态、Display Frame Contract V2
