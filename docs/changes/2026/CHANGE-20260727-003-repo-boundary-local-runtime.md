# CHANGE-20260727-003：仓库边界治理、本地完整原生运行与趋势入口锁定

状态：已完成（Phase 5B-0：ref/sync 退出 Git、CI 防误推、本地完整路由验证、趋势入口审计；未修改 DSA/SMC/Bollinger/Node Cluster 算法，未部署生产，未创建/合并 main PR）
日期：2026-07-27
对应 PRD：`docs/prd/80-system-runtime.md`（新增 SR-15）
对应 Map：`docs/maps/00-system-overview.md`、`docs/maps/20-quant-model.md`、`docs/maps/40-market-stock-experience.md`、`docs/maps/50-watchlist-intraday.md`、`docs/maps/60-permissions-admin.md`、`docs/maps/80-system-runtime.md`、`docs/maps/technical/codebase-modules.md`
对应 Runbook：`docs/runbooks/local-development.md`
对应 Rules：`rules/90-deprecated-forbidden.md`（新增 ref/sync git 跟踪禁止条款）

## 1. 变更原因

- **ref/sync 仓库边界**：`ref/smc_user_source.pine` 与 `sync/README.md` 在 `dev`/`experiment` 分支仍被 Git 跟踪，与"ref/ 仅本地参考、sync/ 已废弃"的定位冲突；`origin/main` 仍含 `ref/smc_user_source.pine`，存在被新开发者误认为正式实现入口的风险。
- **永久防误推机制缺失**：CI 仅依赖测试守护 ref/ 隔离，缺少显式 `git ls-files ref sync` 检查；测试断言曾反向要求 `ref/smc_user_source.pine` 必须被跟踪，与 Phase 5B-0 目标冲突。
- **本地完整运行验证缺口**：Phase 5A 仅验证 Backend/Frontend 健康端点，未覆盖前端所有实际路由的加载、API 响应、数据展示和权限正确性。
- **趋势入口未锁定**：Maps20 §3 "平均成交量"行错误描述 `current_segment_volume_sum` 为 `compute_dsa_history` 直接输出（实际在 `structural_factor_service.py:873-945` 派生）；趋势段方向、长度、涨跌幅、平均成交量的权威实现位置与调用路径未在 Maps 中明确锁定。

## 2. ref/sync 仓库清理

### 2.1 清理前后状态

| 分支 | 清理前 ref/ 文件数 | 清理前 sync/ 文件数 | 清理后 ref/ 文件数 | 清理后 sync/ 文件数 |
|---|---|---|---|---|
| `dev`（本地） | 1（`ref/smc_user_source.pine`） | 1（`sync/README.md`） | 0 | 0 |
| `origin/dev` | 1 | 1 | 0 | 0 |
| `experiment`（本地） | 1 | 1 | 0 | 0 |
| `origin/experiment` | 1 | 1 | 0 | 0 |
| `main`（本地） | 1 | 0 | 1（未修改 main） | 0 |
| `origin/main` | 1 | 0 | 1（待 PR 合并清理） | 0 |

### 2.2 清理操作

- `git rm --cached ref/smc_user_source.pine`：停止跟踪，保留本地实体（按 PRD80 SR-15 与硬约束要求）；
- `git rm sync/README.md`：从 Git 与本地同时删除（sync/ 已废弃）；
- `.gitignore` 加入 `/ref/` 与 `/sync/` 根锚定规则（旧 `ref/` 改为 `/ref/`，新增 `/sync/`）；
- 文档清理：`backend/app/strategy_assets/algorithms/features/smc_pine_core.py` docstring、`backend/tests/test_smc_pine_deterministic.py`、`backend/tests/test_smc_tv_parity.py` 注释中把 `ref/smc_user_source.pine` 描述从"git 跟踪"改为"已退出 git 跟踪，仅本地参考"。

### 2.3 当前树删除与历史未重写的明确区别

- **当前树删除**：`git rm --cached` 仅从当前 HEAD 树中移除文件，保留本地实体；`.gitignore` 阻止未来重新跟踪；提交新增 `c730876`，不修改历史。
- **历史未重写**：`ref/smc_user_source.pine` 的历史提交仍存在于 Git 历史；`archive/*` 注解标签中保留旧版跟踪记录作为只读历史；未执行 `git filter-branch`、`git rebase -i`、`git push --force` 或任何历史重写操作。

### 2.4 提交与 push

- `dev` 提交 `c730876` "ref/sync 退出 git 跟踪 + CI 防误推 + 术语修正"，已 fast-forward push `origin/dev`；
- `experiment` cherry-pick `c730876` 为 `38df3af`，已 push `origin/experiment`；
- `main` 未直接修改；origin/main 仍含 `ref/smc_user_source.pine`，等待 dev → main PR 合并清理（本轮不创建/合并 PR）。

## 3. CI 防误推机制

### 3.1 双重防护

| 防护层 | 位置 | 检查内容 |
|---|---|---|
| CI 显式检查 | `.github/workflows/ci.yml` governance-rules job | `git ls-files ref sync` 输出必须为空，否则 CI 失败 |
| 架构守护测试 | `backend/tests/test_ref_isolation.py` | `test_no_ref_tracked_in_git`：`git ls-files ref/` 必须为空；`test_no_sync_tracked_in_git`：`git ls-files sync/` 必须为空 |

### 3.2 测试改写

旧版 `test_ref_isolation.py` 断言 `ref/smc_user_source.pine` 必须被跟踪（与 Phase 5B-0 冲突）。改写为：

- `test_no_ref_tracked_in_git`：守护 `git ls-files ref/` 为空；
- `test_no_sync_tracked_in_git`：守护 `git ls-files sync/` 为空；
- 注释说明 Phase 5B-0 已 `git rm --cached ref/smc_user_source.pine`，`.gitignore /ref/` 自动忽略新增文件。

## 4. 本地完整原生运行验证

### 4.1 进程状态

| 服务 | PID | RSS | 端口 | 停止命令 |
|---|---|---|---|---|
| SSH 隧道 | 44020 | 288KB | 15432→5432, 16379→6379 | `make tunnel-stop` |
| Backend | 44870 | 8896KB | 8000 | Ctrl+C in Backend terminal |
| Frontend | 45164 | 19520KB | 8008 | Ctrl+C in Frontend terminal |
| Scheduler | - | - | - | 0 实例（无运行） |
| Worker | - | - | - | 0 实例（无运行） |

### 4.2 数据库与 Redis 只读核验

- PostgreSQL：`bz_stock`，PG 16.14，SELECT 1 / current_database / version 成功；仅执行只读查询；
- Redis DB15：PING=True，DBSIZE=0；仅执行 PING/DBSIZE，不写 Key。

### 4.3 路由验证表

完整路由验证表见 `docs/maps/40-market-stock-experience.md` §8（用户级路由）、`docs/maps/60-permissions-admin.md` §8（管理员路由）。覆盖：

- 公开路由：`/`（已知本地 Vite 限制，无限刷新）、`/login`、`/subscription-expired`、`/membership-expired`、`/capture/stock/:symbol`；
- 用户级路由：`/market`、`/replay`、`/stock/000001`、`/settings`、`/messages`；
- 管理员路由：`/admin`、`/admin/users`、`/admin/beta-applications`、`/admin/after-close/pipeline`、`/admin/jobs`、`/admin/strategies`（POST only）、`/admin/stocks/000001/debug`、`/admin/audit-logs`、`/admin/members`、`/admin/message-deliveries`；
- 重定向路由：`/overview`、`/watchlist`、`/screener`、`/admin/strategies`、`/admin/stock-debug/:symbol`、通配符 `*`。

### 4.4 已知限制

- 本地 Vite 开发服务器无 Nginx 前置，访问 `/` 时 `LandingPage` 组件 `window.location.replace('/')` 触发无限刷新；可通过直接访问 `/login` 或 `/market` 绕过；生产环境由 Nginx 精确分流，不受影响。

## 5. 第一金字塔趋势入口锁定

### 5.1 权威实现

| 项目 | 位置 | 状态 |
|---|---|---|
| SSOT 唯一指标实现 | `dsa_selector.py:compute_dsa_history`（line 253-481） | 已核验 |
| 统一调用入口 | `dsa_selector.py:compute_dsa_bundle`（line 533-673，封装 SSOT + 图表字段） | 已核验 |
| 底层算法 | `dynamic_swing_anchored_vwap.py:dynamic_swing_anchored_vwap`（Pine v6 逐行对齐） | 已核验 |
| 趋势段方向 | `regime_value`（1/-1/0，line 437）+ `dsa_dir`（1/-1，line 617-618） | 已核验 |
| 趋势段长度 | `dsa_dir_bars`（line 439，count × dir_vals，按 group_id 累计） | 已核验 |
| 涨跌幅 | `change_pct`（line 451，`close.pct_change() * 100`） | 已核验 |
| 平均成交量（直接输出） | `avg_amount_20d`（line 453，`amount.rolling(20).mean()`）+ `vol_zscore`（line 376-377） | 已核验 |
| 段内成交量（派生，非 SSOT） | `current_segment_volume_sum` / `current_vs_prev_volume_ratio` 在 `structural_factor_service.py:873-945` 派生 | 已核验（文档修正） |

### 5.2 调用路径

| 调用方 | 路径 | 入口 |
|---|---|---|
| 单股实时（结构面板/详情） | `structural_factor_service.py:1633/1789` | `compute_dsa_bundle` |
| 单股实时（时序因子） | `temporal_feature_service.py:211` | `compute_dsa_bundle` |
| 批量（/market/stocks 路由） | `canonical_adapters.py:405` | `compute_dsa_bundle` |
| 全市场选股 | `dsa_selector.py:863` `DSASelector.execute()` | `compute_dsa_bundle` |
| 盘后回补 | `dsa_selector.py:911` | `compute_dsa_bundle` |
| 研究路径（非生产） | `research/feature_computer.py:293` | `compute_dsa_history`（与 SSOT 一致，但不走 bundle） |

### 5.3 与 SMC 边界

DSA 负责趋势段（`regime_value`/`dsa_dir_bars`/`visual_segments`）；SMC `compute_smc_pine` 仅输出 events(BOS/CHoCH)/order_blocks/equal_highs_lows/trailing/swing_bias/pivots，**不维护等价趋势段**。无重复定义。

### 5.4 Phase 5B-1 趋势修改清单（下一轮，本轮不修改算法）

本轮审计仅发现文档描述偏差，未发现算法实现缺陷。下一轮如需修改趋势相关逻辑，应聚焦以下精确位置（详见 `docs/maps/20-quant-model.md` §3 末尾）：

1. 调整趋势段方向判定阈值：`dsa_selector.py:68` 常量 `MIN_DIR_BARS = 50`；
2. 补充段内成交量到 SSOT：`compute_dsa_history` line 434-473 新增 `current_segment_volume_sum` 列；
3. 趋势与板块聚合联动：新增 `board_factor_aggregation_service.py` 处理 QM-50/QM-51 缺口；
4. 研究路径统一：`research/feature_computer.py:293` 改为 `compute_dsa_bundle`。

## 6. 文档与规则更新

| 文件 | 更新内容 |
|---|---|
| `rules/90-deprecated-forbidden.md` | 新增 "ref/sync git 跟踪禁止（Phase 5B-0）" 条款 |
| `docs/prd/80-system-runtime.md` | 新增 SR-15 本地参考/传输目录不得进入仓库 |
| `docs/maps/00-system-overview.md` | §6 偏差索引关闭 ref/ 风险，新增 origin/main ref 清理 PR 与本地 Vite `/` 限制 |
| `docs/maps/20-quant-model.md` | §3 趋势章节全面重写：修正平均成交量字段归属、补充完整调用路径、添加 Phase 5B-1 修改清单 |
| `docs/maps/40-market-stock-experience.md` | §8 新增 Phase 5B-0 前端验证结果表 |
| `docs/maps/50-watchlist-intraday.md` | §7 新增 Phase 5B-0 前端验证结果 |
| `docs/maps/60-permissions-admin.md` | §8 新增 Phase 5B-0 管理员路由验证结果表 |
| `docs/maps/80-system-runtime.md` | §4 Git 与 CI 新增 ref/sync 仓库清理、CI 防误推、main PR 状态；§10 已知偏差补充 |
| `docs/maps/technical/codebase-modules.md` | §1 顶层目录新增 ref/sync 非模块声明；§4 公共入口新增 DSA 趋势计算权威入口 |
| `docs/runbooks/local-development.md` | 新增 "全路由验证（Phase 5B-0 起）" 章节 |

## 7. 验证

- `git ls-files ref sync` 在 dev/experiment 本地与 origin 均为空；
- `.github/workflows/ci.yml` governance-rules job 包含显式 ref/sync 检查；
- `backend/tests/test_ref_isolation.py` 改写后守护 `git ls-files ref/` 与 `git ls-files sync/` 均为空；
- 本地 Backend `/health` 200、`/health/ready` ready、`/version` deployment_mode=native-development；
- 本地 Frontend port 8008 HTTP 200；
- 所有用户级与管理员路由通过 admin token 验证（除 `/` 受本地 Vite 限制）；
- 趋势入口审计：`compute_dsa_history` / `compute_dsa_bundle` 调用路径已通过 `grep` 核验，与 SMC 边界已通过 `compute_smc_pine` 返回字段核验；
- 文档链接检查：本轮更新的 Maps/PRD/Runbook 内部引用路径有效。

## 8. 资源与约束

- 项目新增 < 10MB；.git 新增 < 5MB（满足约束）；
- pip cache 与 npm cache 未增长；
- 未安装/升级依赖；未运行 Docker、Migration、全市场任务、Scheduler 或 Worker；
- 未执行 rebase、stash、reset --hard、git add -A、git gc/prune/repack；
- 未输出密钥、完整 URL 或生产 env；
- 未重写 Git 历史；未 force push；未删除 archive tags。

## 9. 未完成事项

- `origin/main` 仍含 `ref/smc_user_source.pine`，需通过 dev → main PR 合并清理（等待用户授权）；
- Phase 5B-1 趋势修改清单待下一轮执行（本轮不修改算法）；
- 自动部署链路启用仍待服务器侧入口与 GitHub Secrets 配置（Phase 3 准备状态延续）。
