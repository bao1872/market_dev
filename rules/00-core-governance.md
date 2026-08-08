# 00 — Core Governance

## 1. 事实源优先级

- PRD 是目标行为的权威来源；
- 代码、数据、日志和运行状态是当前执行事实的权威来源；
- Maps 用于总结已核验的当前实现，并应与真实代码保持一致；
- Changes 用于解释重要历史演化；
- Runbooks 用于描述当前操作步骤。

文档冲突时：先判断是目标行为还是当前实现；查看对应权威来源；用代码或运行证据核验；
修正文档而不是自行猜测。不得把假设、计划或未验证结果表述为事实。

## 2. 阶段路由

当前默认 `PROJECT_STAGE = EXPLORATION`。`AGENTS.md` 定义了 Exploration 默认执行链、Correctness Gates 和 Hardening Trigger。

Exploration 只减少与当前 hypothesis 无关的全域验证与治理完备性，不减少正确性、测试、数据安全和真实运行证据。

## 3. Hypothesis Slice

任何影响产品/算法行为的任务，在实现前至少明确：

- Hypothesis；
- PRD Basis（对应条款，或明确标注尚未进入 PRD）；
- Visible Outcome；
- Vertical Slice（input → compute → persistence → API → frontend）；
- Correctness Risks（future leakage、ownership、trade_date、canonical、fallback、安全）；
- Required Tests；
- Deferred。

## 4. 修改前最小报告

修改代码前，报告：

- 当前要解决的问题或要验证的 hypothesis；
- 计划修改的文件范围；
- 修改会影响的模块 / 契约 / 数据 / 运行方式；
- 计划执行的测试和真实运行验证；
- 需要用户授权但尚未获得的部分。

## 5. 严重度分级

- **P0**：数据损坏、安全泄漏、契约破坏、时间因果错误、真实业务结果错误。不得在探索模式下静默兜底。
- **P1**：功能缺口、主要流程错误、重要边界错误、明显技术债务。
- **P2**：局部体验、次要一致性问题、与当前 slice 无关的标准化。
- **P3**：低影响工程债务，触发条件满足前 Deferred。

## 6. 文档授权门

- **PRD 门**：只有用户明确发起 PRD 任务才修改 `docs/prd/`。
- **Maps 门**：只有用户验收后明确授权同步 Maps 才修改 `docs/maps/`。
- **Runbooks 门**：只有真实操作验收后明确授权同步才修改 `docs/runbooks/`。
- **Changes 通道**：重要行为/契约/Schema/运行方式变化时，代码任务可新增或更新唯一相关 Change，必须诚实标注实现/验收状态。
- **治理门**：只有用户明确授权治理调整才修改 `AGENTS.md`、`rules/`、`tools/check_governance_rules.py` 及其治理测试，以及 `rules/PROTECTED_GOVERNANCE_FILES.json` 列出的受保护文件。

普通开发任务默认不修改 PRD/Maps/Runbooks/治理。

## 7. Two-Strike Architecture Rule

第一次遇到局部问题优先局部、清晰、可测试地解决；只有同类真实问题至少第二次出现，或已经存在两个明确消费者/重复场景时，才考虑新增通用 abstraction、framework 或治理层。安全与数据正确性问题不受此条限制。

## 8. 闭环与证据

- 未验证结果必须标记为未验证；
- 不得用 mock 冒充真实结果；
- 不得用“测试通过”掩盖真实运行错误；
- 任何与真实数据、远程验证、migration、部署相关的结论，必须有对应证据或明确标注未执行。
