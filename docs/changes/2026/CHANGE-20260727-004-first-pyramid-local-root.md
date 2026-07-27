# CHANGE-20260727-004 第一金字塔统一契约与本地根路径修复

**变更日期**：2026-07-27
**变更分支**：dev
**变更基线**：54c601e
**变更范围**：前端根路径修复、第一金字塔统一契约、趋势段内成交量迁移、权限审计
**对应 PRD**：`docs/prd/20-quant-model.md`、`docs/prd/40-market-stock-experience.md`、`docs/prd/60-permissions-admin.md`、`docs/prd/80-system-runtime.md`

## 1. 变更摘要

本轮 Phase 5B-1 完成以下核心变更：

1. **修复本地根路径无限刷新**：`LandingPage.tsx` 在 Vite 开发环境下跳转至 `/portal/index.html`，生产环境显示稳定入口链接，不再 `window.location.replace('/')` 自跳转。
2. **第一金字塔统一契约**：新增 `FirstPyramidSnapshot` DTO 与 `compute_first_pyramid_snapshot` 编排服务，固定维度顺序 trend→structure→momentum→chip_consensus，前三维必选，chip_consensus 可选。
3. **趋势段内成交量迁移至 SSOT**：将原 `structural_factor_service.py` 中的段内成交量派生逻辑迁回 `dsa_selector.py:compute_dsa_history`，实现单一所有权。
4. **权限系统代码级审计**：核验当前权限实现与 PRD60 差异，更新 Maps60 从"待重建"至真实状态；本轮不修改权限业务代码。

## 2. 修改文件清单

### 前端
- `frontend/src/pages/LandingPage/LandingPage.tsx`（MODIFIED）
  - DEV 模式：`window.location.replace('/portal/index.html')`
  - PROD 模式：稳定兜底 JSX（可点击入口链接）
- `frontend/src/pages/__tests__/landingPageRoot.test.mjs`（NEW）
  - 7 测试：禁止自跳转、portal 路径、DEV 守卫、生产兜底、文件存在、base href、App.tsx 路由

### 后端
- `backend/app/strategy/selectors/dsa_selector.py`（MODIFIED）
  - `compute_dsa_history` 新增段内成交量字段（line 381-407）
  - `_history_row_to_metrics` 同步导出（line 555-563）
- `backend/app/schemas/first_pyramid.py`（NEW）
  - `FirstPyramidSnapshot` DTO + `DimensionResult` + `PyramidEvent`
  - 固定 `ORDERED_DIMENSIONS = ("trend", "structure", "momentum", "chip_consensus")`
  - 必选维度校验器（前三维 available=False 抛 ValueError）
- `backend/app/services/first_pyramid_service.py`（NEW）
  - `compute_first_pyramid_snapshot` SSOT 编排入口
  - 调用 DSA bundle / SMC Pine core / Bollinger+SQZMOM / Node Cluster engine
  - 计算 `inputHash` / `parameterHash` 用于跨入口一致性
  - 聚合状态文本按正确顺序 trend→structure→momentum→chip_consensus
- `backend/tests/test_first_pyramid_contract.py`（NEW）
  - 38 测试：DTO 契约 / 端到端 / 跨入口一致性 / 不变量 / golden fixture / 错误处理 / QM 映射

### 文档
- `docs/maps/20-quant-model.md`（MODIFIED）：段内成交量 SSOT 标注、Phase 5B-1 实施记录、§9.5 第一金字塔契约
- `docs/maps/60-permissions-admin.md`（MODIFIED）：从"待重建"重建为真实状态、§7 已知偏差、§10 下一阶段方案

## 3. 变更前后关键差异

### 3.1 根路径行为
| 场景 | 变更前 | 变更后 |
|---|---|---|
| Vite 开发 `/` | `window.location.replace('/')` → 无限刷新 | `window.location.replace('/portal/index.html')` → 一次性跳转 |
| 生产 Nginx `/` | Nginx 分流门户 | Nginx 分流门户（不变）；若误进入 SPA，显示稳定入口链接 |

### 3.2 趋势段内成交量所有权
| 项目 | 变更前 | 变更后 |
|---|---|---|
| 计算位置 | `structural_factor_service.py:873-945`（派生） | `dsa_selector.py:compute_dsa_history`（SSOT 直接输出） |
| 字段 | `current_segment_volume_sum`（外部派生） | `current_segment_volume_sum/mean`、`prev_segment_volume_sum`、`current_vs_prev_volume_ratio`（SSOT） |
| 单一所有权 | ❌ 外部模块重复派生 | ✅ DSA SSOT 唯一所有权 |

### 3.3 第一金字塔维度顺序
| 项目 | 变更前 | 变更后 |
|---|---|---|
| 顺序 | trend→momentum→structure→volume（atomic_fact_contract） | trend→structure→momentum→chip_consensus（first_pyramid） |
| DTO | 无统一 DTO，各链路自行拼字段 | `FirstPyramidSnapshot` 统一 DTO |
| 编排入口 | 无 | `compute_first_pyramid_snapshot` SSOT |
| 跨入口一致性 | 无保证 | `inputHash`/`parameterHash` 校验 |

## 4. 验证结果

### 4.1 前端根路径测试
```
node --test src/pages/__tests__/landingPageRoot.test.mjs
# 7 tests, 7 pass, 0 fail
```

### 4.2 第一金字塔契约测试
```
APP_ENV=test TEST_DATABASE_URL=postgresql://localhost/panji_test SKIP_ALEMBIC_UPGRADE=1 \
  .venv/bin/python -m pytest tests/test_first_pyramid_contract.py -v
# 38 tests passed in 1.55s
```

### 4.3 第一金字塔模块自测
```
.venv/bin/python -m app.services.first_pyramid_service
# OK: TEST.MOCK 2026-04-24
# ordered: ['trend', 'structure', 'momentum', 'chip_consensus']
# All 4 dimensions available=True
# statusText follows correct order
```

## 5. 受影响行为与契约

- **本地开发根路径**：Vite 下 `/` 稳定跳转至 portal，不再无限刷新
- **生产环境**：Nginx 门户分流行为不变；SPA 兜底显示可点击入口
- **趋势维度输出**：DSA metrics 新增 8 个段内成交量字段
- **第一金字塔契约**：单股/批量/行情列表/盘后 compute 必须复用 `FirstPyramidSnapshot`
- **权限系统**：本轮仅审计，不修改业务代码；Maps60 已重建为真实状态

## 6. 未完成项

- QM-50/QM-51 板块/指数第二金字塔（本轮明确不实施）
- 权限系统 capability grants 重构（Phase 5B-2+ 候选，见 Maps60 §10）
- `structural_factor_service.py` 中的旧段内成交量派生代码清理（保留为兼容期，下次维护时移除）
- 真实浏览器 30 秒稳定性观察（受限于内存约束，由测试覆盖代替）

## 7. 资源使用

- 开始：free 1.17GB，swap 12288MB，disk 27Gi
- 算法测试后：free 1.18GB，swap 11171MB，disk 27Gi
- 持久新增：< 15MB（代码 + 测试 + 文档）
- .git 新增：< 5MB

## 8. 下一阶段建议

1. **前端集成**：在 `/market` 和 `/stock/:symbol` 页面接入 `FirstPyramidSnapshot` API
2. **权限重构**：按 Maps60 §10 方案实施 capability grants
3. **板块第二金字塔**：实施 QM-50/QM-51 板块聚合
4. **生产 Nginx 验证**：上线后验证 `/` 行为不变
