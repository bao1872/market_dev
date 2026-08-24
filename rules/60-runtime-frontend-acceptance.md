# 60 Runtime、API 与前端技术闭环

## 1. 目的

本文件解决 Exploration 最容易被混淆的问题：

**“前端可见”不是“正确性验收”；但产品假设也不能只停留在后端测试。**

工程侧必须把正确结果送到前端消费层；用户负责最后的视觉与产品价值判断。

## 2. 工程技术闭环

当前 slice 涉及前端时，工程必须验证到：

`DB / source facts → service → persistence → API → frontend request → response schema → component binding`

至少确认：

- 真实数据已经产生；
- API 读取正确 canonical result；
- instrument / symbol / trade_date 一致；
- HTTP status 正确；
- response 字段存在且类型/nullability正确；
- 前端 hook/service 读取正确 endpoint；
- 前端组件消费正确字段；
- 不使用 mock 冒充真实数据；
- loading / unavailable / error 不会误显示为正常结果。

> **单 component binding PASS ≠ 整层完成。** 上述链路只能证明“某个字段绑对了”；
> 不能证明“PRD 对该产品层定义的所有 required capabilities 都已有 component”。
> 对 milestone / product-layer closure，必须额外做 **PRD capability completeness audit**
> （见 §10），否则不得把当前 slice 判为整层/整机完成。

## 3. IDE 与用户责任边界

### IDE / 工程侧负责

- 业务逻辑；
- 代码逻辑；
- unit / contract / integration；
- Runtime；
- API；
- frontend 数据绑定；
- build/targeted frontend test；
- 提供访问路径与推荐样本。

### 用户负责

- 页面视觉是否合理；
- 信息密度；
- 文案是否符合研究习惯；
- 指标是否有信息增量；
- 产品/算法假设是否成立；
- 是否进入下一轮 PRD 调整。

IDE 不应把“用户先打开浏览器”作为完成工程技术验收的前置条件。

## 4. Exploration Runtime

Exploration Runtime 的目标是：

**证明当前 hypothesis slice 在真实环境和真实数据上工作。**

不自动要求：

- 全域 after-close；
- 九节点 fully_ready；
- 所有 Worker；
- 所有 API；
- 全市场；
- full release smoke。

## 5. 代表性样本

算法/状态逻辑迭代优先使用 25–60 个 intentional sample。

样本应覆盖适用状态，例如：

- strong trend；
- weak/down trend；
- range；
- high volatility；
- low volatility；
- BOS / CHoCH；
- squeeze / release；
- board leader / tail（若当前 slice 涉及 board）；
- insufficient history / edge；
- 用户熟悉的重点股票。

只有当方向被用户认可、需要验证市场覆盖或性能时，才升级全市场。

## 6. Runtime Success

对当前 slice 报告：

- exact SHA；
- target trade date；
- sample count / universe；
- success / failed / skipped；
- 关键字段 availability；
- API endpoint；
- frontend consumed fields；
- 已知异常。

不因为无关 enhancement 缺失把当前 slice 判失败。

例：

如果 H1 只验证 First Pyramid Core，则 Chip/Auction/Review 不应阻止 H1 Stock Detail 展示。

## 7. Frontend Acceptance Evidence

工程侧至少提供：

- 页面路由/访问方式；
- 推荐检查的 5–10 个代表股票；
- 对应 API；
- 关键字段；
- 前端 component/hook 绑定关系；
- build/test 结果。

如果已有浏览器自动化可低成本运行，可补充 Network/Console/route smoke；但 Exploration 不要求为了一个数据绑定任务搭建新的重型 E2E 框架。

## 8. Product Acceptance

用户确认后，hypothesis 可进入：

- REJECTED；
- ITERATE；
- VALIDATED。

只有 VALIDATED 才值得继续扩大该业务假设的工程投入。

## 9. STOP

当：

- 当前 slice correctness 通过；
- required tests 通过；
- 真实 runtime 通过；
- API/前端技术绑定通过；
- 用户已经可以开始产品判断；

立即 STOP。

不得自动进入下一个域。

## 10. Milestone / Product-Layer Completeness Gate

现有 §2 的链路只能证明“一个 slice 做对了”（单 component binding）；它**不证明**
“整个产品层 / 整机没有漏”。Exploration 模式下局部成功也不得冒充整体成功
（见 AGENTS.md §0.2 第 7、9 条）。本 gate 补足这一缺口。

### 10.1 两级 PASS 语义（必须区分）

- **SLICE PASS**：本轮承诺做的事情实现正确（对应 AGENTS.md §5.x 的 hypothesis slice）。
- **LAYER COMPLETE**：PRD 对该产品层定义的**所有 required capabilities** 都有完整
  `backend owner → persistence → API → frontend formal surface → tests → representative runtime` 链。
- **PRODUCT COMPLETE**：所有 required layer 闭合。

报告不得只写 `R2B PASS` 而暗示整机完成。必须显式拆分：

```text
SLICE STATUS: PASS
PRODUCT COVERAGE IMPACT:
  <product layer>: <before> → <after>
  Review overall: still PARTIAL
```

### 10.2 Milestone Completeness Gate

仅在 **milestone boundary**（如 R1 Current / R2 Scanner / R3 Observation / R4 Overview）
结束时回看整个 PRD，**不只看本次 diff**。检查链：

```text
PRD required capabilities
 ↓ 是否每个都有 backend owner？
Backend
 ↓ 是否 runtime-wired（非仅 unit-test）？
Persistence
 ↓
API
 ↓ 是否暴露给产品？
Frontend
 ↓ 是 Raw Facts 还是 formal UX？
Tests
 ↓
Runtime
```

三个必答问题：

1. PRD required capabilities 是否全部有 owner？
2. 有 owner 的是否全部暴露给产品（API + frontend formal surface）？
3. 产品可见是 Raw Facts 还是正式 UX？

单 component binding PASS 不得作为整层完成证据。

### 10.3 Active Domain Coverage Matrix

每个 **active domain**（正在持续开发的领域）维护一张覆盖矩阵，作为“我们到底做到哪了”
的唯一基准，避免后续 AI/IDE 在 `PRD=新 / Code=新 / Map=旧` 之间认知漂移。格式：

```text
| Product Contract | Backend Owner | Persistence | API | Frontend | Tests | Runtime | 状态 |
```

状态取值：`DONE` / `PARTIAL` / `MISSING_UI` / `MISSING` / `UNAVAILABLE_BY_DESIGN` / `AUDIT`。

矩阵放在该 domain 的 Map（`docs/maps/`）中，不新建治理目录。

### 10.4 Active Map Freshness（里程碑边界同步）

Exploration 模式不要求每次 commit 同步文档（AGENTS.md §6）。但 **active domain 发生
canonical architecture / product-layer milestone 变化后，在开始下一 milestone 前必须同步 Map**。

即：milestone boundary 更新一次，而非每次小 slice 写文档。长期停留在 legacy 状态（如
仍写 V1 已完整实现、旧 P/Q/U/C/V、旧五阶段 UI）的 Map 属于治理缺陷，会误导后续开发。

### 10.5 与现有治理的关系

本 §10 不引入新目录、不推翻现有 Hypothesis Slice / Correctness Gate / dev-only / 测试分层 /
真实 Runtime / API→Frontend closure。它只补“从每个任务是否正确”到“同时知道整个产品还差多少”
的全局覆盖层。普通小 bug（AGENTS.md §5.2）不触发本 gate。
