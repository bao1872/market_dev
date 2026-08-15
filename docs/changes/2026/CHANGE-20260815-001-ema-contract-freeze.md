# CHANGE-20260815-001 — Review v2.3 Historical Dynamics EMA Contract Freeze

## 元数据

- 日期：2026-08-15
- 类型：`docs-only`（PRD contract freeze / Historical Dynamics EMA 数值语义收口）
- 领域：复盘模块 / `docs/prd/70-review.md`（§7.7.5 / §7.9）
- 授权：用户在 `ref/复盘模块修改指令专用.md`（Review v2.3 — Historical Dynamics EMA Contract Freeze）中明确授权进行 docs-only PRD freeze round
- 状态：`prd_confirmed`（docs-only；**未进入 Implementation，未写任何业务/测试代码、未建表、未 migration、未改 API/前端、未改 Maps/Runbooks/治理、experiments/ 未跟踪目录保留不动**）

## 背景

Position Foundation = CLOSED；EMA Contract Audit = ALGORITHM_MAPPING_REQUIRED → EMA Algorithm Mapping = MAPPING_RECOMMENDED → Independent Mapping Audit = PASS。

已接受的 EMA Algorithm Mapping 需要正式冻结进 PRD，使下一轮 implementation（Velocity / Signal / Acceleration）不再存在 numerical semantic ambiguity。

## 变化内容（docs-only）

### A. `docs/prd/70-review.md` §7.9 新增「EMA Numerical Contract（FROZEN）」

冻结 A–J 十项数值语义：

- **A. Standard span definition**：`alpha_N = 2/(N+1)`；递归公式 `EMA_N(t) = alpha_N*x(t) + (1-alpha_N)*EMA_N(previous_valid)`；PRD 公式为唯一 owner，不得依赖 pandas hidden defaults（可与 `ewm(span=N, adjust=False)` 数值对应）。
- **B. Seed**：`EMA_N(first_valid) = x(first_valid)`，内部 EMA state 从第一条 valid input 建立。
- **C. Warmup**：EMA5 至少 5 个 valid inputs ready；EMA20 至少 20 个 valid inputs ready；未满足时 `value=null`、`status=insufficient_history`；明确内部 state 已存在，只是输出未 ready（不得理解成第 20 个 observation 才开始 seed）。
- **D. Valid-observation clock**：EMA span 推进单位是 valid input observation，不是 calendar day；EMA5/EMA20 为产品简称，精确语义 span=5/span=20 valid observations；unavailable 交易日不推进 EMA clock。
- **E. Missing input**：Position unavailable → EMA5/EMA20=null、`status=unavailable_current`；保留 previous internal state，下一条 valid Position 从 previous_valid state 继续递归；禁止 forward-fill / zero-fill / synthetic value / daily decay / dropna 改变日期对齐 / gap reset。
- **F. Gap**：单日或多日 unavailable 不 reset；不定义 3/5/20-day reset 阈值；缺失期间不 update、不 decay、不推进 valid_count。
- **G. Velocity**：`Fast(T)=EMA5(Position)(T)`、`Slow(T)=EMA20(Position)(T)`、`Velocity(T)=Fast(T)−Slow(T)`；ready iff Position ready ∧ Fast ready ∧ Slow ready；否则 null；不 forward-fill。
- **H. Signal**：`Signal=EMA5(Velocity)`，使用完全相同 EMA contract；ready iff Velocity ready ∧ 累计至少 5 个 valid Velocity observations。
- **I. Acceleration**：`Acceleration(T)=Velocity(T)−Signal(T)`；ready iff Velocity ready ∧ Signal ready；否则 null。
- **J. No Future Leakage**：所有 EMA 只用当前和过去 input，未来 observation 永远不能影响历史 EMA/Velocity/Signal/Acceleration。

同步新增：

- **Availability Status（FROZEN）**：复用 Position 的 `ready` / `insufficient_history` / `unavailable_current`；不得创建 `warming` / `gap` / `paused` / `stale` 新 status。
- **Implementation Ownership**：Historical Dynamics EMA math 属 **Analysis B pure domain owner**；下一轮预计 `app/domain/review/analysis/historical_dynamics.py`；该模块不访问 DB、不负责 reconstruction/membership、不持久化、不做 Interpretation。

### B. `docs/prd/70-review.md` §7.9 Velocity 术语修正

原「Fast EMA = 5D，Slow EMA = 20D」改为「Fast EMA = 5，Slow EMA = 20（产品简称 EMA5 / EMA20；精确算法为 span=5 / span=20 valid input observations）」，消除 5D/20D 歧义。

### C. `docs/prd/70-review.md` §7.7.5 Analysis Window 状态同步

- EMA window → numerical contract 已冻结于 §7.9（EMA Numerical Contract）。
- percentile lookback 与 persistence window **仍保持 IMPLEMENTATION DESIGN REQUIRED**；Persistence / Dynamics Phase / Leadership 未标成 ready（严格只更新 EMA 对应状态）。

## 明确未修改（产品语义保留）

- Position：默认历史窗口 120 trading days / 最低有效历史 60 observations 不变。
- Velocity >0 upward migration、<0 downward migration 原产品语义保留。
- Signal / Acceleration 公式保留；**未新增** strong / weak / fast / slow threshold，未新增 `abs(Velocity)>X` 之类判断。
- Persistence 原合同不改。
- Dynamics Phase / Internal Structure / Trading Context / Interpretation 未修改。

## 验证

- `git diff --check`：EXIT=0（无空白错误）。
- 重新读取修改后的 §7.7.5 / §7.9，逐项确认以下歧义均已消除：alpha、adjust、seed、min_period、missing behavior、clock、reset、Velocity readiness、Signal readiness、Acceleration readiness。
- 仅修改 `docs/prd/70-review.md`、新增本 CHANGE、更新 CHANGE INDEX；未修改任何 Python / Tests / DB / Migration / API / Frontend / Maps / Runbooks / 治理。

## 下一步

- **下一轮 implementation**：按 §7.9 EMA Numerical Contract 实现 `historical_dynamics.py`（Velocity / Signal / Acceleration），复用 Position Foundation 与 §7.7.5 Observation Series 契约；Leadership Migration 等仍按各自 ALGORITHM MAPPING 状态处理。
