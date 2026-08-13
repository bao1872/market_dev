# CHANGE-20260812-008 — Review PRD Minimal Repair Correction（外部审计 minor correction）

## 1. 元信息

| 项 | 值 |
|---|---|
| Change ID | CHANGE-20260812-008 |
| 日期 | 2026-08-12 |
| 类型 | docs-only（PRD 外部审计 minor correction；非 PRD redesign） |
| 领域 | 复盘模块 / PRD 70-review.md（Persistence / Acceptance / Publication Gate） |
| 状态 | `prd_confirmed`（docs-only；no product redesign、no code/schema/API/frontend change、no new Filter archetype/threshold invented、experiments/ untouched） |
| baseline SHA | 54844befa63167da130cf483977400f46fa894e0 |
| final SHA | （commit 后回填） |

## 2. 背景

CHANGE-20260812-007（Review PRD Minimal Repair）外部审计结论为
**PARTIAL / MINOR CORRECTION REQUIRED**。大方向正确，以下已 PASS 不重动：

- §8.0 Experimental Filter 已删除；
- Filter 明确只消费 Structured Observation Evidence；
- BREADTH_EXPANSION / PARTICIPATION_CONFIRMATION / delta > 0 不再作为 accepted PRD rule；
- L1 Observation / L2 Evidence ownership 已分离；
- §23.5 P/Q/U/C/V first-layer hard gate 已改为 Canonical Observation readiness。

本轮仅修 4 个残留问题（FIX A–D）。

## 3. 改动明细

### FIX A — §7.9 Persistence 状态残留
- §7.9 标题由「Exploration Persistence Contract」改为「Persistence Contract」；
- §7.9 前言去掉「不是 production implementation / 不是 migration / physical schema DEFER」框架，
  改为明确：Canonical Observation Fact Persistence **已经实现**（`review_scope_observation_facts`，
  grain = `trade_date + scope_type + scope_key`；已完成 serialize / contract validation / upsert / read / idempotency）；
- 仍 DEFER 的是 **consumer cutover**（legacy Filter / Discovery / Publication / API / Frontend 尚未正式消费 canonical path，见 §7.9.8），**persistence schema 本身已落地，不再 pending**；
- 未重新设计 schema、未改 migration、未改 §7.9.1–7.9.7 已正确业务语义。

### FIX B — §20 Case 1 漏改
- 删除 Case 1 旧 first-layer 表达「玻璃基板 Concept 出现明显 P/Q/U/V + migration + volume 改善」；
- 保持 Case 1 业务目的：Concept 必须可独立发现，不依赖 Industry 命中；
- 改用当前 Observation / Evidence 语言：PRICE Breadth 改善、TREND/STRUCTURE/MOMENTUM State+Breadth/Transition 改善、PARTICIPATION 改善；具体成立条件与是否 mandatory 由 Filter/Discovery 正式条件决定（不重新定义 threshold）。

### FIX C — §20 Case 4 逻辑写反
- 原 Case 4 以「Concentration 稳定或下降 + PARTICIPATION 上升 + leader-median gap 扩大」推导「行情向少数龙头收缩」，逻辑错误；
- 改回原业务语义方向：PRICE/Trend breadth 收缩或内部参与减弱 + PARTICIPATION 弱化（扩张不再成立）+ Price/Amount Concentration 上升 + leader-median gap 扩大 → 支持「行情向少数龙头收缩」Discovery；
- 仅定义方向语义，不定义具体 numeric threshold，不发明新 HHI normalization，raw HHI 不用于跨 Scope absolute comparison。

### FIX D — §23.5 Publication Gate 冲突
- §23.5 item 6 删除「且 industry_l1 ready 比例达到配置门槛」，仅保留 market coverage hard gate（`coverage_ratio >= 0.95`）；
- §23 开头 priority 文字收窄：legacy §23 仅优先于 history §7/§11 的 **P/Q/U/C/V legacy baseline** 冲突描述，不得重新覆盖 §6.5.8 / §11.1 的 progressive readiness 合同；
- 新增权威合同声明：当 §23 legacy gate 与 §6.5.8 / §11.1（2026-08-12 current contract，industry_l1 / major_index / style 属 PROGRESSIVE OPTIONAL、数据不可用不阻塞 whole Review publication）冲突时，以 §6.5.8 / §11.1 为当前 authoritative publication contract。

## 4. 本轮未处理（明确排除）
Tushare source 历史条款；Major Index / Style product definition；Filter A/B/C 新条件；Discovery implementation；
Cross-Scope；API；Frontend；Experimental Filter code；CHANGE-006 code cleanup。

## 5. 验证
- `tools/check_docs_consistency.py`：Docs consistency PASS（EXPLORATION mode）；
- `tools/check_governance_rules.py`：Governance check PASS；
- 手工核对：§7.8.6 与 §7.9 不再矛盾；Case 1 无 P/Q/U/V；Case 4 方向一致（breadth/participation 弱化 + concentration 上升 + gap 扩大）；§11.1 与 §23.5 不再同时存在 industry_l1 optional 与 industry_l1 ready ratio hard gate 两个冲突合同。

## 6. 关联
- 前置：CHANGE-20260812-007（本轮为其外部审计 minor correction）；
- 关联 PRD：`docs/prd/70-review.md`（§7.9 / §20 Case 1 / §20 Case 4 / §23 开头 / §23.5）；
- Maps：未修改（待实现验收后单独授权同步）。
