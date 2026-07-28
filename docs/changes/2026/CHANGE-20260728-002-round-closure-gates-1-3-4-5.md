# CHANGE-20260728-002：盘迹一轮收口（Gate 1/3/4/5 代码+验证）

状态：代码完成+源码级验证通过；真实运行验收受本地环境限制（详见 §6）
日期：2026-07-28
类型：architecture
对应 PRD：PRD20（量化模型）、PRD30（盘后编排）、PRD60（权限/管理后台）
对应 Map：`maps/20-quant-model.md`、`maps/30-after-close.md`、`maps/60-permissions-admin.md`、`maps/80-system-runtime.md`

## 1. 变更摘要

本轮完成盘迹现存问题的"一轮收口"，覆盖四个 Gate 的代码实现与可本地完成的验证：

- **Gate 1 第一金字塔（P0）**：统一 VolumeContext + 趋势/结构/动量/筹码四层 + DTO/持久化/API/UI
- **Gate 3 盘后编排**：15:05 Asia/Shanghai 触发 + WenCai/板块同步软失败降级 + 幂等
- **Gate 4 Worker 心跳**：stopped_at 字段 + UI 智能时间显示 + 历史实例折叠
- **Gate 5 GoAccess**：/admin/visitors API + 生产 Compose 设计 + 前端空/错/加载态

## 2. Gate 1 第一金字塔完整契约

### 2.1 统一 VolumeContext

新增 `backend/app/services/volume_context.py`，趋势/结构/动量复用同一计算：

| 字段 | 公式 | 窗口不足 |
|---|---|---|
| volume_ma_20 / volume_ma_200 | 简单移动平均 | null + readiness |
| volume_ratio_20 / volume_ratio_200 | V / MA | null |
| volume_percentile_20 / volume_percentile_200 | 经验分布百分位（0–100） | null |
| volume_zscore_20 / volume_zscore_200 | (V - mean) / std | null |

### 2.2 四层维度

- **趋势 DSA VWAP**：dir 方向 + 段起止/持续bars/涨跌幅/斜率 + 段均量比 + 统一 VC
- **结构 SMC**：BOS/CHoCH/EQH/EQL/OB + 每事件引用事件 bar 的 VolumeContext
- **动量 SQZMOM/Bollinger**：挤压/释放 + 缩量挤压/放量释放判断 + 统一 VC
- **筹码 Node Cluster**：可选层，None 不阻断前三层

### 2.3 后端契约与持久化

- `FirstPyramidSnapshot` DTO（`backend/app/schemas/first_pyramid.py`）
- `feature_snapshot_service.build_summary_payload` 计算并存储 first_pyramid 字段
- API：`GET /api/v1/stocks/{symbol}/first-pyramid`（读已存或实时回退）
- 算法版本：`1.1.0-gate1-volume-context`

### 2.4 前端 UI

- `FirstPyramidPanel.tsx`：顶部综合状态 + 三张必选层卡 + 可折叠筹码卡
- 共享"量能水位"条（20/200 日百分位刻度 + zscore）
- 趋势卡段均量比、结构事件量能徽标、动量缩量/放量转换

### 2.5 验证

- 源码级验证：VolumeContext 模块自测 PASS（250 bars，量能 spike/shrink 检测正确）
- 契约测试：38/38 PASS（DTO、端到端、跨入口一致性、不变量、golden、错误处理、PRD20 QM 映射）
- **未验证**：持久化运行时（需盘后 pipeline 运行；受本地不启动 scheduler/worker 约束）

## 3. Gate 3 盘后编排

### 3.1 触发时间修复

- 根因：原配置 hour=16, minute=0（收盘后 1 小时）
- 修复：`CronTrigger(day_of_week="mon-sun", hour=15, minute=5, timezone=ZoneInfo("Asia/Shanghai"))`
- 非交易日：`is_trading_day_async` 判断，非交易日跳过
- 幂等：`create_after_close_run` 使用 `acquire_job_run_lock`，同 business_date 重复创建返回已有 run

### 3.2 WenCai/板块同步降级

- `BOARD_SYNC_ENABLED=false` → 标记 `status: "skipped"` + `reused_previous_snapshot: True`
- 同步异常 → 标记 `status: "failed"` + `error_code: type(exc).__name__` + `reused_previous_snapshot: True`
- 软失败不 raise，继续后续 DSA/快照/发布
- 核心 EOD 行情/因子失败仍按契约阻断

### 3.3 验证

- 源码级验证：worker.py 15:05 + Asia/Shanghai + is_trading_day_async PASS
- 源码级验证：orchestrator error_code + reused_previous_snapshot + skipped/failed PASS
- 源码级验证：create_after_close_run 幂等锁 PASS
- **未验证**：真实盘后运行（受本地不启动 scheduler/worker 约束）

## 4. Gate 4 Worker 心跳

### 4.1 变化

- `WorkerHeartbeat` 模型新增 `stopped_at`（nullable, timezone-aware）
- 迁移 `070_worker_heartbeat_stopped_at`
- `_heartbeat_loop` 退出时写 `status="stopped"` + `stopped_at=now`（不再覆盖 heartbeat_at）
- `mark_stale_worker_heartbeats` 清理僵尸时同步写 stopped_at
- `WorkerHeartbeatItem` schema + 前端类型新增 stopped_at 字段

### 4.2 UI 变化

- `AdminJobsPage.tsx`：新增 started_at 列 + 智能"时间状态"列
  - running/idle：显示"距上次心跳 Xs"
  - stopped：显示"已停止于 YYYY-MM-DD HH:MM:SS"（stopped_at 回退 heartbeat_at）
- 历史实例折叠：默认仅显示每 worker_name 最新实例，可切换"显示全部"
- 审计数据保留（不 DELETE）

### 4.3 验证

- 源码级验证：_heartbeat_loop stopped_at + status=stopped PASS
- 源码级验证：mark_stale_worker_heartbeats stopped_at PASS
- TSC 编译 PASS
- **未验证**：运行时 stopped_at 写入（需启动 worker；受约束不启动）
- **未应用**：迁移 070 未应用到共享生产库（规则：不 migration 共享生产库）

## 5. Gate 5 GoAccess

### 5.1 后端

- `backend/app/api/admin_visitors.py`：`GET /admin/visitors`（admin only）
- `backend/app/schemas/visitors.py`：VisitorReport / VisitorSummary / VisitorMetricItem
- 数据来源：GoAccess 容器输出 JSON 报告（`GOACCESS_REPORT_PATH`）
- 安全：`_sanitize_path` 脱敏 token/jwt/password 等敏感 query 参数
- 本地开发：报告不存在时返回 `data_source="empty"` + 空态

### 5.2 前端

- `frontend/src/pages/AdminVisitorsPage.tsx`：今日/7日/30日切换 + KPI + 详情列表
- 完整空态/错误态/加载态 + 报告生成时间展示

### 5.3 生产部署设计（仅代码+runbook，本轮不部署）

- `docker-compose.prod.yml`：新增 goaccess 服务 + nginx_logs/goaccess_reports 卷
- `frontend/nginx.conf`：启用 access_log combined 格式
- `docs/runbooks/goaccess-deployment.md`：架构/安全/部署/验证步骤
- GoAccess：`--anonymize-ip` + `--keep-last=30` + 5 分钟周期生成 JSON

### 5.4 验证

- 源码级验证：_sanitize_path（token/jwt/password 脱敏）PASS
- 源码级验证：路由 /admin/visitors 注册 PASS
- TSC + ESLint PASS
- **未验证**：生产 GoAccess 容器实际运行（本轮不部署）

## 6. 未完成项与阻塞

### 6.1 Gate 2 真实验收（BLOCKED）

受本地环境限制，以下三项无法在本地完成：

1. **来源上下文真实验收**：需真实登录态（不自签 token），本地无测试账号
2. **权限邀请码真实页面/API 验收**：需写操作（创建邀请码/grant/revoke），共享生产库禁止写入
3. **结构图片 capture HTTP 链路验收**：需测试库造 5 类 SMC 事件，本地无 `_test` 数据库

已完成的替代验证：
- 来源上下文：V2 resolver 逻辑源码级验证 + API 契约（endpoints 存在 + auth 要求）
- 权限邀请码：上轮 42/42 测试通过（d2fe1be 未改权限代码）
- 结构图片：d2fe1be per-event capture 代码 + 测试通过

### 6.2 运行时验证（受约束不启动 scheduler/worker）

- Gate 1 持久化运行时
- Gate 3 盘后 15:05 真实触发
- Gate 4 stopped_at 运行时写入
- Gate 5 GoAccess 容器实际运行

## 7. 修改文件清单

### 新增文件
- `backend/app/services/volume_context.py`
- `backend/app/api/admin_visitors.py`
- `backend/app/schemas/visitors.py`
- `backend/alembic/versions/070_worker_heartbeat_stopped_at.py`
- `backend/tests/test_admin_visitors_gate5.py`
- `backend/tests/test_worker_heartbeat_gate4.py`
- `frontend/src/pages/AdminVisitorsPage.tsx`
- `docs/runbooks/goaccess-deployment.md`
- `docs/changes/2026/CHANGE-20260728-002-round-closure-gates-1-3-4-5.md`

### 修改文件
- `backend/app/main.py`（注册 admin_visitors_router）
- `backend/app/models/worker_heartbeat.py`（stopped_at 字段）
- `backend/app/schemas/worker_heartbeat.py`（stopped_at 字段）
- `backend/app/schemas/first_pyramid.py`（VolumeContextSchema + PyramidEvent.volumeContext）
- `backend/app/services/first_pyramid_service.py`（VolumeContext 集成）
- `backend/app/services/feature_snapshot_service.py`（first_pyramid 持久化）
- `backend/app/worker.py`（15:05 触发 + stopped_at 心跳退出）
- `backend/app/api/admin_subscription.py`（权限相关）
- `backend/tests/test_after_close_board_sync.py`（Gate3 降级测试）
- `docker-compose.prod.yml`（goaccess 服务）
- `frontend/nginx.conf`（access_log）
- `frontend/src/App.tsx`（AdminVisitorsPage 路由）
- `frontend/src/api/endpoints.ts`（VisitorReport + WorkerHeartbeatItem 类型）
- `frontend/src/features/stock-research/FirstPyramidPanel.tsx`（量能水位 UI）
- `frontend/src/hooks/useApi.ts`（useAdminVisitors hook）
- `frontend/src/navigation/appNavigation.ts`（访问统计菜单项）
- `frontend/src/pages/AdminJobsPage.tsx`（智能时间显示 + 历史折叠）
- `docs/changes/INDEX.md`（新增 002 条目）

## 8. 验证统计

| 验证项 | 结果 | 方法 |
|---|---|---|
| Gate 1 契约测试 | 38/38 PASS | pytest（上轮） |
| Gate 1 VolumeContext 自测 | PASS | python 模块自测 |
| Gate 3 源码级验证 | PASS | inspect.getsource |
| Gate 4 源码级验证 | PASS | inspect.getsource |
| Gate 5 源码级验证 | PASS | inspect.getsource + _sanitize_path |
| TSC | PASS | npx tsc --noEmit |
| ESLint | PASS（1 pre-existing warning） | npx eslint |
| Ruff | PASS（修复 3 错误后） | .venv/bin/ruff check |
| git diff --check | PASS | git diff --check |
| pytest 全量 | BLOCKED | conftest 要求 APP_ENV=test + 测试库 |
| 前端契约测试 | BLOCKED | vitest 未安装，不安装依赖 |
