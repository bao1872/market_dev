# CHANGE-20260730-014：P0 复盘数据链+行情缺口+盘后恢复+99字段筛选+第一金字塔折叠

状态：进行中（代码已合入 main SHA 54fe3a2；review-1.1.0 修复仅静态核验，canary review run 重跑待生产 SSH 可达；浏览器 UI 真实链路验收 PENDING 用户手工登录）
日期：2026-07-30
类型：behavior + contract + architecture + data
领域：复盘模块 / 行情质量 / 盘后编排 / 行情体验 / 量化模型
负责人：panji-dev

相关 PRD：

- `../../prd/70-review.md`：§23 P0 强化条款（review-1.1.0）
- `../../prd/30-after-close.md`：RV-AC-01~04 复盘编排
- `../../prd/40-market-stock-experience.md`：MX-50~MX-53 第一金字塔折叠与类型化筛选
- `../../prd/50-market-data-quality.md`：MQ-01~MQ-40 行情质量扫描与修复合同（新建）
- `../../prd/10-market-data.md`：MD-11 数据修复范围明确

相关 Maps：

- `../../maps/70-review.md`：§11 review-1.1.0 P0 数据链修复
- `../../maps/30-after-close.md`：§12 盘后任务中断恢复机制
- `../../maps/40-market-stock-experience.md`：MX-50~MX-53 实现（待核验）
- `../../maps/10-market-data.md`：行情质量章节（待核验）

相关 Rules：

- `../../../rules/00-core-governance.md`
- `../../../rules/40-testing-quality.md`
- `../../../rules/80-deployment-data-safety.md`

相关提交或 PR：

- 54fe3a2 P0 收口：复盘数据链修复（review-1.1.0）+ 行情缺口修复 + 盘后恢复 + 99字段类型化筛选 + 第一金字塔可折叠

替代：

- 无

被替代：

- 无

## 1. 摘要

本轮在 main SHA 9aea736（CHANGE-013 review-1.0.0 代码骨架）上完成 6 项 P0 修复并合入 main（54fe3a2）：（1）复盘数据链修复 review-1.1.0（history_maps 传递、industry_l1 scope_key 统一 board_id、major_index/style 范围补全、metric_engine history None→insufficient_history、发布门禁强化）；（2）全市场行情缺口修复（`--canary symbols/limit` 在查询前生效、`get_run_by_id`、repair 后 verification scan）；（3）盘后任务中断恢复（worker SIGTERM drain、deploy.sh drain_after_close_worker()、admin timeline API、顶层 run heartbeat 30s fenced UPDATE、item lease 14400s + fencing_epoch、watchdog 最多 3 次恢复）；（4）99 字段类型化筛选（FP_QUERY_FIELD_SPECS SSOT 扩展 data_type/operators/enum_values/input_control、422 结构化 detail、GET /api/v1/market/filter-specs）；（5）第一金字塔可折叠（firstPyramidAvailable/firstPyramidCollapsed 拆分、localStorage panji:first-pyramid-detail-collapsed:v1、StockResearchWorkspace 收起/展开按钮）；（6）前端类型化筛选器（根据字段 data_type 生成操作符下拉和值控件、enum 字段下拉选择不默认 contains）。同时修正 CHANGE-013 中"完整实现"表述为"代码骨架已部署但数据验收失败"。

## 2. 背景与问题

变化前（main SHA 9aea736）的关键问题：

1. **复盘 history 基线未接入**：`review_orchestrator_service.compute_run` 调用 `metric_engine` 时未传入 `history_maps`，分位计算使用空集合，导致 `historyPercentile120d` 永远为 null 或基于错误数据。
2. **industry_l1 scope_key 合同错误**：`scope_key` 混用 `industry_name`（如 `electronics`）与 `board_id`（UUID），导致归因 JOIN 失败、history_maps 错配、第二级下钻路径断裂。
3. **major_index/style 范围缺失**：canary run 只覆盖 market + 6 个 industry_l1，major_index 和 style 完全缺失，违反 PRD §6.1 第一级范围合同。
4. **metric_engine history None 静默吞掉**：历史基线为空（首次运行）时 `history` 参数为 `None`，metric_engine 直接访问 `history[...]` 抛 `AttributeError`，被上层 `try/except` 静默吞掉，返回 `status=None`。
5. **force 发布不可用数据**：`publish_review(force=True)` 跳过所有门禁，market P/Q/U/C/V value 为 null 时仍写入 `factor_publications`；canary run（3e1db415）使用 force=True 发布，普通用户读取入口指向不可用数据。
6. **行情缺口 CLI 语义错误**：`--canary` 在创建 run 后才应用 symbols/limit，导致 canary run 实际扫描了全市场；`--resume` 不要求显式 `--run-id`，可能误 resume 任意历史 run；repair 后无 verification scan，无法验证修复效果。
7. **盘后任务中断不可恢复**：worker 收到 SIGTERM 后直接退出，未 drain 当前 item；deploy.sh 重启 worker 容器前未检查活跃 run，可能中断正在执行的盘后任务；无 admin timeline API 诊断 run 执行时间线。
8. **99 字段筛选无类型化合同**：前端任意字段都使用 `contains` operator，enum 字段（如趋势方向）无法下拉选择；后端 422 错误无结构化 detail，前端只能显示"行情列表加载失败"。
9. **第一金字塔不可折叠**：detail 区域占用大量纵向空间，用户无法收起查看其他内容；折叠状态与资格混用单一布尔值，unavailable 时仍显示折叠按钮。

## 3. 变化前

- `metric_engine.compute_metrics` 不接收 `history_maps` 参数；`review_orchestrator_service.compute_run` 不构造历史基线。
- `industry_l1` 的 `scope_key` 由 `industry_name` 填充；`board_id` 未使用。
- `review_scope_service.list_scope_snapshots` 只返回 market + 6 个 industry_l1。
- `metric_engine` 中 `history is None` 时抛 `AttributeError`，被 `try/except` 吞掉。
- `publish_review(force=True)` 跳过所有门禁并写入 `factor_publications`。
- `market_data_quality_cli.py` 的 `--canary` 在 run 创建后应用；`--resume` 不要求 `--run-id`；无 `--verify` mode。
- worker 无 SIGTERM 信号处理器；deploy.sh 无 `drain_after_close_worker()`；无 admin timeline API。
- 前端筛选器所有字段使用 `contains`；后端 422 无结构化 detail；无 `GET /api/v1/market/filter-specs`。
- 第一金字塔 detail 区域不可折叠；无 `firstPyramidAvailable` / `firstPyramidCollapsed` 拆分。

## 4. 变化内容

### 4.1 复盘数据链修复（review-1.1.0）

- **history_maps 传递**：`review_orchestrator_service.compute_run` 在调用 metric_engine 前显式构造 `history_maps`（按 `scope_type + scope_key` 从 `market_review_scope_snapshots` 读取），并传入 `compute_metrics`。
- **industry_l1 scope_key 统一 board_id**：所有第一级 scope 的 `scope_key` 统一为 `board_id`（industry_l1）/ `index_code`（major_index）/ `style_code`（style）/ `"market"`（market）。
- **major_index/style 范围补全**：第一级范围合同强制覆盖 market + major_index（≥2）+ style（≥2）+ industry_l1（≥25）。
- **metric_engine history None → insufficient_history**：`metric_engine` 显式判空，`history is None` 或 `len(history) < 60` 时返回 `status=insufficient_history`，`value/normalizedValue/historyPercentile120d/delta1d/delta5d` 全部为 `null`。
- **发布门禁强化**：`publish_review(force=False)` 新增 6 项门禁（market P/Q/U/C/V value 非空、source_board_run_id 一致、source_core_run_id 一致、无 failed signals、无 failed run_items、coverage_ratio >= 0.95）；`force=True` 不得写入 `factor_publications`，返回 `is_provisional=true`。
- **算法版本升级**：`algorithm_version` 从 `review-1.0.0` 升级到 `review-1.1.0`；`filter_version` 保持 `filters-1.0.0`。

### 4.2 全市场行情缺口修复

- **`--canary` 查询前应用**：`market_data_quality_cli.py` 在数据库查询、API 调用、run/items 创建之前完成 symbols 列表解析；`--canary` 默认选取 5 只代表性股票；`--symbols` 优先级最高，`--limit` 次之，`--canary` 最低。
- **`--resume` 显式 `--run-id`**：`--resume` 必须搭配 `--run-id`；run 必须存在；mode 必须一致；状态必须为 running/partial_failed/interrupted；只处理 pending/failed/lease 过期 running items。
- **`--verify` 新 mode**：repair 后必须执行 verification scan，创建新 run（mode=verification），禁止复用 scan 或 repair run；与原 repair run 通过 `parent_run_id` 关联。
- **`get_run_by_id`**：admin API 新增 `GET /api/v1/admin/market-data-quality/runs/{run_id}`，返回 run 详情和 items 摘要。

### 4.3 盘后任务中断恢复

- **顶层 run heartbeat（30s fenced UPDATE）**：`SchedulerJobRun` / `market_review_runs` 在 running 状态下，worker 每 30 秒执行 fenced UPDATE 刷新 `heartbeat_at`；`lease_epoch` fencing 防止旧 worker 写入；超时阈值 180 秒。
- **item lease（14400s）+ fencing_epoch**：每个 item 持有独立 lease（4 小时），`lease_epoch` 在 claim 时递增，`mark_item_*` 必须携带 `lease_epoch`。
- **watchdog 恢复同一 run（最多 3 次）**：`auto_resume_interrupted_after_close_runs` 扫描 interrupted run，attempt_no < 3 时切换为 resume_queued；>= 3 时标记 failed。
- **SIGTERM drain**：worker 收到 SIGTERM 后停止领取新 item，完成当前 item 后退出；run 状态保持 running，由 watchdog 切换为 interrupted → resume_queued。
- **deploy.sh drain_after_close_worker()**：部署脚本重启 worker 前检查活跃 run，有活跃 run 时发送 SIGTERM 等待完成（最长 30 分钟），超时拒绝重启。
- **admin timeline API**：`GET /api/v1/admin/review/runs/{run_id}/timeline` 返回 run 执行时间线事件列表（run_created / scope_item_claimed / heartbeat_timeout / watchdog_resumed / sigterm_drain_started 等）。

### 4.4 99 字段类型化筛选

- **FP_QUERY_FIELD_SPECS SSOT 扩展**：`backend/app/config/fp_query_field_specs.py` 为每个字段声明 `data_type` / `operators` / `enum_values` / `input_control`；data_type 支持 text / enum / boolean / number / percent / datetime / multi_enum 七种。
- **422 结构化 detail**：后端 `/market/stocks` 接收 `(field, operator, value)` 三元组，按 `data_type` 校验 operator 合法性，非法组合返回 422 + 结构化 `detail`（含 `field` / `operator` / `reason` / `allowed_operators`）。
- **GET /api/v1/market/filter-specs**：新增字段元数据 API，直接序列化 `FP_QUERY_FIELD_SPECS`；版本化（`fp-query-specs-v1`）；任何登录用户可读；Redis 缓存 TTL 3600 秒。

### 4.5 第一金字塔可折叠

- **firstPyramidAvailable / firstPyramidCollapsed 拆分**：`firstPyramidAvailable`（资格，只读，后端返回）+ `firstPyramidCollapsed`（偏好，用户可点击）；`false` 时显示结构化 unavailable 状态，折叠按钮不可用。
- **localStorage 持久键**：`panji:first-pyramid-detail-collapsed:v1`；默认展开；与 symbol 解耦（全局偏好）；版本号升级时不就地覆盖。
- **StockResearchWorkspace 收起/展开按钮**：按钮位于 detail 区域顶部右侧，`aria-expanded` 反映状态；点击不触发数据请求；折叠时保留顶部 4 个 chip 摘要；capture 模式强制展开。

### 4.6 前端类型化筛选器

- **操作符下拉**：根据字段 `data_type` 渲染允许的 operators；enum 字段默认 `eq`（禁止默认 `contains`）。
- **值控件**：`text` → 文本输入框；`enum` → 下拉单选（`in` / `not_in` 切换多选）；`boolean` → 三态开关；`number` → 数字输入框（`between` 显示两个）；`percent` → 数字输入框 0—100；`datetime` → 日期选择器；`multi_enum` → 多选下拉。
- **旧 URL 迁移**：`enum + contains` 若值精确匹配枚举可迁移为 `eq`（静默），否则提示；`number + contains` 直接丢弃并提示；缺失 operator 按 `default_operator` 补齐。

## 5. 变化后

- review-1.1.0 代码已合入 main (54fe3a2)，但 canary review run 重跑与生产数据验收未完成（服务器 SSH 不可达）。
- `market_data_quality_cli.py` 的 `--canary` / `--resume` / `--verify` 合同符合 PRD MQ-01~MQ-40。
- worker SIGTERM drain、deploy.sh drain_after_close_worker()、admin timeline API 已落代码。
- 99 字段类型化筛选 SSOT 与 API 已落代码；前端筛选器组件按 data_type 渲染。
- 第一金字塔 detail 可折叠；`firstPyramidAvailable` / `firstPyramidCollapsed` 拆分。
- CHANGE-013 中"完整实现"表述已修正为"代码骨架已部署但数据验收失败"。

当前实现状态以相关 Maps 为准（`maps/70-review.md` §11、`maps/30-after-close.md` §12、`maps/40-market-stock-experience.md` 待核验）。

## 6. 影响范围

### 用户行为

- 复盘页 review-1.1.0 修复后首次 run 不会伪造分位（status=insufficient_history），但需要 ≥60 个交易日持续运行才能产生有效 P/Q/U/C/V value。
- 行情质量扫描 canary 5 只股票可正确执行；`--resume` 不会误操作历史 run。
- 盘后任务中断后可自动恢复（最多 3 次）；部署不会中断活跃 run。
- `/market` 列表 99 字段筛选按类型化合同执行；enum 字段下拉选择不默认 contains。
- 个股详情第一金字塔可折叠；折叠状态持久化。

### API 或契约

- `algorithm_version`：`review-1.0.0` → `review-1.1.0`。
- `publish_review(force=True)` 不再写入 `factor_publications`，返回 `is_provisional=true`。
- 新增 `GET /api/v1/market/filter-specs`（字段元数据 API）。
- 新增 `GET /api/v1/admin/review/runs/{run_id}/timeline`（admin timeline API）。
- 新增 `GET /api/v1/admin/market-data-quality/runs/{run_id}`（行情质量 run 详情）。
- `/market/stocks` 422 错误返回结构化 `detail`。
- `market_data_quality_cli.py` 新增 `--verify` mode；`--resume` 必须搭配 `--run-id`。

### 数据

- 无 schema 变化（migration 075/076 已在 CHANGE-012/013 应用）。
- review-1.0.0 canary run（3e1db415）保留为审计记录，不修改历史数据。
- review-1.1.0 必须通过新 run 切换 `factor_publications` pointer。

### 前端

- 99 字段筛选器组件按 data_type 渲染操作符下拉和值控件。
- 第一金字塔 detail 区域新增收起/展开按钮。
- `StockResearchWorkspace` 接入 `firstPyramidAvailable` / `firstPyramidCollapsed`。
- 旧 URL 筛选迁移在前端 hydration 阶段执行。

### 后端

- `review_orchestrator_service.py`：构造 history_maps 并传入 metric_engine。
- `review_scope_service.py`：scope_key 统一 board_id；major_index/style 范围补全。
- `metric_engine.py`：history None → insufficient_history。
- `review_publication_service.py`：6 项发布门禁强化。
- `market_data_quality_cli.py`：--canary 查询前应用；--resume 显式 --run-id；--verify mode。
- `worker.py`：SIGTERM 信号处理器 + drain 标志。
- `after_close_orchestrator_service.py` / `review_orchestrator_service.py`：drain 标志检查点。
- `fp_query_field_specs.py`：SSOT 扩展 data_type/operators/enum_values/input_control。
- `market.py` API：422 结构化 detail；新增 `/market/filter-specs`。
- `admin_review.py`：新增 `/runs/{run_id}/timeline`。
- `deploy.sh`：新增 `drain_after_close_worker()`。

### Worker 与任务

- worker SIGTERM drain 流程嵌入主循环。
- watchdog 恢复 run 时递增 lease_epoch 和 attempt_no。
- review run 与 after_close run 的 watchdog 恢复逻辑独立（已知缺口，见 §11）。

### 部署与运行

- `deploy.sh drain_after_close_worker()` 在重启 worker 前检查活跃 run。
- 待生产 SSH 可达后执行 review-1.1.0 canary review run 重跑。
- 待生产 SSH 可达后执行行情质量 canary 5 只股票流程（见 `runbooks/market-data-quality-scan-repair.md`）。

## 7. 迁移与兼容

- **无新 migration**：本轮不修改 schema，migration 075/076 已在 CHANGE-012/013 应用。
- **algorithm_version 升级**：review-1.0.0 → review-1.1.0；旧 review-1.0.0 run 保留可查询，新 run 必须使用 review-1.1.0。
- **factor_publications 兼容**：review-1.0.0 canary run 已写入 `factor_publications`（publication_id=c01afda0），保留为审计记录；review-1.1.0 修复后通过新 run 切换 pointer，旧 pointer 自然被覆盖（`on_conflict_do_update`）。
- **force=True 路径**：review-1.1.0 后 `force=True` 不再写入 `factor_publications`；旧 force=True 发布的 run 保留为审计记录，不删除。
- **前端类型化筛选兼容**：旧 URL 中 `enum + contains` 按迁移规则处理（PRD MX-53）；后端 422 结构化 detail 前端按 `detail` 提示。
- **第一金字塔折叠兼容**：旧 localStorage 无 `panji:first-pyramid-detail-collapsed:v1` 键时默认展开。

## 8. 验证与证据

| 验证项 | 范围 | 结果 | 证据 |
|---|---|---|---|
| review-1.1.0 代码静态核验 | history_maps 传递、scope_key 统一、metric_engine history None、发布门禁 6 项 | PASS | main SHA 54fe3a2 源码核验 |
| review-1.1.0 canary review run 重跑 | trade_date=2026-07-29 或最新交易日 | 未验证 | 服务器 SSH 不可达，待 admin 手工触发 |
| 行情质量 CLI 合同 | --canary / --resume / --verify | PASS（静态） | `market_data_quality_cli.py` 源码核验 |
| 行情质量 canary 5 只股票流程 | dry-run / scan / repair / verify | 未验证 | 服务器 SSH 不可达，待 admin 手工执行 |
| 盘后任务中断恢复 | SIGTERM drain / watchdog / admin timeline API | PASS（静态） | `worker.py` / `deploy.sh` / `admin_review.py` 源码核验 |
| 99 字段类型化筛选 SSOT | FP_QUERY_FIELD_SPECS data_type/operators/enum_values/input_control | PASS（静态） | `fp_query_field_specs.py` 源码核验 |
| GET /api/v1/market/filter-specs | 字段元数据 API 响应 | 未验证 | 待生产部署后 curl 核验 |
| 第一金字塔可折叠 | firstPyramidAvailable / firstPyramidCollapsed / localStorage | PASS（静态） | `StockResearchWorkspace.tsx` 源码核验 |
| 前端类型化筛选器 | 操作符下拉 / 值控件 / enum 不默认 contains | PASS（静态） | 前端筛选器组件源码核验 |
| 旧 URL 筛选迁移 | enum+contains 迁移规则 | PASS（静态） | 前端 hydration 代码源码核验 |
| 浏览器 UI 真实链路验收 | /market /stock /review | 未验证 | 受 Owner 账户保护规则约束，TRAE 不得自动登录；PENDING 用户手工验收 |

## 9. 文档更新

| 文档 | 更新内容 |
|---|---|
| PRD 70-review | 新增 §23 P0 强化条款（review-1.1.0）：23.1 历史原始组件 bootstrap、23.2 至少 60 日才允许生成 P/Q/U/C/V、23.3 canary 不得切正式 pointer、23.4 完整第一级范围合同、23.5 禁止 force 发布不可用数据、23.6 history_maps 读取合同 |
| PRD 40-market-stock-experience | 新增 §6 MX-50 第一金字塔折叠交互、MX-51 类型化筛选操作符合同、MX-52 字段元数据 API、MX-53 旧 URL 筛选迁移规则 |
| PRD 50-market-data-quality | 新建：MQ-01~MQ-40 行情质量扫描与修复合同（四阶段、--resume、--canary、数据模型、CLI） |
| Maps 70-review | 新增 §11 review-1.1.0 P0 数据链修复（11.1 算法版本升级、11.2 已修复项、11.3 当前限制、11.4 上一轮 canary run 审计保留、11.5 下一轮核验清单） |
| Maps 30-after-close | 新增 §12 盘后任务中断恢复机制（12.1 heartbeat、12.2 item lease、12.3 watchdog、12.4 SIGTERM drain、12.5 deploy.sh drain、12.6 admin timeline API、12.7 已知缺口） |
| Runbooks market-data-quality-scan-repair | 新建：scan/repair/verification 标准流程、canary 5 只股票操作步骤、故障排查 |
| Changes INDEX | 新增 CHANGE-20260730-014 索引行 |
| Changes 013 | 修正"完整实现"表述为"代码骨架已部署但数据验收失败"（状态行、§1 摘要、§4.3 标题、§5 末尾、§12 后续变化） |

## 10. 回滚方案

- **代码回滚**：`git revert 54fe3a2`（不影响已发布数据；review-1.0.0 canary run 保留）。
- **algorithm_version 回滚**：将 `algorithm_version` 改回 `review-1.0.0`；旧 review-1.0.0 run 保留可查询。
- **factor_publications 回滚**：review-1.1.0 修复后若新 run 失败，可手动删除新 pointer，旧 review-1.0.0 pointer 自动恢复（`on_conflict_do_update` 反向操作需手工 SQL）。
- **force=True 路径回滚**：恢复 `publish_review(force=True)` 写入 `factor_publications` 的旧逻辑（不推荐，违反 PRD §23.3）。
- **CLI 合同回滚**：恢复 `--canary` 在 run 创建后应用、`--resume` 不要求 `--run-id`（不推荐，违反 PRD MQ-10/MQ-20）。
- **前端类型化筛选回滚**：恢复所有字段使用 `contains`（不推荐，违反 PRD MX-51）。
- **第一金字塔折叠回滚**：删除 `firstPyramidCollapsed` 状态，detail 区域强制展开（不影响数据）。

## 11. 遗留问题与风险

1. **服务器 SSH 不可达**：review-1.1.0 canary review run 重跑、行情质量 canary 5 只股票流程、盘后任务中断恢复的生产核验均未完成，待 SSH 可达后由 admin 手工执行。
2. **review-1.1.0 修复后首次 run 无有效 P/Q/U/C/V value**：history_maps 从 `market_review_scope_snapshots` 读取，首次运行无历史数据时所有 component `status=insufficient_history`；需要持续运行 ≥60 个交易日才能产生有效 value（PRD §23.2）。
3. **docker `stop_grace_period` 未配置**：`docker-compose.yml` 中 worker 容器未设置 `stop_grace_period`，默认 10 秒后 SIGKILL；SIGTERM drain 流程在 10 秒内无法完成长 item（见 `maps/30-after-close.md` §12.7）。
4. **watchdog 不调用 `recover_stale_running_items`**：恢复 interrupted run 时只重置 run 级状态，不重置 stale running items；卡在 running 的 item 需要 lease 过期后才能被抢占（见 `maps/30-after-close.md` §12.7）。
5. **review run 与 after_close run 的 watchdog 共用同一恢复服务**：当前 `auto_resume_interrupted_after_close_runs` 只扫描 `job_name=after_close_orchestrator`；review run 的 watchdog 恢复由独立实现，未来可能需要统一。
6. **浏览器 UI 真实链路验收 PENDING**：受 Owner 账户保护规则约束，TRAE 不得自动登录；用户将手工验收 /market 类型化筛选、/stock 第一金字塔折叠、/review review-1.1.0 修复效果。
7. **旧 review-1.0.0 canary run 保留**：`factor_publications` 中 `publication_id=c01afda0` 指向 review-1.0.0 run（3e1db415），review-1.1.0 修复后必须通过新 run 切换 pointer，旧 pointer 不删除（审计保留）。

## 12. 后续变化

- 待生产 SSH 可达后执行 review-1.1.0 canary review run 重跑，核验 §11.2 全部修复项（见 `maps/70-review.md` §11.5 下一轮核验清单）。
- 待生产 SSH 可达后执行行情质量 canary 5 只股票流程（见 `runbooks/market-data-quality-scan-repair.md`）。
- 待生产 SSH 可达后核验盘后任务中断恢复机制（SIGTERM drain、watchdog、admin timeline API）。
- review-1.1.0 修复后持续运行 ≥60 个交易日，产生有效 P/Q/U/C/V value 后再评估筛选器阈值校准（PRD §22 Phase 5）。
- docker `stop_grace_period` 配置和 watchdog 调用 `recover_stale_running_items` 作为后续 P1 改进。
