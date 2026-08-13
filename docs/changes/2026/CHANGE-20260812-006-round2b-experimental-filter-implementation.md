# CHANGE-20260812-006 — Round 2B Experimental Filter Implementation

## 1. 概要

实现 L2-A Objective Evidence → Experimental Filter Evaluation 的纯评估层（ROUND 2B-1
PRD CLOSURE = PASS 前置）。CandidateResult 仅是 Experimental Filter 的 shadow /
exploration result，**不新增永久 Candidate domain layer / repository / table / lifecycle**。

- 业务目标：回答「这个 Scope 的 Evidence 是否命中了一个值得进一步检查的实验 pattern？」
  —— 不是推荐 / 机会 / Signal / Discovery / 强弱排名。
- 前置 PRD：`docs/prd/70-review.md §8.0 Experimental Filter Redesign Contract`（dev
  `73f6a0d`）。
- 本轮 frozen SHA：`REVIEW_EXPERIMENTAL_FILTER_ROUND2B_SHA`（见 §9）。

## 2. 新增文件

| 文件 | 职责 |
|---|---|
| `backend/app/domain/review/experimental_filter.py` | PURE domain owner。消费 `compute_scope_evidence()` 返回的 Evidence dict，输出 `CandidateResult` dict。不读 DB / 不读 bars / 不重算 Observation/Evidence / 不写 DB。不 import 任何 legacy `P/Q/U/C/V` payload。 |
| `backend/app/services/review_experimental_filter_service.py` | 薄 service。调 `compute_scope_evidence()` 再评 Experimental Filter，返回结果列表。不写 DB、不 save Candidate/Signal/Discovery、不调 legacy Filter、不读 legacy P/Q/U/C/V。 |
| `backend/tests/test_review_experimental_filter.py` | 21 项 modified-scope pure/unit tests（覆盖 §19 A–R）。 |

未新增 `candidate_repository.py` / `rule_engine_framework.py` / `filter_registry_v2.py`
/ `rule_dsl.py` 等架构文件（prompt §3）。

## 3. CandidateResult 形状（最小输出，prompt §5）

```
scope, trade_date, experiment_id,
evaluation_status: evaluable | not_evaluable,
matched: true | false,
conditions: [ { condition_id, primitive, horizon, mandatory,
               status: matched | not_matched | unavailable, evidence: {delta} } ],
supporting_evidence,                 # historical/peer percentile，只读透出，不阻塞
diagnostics: { mandatory_missing, optional_missing }
```

**禁止字段**：score / rank / grade / strength / confidence / recommendation /
opportunity / risk / bullish / bearish（test M 断言）。

## 4. EXPERIMENT_CONFIG_V0（prompt §7 / §8）

`ExperimentConfig(version="EXPERIMENT_CONFIG_V0", operator="gt", boundary=0.0)`。
- 仅用于 pipeline / real-data experiment；不声称 optimal / canonical / production
  threshold。
- `delta > 0` = current 高于该 historical anchor（最低可解释边界）。未引入 0.05 /
  80 percentile / 1.2x 等未经验证参数。
- 不修改 `backend/config/review_filters.yaml`，不接入 legacy `FilterDefinition` /
  `FilterCondition` 作为新 Experimental Filter runtime owner。

`operator="ge", boundary=0.0` 仅在 test K 内用于演示「同 Evidence + 不同 V0 config →
不同结果」，非默认生产语义。

## 5. Phase-1 两个 archetype（prompt §10 / §11）

### BREADTH_EXPANSION
发现 Trend breadth 与 Price breadth 相对 historical anchors 同时扩张。
- mandatory：`trend_up_ratio.d1.delta`、`trend_up_ratio.d3.delta`、
  `price_advance_ratio.d1.delta`、`price_advance_ratio.d3.delta` 全部 available 且
  满足 V0 min delta → MATCHED。
- 任一 mandatory available 但不满足 → NOT_MATCHED；任一 mandatory unavailable →
  NOT_EVALUABLE。
- D5（`trend_up_ratio.d5.delta`、`price_advance_ratio.d5.delta`）为 optional supporting，
  缺失不导致 NOT_EVALUABLE。
- 不声称「连续5日单调扩张」；只说 current 相对 D1/D3/D5 anchors 的 breadth expansion evidence。

### PARTICIPATION_CONFIRMATION
成交参与变化是否与价格广度变化形成同步确认。
- mandatory：`participation_volume_p50.d1.delta`、`participation_volume_p50.d3.delta`、
  `price_advance_ratio.d1.delta`、`price_advance_ratio.d3.delta`。
- `trend_up_ratio.d1/d3` 为 optional confirmation；D5 optional。
- 不把 volume participation 高直接解释成资金流入 / 机会 / 强势。

## 6. 三态缺失语义（prompt §6）

- mandatory Evidence unavailable → **NOT_EVALUABLE**（不是 NOT_MATCHED）。
- mandatory available 但 condition 不满足 → NOT_MATCHED。
- 全部 mandatory 满足 → MATCHED。
- optional unavailable → 仍 evaluable。
- missing **永远不能当 0**（test Q；`_get_delta` 对 status!=ready / 非 finite / 缺字段
  一律返回 None，绝不 fallback 0）。

## 7. Horizon contract（prompt §9）

- Phase-1：D1/D3 mandatory；D5 optional supporting。
- 所有 condition 显式读 `primitive["d1"]["delta"]` / `["d3"]["delta"]` / `["d5"]["delta"]`；
  禁止比较 "d1"/"d3"/"d5" 整个 dict；禁止用 `reference_value` 冒充 delta。

## 8. 未进入本轮（prompt §12–§15）

- Historical percentile：Phase-1 不作 mandatory；`insufficient_history` 不阻塞 evaluable/
  matched（已存在于 Evidence `historical.status`，只读透出到 supporting_evidence）。不制造历史，
  不改 min60。
- Peer percentile：两 archetype 不要求 peer 作 mandatory；可附 supporting context，非成立条件。
  真实 replay 不为 Candidate 强制生成完整 peer cohort。
- Concentration（`price_raw_hhi`）：完全 DEFER，不进入上述两 archetype，不新增
  PRICE_CONCENTRATION_DIVERGENCE，不实现 normalized HHI。
- Momentum / Return（`momentum_expanding_ratio` / `price_return_mean`）：不作 mandatory
  condition / 独立 archetype。

## 9. 范围、隔离与验证状态

- Scope activation（prompt §17）：Phase-1 `concept` / `industry_l1`；`industry_l2` /
  `industry_l3` 仅架构兼容 / smoke；`market` / `major_index` / `style` 不激活（test O）。
- Legacy isolation（prompt §18）：未修改 `filter_definitions.py` / `filter_engine.py` /
  `review_signal_service.py` / `discovery.py` / publication / API / Frontend；未 import
  legacy P/Q/U/C/V 作为 Experimental Filter input（test N）。
- Persistence（prompt §25）：NO NEW TABLE / NO migration / NO Candidate persistence /
  NO Signal persistence。replay 结果只写 `/tmp`。
- **本轮 frozen SHA**：`REVIEW_EXPERIMENTAL_FILTER_ROUND2B_SHA` = 提交后由 git 记录。
- 真实 bz_stock 实际 replay（prompt §21–§24）：脚本已备于 `/tmp/replay_round2b.py`
  （READ ONLY），需在 panji-prod 验证运行时对 frozen SHA 执行；本地因禁止连本地/Docker
  PG（AGENTS.md §8 / Map 80），real-data replay **尚未在本轮本地执行**，状态为
  `pending_remote_replay`（不在本地伪造运行证据）。

## 10. 本地质量门（prompt §26）

| 门 | 结果 |
|---|---|
| PURE_UNIT_TEST 新测试 | 21 passed（postgres=0） |
| 回归 Round 2A evidence | 24 passed 无回归 |
| 回归 Round 1 L1 / review observation | 90 passed 无回归 |
| ruff（3 个 changed 文件） | PASS |
| mypy（changed 文件） | 0 个新错误（repo 既有 pandas-stub 报错与本轮无关） |
| compileall（changed 文件） | PASS |
| governance check | PASS（exit 0） |

## 11. 状态

`implemented_unconfirmed`（本地 pure/unit + 回归 + ruff/mypy/compileall/governance PASS；
remote real-data replay pending_remote_replay，待外部审计授权后执行；Maps 待实现验收后
单独授权同步；PRD §8.0 不修改）。

## 12. 关联

- PRD：`docs/prd/70-review.md` §8.0（前置 ROUND 2B-1 = PASS，SHA `73f6a0d`）
- Maps：`docs/maps/70-review.md`（未修改）
- 前置：`CHANGE-20260812-004`（L2-A Objective Evidence）、`CHANGE-20260812-005`（§8.0 收口）
