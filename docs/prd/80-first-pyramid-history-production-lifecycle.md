# PRD 80 — First Pyramid History 生产生命周期契约

> 本文档只定义 **Review 上游 First Pyramid canonical history 的 producer 契约**。
> 它是 `docs/prd/70-review.md` 的**上游输入契约**，不修改、不重新定义任何现有 Review consumer contract（`70-review.md` 处于 Review Freeze，其 lineage/version 读取语义保持不变）。
> 本文档的权威事实来源依赖 Task 1–4 的只读审计证据（基线 `ecc2388ef736a42f89d9d2a4b1b74907cc806253`）。

## 0. 范围与动机

正常 AfterClose 已计算当日 canonical First Pyramid，并将其投影进 `StockFeatureSnapshot.summary_payload.first_pyramid`。
但生产链未把当日 canonical result materialize 到：

- `first_pyramid_history_daily_state`
- `first_pyramid_history_events`

生产 PG 只读审计（2026-08-21，基线 `ecc2388ef...253`）确认：

- 上述两表 `MAX(trade_date)` / `MAX(event_time)` 停在 **2026-08-10 / 2026-08-07**；
- `first_pyramid_history_runs` 在 2026-08-10 之后 **0 条新 run**；
- `first_pyramid_history_run_items` 在 2026-08-11～2026-08-20 窗口内 started/completed/created **全部为 0**；
- `advance_history_to_trade_date` 的全部 caller 仅为手动 CLI + 测试，无任何 scheduler / worker / AfterClose / admin endpoint 自动驱动；
- `first_pyramid_history_runs` 的唯一生产者是 `first_pyramid_history_backfill_cli.py`（语义=历史回补，非每日 production）。

结论（非算法问题，也不仅是"AfterClose 缺一行调用"）：

> **缺的是 First Pyramid canonical history 的 production lifecycle owner**：current-run resolution、membership reconciliation、daily advancement、明确 failure semantics。

本 PRD 收口该 owner 的**最小生产契约**。实现 slices 与 PRD 文本解耦，按用户既定 Phase 1–7 推进，不在此文档内展开。

## 1. Owner（Production Orchestration Owner）

**FP-HPO-01**：`AfterClose` 是 daily First Pyramid history advancement 的 orchestration owner。

- AfterClose 负责在 Review 前置阶段确保 canonical FP history 已推进到当日 `T`。
- Orchestrator（`after_close_orchestrator.py`）**只编排调用**，具体 lifecycle 逻辑保持在 `first_pyramid_history_service.py`。
- 不把 lifecycle 逻辑直接写进 orchestrator；orchestrator 调用 `ensure/reconcile → advance → validate` 三段。

## 2. Canonical Identity（禁止硬编码 UUID）

**FP-HPO-02**：`FirstPyramidHistoryRun` 是 **version / scope / contract 对应的 canonical dataset lineage identity**。

- 不允许硬编码任何 production run id（代码已明确禁止硬编码 `be56dcd2...`）。
- `create_history_run` 的 lookup key = `algorithm_version + parameter_hash + scope`（`parameter_hash` 含 `HISTORY_CONTRACT_VERSION`），幂等复用；trade_date / time-window / status / universe snapshot **不进入 identity**。
- 同 key 今日/明日调用返回**同一 run**；algorithm / hash / contract 变化 → 旧 run 不匹配 → 创建新 run。
- **Canonical compatibility invariant（硬）**：`parameter_hash` 必须涵盖所有影响 canonical First Pyramid history semantics 的 history contract 维度（`HISTORY_CONTRACT_VERSION` 及其后续相关参数）。Producer 不得在任何 contract 变化后仍把 target-T materialization 解析/写入旧 canonical run。若有人修改 `parameter_hash` 生成逻辑但遗漏了 contract 维度，导致 `contract V2 → V3` 而 hash 未变，结果将是 producer 继续往 R1 写 → 破坏 lineage——这是禁止的。任何影响 canonical history 语义的 contract 变化都必须导致 hash 变化，从而解析到（或创建）新 canonical run。

## 3. Run Rollover（Producer 侧必须拥有自己的 current-run resolver）

**FP-HPO-03**：algorithm / hash / contract 变化 → 创建新 canonical run；production producer 自动切换。

- Review 侧已有 `_resolve_canonical_history_source()`（解决"consumer 读哪个 run"，bind 后 fail closed 不漂移）——这是 **consumer resolution**，不可用于写数据。
- Production 侧必须建立独立的 current-run resolver / producer owner（具体函数名由 implementation design 决定），职责：
  - 取 current `algorithm_version` / `parameter_hash` / `history_contract_version` / `scope`；
  - 寻找 compatible canonical run（复用 `create_history_run` 幂等）；
  - 无 → create，有 → reuse；
  - reconcile participating universe；
  - advance target trade_date。
- **契约硬约束**：Producer current-run resolver 与 Review consumer resolver 是**两个独立函数/职责**，不得混用（一个负责"今天往哪个 canonical dataset 写 / 是否需要创建切换"，一个负责"consumer 读哪个 run"）。具体实现选择（是否复用现有表、函数命名、为什么不采用 Model B、为何暂保留混合 HistoryRun model）属于架构决策，见对应 CHANGE，不在此 PRD 绑定。

## 4. Membership Reconciliation（不允许永久 frozen set）

**FP-HPO-04**：daily producer 必须 reconcile universe；不允许 `PARTICIPATING_SET = FROZEN` 作为生产终态。

现状（审计事实）：run_items 是 creation-time snapshot，无 ensure-member / add-missing / reconcile-universe / refresh / retry-skipped；advance 只取 `status=='succeeded'` items。这对历史回补可接受，对盘后每日 Review 不成立。

生产侧 V1 增量 reconcile 业务语义：

- **existing active** → 正常 advance；
- **new / missing**（新上市等）→ 新建 run_item，bootstrap 所需 lookback 历史（`compute_first_pyramid_history(T)` 需 T-N…T 窗口，不能只算今天），生成历史 daily state / events，run_item → succeeded，再进入正常 daily advancement；
  - **硬约束**：`NEW/MISSING` instrument **MUST NOT** 被视为 daily-ready，直到其所需 historical bootstrap 与 lineage persistence 已成功完成。未完成 onboarding 前不得进入正常 daily advancement。
- **skipped / failed** → 明确 reevaluate（非永久缺席）；
- **no-longer-current**（退市 / 退出当前 eligible universe）→ 必须从后续 expected participating set 中排除，同时保留其既有历史事实与 lineage；**不得通过物理删除历史记录实现**。

实现层用 `inactive status` / `active flag` / `current universe join` / `derived eligibility` 哪一种由 implementation design 决定，PRD 不绑定 schema。

禁止物理删除历史成员。

## 5. Failure Semantics

**FP-HPO-05**：Daily producer 必须使 target-T First Pyramid history 满足**现有 Review canonical-history readiness contract**。任何 materialization failure、coverage shortfall 或 lineage mismatch 必须显式暴露，不得静默成功。若 target-T 未达到现有 Review readiness，则 Review 不发布，AfterClose 按既有失败隔离契约进入 `partial_success`，并允许 resume。

关键边界（适配现有 Review，不重新定义 Review 成功标准）：

- 不是"5293 只股票少 1 只 → 当天整个 Review 必须停止"；
- 而是"target-T 满足**现有 Review readiness**（其本身已允许 `partial`/`succeeded` 的合法 canonical HistoryRun）→ 才能 Review"；
- **Review readiness 本身不在此 PRD 内修改**（Review Freeze 生效）。

层级（结合现有 AfterClose closure 六态，仅描述 producer 缺失时的既有隔离行为）：

```
stock core             SUCCESS
board aggregation      SUCCESS
FP history             INCOMPLETE（未达现有 Review readiness）
Review                 BLOCKED / 不发布
AfterClose final       PARTIAL_SUCCESS（既有失败隔离契约）
```

- `advance()` 内部保留逐股收集失败（`result.failed > 0`）本身不是最大问题；
- 真正要建立的是 **caller contract**：`if not target_T_meets_review_readiness: Review prerequisite incomplete → 不继续作为成功 Review 输入`；
- checkpoint 不标记 Review 完成；publication 不发生；resume 可重新尝试；AfterClose = `partial_success`（非整轮 SUCCESS，也非整轮 FAILED）。
- 禁止：`history FAIL → except: log.warning → AfterClose = SUCCESS`（Correctness Gate #9 挡）。

## 6. Consumer（保持现有 fail-closed 语义）

**FP-HPO-06**：Review 必须使用其绑定的 canonical history lineage，保持现有 fail-closed 读取语义（现有 `review_observation_prep_service` 已强制 `algorithm_version` + `history_contract_version` + `source_history_run_id IS NOT NULL` + join run 校验 status/completed_at/run_item status）。

- 本 PRD **不修改**任何现有 Review consumer contract。
- Producer 必须产出带正确 `source_history_run_id` 的 daily_state / events，否则会被 consumer 拒绝（fail closed），不会静默污染跨 lineage/version 序列。
- DB unique constraint：`daily_state(instrument_id, trade_date, algorithm_version)` 唯一（同 version 同 instrument+date 只一行，advance upsert 安全；跨 version 可共存）。

## 7. 推荐 AfterClose 顺序（编排契约，非实现细节）

```
stock_core
  → board aggregation
  → ensure / reconcile FP canonical history run
  → advance FP history to T
  → validate exact-T coverage
  → Review compute
  → Review publish
```

## 8. 非目标（产品层面明确排除）

- 不修改 `docs/prd/70-review.md` 任何条款（Review Freeze 生效；canonical binding / lineage resolution / composition / publication 全部保持现有契约）。
- 不以人工 backfill / repair path 作为正常 daily production history advancement 的替代方案。
- 架构决策（现有 HistoryRun/schema 的复用方式、production model 选择及演化方向）见对应 CHANGE `CHANGE-20260821-001`；本 PRD 不绑定具体 schema、函数名或模型实现。

## 9. 验收（生产契约层面）

- [ ] AfterClose 每日自动 ensure/reconcile canonical run 并 advance 到 T，无需手动 CLI；
- [ ] 新上市股票在 onboarding 后拥有连续历史并可进入 daily advancement；
- [ ] algorithm/hash/contract 升级后 producer 自动创建并切换到新 run；
- [ ] exact-T materialization 不完整时 AfterClose = `partial_success` 且 Review publication 不发生；
- [ ] 08-11 之后的历史在 producer 修复后可通过同一 canonical path 补齐（非独立 repair 路径）；
- [ ] Review consumer lineage/version 读取语义未被本 PRD 改变。
