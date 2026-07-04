# AUDIT-20260704: Ruff / Mypy 历史债务分级审计

> 审计日期：2026-07-04
> 审计基线：main HEAD = `44d37fd`（PR #15 合并后），本审计分支 `fix/screener-result-join-by-instrument`
> 审计类型：READ-ONLY 债务分级审计 + 后续 PR 拆分计划
> 审计约束：不清债务，不改 backend/app 生产代码；只做 P0/P1/P2 分级和建议
> 审计依据：`tools/quality_baselines/ruff.json`、`tools/quality_baselines/mypy.json`、`AGENTS.md` 第 13 条质量门禁

---

## 0. 总览

| 维度 | baseline (64ed75c) | 当前 (44d37fd) | 差值 | 策略 |
|---|---|---|---|---|
| Ruff diagnostics | 930 | 861 | -69 | 三层门禁：new-files 阻断 / baseline-non-regression 阻断 / full-report 非阻断 |
| Mypy diagnostics | 242 | 227 | -15 | 三层门禁：new-files 阻断 / baseline-non-regression 阻断 / full-report 非阻断 |

债务整体呈下降趋势。CI 三层门禁已上线，能阻断"新增/增加"债务，但历史债务本身不在本审计范围内清除。

### 分级定义（与用户本轮目标对齐）

| 级别 | 定义 | 处理策略 |
|---|---|---|
| P0 | 影响当前生产验证/数据正确性的债务 | 立即修复，独立 PR |
| P1 | 导致 SSOT、docs/code 冲突或 Trae 误判的债务 | 按 PR 顺序处理，本轮不做 |
| P2 | 普通 Ruff/Mypy 历史风格债务 | 保留在 ALIGN-021，长期治理 |

---

## 1. P0 债务（影响生产验证/数据正确性）

**结论：本审计未发现 P0 债务。**

候选排查项及判定理由：

| 候选 | 文件 | 类型 | 判定 |
|---|---|---|---|
| `Instrument` 未导入 | `app/models/strategy_run.py:273` | F821 | `from __future__ import annotations` 在 L33，注解为字符串字面量，运行时不求值；`relationship("Instrument", ...)` 用字符串引用 SQLAlchemy mapper registry，运行时正常。**非 P0**，归 P1（Trae 误判风险） |
| `SHANGHAI_TZ` 未导入 | `app/services/chart_bars_service.py:113` | F821 | 调用点分析：`_filter_unfinished_daily_bars` 仅在自测代码（`__main__`）调用，生产代码无调用。**非 P0**，归 P2（dead path） |
| `TdxHq_API`/`TdxConnectionError` 未导入 | `app/strategy_assets/algorithms/features/dynamic_swing_anchored_vwap.py:70/86/89` | F821 (×5) | `_connect_pytdx` 函数仅在该模块内部独立调用，`dsa_selector.py` 调用的是 `dynamic_swing_anchored_vwap(df, cfg)` 计算函数，不会触发 `_connect_pytdx`。**非 P0**，归 P2 |
| `offset` 未定义 | `app/strategy_assets/algorithms/features/liquidity_zones_plotly.py:679` | F821 | 该模块无任何 import 引用（`grep -rn "liquidity_zones_plotly" app/` 无结果），完全死代码。**非 P0**，归 P2 |
| `AsyncSession` 未导入 | `tests/test_selector_query_integration.py:706`, `tests/test_strategy_batch.py:187` | F821 (×2) | 测试文件，且 `from __future__ import annotations` 保护。**非 P0**，归 P2 |

---

## 2. P1 债务（SSOT / docs-code 冲突 / Trae 误判）

### P1-1: `after_close_orchestrator.py` 22 个 mypy 错误（`SchedulerJobRun | None` 类型不匹配）

**文件**: `app/services/after_close_orchestrator.py`

**错误清单**（22 处）:
- L242/466/519/571/593/643/682/718: `Argument "job_run" to "_update_orchestrator_status" has incompatible type "SchedulerJobRun | None"; expected "SchedulerJobRun"` [arg-type]
- L251/525/526/528/542/571/578/623/672/698/724/725/727: `Item "None" of "SchedulerJobRun | None" has no attribute "id/status/finished_at"` [union-attr]
- L631: `Incompatible types in assignment (expression has type "StrategyRun | None", variable has type "StrategyRun")` [assignment]

**判定**:
- PR #14 已修复 DSA 总超时问题，生产 DSA run 已能正常完成（5106 succeeded/187 skipped/0 failed）。
- 这些 mypy 错误表明 `_create_job_run` 返回 `SchedulerJobRun | None`，但调用方未处理 None 分支。
- 生产中 None 路径未触发（run 都成功创建 job_run），但潜在 None 访问仍是 bug 隐患。
- 影响 SSOT：Trae 看到 mypy 错误可能误判 `after_close_orchestrator` 不可靠，添加无谓的 None 检查或重写。

**建议 PR**: `fix/mypy-after-close-orchestrator-none-handling`
- 优先级：P1-高（核心编排路径）
- 工作量：小（在调用方添加 None 检查 + 早返回，或修改 `_create_job_run` 返回类型）

### P1-2: `worker.py` 2 个 mypy 错误（`SchedulerJobRun | None`）

**文件**: `app/worker.py:472, 522`

**错误清单**:
- L472: `Argument 2 to "_update_job_heartbeat" has incompatible type "SchedulerJobRun | None"; expected "SchedulerJobRun"` [arg-type]
- L522: `Argument 2 to "_finish_job_run" has incompatible type "SchedulerJobRun | None"; expected "SchedulerJobRun"` [arg-type]

**判定**:
- 与 P1-1 同源：`_create_job_run` 返回 Optional，调用方未处理 None。
- 影响：Trae 可能误判 worker 心跳/完成路径不可靠。

**建议 PR**: 与 P1-1 同一 PR 处理（`fix/mypy-after-close-orchestrator-none-handling`）

### P1-3: `bars_metrics.py` 10 个 mypy 错误（访问 prometheus_client 私有属性）

**文件**: `app/services/bars_metrics.py:267-280`

**错误清单**:
- L267-269: `"Counter" has no attribute "_values"; maybe "_value"?` [attr-defined] ×3
- L272/276: `"Histogram" has no attribute "_counts"` [attr-defined] ×2
- L274/276: `"Histogram" has no attribute "_sums"; maybe "_sum"?` [attr-defined] ×2
- L278/280: `"Gauge" has no attribute "_values"; maybe "_value"?` [attr-defined] ×2
- L223: `Incompatible return value type (got "Any | None", expected "tuple[str, ...]")` [return-value]

**判定**:
- 代码访问 `Counter._values`、`Histogram._counts`、`Histogram._sums`、`Gauge._values` 私有属性。
- prometheus_client 版本升级（如 0.20+）可能重命名或移除私有属性，导致生产 metrics 导出失败。
- 影响：Trae 可能误判 metrics 系统不可靠；prometheus_client 升级会静默破坏。

**建议 PR**: `fix/bars-metrics-prometheus-private-attr`
- 优先级：P1-中（监控指标，非业务核心）
- 工作量：中（需调研 prometheus_client 公开 API 替代方案，或加 try/except + type: ignore）

### P1-4: `chart_bars_service.py` F821 `SHANGHAI_TZ` 未导入

**文件**: `app/services/chart_bars_service.py:113`

**判定**:
- 函数 `_filter_unfinished_daily_bars` 仅自测代码调用（生产无调用），但 Trae 静态分析会看到 F821，可能误判需要补 import 或重写函数。
- SSOT 冲突风险：若 Trae 修复此 F821，可能引入 `from app.core.time import SHANGHAI_TZ`，但函数本身是死代码，应删除而非修复。

**建议 PR**: `chore/remove-dead-code-chart-bars-trim-today`
- 优先级：P1-低（死代码，但 Trae 误判风险高）
- 工作量：小（删除 `_filter_unfinished_daily_bars` 函数及自测代码）

### P1-5: `strategy_run.py` F821 `Instrument` 未导入

**文件**: `app/models/strategy_run.py:273`

**判定**:
- `from __future__ import annotations` 保护，运行时正常。
- 但 Trae 静态分析会看到 `Mapped[Instrument]` 中 `Instrument` 未定义，可能误判需要 import。
- 正确修复：`from app.models.instrument import Instrument` 或 `if TYPE_CHECKING: from app.models.instrument import Instrument`。

**建议 PR**: `fix/mypy-strategy-run-instrument-import`
- 优先级：P1-低（Trae 误判风险，运行时无影响）
- 工作量：小（加 TYPE_CHECKING import）

### P1-6: `metrics.py` 16 个 mypy 错误（prometheus_client 版本兼容 hack）

**文件**: `app/api/metrics.py:137-213`

**错误清单**:
- L137: `All conditional function variants must have identical signatures` [misc]
- L212-213: `List item 0/1 has incompatible type "Counter/Histogram"; expected "_FallbackMetric"` [list-item]
- 其他 13 个：`type: ignore[xxx]` 不覆盖实际错误码

**判定**:
- 代码用 `if/else` + `type: ignore` hack 兼容不同 prometheus_client 版本的 `generate_latest` 签名。
- prometheus_client 版本变化会让现有 type: ignore 失效。
- 影响：Trae 看到 16 个错误可能误判 metrics endpoint 不可靠。

**建议 PR**: `fix/metrics-prometheus-version-compat`
- 优先级：P1-低（仅监控 endpoint，不影响业务）
- 工作量：中（需重写兼容逻辑或固定 prometheus_client 版本）

---

## 3. P2 债务（普通 Ruff/Mypy 历史风格债务）

### Ruff P2 分布（共 849 个，占比 98.6%）

| 错误码 | 数量 | 说明 | 处理建议 |
|---|---|---|---|
| C408 | 440 | `dict()` 调用（应改字面量） | `ruff check --fix --unsafe-fixes` 可批量修，但需 review |
| N806 | 175 | 非小写变量名（算法变量如 `lI`/`pR`/`plL`/`phL`/`sXY`） | 算法实现保留，加 `# noqa: N806` 或在 `pyproject.toml` per-file ignore |
| N815 | 68 | 混合大小写类变量 | 算法 schema 保留，per-file ignore |
| I001 | 32 | 导入排序 | `ruff check --fix` 可自动修 |
| W293 | 27 | 空格空白行 | `ruff check --fix` 可自动修 |
| F841 | 26 | 未使用变量 | 部分是死代码，部分是测试 stub，需人工 review |
| B905 | 15 | `zip()` 缺 `strict=` | 加 `strict=False` 或 review 长度匹配 |
| E741 | 12 | 模糊变量名 `l` | 改名 `low` 或 `level` |
| E402 | 11 | 导入位置（module-import-not-at-top） | 多数是 `dsa_selector.py` 等 noqa 标注，保留 |
| N803 | 10 | 参数名 | 算法参数，per-file ignore |
| F401 | 9 | 未使用导入 | `ruff check --fix` 可自动修 |
| B011 | 6 | `assert False`（测试用） | 改 `raise AssertionError()` |
| N802 | 6 | 函数名 | 测试 stub，per-file ignore |
| F811 | 3 | 重定义 while unused | 人工 review |
| N999 | 3 | 模块名 | 测试文件名，per-file ignore |
| 其他 | 7 | N812/W291/C401/E701/N818 | 风格类 |

### Mypy P2 分布（共 189 个）

| 错误码 | 数量 | 说明 | 处理建议 |
|---|---|---|---|
| attr-defined | ~89 | 访问不存在的属性（多数是 SQLAlchemy 模型 `.columns`/`.constraints`/`.indexes`、prometheus 私有属性） | 多数需 `# type: ignore[attr-defined]` 或修复 SQLAlchemy 2.0 类型注解 |
| arg-type | ~62 | 参数类型不匹配（多数是测试 mock `_MockSession`/`_MockPytdxAdapter`） | 测试 mock 加 `# type: ignore[arg-type]` 或用 `Protocol` |
| assignment | ~18 | 赋值类型不匹配 | 人工 review |
| union-attr | ~5 | Optional 属性访问 | 加 None 检查 |
| return-value | ~5 | 返回值类型 | 人工 review |
| list-item | ~5 | List 项类型 | 人工 review |
| valid-type | 4 | 变量作为类型（`AsyncSessionLocal` 作为类型注解） | 改 `AsyncSession` 或加 `# type: ignore` |
| name-defined | 9 | 名称未定义 | 与 Ruff F821 重叠，多数 `from __future__ import annotations` 保护 |
| operator | 5 | 操作符类型 | 人工 review |
| misc | 3 | 其他 | 人工 review |
| var-annotated | 2 | 变量注解 | 加类型注解 |

### P2 处理策略

- **不批量清 debt**：本轮不做。
- **保留 ALIGN-021**：所有 P2 债务继续在 `docs/current/code-doc-alignment.md` ALIGN-021 跟踪。
- **CI 三层门禁已上线**：
  - `Ruff New Files` / `Mypy New Files`：阻断新增文件错误。
  - `Ruff Baseline Regression` / `Mypy Baseline Regression`：阻断历史债务新增/总数超基线。
  - `Ruff/Mypy Full Repository Report`：非阻断上传报告。
- **长期治理**：每个新 PR 顺手修复涉及文件的 P2 债务，逐步收敛。

---

## 4. 建议 PR 顺序

| 序号 | PR | 优先级 | 工作量 | 涉及 P1 |
|---|---|---|---|---|
| 1 | `fix/mypy-after-close-orchestrator-none-handling` | P1-高 | 小 | P1-1 + P1-2 |
| 2 | `chore/remove-dead-code-chart-bars-trim-today` | P1-低 | 小 | P1-4 |
| 3 | `fix/mypy-strategy-run-instrument-import` | P1-低 | 小 | P1-5 |
| 4 | `fix/bars-metrics-prometheus-private-attr` | P1-中 | 中 | P1-3 |
| 5 | `fix/metrics-prometheus-version-compat` | P1-低 | 中 | P1-6 |

**建议**：PR 1+2+3 可合并为一个 PR（`fix/mypy-p1-cleanup`），都是小改动；PR 4+5 因涉及 prometheus_client 兼容性，需独立 PR + 测试。

---

## 5. 审计边界

本审计 **不涉及**：

- 前端 ESLint/TypeScript 债务（`frontend/`）
- 测试覆盖缺口（见 `docs/maps/test-coverage-map.md`）
- 文档/代码漂移（见 `docs/current/code-doc-alignment.md` KNOWN_GAP）
- 架构边界（见 `docs/architecture-audits/AUDIT-20260704-worker-notification-capture-boundaries.md`）

本审计 **不修改**：

- 任何 `backend/app/` 生产代码
- 任何 `backend/tests/` 测试代码
- 任何 `frontend/` 代码
- 任何 `tools/quality_baselines/` 基线
- 任何 `.github/workflows/ci.yml` CI 配置

---

## 6. 审计证据

### Ruff 证据

```
$ cd backend && ruff check app tests --statistics
440     C408    [ ] unnecessary-collection-call
175     N806    [ ] non-lowercase-variable-in-function
 68     N815    [ ] mixed-case-variable-in-class-scope
 32     I001    [*] unsorted-imports
 27     W293    [ ] blank-line-with-whitespace
 26     F841    [ ] unused-variable
 15     B905    [ ] zip-without-explicit-strict
 12     E741    [ ] ambiguous-variable-name
 11     E402    [ ] module-import-not-at-top-of-file
 11     F821    [ ] undefined-name
 10     N803    [ ] invalid-argument-name
  9     F401    [*] unused-import
  6     B011    [ ] assert-false
  6     N802    [ ] invalid-function-name
  3     F811    [*] redefined-while-unused
  3     N999    [ ] invalid-module-name
  2     N812    [ ] lowercase-imported-as-non-lowercase
  2     W291    [ ] trailing-whitespace
  1     C401    [ ] unnecessary-generator-set
  1     E701    [ ] multiple-statements-on-one-line-colon
  1     N818    [ ] error-suffix-on-exception-name
Found 861 errors.
```

### Mypy 证据

```
$ cd backend && python -m mypy app
Found 227 errors in 68 files (checked 226 source files)

Top files by error count:
  22 app/services/after_close_orchestrator.py  (P1-1)
  19 app/worker.py                              (P1-2 + AsyncSessionLocal valid-type)
  16 app/services/system_overview_service.py    (P2: _determine_monitor_status Optional 参数)
  16 app/api/metrics.py                         (P1-6)
  15 app/strategy_assets/algorithms/features/ths_query.py  (P2: 死代码)
  13 app/repositories/bar_repository.py         (P2: `type` 变量名冲突 + 测试 mock)
  10 app/services/bars_metrics.py               (P1-3)
   9 app/strategy_assets/algorithms/features/dynamic_swing_anchored_vwap.py  (P2: 死代码)
   9 app/api/bars.py                            (P2: 主要是 type: ignore 不覆盖)
   8 app/services/bars_scheduler_service.py     (P2)
   7 app/strategy/_plotly_mock.py               (P2: 测试 mock)
   7 app/services/reconcile_bars.py             (P2)
   7 app/api/watchlist.py                       (P2)
```

### F821 (undefined-name) 逐项判定证据

| 文件:行 | 名称 | 调用点分析 | 判定 |
|---|---|---|---|
| `app/models/strategy_run.py:273` | `Instrument` | `from __future__ import annotations` 在 L33，`Mapped[Instrument]` 为字符串字面量 | P1-5（Trae 误判） |
| `app/services/chart_bars_service.py:113` | `SHANGHAI_TZ` | `_filter_unfinished_daily_bars` 仅 `__main__` 调用，生产无调用 | P1-4（死代码） |
| `app/strategy_assets/algorithms/features/dynamic_swing_anchored_vwap.py:70/86/89` (×5) | `TdxHq_API`/`TdxConnectionError` | `_connect_pytdx` 内部辅助，`dsa_selector` 调用 `dynamic_swing_anchored_vwap` 计算函数不触发 | P2（死路径） |
| `app/strategy_assets/algorithms/features/liquidity_zones_plotly.py:679` | `offset` | 模块零 import 引用，完全死代码 | P2（死代码） |
| `tests/test_selector_query_integration.py:706` | `AsyncSession` | 测试文件，`from __future__ import annotations` 保护 | P2 |
| `tests/test_strategy_batch.py:187` | `AsyncSession` | 同上 | P2 |
