# CHANGE-20260812-010 — Review PRD Specification Repair Follow-up：Filter/Signal 强制架构残留一致性校正

## 元数据

- 日期：2026-08-12
- 类型：`docs-only`（Specification Repair Follow-up / 跨章节 target-contract 一致性校正）
- 领域：复盘模块 / `docs/prd/70-review.md`
- 授权：用户在 Round 2C-1 后续中明确授权进行 docs-only PRD 一致性修复
- 状态：`prd_confirmed`（docs-only；**未进入 Implementation，未写任何业务/测试代码、未建表、未 migration、未改 API/前端、未改 Maps/Runbooks/治理、experiments/ 未跟踪目录保留不动**）

## 背景

CHANGE-20260812-009 已正确冻结事实链：

```
Canonical Scope Observation → Objective Evidence → [Discovery Product Design — NOT YET FROZEN]
```

并明确 Filter/Signal/Signal→Discovery 全部 NOT YET FROZEN。但 PRD 其他章节仍残留将 Filter/Signal/Signal→Discovery
写成**强制目标合同**的句子，与 §8/§10A/§10B/§22 NEXT 的自述相矛盾。本轮只消除这些自相矛盾，**不是新产品设计**。

## 修复内容（均为 docs-only consistency correction）

### A. §7.9.8 consumer cutover 语义
- 删除「legacy consumer（Filter/Discovery/...）尚未正式 cutover … consumer cutover pending（DEFER）」的暗示性表述；
- 改为 5 条明确说明：persistence 已实现冻结；legacy consumers 继续保持 legacy compatibility；是否/哪些 Discovery-side consumer 未来消费 Objective Evidence 须等 Discovery Product Design 冻结后决定；**不得把 legacy Filter/Signal cutover 表述成 V2 mandatory pending work**；独立于 Discovery 的 canonical fact consumer 其真实 pending 可单独保留，不绑定 Filter/Signal。

### B. §8 历史定位残留
- 「`MarketReviewSignal` 保留为 legacy atomic evidence record；**新的 `Discovery` domain object 负责 user-level finding 聚合**」改为历史语义：在历史设计中 Discovery 曾被定义为聚合 legacy Signal，**仅作 legacy implementation reference，不构成 V2 target architecture**，Signal/Discovery 是否保留该聚合关系仍 NOT YET FROZEN。

### C. §10A.3 Discovery Domain Object
- 降级为 **历史提案 / PRODUCT DESIGN INPUT / NOT CURRENT V2 TARGET SPEC**；
- 原 Market Review 结构图、Discovery YAML 草图标注为历史提案，非当前冻结 schema；
- 原「Signal 继续负责算法命中、证据、版本追踪。Discovery 聚合多个 evidence」「新 Discovery 应 consume existing/new signals」以删除线 + NOT YET FROZEN 明确不再作为 V2 target requirement；
- 保留为后续 Discovery Product Design 的设计历史输入（可参考、修改或完全放弃）。

### D. §10A.4 历史兼容
- 改为纯 **Legacy Implementation Compatibility / NOT V2 TARGET REQUIREMENT**；
- `MarketReviewSignal` / A/B/C/D 可继续存在于 legacy runtime；是否成为未来 Discovery 输入尚未决定；不要求 V2 consume，不要求立即 cleanup。

### E. §10B 信号生命周期
- 标注 **LEGACY IMPLEMENTATION COMPATIBILITY / NOT V2 TARGET REQUIREMENT**；
- new→continuing→...→transformed 状态机仅作 legacy 参考，**不得作为未来 Discovery 生命周期默认模板**；Discovery lifecycle 具体实现继续 NOT YET FROZEN。

### F. §11 任务编排
- 「信号和归因幂等」拆分：**Attribution 幂等**可保留（若属已确认目标）；**Signal 幂等仅属 legacy path compatibility**，不作为 V2 future architecture requirement。

### G. §11.1 发布门禁
- 「signal evaluation 无系统性异常」**从 V2 target publication gate 中移除**；若现有 legacy runtime 仍依赖，仅作 Legacy compatibility runtime condition，不构成 V2 Signal mandatory requirement；本轮不发明新 Discovery gate。

### H. §20.2 完整验收
- 删除「Filter Engine 均能给出结构化证据」（Filter 是否存在未冻结，不得替换成新 Filter 验收标准）；
- 保留已冻结事实层验收（Observation/Evidence 可追溯、coverage/readiness 真实等）；Discovery 相关验收若依赖未冻结架构，须标 PRODUCT DESIGN REQUIRED / ACCEPTANCE CONTRACT NOT YET FROZEN。

### I. §22 Roadmap 自相矛盾
- 在 NEXT 之后、P0-A 之前插入 **HISTORICAL ROADMAP / SUPERSEDED AS CURRENT EXECUTION PLAN** 边界块：P0-A/B/C/P1/Phase 5 仅保存历史上下文，不再是当前 NEXT；Filter/Signal/Signal→Discovery 部分须待 Discovery Product Design 决策后重新确认；IDE 不得据此自动生成任务；
- P0-B「新增 Discovery domain object（Signal → Discovery 聚合）」标注为历史方案，非当前 P0-B；
- Phase 5「历史回放与阈值校准」标注为 **legacy filter roadmap**，不是当前 V2 future phase；「使用历史 Review Run 验证筛选器稳定性」「阈值变化升级 filter_version」不得作为当前 V2 future requirement。

## Residual Matrix（Filter/Signal/Signal→Discovery 命中分类）

| location | phrase | classification | action |
|---|---|---|---|
| §8 / §10A / §10B 标题与块 | Legacy Filter/Signal Compatibility / LEGACY COMPATIBILITY | B. LEGACY/HISTORICAL | 保留（正确标注） |
| §7.9.8 | consumer cutover 语义 | B. LEGACY（已重写说明） | 已修正，不再暗示 mandatory cutover |
| §10A.3 | Discovery Domain Object 草图 | B. HISTORICAL PROPOSAL | 已标 NOT CURRENT V2 TARGET SPEC |
| §10A.3/10A.4 | Signal 负责命中 / Discovery consume signals | C. NOT YET FROZEN（删除线） | 已降级，不再 ACTIVE |
| §10B | 信号生命周期状态机 | B. LEGACY COMPATIBILITY | 已标注非 V2 模板 |
| §11 | 信号幂等 | B. LEGACY only | 已拆分，Signal 幂等非 V2 要求 |
| §11.1 | signal evaluation 无系统性异常 | B. LEGACY runtime condition | 已从 V2 gate 移除 |
| §20.2 | Filter Engine 均能给出结构化证据 | 删除 | 已移除 |
| §22 P0-B | 新增 Discovery domain object（Signal→Discovery） | B. HISTORICAL | 已标历史方案 |
| §22 Phase 5 | 筛选器稳定性 / filter_version | B. HISTORICAL legacy filter | 已标 legacy filter roadmap |
| §5 schema / §12 API | market_review_signals / filter_version 字段 | B. LEGACY schema description | 保留（历史 schema 描述，非 target contract） |

**结论**：无任何 Filter / Signal / Signal→Discovery 命中以 **ACTIVE V2 TARGET** 身份存在（除描述「不得/不要求/未冻结」的否定性语句外）。

## 未修改（遵守边界）

- 未修改任何业务代码 / 测试代码 / 数据库 / API / Frontend / Maps / Runbooks / 治理文件；
- 未开始 Filter 实现；未开始 Signal 设计；未开始 Discovery 产品设计；
- `experimental_filter.py` 等 legacy 文件保留；`docs/maps/70-review.md` 未修改（实现尚未 cutover）。

## 验证

- `git diff --check`：无 trailing whitespace / 无冲突标记；
- 文档级 grep：下列句子不再作为 ACTIVE V2 TARGET：
  - ~~Filter Engine 均能给出结构化证据~~（已删）
  - ~~signal evaluation 无系统性异常~~（已从 V2 gate 移除）
  - ~~Signal 继续负责算法命中~~（删除线 + NOT YET FROZEN）
  - ~~新的 Discovery 应 consume existing/new signals~~（历史方案标注）
  - ~~新增 Discovery domain object（Signal → Discovery 聚合）~~（历史方案标注）
  - ~~使用历史 Review Run 验证筛选器稳定性 / 阈值变化升级 filter_version~~（legacy filter roadmap 标注）
- 未跑全系统测试 / PG 测试 / migration / deployment。

## 状态诚实声明

本 CHANGE 为 **docs/specification change**，描述 PRD 一致性校正；**不得写成 implementation completed**。
V2 正式冻结边界停在 Canonical Observation → Objective Evidence；Discovery organization 仍 NOT YET FROZEN。
CHANGE-20260812-009 核心修复方向正确，本轮不回滚、不 reset、不改历史 commit。
