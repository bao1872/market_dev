# AGENTS.md — 盘迹任务路由与工作协议

本文件是 AI 代理、IDE 助手和自动化开发工具在本仓库工作时遵循的长期工作协议。
它只定义：如何定位权威信息；如何判断项目阶段与任务类型；不同任务应走哪条开发路径；
什么时候需要更新项目文档；如何验证修改；哪些边界在未获得明确授权前不得跨越。

它不是 PRD、架构文档、实现地图、项目状态报告或操作手册。项目具体需求、实现细节、
运行结构、部署方式、数据模型和指标定义必须维护在各自对应的文档中。

## 0. 通用执行主体合同

所有 IDE、编码助手和自动化 Agent 遵守同一套仓库规则。

治理按**实际操作**定义（是否修改代码、是否连接远程运行环境、是否执行部署、是否写入数据），
不按 IDE、Agent、模型或客户端区分。规则体系中不得出现按工具命名的角色定义、
能力矩阵、模式切换表或工具专属状态值。

一个执行主体能做什么，取决于用户在本轮授予的授权和本文件 §8「基础安全边界」，
而不取决于它是哪个工具。详见 `rules/50-git-development-flow.md`「通用执行主体合同」。

### 0.1 当前项目阶段

**PROJECT_STAGE = EXPLORATION**

盘迹当前处于产品与算法假设验证阶段。默认开发与审计模式为：

**FAST_ITERATION / EXPLORATION MODE**

当前首要目标不是达到 release-grade 的治理完备性，而是缩短以下反馈链：

`PRD hypothesis → correct implementation → required tests → real runtime → API/frontend technical closure → human product evaluation`

探索模式只能减少与当前假设无关的治理、标准化、全域验证和发布流程，**不得降低业务正确性、代码正确性、测试质量、数据安全或真实运行结果的可信度**。

除非命中 §0.4 的 Hardening Trigger，或用户明确要求进入发布/硬化流程，否则不得自动把局部开发任务升级为完整 Release Audit、全域 RTM、全量 closure、production-clone rehearsal 或与当前 hypothesis slice 无关的治理项目。

### 0.2 永久生效的 Correctness Gates

无论项目处于 Exploration 还是 Hardening，以下门槛始终生效：

1. **PRD / 业务逻辑正确性必须确认**：实现不得偏离已确认 PRD；发现 PRD 缺失或冲突时必须如实报告，不得自行猜测。
2. **代码逻辑必须审查**：调用链、ownership、数据生命周期、trade date、canonical result、错误处理和关键 fallback 必须符合业务逻辑。
3. **修改范围对应的单元测试必须完成**：不得以“探索模式”“先看前端”为理由跳过受影响业务逻辑的 unit tests。
4. **必要的 Contract / Integration Test 必须完成**：涉及数据库持久化、API、跨服务契约、运行编排或真实数据依赖时，按风险增加最小必要集成验证。
5. **真实运行证据必须与任务匹配**：当行为依赖真实数据库、行情或运行环境时，仅有 mock/unit test 不足以宣称运行正确。
6. **API → Frontend 技术绑定必须验证**：后端返回正确不等于前端闭环；必须确认前端实际 endpoint、response schema、字段映射和组件消费一致。
7. **用户产品验收不能替代工程验收**：页面“看起来有结果”不能替代业务逻辑、代码逻辑和测试正确性。
8. **工程验收也不能替代用户价值判断**：测试通过只能证明实现符合当前逻辑，不能证明产品/投资理论有价值。
9. **禁止结果污染**：不得 future leakage、错误 time-key、错误 canonical pointer、mock 冒充真实结果、静默 fallback 造成 false-green。
10. **数据安全始终优先**：任何可能损坏不可恢复数据、引入错误历史数据或产生不可逆影响的操作，必须遵守 §8。

### 0.3 Exploration 默认执行链

Exploration 模式下，每一轮开发优先定义一个可独立判断的 **Hypothesis Slice**：

1. **Hypothesis**：这一轮要验证什么产品/算法假设？
2. **Visible Outcome**：用户最终应该在哪个页面或结果中看到什么？
3. **Shortest Vertical Slice**：只追当前假设需要的 `input → compute → persistence → API → frontend`。
4. **Business Logic Review**：先确认业务逻辑和 PRD，再修改代码。
5. **Code Logic Review**：确认实现路径、ownership、时间口径、canonical result 和错误处理。
6. **Required Tests**：至少完成 modified-scope unit tests；按风险增加 contract / PG / integration。
7. **Real Runtime**：用真实数据验证当前 slice，而不是默认跑所有域。
8. **Frontend Technical Closure**：验证 API 到前端字段绑定；不要求 IDE 替用户做主观视觉或产品价值判断。
9. **Human Evaluation**：用户判断结果是否有用、理论是否合理、是否值得继续。
10. **STOP**：当前 slice 达到可判断状态后立即停止，不得自动扩展到其他域或 release hardening。

Exploration 模式下，P2/P3 的治理、标准化、通用化、文档同步、全域 readiness 和 release 完备性问题默认进入 Deferred Debt；只有当它们会导致当前结果错误、测试失真、数据损坏、真实运行阻塞或前端技术闭环失败时，才升级为当前任务 blocker。

### 0.4 Hardening Trigger

以下任一情况出现时，才默认升级到 Hardening / Release 模式：

- 用户明确要求“上线”“发布”“release”“正式部署”“全面代码审计”或同等含义；
- destructive / irreversible Migration；
- 用户数据、权限、安全、支付或凭据风险；
- 大规模 production backfill / rewrite；
- 核心 schema、长期外部 API 或稳定兼容契约准备冻结；
- 多用户正式使用、付费用户或公开 beta；
- shared foundation / 核心基础设施变更可能造成大范围不可恢复影响；
- 用户明确要求完整 RTM、完整 closure、完整 migration rehearsal 或 release certification。

未命中以上条件时，**不得仅因为已有重型验证基础设施就自动启用它。**

### 0.5 Governance 与 PRD 边界

**项目阶段或治理模式的变化不得自动触发 PRD 重写。** Exploration / Hardening 只改变实现、验证与交付流程，不改变已确认产品语义。任何 PRD 内容变化必须由用户单独明确发起。

## 1. 必读入口与权威层级

开始任务前必须按以下顺序读取入口：

1. **AGENTS.md**（本文件）：项目阶段、任务路由与最高安全边界。
2. **rules/README.md**：详细强制规则的索引与状态语义。
3. **rules/** 详细规则文件（按需读取与任务相关的）：
   - `rules/00-core-governance.md`：事实源优先级、闭环、修改前最小报告。
   - `rules/10-product-domain-invariants.md`：产品边界、策略、DSA、自选与监控、飞书。
   - `rules/20-market-data-computation.md`：行情、复权、点时、Canonical、第一金字塔与板块计算不变量。
   - `rules/25-engineering-implementation.md`：通用工程实现规范（Cross-cutting Implementation Standard，不拥有产品/安全/测试/Git/部署合同）。
   - `rules/30-security-data-safety.md`：权限、秘密、真实业务数据与不可恢复风险。
   - `rules/40-testing-quality.md`：modified-scope unit、合同/PG/真实数据、测试分层与证据纪律。
   - `rules/50-git-development-flow.md`：dev-only、提交/push、checkpoint、continue mode 与计划授权。
   - `rules/60-runtime-frontend-acceptance.md`：真实 Runtime、API→Frontend 技术闭环与用户验收边界。
   - `rules/70-hardening-release.md`：只在触发条件命中时启用的 Hardening/Release 规则。
   - `rules/80-deployment-migration.md`：远程部署、Migration 风险分级与运行安全。
   - `rules/90-deprecated-forbidden.md`：禁止行为清单、废弃项、禁止恢复项。
4. **docs/prd/**：已确认需求和目标行为的事实源。
5. **docs/maps/**：已核验当前实现和项目记忆。
6. **docs/changes/INDEX.md**：重要变更索引；具体变更文件在 `docs/changes/YYYY/`。
7. **docs/runbooks/**：可重复执行的操作步骤。
8. Git 历史：普通代码修改、小 Bug 和具体开发历史。

`AGENTS.md` 与 `rules/` 共同构成项目强制规则体系；冲突时以本文件 §8「基础安全边界」为最高边界，其余以更具体的一方为准。

**阶段路由优先于流程扩张**：在 `PROJECT_STAGE=EXPLORATION` 时，`rules/` 中属于 release/hardening 完备性的要求不得自动扩张当前 hypothesis slice；但 Correctness、Testing、Data Safety、Security 和明确禁止项始终生效。若现有 `rules/` 与本阶段路由存在冲突，应报告冲突并等待用户授权调整规则，不得自行绕过硬规则。

## 2. 文档体系

| 位置 | 职责 |
|---|---|
| `AGENTS.md` | 稳定的项目阶段、任务路由和工作协议 |
| `rules/` | 项目相关的开发原则和硬约束 |
| `docs/prd/` | 已确认的需求和目标行为 |
| `docs/maps/` | 已核验的当前实现和项目记忆 |
| `docs/changes/` | 重要变化及其原因 |
| `docs/runbooks/` | 可重复执行的操作步骤 |
| Git 历史 | 普通代码修改、小 Bug 和具体开发历史 |

> 硬规则（2026-07-29 收口）：禁止新建未经用户确认的报告/治理目录；
> 完整过程只在对话输出，不写入仓库；普通 Bug 由 Git 记录，只有重要行为变化才写一个 CHANGE。
> `docs/current/` 标记为 legacy 只读，不得新增或修改其中文件，后续另行迁移。

**PRD** 回答：系统在正确状态下应该怎样工作？可以包含产品行为、业务规则、输入输出、
数据要求、用户交互、边界条件、验收标准、已确认的设计决策。除非需求已被明确修改，
不得用现有实现反向改写已确认的需求。

在 Exploration 模式下，PRD 仍然是业务逻辑 SSOT；快速迭代意味着优先验证一个清晰 hypothesis slice，而不是绕开 PRD。若假设已经写入正式 PRD，代码必须按 PRD 实现；若假设尚未确认，则先按 §5.3 走探索性实验，不得把实验结果直接写成正式需求事实。

**Maps** 回答：当前系统实际上是怎样实现的，修改时应该从哪里进入？可以包含代码入口、
模块职责、数据流、存储关系、任务与 Worker 关系、API 与前端关系、验证入口、当前已知缺口、
已废弃路径。Maps 必须描述已经核验的当前实现。计划中的工作不得被写成已经完成的事实。

**Changes** 用于记录重要演化。Exploration 模式下不得为了形式为每个小 iteration 创建 Change；只有重要行为、契约、Schema、运行方式或长期可追溯决策发生变化时才需要记录。

**Runbooks** 用于记录可重复执行的操作步骤。只说明如何执行某项操作，不重新定义产品行为或实现架构。

## 3. 事实源

- PRD 是目标行为的权威来源；
- 代码、数据、日志和运行状态是当前执行事实的权威来源；
- Maps 用于总结已核验的当前实现，并应与真实代码保持一致；
- Changes 用于解释重要历史演化；
- Runbooks 用于描述当前操作流程。

文档之间出现冲突时：先判断问题涉及目标行为还是当前实现；查看对应的权威来源；
必要时通过代码或运行证据进行核验；修正文档，而不是自行猜测。不得把假设、计划或未验证结果表述为事实。

涉及远程服务器、数据库、Redis、路径和端口时，必须先读取对应 Map（如 `docs/maps/80-system-runtime.md`）获取权威身份和连接信息；聊天记忆和本机任意 SSH 别名不能作为权威来源。

## 4. 最小读取路径

默认不要阅读全部文档。开始任务前，只读取当前任务真正需要的内容：AGENTS.md；
`rules/` 中与当前 slice 和操作风险直接相关的规则；与当前任务相关的 PRD；与当前实现路径相关的 Maps；
当历史背景确实重要时，查看最近相关 Change；为完成核验所需的真实代码、数据、日志或运行状态。

当实现可以直接核验时，不得只依赖旧描述推测当前状态。

Exploration 模式下禁止为了“全面”而默认读取全部 PRD、Maps、Changes、Runbooks 或全仓治理材料；只有当前 hypothesis slice 或安全风险需要时才扩展读取范围。

### 4.1 治理变更授权

只有用户在当前任务中明确要求调整治理体系、治理规则或对应检查器时，才允许修改
`AGENTS.md`、`rules/`、`tools/check_governance_rules.py` 或其治理测试。普通代码、文档、
Bug 修复和功能任务即使发现治理冲突，也只能报告冲突并提出建议，不得顺带修改上述文件。

`rules/PROTECTED_GOVERNANCE_FILES.json` 是治理绑定文件的机器可读清单。清单覆盖治理文本、
治理检查器，以及与治理合同不可分割的远程验证入口、计划、编排、清理、证据、Compose 和
合同测试。清单内文件构成同一**受保护治理变更域**：只有用户在当前任务明确授权治理调整时
才允许修改；普通功能开发、Bug 修复、重构、部署或测试任务均不得改动。修改其中任何文件前，
必须读取清单并核对影响；涉及合同变化时，应同步修改适用的规则、实现、检查器和测试，禁止只改
文字或只改脚本造成漂移。清单本身也只能在同一治理授权下修改。

用户要求“更新文档”不自动构成治理变更授权；必须明确指向治理、规则、`AGENTS.md` 或治理检查器。历史任务中的授权不得沿用到新任务。

### 4.2 产品与实现文档授权

PRD、Maps 和 Runbooks 均采用用户主动发起、当轮有效的独立授权，历史授权和笼统的
“更新文档”“完成闭环”不得沿用或推定：

- **PRD 门**：只有用户在当前任务中明确要求新增、修改或校准 PRD，才允许修改 `docs/prd/`。
- **Maps 门**：只有用户在当前任务中明确要求更新 Maps，或在实现验收后明确确认允许同步 Maps，才允许修改 `docs/maps/`。
- **Runbooks 门**：只有用户在当前任务中明确要求更新 Runbooks，或在真实操作验收后明确确认允许同步 `docs/runbooks/`。
- **Changes 通道**：代码、测试、Migration、契约或运行方式发生重要变化时，代码任务可以新增或更新唯一相关 Change；普通无行为变化的小修仍可只由 Git 历史记录。

代码实现阶段默认只允许修改实现所需的代码、测试、Migration、配置和对应 Change，不得顺带
修改 PRD、Maps、Runbooks 或治理文档。候选实现尚未获用户验收时，Change 必须使用
`implemented_unconfirmed`、`verified_code_pending_acceptance` 或等价诚实状态，不得写成正式闭环。

## 5. 任务路由

修改代码前，按以下顺序路由：

1. 先判断项目阶段：当前默认 `EXPLORATION`。
2. 再判断本轮 task type：新需求/行为变化、小 Bug、探索性实验、重构、数据修复、故障恢复。
3. 在 Exploration 模式下，先定义当前 hypothesis slice，再决定最小代码、测试、真实运行和前端技术闭环范围。
4. 只有命中 §0.4 Hardening Trigger，才扩大为 release-grade 验证。

### 5.0 Exploration Hypothesis Slice

任何会影响产品/算法行为的 Exploration 任务，在开始实现前至少明确：

- **Hypothesis**：当前要验证的业务/算法假设；
- **PRD Basis**：对应 PRD 条款或明确说明该想法尚未进入正式 PRD；
- **Visible Outcome**：用户最终应在哪个页面/结果看到什么；
- **Vertical Slice**：`input → compute → persistence → API → frontend` 的最短链；
- **Correctness Risks**：future leakage、ownership、trade_date、canonical、fallback、数据安全等；
- **Required Tests**：必须执行的 unit / contract / integration / runtime 验证；
- **Deferred**：与当前假设判断无关的治理、标准化、重构和其他域。

以上内容可以在任务计划或对话中表达；除非已有文档规则要求，不得为了形式新增报告文件。

### 5.1 新需求或行为变化

适用于：新增能力；改变预期行为；改变契约或数据定义；改变主要流程或交互；
改变验收标准；发现预期行为从未被明确规定。

**Exploration 默认工作流：**

用户发起 PRD 更新或确认现有 PRD 依据
→ 明确 hypothesis / visible outcome / vertical slice
→ 审查业务逻辑
→ 审查代码逻辑与现有实现路径
→ 按 PRD 做最小必要实现
→ 运行 modified-scope unit tests
→ 按风险运行必要 Contract / Integration / PG tests
→ 用真实数据执行当前 slice
→ 验证 API 与 frontend 实际字段绑定
→ 提交候选实现
→ 用户进行产品/视觉/价值验收
→ STOP。

只有重要行为、契约、Schema 或运行方式变化时更新唯一相关 Change；Maps/Runbooks 仍需用户验收后单独授权。

原则：
- **前端可见不是正确性的替代品**；业务逻辑、代码逻辑和必要测试必须先通过。
- **全域治理也不是当前假设验证的前置条件**；与当前 slice 无关的 P2/P3 默认 Deferred。
- 只有命中 §0.4 时才进入 Hardening。

### 5.2 小 Bug

当任务同时满足以下条件时使用本路径：预期行为已经明确；不改变需求或契约；
问题范围局部；可以直接验证修复效果。

工作流程：复现 Bug → 确认预期行为 → 找到根因 → 做最小必要修复 →
进行 modified-scope unit test 和必要的直接回归验证 → 如涉及真实数据/API/前端则验证对应技术链 →
提交聚焦本问题的修改。

普通小 Bug 默认：不更新 PRD、Maps 或 Runbooks；
除非修复具有重要长期价值否则不单独记录 Change；不顺带做无关重构；
不运行无关的大范围测试；不为了形式增加流程或文档。

如果预期行为本身不明确，必须把任务重新归类为「新需求或行为变化」。

### 5.3 探索性实验

适用于尚未确认的想法、备选实现、参数比较或技术可行性测试。

工作流程：明确实验问题与判断标准 → 确认实验是否需要真实数据 → 实现最小可用实验 →
完成与实验逻辑匹配的 unit tests → 必要时做 targeted integration / real-data sampling →
让用户观察结果 → 放弃该方案或决定正式采用。

未确认的实验阶段不更新正式 PRD 和 Maps。决定采用后再进入「新需求或行为变化」路径。

探索性实验也不得绕过 Correctness Gates：若实验输出将用于判断理论合理性，必须保证时间口径、输入数据、核心算法实现和结果读取链真实可信。

### 5.4 不改变行为的重构

适用于外部行为保持不变的代码调整。

工作流程：确认必须保持不变的行为 → 仅重构相关实现 → 运行受影响 unit tests 和必要回归验证 →
如果代码入口/职责/依赖/数据流发生变化则报告 Maps 同步需要，等待用户授权 →
只有结构变化具有实质影响时才记录 Change。目标行为未变化时不修改 PRD。

Exploration 模式下不得为了“以后可能需要”主动发起与当前 hypothesis slice 无关的大规模重构。

### 5.5 数据修复或历史回填

当数据违反现有规则时：确认正确的数据规则 → 明确影响范围 → 修复或回填数据 →
验证结果和重复执行行为 → 重要变化更新 Change；Maps 等待用户验收后单独授权。

如果变化的是数据规则本身，使用「新需求或行为变化」路径。

涉及真实数据不可逆风险时，即使项目处于 Exploration，也必须按 §0.4 升级风险处理，不得以迭代速度为由跳过安全检查。

### 5.6 故障或紧急恢复

当运行系统或关键流程发生故障时：确认故障事实和影响 → 优先恢复必要能力 →
定位根因 → 实现并验证修复 → 只有当事故暴露出实质性实现/历史/操作变化时
才更新 Change 并报告 Maps/Runbooks 同步需要，等待用户单独授权。

不得因为先补文档、做全域审计或追求治理完备性而延误必要恢复。

## 6. 文档更新规则

**更新 PRD** 当且仅当：用户当前任务明确发起 PRD 更新，并且目标行为发生变化、新需求被确认、
验收标准变化、原需求存在歧义或契约/数据定义变化。

**更新 Maps** 当且仅当：用户验收实现后明确授权同步 Maps，并且代码入口变化；模块职责变化；依赖关系或数据流变化；
存储关系变化；任务或 Worker 关系变化；API 与前端关系变化；旧 Map 会导致后续开发从错误位置开始或误解系统结构。

**更新 Changes** 当：重要业务规则变化；重要契约变化；主要实现结构变化；
重要流程变化；重大数据修复或兼容性变化。

**更新 Runbooks** 当且仅当：用户在真实操作验收后明确授权同步，并且可重复执行步骤发生变化。

Exploration 模式下：
- 不得为了证明“完整闭环”而同步更新所有 Maps/Runbooks；
- 不得把文档同步作为当前 hypothesis 前端可见的默认 blocker；
- 只有当前实现入口、关键数据流、Schema、API 契约或长期维护事实发生实质变化时，才报告相应文档同步需求；
- 可在 3–5 个相关 hypothesis iteration 后，或某一业务模块进入 Stable/Hardening 前，再集中收口相关 Maps。

不得为了让任务显得更完整，而更新与任务无关的文档。

## 7. 工作原则

7.1 **先复现，再修改**：当问题可以被复现或核验时，不得仅凭直觉修改代码。

7.2 **优先解决根因**：避免通过堆叠特殊分支、重复逻辑或静默兜底来掩盖真实问题。

7.3 **最小必要修改**：修改范围应聚焦当前任务。没有明确理由时不扩大任务范围。

7.4 **流程与风险匹配**：局部问题使用轻量流程。只有当当前 slice 的正确性、安全、数据、契约或运行方式真正受到影响，或命中 Hardening Trigger 时才扩大验证范围。

7.5 **最小有效验证，但测试不可省略**：验证范围应足以证明受影响行为正确并覆盖最直接的回归风险。modified-scope unit tests 是代码变更的默认硬门；按风险增加 contract / integration / runtime。不得默认运行全系统测试、大范围重建或与当前修改无关的验证，也不得用“探索模式”跳过必要测试。

7.6 **如实保留不确定性**：未知、未验证、部分完成、阻塞和失败状态必须明确标记。没有证据时不得声称代码、测试、部署、数据修复或运行行为已经成功。

7.7 **文档必须有用**：文档的目的是降低未来理解和调试成本。不得让文档维护本身成为主要开发任务。

7.8 **避免过早抽象**：没有明确现实需求时不得主动引入新框架、新层级、通用抽象、治理系统、文档体系、额外流程。

7.9 **提交保持聚焦**：一个提交应尽量对应一个明确问题或一组紧密相关的修改，并能够说明验证结果。

7.10 **Value Before Governance**：在 Exploration 模式下，治理必须保护结果可信度和学习速度。只要不影响当前业务判断、代码正确性、测试真实性、数据安全、Runtime 或前端技术闭环，release-grade 完备性不得阻塞 hypothesis validation。

7.11 **Correctness Before Visibility**：目标是尽快看到真实前端结果，但不得以页面可见替代业务逻辑审查、代码逻辑审查和必要测试。

7.12 **Frontend 技术闭环边界**：
- 工程侧负责验证：`DB → service → API → frontend request → response schema → component binding`；
- 用户负责：视觉验收、研究价值判断、产品假设判断；
- IDE 不应把“用户是否已经打开浏览器”作为完成工程技术验收的前置条件；
- 用户看到页面也不能替代 API/字段绑定和测试证据。

7.13 **Two-Strike Architecture Rule**：第一次遇到局部问题优先局部、清晰、可测试地解决；只有同类真实问题至少第二次出现，或已经存在两个明确消费者/重复场景时，才考虑新增通用 abstraction、framework 或治理层。安全与数据正确性问题不受此条限制。

7.14 **Deferred Debt 必须有触发条件**：非当前 blocker 的治理/标准化/兼容性/文档/重构问题可以 Deferred，但应说明何时必须处理，例如进入 beta、接口冻结、正式 release、大规模迁移或出现第二个真实消费者。不得发现即修，也不得无期限遗忘。

7.15 **Hypothesis Slice 完成即 STOP**：当前 slice 已通过业务逻辑、代码逻辑、必要测试、真实 Runtime、API/frontend 技术绑定并达到用户可判断状态后，应停止扩展；不得自动继续实现其他域或 release hardening，除非它们就是当前 hypothesis 的组成部分或用户明确要求。

## 8. 基础安全边界

未经明确授权，不得：

- 执行影响范围不明确的破坏性操作；
- 删除持久化数据或资源的唯一副本；
- 暴露或提交密钥、凭据或私钥；
- 使用强制推送覆盖共享历史；
- 创建任何新的本地或远程分支（含 backup 分支）；仓库只保留 `main` / `dev` / `experiments`；
- 从 `dev` 切换到其他工作分支；所有 AI 助手默认直接在 `dev` 提交，需要可恢复点时使用 checkpoint commit；
- 修改、合并或推送 `main`；`experiments` 仅用于授权的隔离实验，不得作为远程开发部署来源；
- 静默改变已确认需求；
- 用虚假的成功状态掩盖失败；
- 新增未经确认的文档层或治理层；
- 在处理局部任务时进行大范围无关修改；
- 修改或删除 `8752028@qq.com`（受保护 Owner 账户）的 email、password_hash、status、角色、权限或订阅；清理测试数据前必须先排除此邮箱；
- 创建或复用任何独立/临时测试数据库（本地、远程、CI、Docker 容器均禁止），**唯一例外**是 `rules/80-deployment-migration.md` 定义的远程临时验证数据库 `bz_stock_verify_<sha>`。测试只允许两种模式：本地/CI 使用 `PURE_UNIT_TEST=1`（纯单元/mock，不连库、不联网）；真实 PostgreSQL 测试使用 `PANJI_REMOTE_VERIFY_DB_TEST=1`，且只能在 `panji-prod` 的远程验证库运行。本地和 CI 禁止连接 `bz_stock` 运行 pytest，禁止 DDL/Alembic；
- 本地启动 Scheduler、远程常驻 Worker、盘后编排或全市场任务；本地只启动 Backend、Frontend、Capture 和 SSH Tunnel；
- 在本地创建测试用户、测试邀请码、测试权限、测试快照或测试通知渠道；本地写入均为真实业务写入；
- 在命令、日志、浏览器自动化或报告中写入 Owner 真实密码；任何 IDE/Agent 不得自动登录 Owner 账户；
- 使用 `panji-server`/`55-server`/原始 IP 或任何非 `panji-prod` 别名访问盘迹远程开发运行服务器；远程开发运行 SSH 入口唯一为 `scripts/ops/panji-prod-ssh`（`panji-prod` 是历史兼容技术标识，不表示当前处于生产发布阶段），部署前必须运行 `scripts/ops/panji-prod-preflight`（详见 `rules/80-deployment-migration.md`）。

### 远程验证运行时（单可复用，CHANGE-20260806-012）

以下定义的是**允许且受保护的验证基础设施**，不是 Exploration 每轮必须执行的完整验证清单。

- 唯一正式入口 `scripts/ops/panji-verify`；废弃第二入口 `panji-verify-run` 已删除，不得恢复。
- 单可复用验证镜像 `panji-verify-runtime:current`（由 `backend/Dockerfile` 的 `verification` target 构建），Docker label `panji.verify.dependency-hash = SHA256(Dockerfile+pyproject+lockfile)`；入口以 `expected hash` 与运行容器 image label 两方比较，不一致才 rebuild 并 recreate。
- 单一长期容器 `panji-verify-python`（`command: sleep infinity` 常驻空闲，禁 Scheduler/Worker/Uvicorn/pytest/seed）；固定 Compose project `panji-verify`；不发布 host port；不每 SHA 重建镜像或 compose project。
- 最外层 single-flight `flock`（`/root/.panji-verify/verify.lock`）覆盖整个 remote lifecycle，并发第二 attempt 直接 exit 75；attempt 内不再持有第二层锁。
- attempt env（DATABASE_URL/MIGRATION_DATABASE_URL/TARGET_SHA/JWT_SECRET 等）由 `prepare_verify_environment.py` 生成到固定 runtime 路径 `/root/.panji-verify/runtime/attempt.env`（0600），经 `verify_exec.py` 在每个 fresh process 动态注入；容器常驻 env 仅持有稳定变量（APP_ENV/PANJI_SCHEDULER_ENABLED/TZ）。
- 当前注册 plan 请求的 gate（Migration/PG/Seed/E2E）串行以 `docker exec panji-verify-python verify_exec.py <cmd>` 运行 fresh process；异常/timeout/interrupted 以 `docker restart panji-verify-python` 恢复干净环境（不删 container/image/network/PG/Redis/稳定栈）。
- `full-closure` 验证执行路径不依赖 Redis，仅连 PostgreSQL；verification 不连接 Redis，不引入 `verify-redis` 容器，也不强制复用 `trading-redis`。
- 验证库 `bz_stock_verify_<40位SHA>` 跑在已有 `trading-postgres` 容器内（DS-110），不新建 PG 容器/Volume；cleanup 只 drop 该库 + 删 attempt 临时状态，不 `compose down`、不删 Volume、不 `FLUSHALL`。

**注册验证计划：**
- `targeted-pg`：Exploration 默认远程 PG 证据；只做 schema upgrade + 已注册 PG contract tests；
- `migration-roundtrip`：只验证 Migration upgrade/downgrade/upgrade；
- `full-closure`：Hardening/Release 使用的 PG + Synthetic Seed + Closure E2E。

**Exploration 路由规则：**
- 存在 `panji-verify` 不等于每轮必须运行 `full-closure`；
- 仅当当前 slice 需要真实 PG/remote integration 证据时才调用相关验证能力；
- 不得为了“形式完整”自动追加 Seed/E2E/全闭环 gate；
- full-closure、migration round-trip、production-clone rehearsal 默认属于 Hardening/Release 或明确高风险变更；
- 若相关 `rules/` 或正式验证计划仍把 full-closure 作为所有开发任务的默认要求，应视为治理路由待同步项，未经用户授权不得自行绕过或修改。

### 允许的远程临时验证数据库

仅允许在以下**全部**条件成立时，由正式验证脚本在 `panji-prod` 已有 PostgreSQL 容器内创建 `bz_stock_verify_<7到40位SHA>`：

- 位于 `panji-prod` 已有 PostgreSQL 容器；
- 数据库名严格匹配 `bz_stock_verify_<40位SHA>`；
- 不新建 PostgreSQL 容器或 Volume；
- 不从验证环境写入 `bz_stock`；
- 由正式验证脚本创建、检查和删除；
- 应用连接后必须执行 `SELECT current_database(), current_user;` 并确认数据库名是验证数据库；
- 只用于 Migration、PG 集成、Synthetic E2E 和远程手动验收；
- 每次远程验证或调试尝试结束后，无论成功、失败、取消或超时，都必须先导出最小诊断证据，再由正式清理入口删除该次尝试创建的验证容器、网络、临时文件和精确命名验证数据库；
- 上述自动清理授权只覆盖本次尝试创建且可由 target SHA / Compose project 精确识别的临时资源；永不覆盖 `bz_stock`、共享 PostgreSQL/Redis Volume、稳定运行容器、受保护镜像或来源不明资源。

详见 `rules/80-deployment-migration.md`。

### 任务范围授权

用户批准一份包含明确目标、数据库名、部署目标、允许操作和停止条件的执行计划，即视为对该计划完整闭环的一次授权。该授权覆盖计划内明确列出的：修改代码、测试、Migration、配置和 Change，提交并推送 `dev`、创建验证数据库、对验证数据库执行 Migration、启动验证栈、写入验证数据、执行验证任务，以及每次验证尝试结束后的强制资源清理。计划授权不得隐式覆盖 PRD、Maps、Runbooks 或治理文档；这些文档仍分别遵守 §4.1/§4.2 的明确授权门。

远程验证授权不自动包含稳定运行栈部署或 `bz_stock` 数据操作授权。

**同一闭环内不得逐条重复索要授权。** 只有以下情况必须重新询问：

- 要操作 `bz_stock`；
- 要部署到正式运行栈；
- 要删除计划外数据或资源；
- 实际动作超出批准计划；
- 检测到数据损坏或不可逆影响。

项目特定的高风险操作和环境约束，应记录在 `rules/` 或对应 Runbook 中。

## 9. 完成标准

### 9.1 Exploration Definition of Done

一个 hypothesis iteration 只有同时满足以下条件，才算达到“可以交给用户判断”的工程完成状态：

1. 当前目标 / hypothesis 明确；
2. 对应 PRD 依据明确，或已明确标注为尚未进入 PRD 的实验；
3. 业务逻辑已审查；
4. 代码逻辑和关键数据链已审查；
5. 实现范围与当前 vertical slice 匹配；
6. modified-scope unit tests 已完成并通过；
7. 必要的 contract / integration / PG tests 已按风险完成并通过；
8. 真实数据/真实运行证据足以证明当前 slice 工作；
9. API 返回正确 canonical result；
10. frontend 实际 endpoint、schema、字段映射和组件绑定已验证；
11. 未验证结果没有被描述为事实；
12. 当前结果已达到用户可以进行视觉、业务价值或理论合理性判断的状态。

达到以上条件后应 **STOP 并交给用户验收**。不要求：

- 所有域同时完成；
- 九节点 fully_ready；
- full PURE_UNIT / full regression；
- full RTM / full governance audit；
- production clone / release certification；
- 与当前 hypothesis 无关的 Maps/Runbooks 同步；
- 无关 P2/P3 engineering debt 清零。

### 9.2 Hardening / Release Definition of Done

只有命中 §0.4 Hardening Trigger 后，才根据相关 `rules/`、PRD、Acceptance、Migration、Deployment 和 Runbook 要求扩大到完整 release-grade 验证、兼容性、数据迁移、运行闭环和文档收口。

### 9.3 通用完成要求

无论哪种模式，任务完成时都必须满足：

- 修改范围与任务相匹配；
- 受影响行为已得到有效验证；
- 必要单元测试没有因为流程轻量化被跳过；
- 不确定性和失败如实保留；
- 数据安全边界没有被突破；
- 提交内容保持聚焦；
- 需要 PRD、Maps、Runbooks 或治理同步但未获授权时已如实报告；
- Deferred Debt 如可能影响后续阶段，应记录触发条件。

完成不以修改文件数量、治理文档数量或验证 gate 数量衡量。
完成的判断标准是：**当前阶段真正需要证明的行为已经被可靠证明，并且没有为了工程形式完整而阻塞产品学习速度。**
