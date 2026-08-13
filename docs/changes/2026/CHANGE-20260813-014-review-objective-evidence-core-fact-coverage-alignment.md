# CHANGE-20260813-014 — Review Objective Evidence Core Fact Coverage Alignment

## 元信息

- **日期**：2026-08-13
- **类型**：behavior + contract + architecture（Implementation Slice）
- **领域**：复盘模块 / Objective Evidence（L2-A）/ PRD 70-review.md（§7.1/§7.2/§7.3/§7.4/§7.6/§7.7/§7.8.5/§7.9）
- **状态**：`verified_code`（PURE_UNIT 75 passed / 0 failed；ruff / compileall PASS；无 migration、未扩 L1、未进 Discovery/Filter/Signal、未改 API/前端）
- **baseline SHA**：`ce01d3cfabaf06421e55923dc85aff57f81c7737`
- **final SHA**：`b68003b90ec0b7b6e84cc3dd591ae2b7f8357a83`
- **关联 PRD**：`docs/prd/70-review.md`（§7.1/§7.2/§7.3/§7.4/§7.6/§7.7/§7.8.5/§7.9）
- **关联 Maps**：`docs/maps/70-review.md`（未修改；L2 实现已 cutover 但 Maps 同步需用户验收后授权）

## 一、背景与本轮边界

第四阶段 A 的目标：把已由 L1 Canonical Observation 正式计算并保存的 **CORE scope-level numeric facts**，完整接入现有 Objective Evidence 的 6 类 Context：

- Current
- D1
- D3
- D5
- Historical Position
- Peer Position

**本轮不是重新设计 Evidence Engine**。Percentile primitive（`percentile_rank`）、D1/D3/D5 exact trading-date resolution、historical >= 60 gate、`peer percentile`、`exact trading-date resolution` 这些框架在 Round 2A（CHANGE-004）已建立并验证，**继续复用，不重写**。

本轮只解决一个明确的 Implementation Gap：

> L1 已经有很多正式事实，但 L2 当前只消费 6 个。

因此本轮把 `PRIMITIVE_PATHS` 从 6 个标量提取路径扩充为 **29 个 CORE scalar extraction paths**，并修正 Peer Scope Contract 与 raw HHI peer 禁用逻辑。

### 明确不处理（本轮边界）

1. Transition
2. member-level amount_share
3. Signed Return Contribution
4. CHIP
5. P/Q/U/C/V
6. Filter
7. Signal
8. Discovery
9. ranking / score / strong / weak
10. API / Frontend

Transition 本身是 T-1→T change fact，是否还需要 D1/D3/D5 / Historical / Peer 需要单独的 4B 语义判断，不在本轮。amount_share 是 member-level fact，不属于当前 Scope Evidence scalar mapping。Signed Return Contribution 仍 `PRD_CLARIFICATION_REQUIRED`。

## 二、核心修改 1 — L2 基础事实映射扩充

文件：`backend/app/domain/review/scope_evidence.py`

`PRIMITIVE_PATHS` 由 6 项扩充为 29 项 CORE scalar extraction paths，覆盖：

- **PRICE — Return Level / Distribution**：`price_return_mean/median/p25/p75`
- **PRICE — Breadth**：`price_advance_ratio/decline_ratio/unchanged_ratio`
- **PRICE — Concentration**：`price_raw_hhi/price_normalized_hhi`
- **PRICE → Amount Concentration**：`amount_raw_hhi/amount_normalized_hhi`
- **TREND — complete State/Breadth**：`trend_up_ratio/neutral_ratio/down_ratio`
- **STRUCTURE — Swing State/Breadth**：`structure_swing_up/neutral/down_ratio`
- **STRUCTURE — Internal State/Breadth**：`structure_internal_up/neutral/down_ratio`
- **MOMENTUM — complete State/Breadth**：`momentum_expanding/flat/contracting_ratio`
- **PARTICIPATION — Volume Distribution**：`participation_volume_p25/p50/p75`
- **PARTICIPATION — Amount Distribution**：`participation_amount_p25/p50/p75`

`extract_primitive()` 基本工作方式不变（显式 closed mapping，无 JSONPath/DSL）。

**未加入**（明确排除）：

- `price.return.p10` / `price.return.p90`：PRD 定义为 EXPLANATORY tail，非 CORE Evidence coverage 必要项。
- `price.amount.total_amount`：scope aggregate/supporting fact，跨 Scope member 数量不同，跨 Scope comparison 语义未冻结。
- 任何 `count` / `denominator` / `valid_count`：属于 readiness/diagnostics，不是横截面排名业务值。

## 三、核心修改 2 — raw HHI peer 禁用覆盖 Price 与 Amount

文件：`backend/app/domain/review/scope_evidence.py` + `backend/app/services/scope_evidence_service.py`

原代码仅特殊处理 `primitive == "price_raw_hhi"`。改为显式 metadata：

```python
PEER_DISABLED_REASON_BY_PRIMITIVE: dict[str, str] = {
    "price_raw_hhi": "raw_hhi_not_cross_scope_comparable",
    "amount_raw_hhi": "raw_hhi_not_cross_scope_comparable",
}
```

`_compute_primitive()` 改为：

```python
disabled_reason = scope_evidence.PEER_DISABLED_REASON_BY_PRIMITIVE.get(primitive)
peer_values = [v for p in peer_facts if (v := _extract_finite(p.observation_payload, primitive)) is not None]
out["peer"] = scope_evidence.build_peer_context(current, peer_values, disabled_reason=disabled_reason)
```

结果：

- `price_raw_hhi` / `amount_raw_hhi` peer = unavailable（`reason = raw_hhi_not_cross_scope_comparable`）；historical 仍可正常计算。
- `price_normalized_hhi` / `amount_normalized_hhi` 可正常做 same-family peer percentile（不再被禁）。

## 四、核心修改 3 — Peer Scope Contract 修正

文件：`backend/app/services/scope_evidence_service.py`

原 `PEER_SCOPE_TYPES = frozenset({"concept","industry_l1","industry_l2","industry_l3"})` + “不在集合里一律返回 `market_has_no_peer_cohort`” 语义不准确，违反 PRD §7.8.5。

改为显式 Scope contract：

```python
PEER_COHORT_SCOPE_TYPE: dict[str, str | None] = {
    "market": None,
    "major_index": "major_index",
    "style": "style",
    "industry_l1": "industry_l1",
    "industry_l2": "industry_l2",
    "industry_l3": "industry_l3",
    "concept": "concept",
}

def _resolve_peer_scope_type(scope_type: str) -> str | None:
    try:
        return PEER_COHORT_SCOPE_TYPE[scope_type]
    except KeyError as exc:
        raise ValueError(f"unsupported scope_type for evidence: {scope_type}") from exc
```

`compute_scope_evidence()` 内：

```python
peer_scope_type = _resolve_peer_scope_type(scope_type)
peer_facts = [] if peer_scope_type is None else await list_scope_observation_facts(
    session, scope_type=peer_scope_type, from_date=trade_date, to_date=trade_date,
)
```

`_compute_primitive()` 接收显式 `peer_scope_type`：

- `peer_scope_type is None`（market）→ `peer.status = unavailable, reason = no_cross_sectional_peer, peer_count = 0`
- 其他 family → 构造 `peer_values`；若当前无 persisted peer facts（如 major_index/style 尚未 activation），自然得到 `status = unavailable, peer_count = 0`，但**原因不是** `no_cross_sectional_peer`（它们架构上是有 peer cohort 的）。

本轮**不为** major_index/style 制造假数据、不 fallback 到 concept/industry、不用当前 market universe 代替、不改 activation contract。

## 五、未重写 Engine

以下函数保持原逻辑：`percentile_rank()`、`compute_delta()`、`build_current_context()`、`build_delta_context()`、`build_historical_context()`、`build_peer_context()`、`_nth_previous_trading_day()`。未引入 EvidenceRegistry / Plugin framework / JSONPath DSL / generic expression engine / score framework。

## 六、Transition 本轮明确不接

PRD Transition 是 member exact T-1→T state migration，本身已是 change fact（如 Neutral→Up ratio = 0.18）。本轮不擅自定义 Transition D1/D3/D5/Historical/Peer percentile，也不为静态 PRIMITIVE_PATHS 重构 dynamic transition schema。留到 **第四阶段 B — Transition / Peer 语义收口**。

## 七、测试（test_review_scope_evidence.py）

- `_payload()` fixture 重写为完整 CORE L1 payload（含 `overrides` 微小 helper，非第二套 production schema）。
- **Test 1 `test_core_evidence_primitive_coverage`**：冻结 29 个 scalar extraction paths 集合。
- **Test 2 `test_all_paths_extract_from_canonical_payload`**：29 个路径全部非 None。
- **Test 3 `test_full_state_breadth_has_d1`**：`trend_neutral/down`、`structure_swing_up`、`structure_internal_down`、`momentum_flat/contracting` 均解析 D1。
- **Test 4 `test_participation_amount_in_l2`**：participation amount p25/p50/p75 生成 current/d1/d3/d5/historical/peer。
- **Test 5 `test_normalized_hhi_peer_comparable`**：同日 concept peers（A=0.10, B=0.20, C=0.30）下，price/amount normalized_hhi peer 均 ready、peer_count=3、percentile 正常。
- **Test 6 `test_raw_hhi_peer_disabled_both_price_amount`**：price/amount raw_hhi peer 均 unavailable + reason 正确；historical 仍计算。
- **Test 7 `test_market_has_no_peer`**：market → peer unavailable + reason=no_cross_sectional_peer。
- **Test 8 `test_major_index_and_style_architecture_support`**：major_index/style mock 同 family peer facts 可正常计算（不依赖生产 DB）。
- **Test 9 `test_cross_family_isolation_all_families`**：concept/industry_l1/l2/l3/style 各只查询各自 family。
- **Test 10 `test_no_subjective_fields_in_primitives`**：输出无 score/rank/grade/opportunity/risk/strong/weak/filter/signal/discovery。

既有 6 个 Evidence 行为（exact D1/D3/D5、missing 不 fallback、historical<60、current excluded、same-family peer、raw HHI peer disabled、bool/NaN/inf、L1 payload 不可变）全部继续 PASS，数学与 gate 行为未改。

## 八、性能边界

未为 29 个 primitives 发 29 组 DB query。架构不变：current fact 1 次、D1/D3/D5 固定次数、history 1 次、same-day peers 1 次，Python 内对不同 primitive 提取。query 次数不随 primitive 数量增长。

## 九、未修改

- `scope_observation.py`、`review_observation_persistence_service.py`、`member_fact.py`
- DB schema / migration / API / frontend
- Filter / Signal / Discovery
- legacy P/Q/U/C/V
- `scripts/verify/*`（现有 tests 未证明 verify infrastructure 回归，本轮不碰）

## 十、验证结果

- `ruff check`：All checks passed.
- `compileall`：OK.
- `PURE_UNIT_TEST=1 pytest test_review_scope_evidence.py test_review_scope_observation.py`：**75 passed / 0 failed**（原 41 + 新 34）。
- 本轮无 DB schema/persistence 变化，默认不重跑 L1 persistence targeted-pg（无新增 DB query shape，mock/unit 已覆盖）。
