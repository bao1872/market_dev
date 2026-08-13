# CHANGE-20260813-015 — Review Transition Objective Evidence Contract + Implementation

## 元信息

- **日期**：2026-08-13
- **类型**：behavior + contract + architecture（Implementation Slice）
- **领域**：复盘模块 / Objective Evidence（L2-A）/ PRD 70-review.md（§7.3 / §7.7 / §7.8.5 / §7.9）
- **状态**：`verified_code`（PURE_UNIT 87 passed / 0 failed；ruff / compileall PASS；无 migration、未改 L1、未进 Discovery/Filter/Signal、未改 API/前端）
- **baseline SHA**：`d95410bac7252e6f4a83261d1fa777d586a45372`
- **final SHA**：（待 commit 后填入）
- **关联 PRD**：`docs/prd/70-review.md`（§7.3 追加 Transition→Evidence 合同；§7.7 / §7.8.5 / §7.9）
- **关联 Maps**：`docs/maps/70-review.md`（未修改；L2 实现已 cutover 但 Maps 同步需用户验收后授权）
- **关联 CHANGE**：CHANGE-013（L1 Code Alignment + PG closure）、CHANGE-014（4A CORE fact coverage，29 scalar）

## 一、背景与本轮边界

第四阶段 B 解决一个明确问题：

> L1 已经保存 Trend / Structure / Momentum 的 Transition，但 L2 Objective Evidence 目前完全没有消费这些 Transition。

本轮把 **Transition ratio** 正式接入 Objective Evidence 的 6 类 Context（Current / D1 / D3 / D5 / Historical Position / Peer Position）。

Transition 本身是 T-1→T 的变化事实，但「今天的迁移比例与昨天相比、与自己的历史相比、与同类 Scope 相比」仍然是客观比较事实，因此允许进入 Evidence。本轮**绝不产生** improving/deteriorating、expanding/contracting diffusion label、strong/weak、opportunity/risk、score、ranking、Filter/Signal/Discovery。

## 二、PRD 合同澄清（docs/prd/70-review.md §7.3）

在 §7.3 Transition 说明中追加最小合同：

1. L1 Transition 不变（`member exact canonical T-1 → T categorical migration`），跨 Scope 主表达为 transition ratio；raw count 仅用于解释 / audit，不是跨 Scope 比较基础事实。
2. L2 对每个**合法 transition ratio** 计算 Current / D1 / D3 / D5 / Historical / Peer；D1/D3/D5 仅连续数值变化，不得命名 acceleration / diffusion / improving。
3. 合法 Transition 集合：方向三状态 / Momentum 三状态，仅保留有方向变化的 6 项；Trend / Structure Swing / Structure Internal 各 6 项，Momentum 6 项；共 **18 + 6 = 24** 个 transition ratio primitive。
4. **zero-transition 编码规则（冻结）**：L1 sparse encoding（仅实际发生的非同态迁移产生 key，容器含 `denominator`）。
   - A. `denominator > 0` 且 key 不存在 → `count = 0`、`ratio = 0.0`（零成员发生该迁移，**不是**「没有数据」）。
   - B. `denominator <= 0` 或 denominator unavailable → `ratio = unavailable / None`。
5. **Stable state 不属于 Transition primitive**：`Up→Up` / `Neutral→Neutral` / `Down→Down`（Momentum 同理）已体现在 denominator 中，但**不是 transition event**，不新增 stable ratio；transition **count 不进入** Evidence primitive。

## 三、核心修改 1 — Transition primitive contract（scope_evidence.py）

保留现有 `PRIMITIVE_PATHS`（29 个普通 scalar paths）不变。新增：

```python
TRANSITION_PRIMITIVE_SPECS: dict[str, tuple[tuple[str, ...], str]] = {
    # TREND (6) / STRUCTURE SWING (6) / STRUCTURE INTERNAL (6) / MOMENTUM (6)
    "trend_transition_neutral_to_up_ratio": (("trend", "transition"), "Neutral→Up"),
    ...
}
```

共 **24** 项，明确 frozen。不把 dynamic transition key 硬塞进 `PRIMITIVE_PATHS`。

## 四、核心修改 2 — Transition ratio 唯一提取函数（scope_evidence.py）

新增 `_extract_transition_ratio(observation_payload, container_path, transition_key)`：

- walk `container_path`；缺失容器 → None（unavailable）；
- `denominator` absent / 非有限 / `<= 0` → None（unavailable，规则 B）；
- transition_key absent 但 denominator > 0 → `0.0`（规则 A，sparse 编码零迁移）；
- transition_key 存在但畸形 / ratio absent / 非有限 → None；
- ratio 越界 [0,1] → None。

L2 **从不**根据 count/denominator 重算 ratio，只解释 L1 正式 sparse encoding。

## 五、核心修改 3 — extract_primitive 同时支持两类事实（scope_evidence.py）

`extract_primitive(observation_payload, primitive)` 改为：

1. 先在 `PRIMITIVE_PATHS` 查 scalar path → 走原 scalar 提取；
2. 否则在 `TRANSITION_PRIMITIVE_SPECS` 查 → 走 `_extract_transition_ratio`；
3. 都不命中 → `KeyError("unknown evidence primitive: ...")`。

## 六、PRIMITIVE_NAMES 更新

```python
PRIMITIVE_NAMES: tuple[str, ...] = (
    *PRIMITIVE_PATHS.keys(),      # 29 CORE scalar
    *TRANSITION_PRIMITIVE_SPECS.keys(),  # 24 transition ratio
)
```

最终 **29 + 24 = 53**（deterministic 顺序）。53 是内部 numeric evidence extraction facts 数量，不是「53 个产品指标」、不是 UI 一级结构、不是 score。

## 七、Service 层

现有 `_compute_primitive()` **直接复用**，无特殊 Transition 分支、无第二个 transition evidence service。`extract_primitive()` 处理 sparse encoding 后，现有 Evidence pipeline（Current / D1 / D3 / D5 / Historical / Peer）自动支持 Transition，DB query 数不变（current 1 + D1/D3/D5 固定 + history 1 + peers 1）。

## 八、Peer 语义

24 个 Transition ratio 允许 same-family Peer（它们已是 `count / common-valid denominator` 比例，不是 raw count）。`PEER_DISABLED_REASON_BY_PRIMITIVE` 仅保留 `price_raw_hhi` / `amount_raw_hhi`；Transition count 不进入 `PRIMITIVE_NAMES`。

## 九、未修改

- `scope_observation.py`（L1 未改；sparse encoding 保留，L2 用规则 A/B 解释）。
- `review_observation_persistence_service.py` / `member_fact.py` / `scripts/verify/*`。
- DB schema / migration / API / frontend / Filter / Signal / Discovery / legacy P/Q/U/C/V。

## 十、测试（test_review_scope_evidence.py）

- `_payload()` fixture 四个 transition container 加入 `denominator=10` 与显式发生的 transition（含 Trend Neutral→Up=0.2 / Down→Up=0.1、Swing Up→Down=0.1、Internal Down→Neutral=0.3、Momentum Flat→Contracting=0.2）。
- **Test 1 `test_transition_primitive_contract`**：`len(TRANSITION_PRIMITIVE_SPECS)==24` + 冻结 24 名。
- **Test 2 `test_total_evidence_fact_count_is_53`**：`len(PRIMITIVE_NAMES)==53`（29 CORE ∪ 24 Transition）。
- **Test 3 `test_explicit_transition_ratio_extraction`**：Neutral→Up=0.2 提取正确。
- **Test 4 `test_legal_transition_absent_is_zero`**：合法 key 不存在 + denominator>0 → 0.0。
- **Test 5 `test_transition_denominator_zero_is_unavailable`**：denominator=0 → None（非 0）。
- **Test 6 `test_all_four_transition_families_extract`**：Trend/Swing/Internal/Momentum 均解码。
- **Test 7 `test_transition_d1_d3_d5_are_deltas_only`**：current=0.20、D1=0.10、D3=0.05、D5=0.15 → delta=0.10/0.15/0.05，仅 delta 无 improving/accelerating label。
- **Test 8 `test_transition_historical_ready`**：≥60 历史 sample → historical.status=ready、sample_count=60、percentile 有值。
- **Test 9 `test_transition_peer_percentile`**：concept A/B/C Neutral→Up=0.10/0.20/0.30 → B peer ready、peer_count=3、percentile 正常。
- **Test 10 `test_transition_count_not_a_primitive`**：无 `*_count` transition primitive。
- **Test 11 `test_no_stable_transition_primitive`**：无 stable identity transition primitive。
- **Test 12 `test_no_diffusion_state_in_output`**：输出无 diffusion/expanding_scope/contracting_scope/stable_scope。
- 4A 既有 `test_core_evidence_primitive_coverage` 更新为冻结 29 CORE scalar（`PRIMITIVE_PATHS`），不要求 PRIMITIVE_NAMES==29。

## 十一、验证结果

- `ruff check`：All checks passed.
- `compileall`：OK.
- `PURE_UNIT_TEST=1 pytest test_review_scope_evidence.py test_review_scope_observation.py`：**87 passed / 0 failed**（原 75 + 新 12）。
- 本轮无 DB schema/persistence 变化、无新增 DB query shape，默认不重跑 targeted-pg。

## 十二、下一阶段

Transition 已正式接入 L2 全部 6 类 Context；下一阶段可评估是否需 Discovery / Presentation layer 解释（仍不得反向创建新 Observation primitive）。
