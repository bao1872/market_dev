# CHANGE-20260812-004 — Round 2A Objective Evidence Engine Implementation

- **类型**：behavior+architecture（Objective Evidence Engine；query-time derived，无新表、无 migration）
- **领域**：复盘模块 / Canonical Scope Observation Facts → Objective Evidence（L2-A）
- **状态**：`verified_code_pending_acceptance`（本地 pure/unit 22、L1 74 无回归、ruff、mypy-changed、compileall、governance 均通过；remote isolated verification + PG contract 4 passed + real-data short-window / historical / peer 验证全部通过。待用户产品/理论验收后再收口）
- **关联 PRD**：`docs/prd/70-review.md`（§7.6 min-sample=60 / §7.9 Canonical Facts；本轮不新增主观产品语义）
- **关联 Maps**：`docs/maps/70-review.md`（未修改；待实现验收后单独授权同步）
- **关联 Rules**：无（本轮不修改治理；AGENTS.md / rules/* / governance checker 均未改动，governance check PASS）
- **No new table / No migration / No cache table / No API / No frontend / No Filter / Discovery / Publication / Round 2B**

## 1. 背景

Round 1C（CHANGE-20260812-003）已 CLOSED：`review_scope_observation_facts` 持久化每日
canonical objective facts。本轮 **Round 2A** 在 L1 facts 之上派生 Objective Evidence：

> “今天的这些客观事实，和自己的过去相比、和今天同类 Scope 相比，
> 处在什么位置、发生了什么变化？”

仍然只允许 objective evidence；禁止 Opportunity / Risk / Strong / Weak / Candidate / Filter /
Discovery / Ranking / Score / Grade / Recommendation。

## 2. 变更内容

### 2.1 新增 pure domain module `backend/app/domain/review/scope_evidence.py`

- `percentile_rank(value, samples) -> float | None`：neutral 0..100 percentile rank；过滤
  None/NaN/inf；empty → None；deterministic ties；无 direction / weight / negative inversion /
  score normalization（prompt §7）。只提取 repo 既有 cross-sectional rank 的纯数学语义
  （`below_or_equal / n * 100`），**不 import legacy `_normalize_component` / P/Q/U/C/V**。
- `PRIMITIVE_PATHS`：6 个 Phase-1 primitive 的显式 path mapping（prompt §10 / §14）：
  `price_return_mean → price.return.mean`、`price_advance_ratio → price.breadth.advance_ratio`、
  `trend_up_ratio → trend.state.up_ratio`、`momentum_expanding_ratio → momentum.state.expanding_ratio`、
  `participation_volume_p50 → participation.volume.p50`、`price_raw_hhi → price.concentration.raw_hhi`。
- `extract_primitive(payload, primitive) -> float | None`：只接受 finite int/float；**bool 拒绝**
  （prompt §11）；None/NaN/inf → None（不 NULL→0）。
- `compute_delta(current, reference)`：`current - reference` 原生单位，无 improving/deteriorating 标签。
- context builders：`build_current_context` / `build_delta_context` / `build_historical_context`
  / `build_peer_context`，每个 context 独立 status（ready / unavailable / insufficient_history），
  无 overall Evidence Score / overall readiness。
- `HISTORICAL_MIN_SAMPLE = 60`（PRD §7.6）：历史有效样本 < 60 → `insufficient_history`，percentile=None，
  不得用 peer percentile 替代。
- `RAW_HHI_PEER_DISABLED_REASON = "raw_hhi_not_cross_scope_comparable"`：raw HHI 禁 peer position
  （prompt §15）。

### 2.2 新增薄 service `backend/app/services/scope_evidence_service.py`

- `compute_scope_evidence(session, trade_date, scope_type, scope_key) -> dict`。
- 职责仅：读 L1 fact（`get_scope_observation_fact`）；`_nth_previous_trading_day` 复用
  `calendar_service.get_previous_trading_day_async` 迭代解析 exact D1/D3/D5（**禁止**
  nearest/latest/calendar-day subtraction/fallback）；读历史 facts（same scope, trade_date < T）；
  读 same-family peer facts（same trade_date, same scope_type）；调 pure calc；返回 dict。
- peer cohort：`concept/industry_l1/industry_l2/industry_l3` same-family only；market → no peer
  （prompt §14）。不做 peer registry。
- **不写回 L1 facts，不读 legacy p/q/u/c/v payload**（prompt §2 / §4）。
- 输出 shape 按 prompt §19（scope / trade_date / primitives{ primitive: { current, d1, d3, d5,
  historical, peer } }），每 context 独立 status。

## 3. 不改动 Round 1C / L1 Core

- 未修改 `scope_observation.py`（不改 `_percentile` quantile-value helper，prompt §6）。
- 未修改 `review_observation_persistence_service.py` / `review_observation_prep_service.py` /
  `observation_prep.py` / `market_review.py` / migration。
- 未新增 Evidence 表 / migration / cache（prompt §3，NO NEW TABLE）。
- 未实现 Filter / threshold / Candidate / Opportunity / Risk / Discovery / Ranking / Score /
  Tracking / Publication / API / Frontend（prompt §29）。

## 4. 本地验证

- 新增 `tests/test_review_scope_evidence.py`（22 tests，prompt §20 A–R）：percentile_rank basic/
  ties/NaN-inf、primitive extraction、bool rejection、delta、D1/D3/D5 exact、missing→unavailable
  no fallback、historical <60 / >=60、current excluded、same-family peer、cross-family isolation、
  raw HHI peer disabled、no subjective keys、input not mutated。
- 新增 `tests/test_review_scope_evidence_pg.py`（targeted PG，prompt §21）：evidence 不写行、
  exact D1/D3/D5 query、missing exact date、peer same-family query、raw HHI peer disabled。
- L1 regression：`test_review_scope_observation.py` / `test_review_observation_prep.py` /
  `test_review_observation_persistence.py` 74 passed（无回归）。
- ruff：All checks passed；compileall OK；mypy modified scope 无新增错误（既有 baseline 在
  未修改文件）；governance check PASS。

## 5. 遗留 / Deferred

- Diffusion 保持 PROVISIONAL：仅 D1/D3/D5 breadth delta 作为客观 Evidence，不新增 Diffusion
  Score/State/Label/persistence（prompt §18）。
- Round 2B（threshold / Filter / Candidate / Opportunity / Discovery / ranking / score）不进入本轮；
  只在路线图标记其输入接口。
- Historical window 不硬编码 120-day product window（prompt §8）：读取该 Scope 当前可获得的所有
  历史 canonical facts，按 min60 门槛输出 `insufficient_history` / `ready`。

## 6. 外部验证（远程 isolated verification + real-data）

在 frozen SHA `3aa82840badf1e1eaecfb24098b8a54ebc29fe4e` 的 isolated verification DB
（`bz_stock_verify_3aa82840...`，跑在既有 `trading-postgres` 容器内，不写 production `bz_stock`）
完成。验证结果全部通过：

- **PG query contract**：`tests/test_review_scope_evidence_pg.py` 4 passed（evidence 不写行、
  exact D1/D3/D5、missing exact date→unavailable no fallback、peer same-family isolation、
  raw HHI peer disabled）。
- **Short window（§25）**：2 concept + 2 industry_l1，T0=2026-08-10。全部 `current_status=ready`，
  D1/D3/D5 `ready`，D1 reference_date=2026-08-07，delta 按原生单位计算（含 0 delta 与正值）。
- **Historical <60（§26）**：真实数据仅 6 个交易日，sample_count=5 → `insufficient_history`，
  percentile=null（真实数据不足以触发 ≥60 分支，如实保留）。
- **Historical ≥60（§26）**：真实数据仅 6 天，无法天然到达 min60；在**隔离验证库**写入 65 个
  synthetic canonical facts（`concept/synth_min60_*`）演示 ≥60 分支：`historical_status=ready`，
  sample_count=65，percentile=56.92。真实 ≥60 为 `DATA_BLOCKED`（数据不足，非实现缺陷）。
- **Peer cohort（§27）**：concept 389/389 facts generated（full_cohort_verified=true，
  peer_count=390，percentile=82.56）；industry_l1 257/257（peer_count=257，percentile=68.87）。
  仅 same-family 入 cohort，market 无 peer cohort。

**数据可用性记录**：`review_scope_observation_facts` 在真实 `bz_stock` 中不存在（L1 facts 仅写入
验证库）；短期真实数据受 PIT membership 可用性限制（08-03..08-10 共 6 个交易日）。CHANGE 状态保持
`verified_code_pending_acceptance`，待用户做产品/理论验收。

## 7. 清理

验证结束已删除本轮创建的 verify 库与临时文件（见 §8 清理执行）。未触碰 production `bz_stock`、
共享 PostgreSQL/Redis 卷、稳定运行容器或受保护镜像。
