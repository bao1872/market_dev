# CHANGE-20260815-002 — Review v2.3 Historical Dynamics Persistence Contract Freeze

## 元数据

- 日期：2026-08-15
- 类型：`docs-only`（PRD contract freeze / Historical Dynamics Persistence 数值与 availability 语义收口）
- 领域：复盘模块 / `docs/prd/70-review.md`（§7.7.5 / §7.9）
- 授权：用户在 `ref/复盘模块修改指令专用.md`（Review v2.3 — Historical Dynamics Persistence Contract Freeze）中明确授权进行 docs-only PRD freeze round
- 状态：`prd_confirmed`（docs-only；**未进入 Implementation，未写任何业务/测试代码、未建表、未 migration、未改 API/前端、未改 Maps/Runbooks/治理、experiments/ 未跟踪目录保留不动**）

## 背景

Position = CLOSED；Velocity / Signal / Acceleration = CLOSED；Persistence Contract Audit = ALGORITHM_MAPPING_REQUIRED → Persistence Algorithm Mapping = MAPPING_RECOMMENDED → Independent Mapping Audit = PASS。

已接受的 Persistence Algorithm Mapping 需要正式冻结进 PRD，使下一轮 implementation（Persistence）不再存在 numerical semantic ambiguity。

## 变化内容（docs-only）

### A. `docs/prd/70-review.md` §7.9 新增「Persistence Numerical Contract（FROZEN）」

冻结 A–K 十一项数值与 availability 语义：

- **A. Window contract**：`PERSISTENCE_WINDOW_SIZE = 20`；`Persistence(T)` 使用以 T 为右端、**包含 T** 的最近最多 20 个 trading observations `[T-19, T]`；series 不足 20 observations 时只用实际已有观测，不得向未来补齐；禁止向更早历史找 20 valid、dropna 压缩、使用 T+1；`window_size=20`、`candidate_count = min(20, available through T)`（不硬编码 20）。
- **B. Current-day inclusion**：`Persistence(T)` 包含 `Position(T)`；产品含义=「截至 T 含 T，当前状态在最近 20 trading observations 中是否持续」；与 Position percentile 的 pre-T baseline 不同，**不得复制 Position pre-T rule**。
- **C. Valid Position**：upstream `Position.status == ready` 且 `position` 为 finite numeric（合法值域 `[0,100]`）；`status==ready` 但 position None/NaN/inf/越界 = **upstream contract violation，fail fast**，不得静默转 historical missing；禁止 zero fill / forward fill / clamp / silent drop。
- **D. Historical missing**：窗口内非当前 T 的 observation，若 `Position.status` 为 `unavailable_current` 或 `insufficient_history`：仍占 window slot，但**不进 Upper/Lower numerator、不进 valid denominator**；不得向前补找 valid Position。
- **E. Denominator**：`valid_count` = 20-slot 窗口内 valid Position 数；`upper_count = count(valid Position >= 80)`；`lower_count = count(valid Position <= 20)`；`upper_occupancy = upper_count / valid_count`；`lower_occupancy = lower_count / valid_count`；**禁止固定 denominator=20**（fixed20 会把 missing 隐式当成 middle）。
- **F. Minimum valid contract**：`PERSISTENCE_MINIMUM_VALID_COUNT = 15`（等价目标 coverage `valid_count/20 >= 0.75`），必须 exact 15；`valid_count < 15` 且当前 Position 非 unavailable_current → `insufficient_history`、Upper/Lower=null。
- **G. Current status precedence**：① `Position(T)=unavailable_current` → Persistence `unavailable_current`、Upper/Lower=null（即使窗口 valid_count>=15 也不得输出旧 Persistence）；② `Position(T)=insufficient_history` → `insufficient_history`、Upper/Lower=null；③ `Position(T)=ready` 但 `valid_count<15` → `insufficient_history`；④ `Position(T)=ready` 且 `valid_count>=15` → `ready`。**current upstream availability 优先于 historical-window coverage**。
- **H. Upper / Lower contract**：边界 inclusive（80 属 Upper、20 属 Lower）；80>20 故同一 Position 不可能同时属两者；`Upper + Lower` 不要求 =1；两者可同时为 0 且 Persistence 仍 `ready`（如全部 Position 为 50）；**不新增 Middle Occupancy**。
- **I. Metadata**：至少输出 `window_size=20`、`minimum_valid_count=15`、`candidate_count`（≤20）、`valid_count`、`coverage = valid_count/window_size`（即 `valid_count/20`）、`upper_count`、`lower_count`、`upper_occupancy`、`lower_occupancy`、`status`；coverage denominator 是 target window_size=20，**不是 candidate_count**（series 开头不足 20 observations 时不得伪报 100% coverage）。
- **J. No Future Leakage**：`Persistence(T)` 只读取 `<= T`；T+1/T+2 不得改变 `Persistence(T)` 的任何输出。
- **K. Status vocabulary**：只复用 `ready` / `insufficient_history` / `unavailable_current`；不得新增 `partial` / `low_coverage` / `warming` / `stale` / `gap` / `paused`。

同步新增 **Deterministic examples（FROZEN）Case A–G**：A）20/20 valid、5 upper、5 lower → ready、Upper=0.25、Lower=0.25；B）16 valid、16 upper、4 historical missing、T ready → ready、Upper=1.0、Lower=0、coverage=0.80；C）14 valid、T ready → insufficient_history、Upper/Lower=null；D）19 valid 但 `Position(T)=unavailable_current` → unavailable_current、Upper/Lower=null；E）19 valid 但 `Position(T)=insufficient_history` → insufficient_history；F）20 valid 全在 20~80 → ready、Upper=0、Lower=0；G）series 开头仅 10 observations → candidate_count=10、window_size=20、coverage≤0.5（不伪报 candidate_count=20）。

### B. `docs/prd/70-review.md` §7.7.5 Analysis Window 状态同步

- Persistence numerical / availability contract 已冻结于 §7.9（Persistence Numerical Contract，FROZEN）。
- **Dynamics Phase / Leadership / Interpretation thresholds** 仍保持 **IMPLEMENTATION DESIGN REQUIRED**；未标成 ready（严格只更新 Persistence 对应状态）。

### C. `docs/prd/70-review.md` §7.9 Implementation Ownership 同步

- Persistence 明确属于 **Analysis B pure domain owner**；下一轮扩展 `app/domain/review/analysis/historical_dynamics.py`（或按最终 module ownership）。
- **Persistence 直接消费 Position series，不是 Velocity / Signal / Acceleration**；与 Velocity / Signal / Acceleration 为 Historical Dynamics 同层派生结果。
- 模块不访问 DB、不 reconstruction、不 membership、不持久化、不 API、不 Interpretation。

## 明确未修改（产品语义保留）

- **Position**：默认历史窗口 120 trading days / 最低有效历史 60 observations / baseline strictly pre-T 不变。
- **EMA / Velocity / Signal / Acceleration**：全部 FROZEN 数值合同（alpha=2/(span+1)、recursive、first-valid seed、warmup、valid-observation clock、state-preserve missing、gap 不 reset、公式、No Future Leakage、Availability precedence、status propagation）**未改变**。
- **产品公式**：`Persistence = 20D Historical Position Occupancy`、`Upper Occupancy = 最近20日 Position >= 80 的占比`、`Lower Occupancy = 最近20日 Position <= 20 的占比` 保留。
- **Dynamics Phase / Internal Structure / Trading Context / Leadership** 未修改。
- 未新增 Middle Occupancy 作为 v2.3 product fact。

## 验证

- `git diff --check`：EXIT=0（无空白错误）。
- 重新读取修改后的 §7.7.5 / §7.9，逐项确认以下歧义均已消除：window clock（20 trading observations 含 T）、current inclusion（含 Position(T)）、denominator（valid_count，非固定 20）、minimum valid（exact 15）、missing handling（占 slot、排除于分子/分母）、current status precedence（unavailable > insufficient > coverage）、upper/lower inclusivity（>=80、<=20）、metadata（window_size/minimum_valid_count/candidate_count/valid_count/coverage/upper_count/lower_count/upper_occupancy/lower_occupancy/status）、No Future Leakage、status vocabulary。
- Deterministic examples Case A–G 可由 PRD 文字唯一推导。
- 仅修改 `docs/prd/70-review.md`、新增本 CHANGE、更新 CHANGE INDEX；未修改任何 Python / Tests / DB / Migration / API / Frontend / Maps / Runbooks / 治理。

## 下一步

- **下一轮 implementation**：按 §7.9 Persistence Numerical Contract 扩展 `historical_dynamics.py` 实现 Persistence（直接消费 Position series），并新增相应 unit tests（含 Case A–G deterministic examples、fail-fast contract violation、No Future Leakage）。
