# CHANGE-20260729-005：99字段真实筛选排序修复 + GoAccess logrotate/healthcheck + 部署脚本补 goaccess

状态：进行中（代码+目标纯单元测试61+Ruff+TSC+ESLint 通过；CI 待查询；浏览器验收待用户手工）
日期：2026-07-29
类型：bugfix+architecture
领域：行情体验/量化模型/运维

负责人：TRAE CN (Local Native)

相关 PRD：
- `../../prd/40-market-stock-experience.md`：MX-20（列表视图 99 字段服务端筛选排序）
- `../../prd/20-quant-model.md`：QM-01～QM-43（第一金字塔结构化状态）

相关 Maps：
- `../../maps/40-market-stock-experience.md`
- `../../maps/20-quant-model.md`
- `../../maps/80-system-runtime.md`

相关提交或 PR：
- 待 push 后回填

## 1. 背景

CHANGE-20260729-004 声称"99 字段全部可筛选排序"，实际存在 5 个正确性缺陷：
1. 大量字段无 `json_path`，服务端返回 422；
2. DSA 量比字段读取废弃的 sum/sum 口径；
3. 布尔字段 text 与 bool 比较类型错误；
4. 筹码字段从 review-core 的 `chipConsensus` 读取（解耦后应为空）；
5. 多筛选各自创建相关子查询，查询过重。

同时 GoAccess 生产运行态未启动，且部署脚本从未包含 goaccess 服务。

## 2. 修改内容

### 2.1 99字段数据源矩阵

| 数据源类型 | source | 读取位置 | 字段数 |
|---|---|---|---|
| 扁平对象（含事件） | flat | `summary_payload.first_pyramid_flat.<fp_key>` | 86（含结构事件21+动量事件9=30） |
| 独立筹码表 | chip | `stock_chip_consensus_snapshots.chip_payload.chip_flat.<fp_key>` | 10 |
| 真实列 | column | `StockFeatureSnapshot.created_at` / `source_run_id` | 2 |
| 常量 | literal | 固定 `"feature_snapshot"` | 1 |
| 合计 | — | — | 99 |

全部 99 字段均有 queryable source，`FP_SERVER_FILTERABLE_KEYS == FP_SERVER_SORTABLE_KEYS == 99`。

### 2.2 DSA 字段修复

- `fp_segment_volume_ratio` → `current_vs_prev_volume_mean_ratio`（非废弃 sum/sum）
- `fp_segment_amount_ratio` → `current_vs_prev_amount_mean_ratio`
- `fp_trend_strength` 优先 `regime_strength`，`trend_strength` 仅 fallback
- `fp_prev_segment_volume`/`fp_prev_segment_amount` 使用 mean 字段，sum 仅兼容旧快照

### 2.3 布尔字段 cast

`_cast_fp_value(data_type="boolean")` → `cast(expr, Boolean)`，禁止 text 与 bool 比较。

### 2.4 筹码字段独立表关联

- 筹码字段（10 个）`source=chip`，从 `stock_chip_consensus_snapshots.chip_payload.chip_flat` 读取
- `after_close_chip_consensus_service` 写入 `chip_flat` 扁平对象
- 查询使用 LATERAL JOIN 关联最新 succeeded chip（匹配 instrument_id + status=succeeded）

### 2.5 LATERAL JOIN 查询优化

- `_build_snap_lateral()` + `_build_chip_lateral()` 按需构建
- 最新 snapshot + chip 单次 LATERAL JOIN，避免多筛选各自重复子查询
- 排序 NULLS LAST，`Instrument.symbol` 为第二排序键

### 2.6 解析函数纯化

`FpFilterSpec`/`FpSortSpec`/`parse_fp_filter`/`parse_fp_sort` 移入 `first_pyramid_flatten.py`（无 DB 依赖），`market_stocks_service.py` 保留 thin wrapper 兼容 API 层调用。

### 2.7 GoAccess logrotate + healthcheck

- 新增 `frontend/logrotate-nginx.conf`：daily + maxsize 50M + rotate 7 + compress + copytruncate
- 修改 `frontend/Dockerfile`：安装 logrotate + crond + 自定义 entrypoint
- `docker-compose.prod.yml` 新增 goaccess healthcheck（access.log 存在 + report.json 非空 + JSON 起始字符 + 10 分钟内更新）
- 修复误导性注释（json-file 不轮转命名卷内 access.log）

### 2.8 部署脚本补 goaccess

- `scripts/deploy_live_runtime.sh`：force-recreate 列表补 `goaccess`
- `scripts/deploy.sh` CORE_ONLY 模式：force-recreate 列表补 `goaccess`

## 3. 修改文件

| 文件 | 修改 |
|---|---|
| `backend/app/services/first_pyramid_flatten.py` | source 类型系统 + DSA 字段修复 + flatten_chip_fields + 解析函数纯化 |
| `backend/app/services/feature_snapshot_service.py` | 写入 `first_pyramid_flat` |
| `backend/app/services/market_stocks_service.py` | LATERAL JOIN + flat/chip/column 读取 + boolean cast + 委托纯解析 |
| `backend/app/services/after_close_chip_consensus_service.py` | 写入 `chip_flat` |
| `backend/tests/test_first_pyramid_flatten.py` | 补 flatten_chip_fields 测试 + source 类型测试（61 passed） |
| `frontend/Dockerfile` | 安装 logrotate + crond + entrypoint |
| `frontend/logrotate-nginx.conf` | 新增日志轮转配置 |
| `frontend/docker-entrypoint.sh` | 新增 crond + nginx 入口 |
| `docker-compose.prod.yml` | goaccess healthcheck + 修复注释 |
| `scripts/deploy_live_runtime.sh` | 补 goaccess 到部署范围 |
| `scripts/deploy.sh` | CORE_ONLY 补 goaccess |

## 4. 验证

- 纯单元测试：`test_first_pyramid_flatten.py` 61 passed（DSA 字段/布尔/事件/chip/解析）
- Ruff：全部通过
- TSC：通过
- ESLint：0 errors（2 pre-existing warnings in MiniKlineCard.tsx）
- Contract tests：477/479 passed（2 pre-existing failures in CaptureStockPage computeTypeSpecificReady）
- 深科技：`test_chip_status.py` 验证 `M15_BARS_INSUFFICIENT` 原因码正确

## 5. GoAccess 服务器只读诊断结论

| 检查项 | 结果 |
|---|---|
| 服务器 HEAD | `37c9fa3` on `main`（旧于 dev goaccess 改动） |
| trading-goaccess 容器 | **不存在** |
| trading-frontend mounts | 无 `nginx_logs` 卷（Live Mount bind mount） |
| trading-backend mounts | 无 `goaccess_reports` 卷 |
| frontend access.log | `-> /dev/stdout` 符号链接（默认 nginx，非文件） |

**根因**：
1. 部署脚本从未包含 goaccess（已在 deploy_live_runtime.sh / deploy.sh 修复）
2. 服务器 compose 文件来自旧 main，可能未定义 goaccess 服务
3. frontend 使用 Live Mount bind mount，nginx_logs 命名卷未挂载
4. frontend nginx 写 stdout 而非文件，goaccess 无法读取 access.log
