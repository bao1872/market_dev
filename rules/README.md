# 盘迹规则体系

`AGENTS.md` 是 Constitution + Router；`rules/` 只保存项目不变量、重复真实 failure mode
和无法由更低层 owner 充分约束的安全合同。

当前项目阶段由 `AGENTS.md` 定义。当前默认：

- `PROJECT_STAGE = EXPLORATION`
- `DEFAULT_MODE = FAST_ITERATION`

## 1. Failure-mode-driven governance

长期治理只解决以下七类 failure mode：

1. Contract / SSOT drift；
2. false-green；
3. stale test 被误判为 runtime bug；
4. fixture 与 production contract 不一致；
5. required evidence 未被 Gate 实际执行；
6. crash/resume/lineage/idempotency 违规；
7. wrong environment 或 destructive data operation。

无法说明保护哪类 failure mode 的要求，不应升级为长期治理。能由 production owner、
schema、manifest、test、checker 或 runner 证明的合同，不在多个 Markdown 文件重复表达。

## 2. Risk router

- **Level 1 Normal Exploration**：局部非契约改动；modified-scope evidence。
- **Level 2 Contract-Sensitive**：owner、canonical、lineage、readiness、workflow、resume、
  idempotency、artifact、shared contract、evidence registration。
- **Level 3 Operational / Destructive**：migration、deployment、production data/runtime、
  repair、withdraw、bootstrap、destructive cleanup。

多个条件命中时取最高等级。详细路由由 `AGENTS.md` 唯一拥有。

## 3. 权威文件

| 文件 | 主题 | Exploration 默认 |
|---|---|---|
| `00-core-governance.md` | 阶段路由、事实源、Hypothesis Slice、严重度、文档授权 | Always On |
| `20-market-data-computation.md` | MDAS、复权、点时、Canonical、DSA/SMC/Momentum/Chip、Chart、板块 | Always On |
| `25-engineering-implementation.md` | Owner、production-path reuse、failure transparency、有界资源等项目实现不变量 | When applicable |
| `30-security-data-safety.md` | 账户、权限、秘密、真实业务数据安全 | Always On |
| `40-testing-quality.md` | 单元/合同/PG/真实数据/前端技术闭环的分层验证 | Always On |
| `50-git-development-flow.md` | dev-only、提交、推送、checkpoint、任务收尾 | Always On |
| `60-runtime-frontend-acceptance.md` | 真实 Runtime、API→前端绑定、代表性样本、人工产品验收 | Always On when applicable |
| `70-hardening-release.md` | full RTM、full closure、全面回归、release certification | Triggered Only |
| `80-deployment-migration.md` | 远程部署唯一性、Migration 风险分级、运行身份、资源安全 | Always On; heavy gates risk-based |
| `90-forbidden.md` | 永久禁止项 | Always On |
| `PROTECTED_GOVERNANCE_FILES.json` | 受保护治理变更域 | Always On |

## 4. 冲突与优先级

### 4.1 目标行为

按以下顺序判断：

1. 用户当前明确要求；
2. `docs/prd/` 已确认需求；
3. 其他产品文档或历史 Change；
4. 旧聊天、archive。

代码当前怎么做，不能反向覆盖已确认 PRD。

### 4.2 当前实现事实

按以下顺序判断：

1. 当前分支代码；
2. 数据库、运行状态、日志、API/前端真实行为；
3. `docs/maps/`；
4. 最新相关 Change；
5. 历史材料。

测试结果是“实现证据”，不能覆盖需求定义。

### 4.3 规则冲突

- `AGENTS.md` 的基础安全边界最高；
- `rules/` 中更具体的安全/业务不变量覆盖更宽泛流程描述；
- Exploration 与 Hardening 的冲突，先按 `AGENTS.md` 的 Stage Router 选择模式；
- Correctness / Security / Data Safety / Forbidden 永远不能被 Exploration 降级。

## 5. Exploration 的默认 Definition of Done

一个 hypothesis iteration 只有在以下工程门槛全部满足后，才进入用户产品判断：

`PRD/业务逻辑 → 代码逻辑 → modified-scope unit → 必要 contract/integration → 真实 runtime → API → frontend binding`

然后由用户判断：

- 视觉表达是否合适；
- 研究价值是否成立；
- 产品假设是否值得继续；
- PRD 是否需要下一轮调整。

用户“看到了页面”不能代替工程正确性；工程测试通过也不能代替用户产品价值判断。

## 6. Hardening Trigger

仅在以下情况之一发生时启用 `70-hardening-release.md`：

- 用户明确要求上线、release、正式部署、公开 beta、付费使用或全面代码审计；
- destructive / irreversible Migration；
- 权限、安全、支付、凭据或不可恢复用户数据风险；
- 大规模真实数据 backfill / rewrite；
- 核心 Schema、长期外部 API 或兼容契约准备冻结；
- shared foundation 变更可能造成大范围不可恢复影响；
- 用户明确要求 full RTM / full closure / production-clone rehearsal / release certification。

普通功能迭代、算法假设调整、UI 表达修改、局部 Bug 不得仅因“已有验证设施”而自动进入 Hardening。

## 7. Deferred Debt

Exploration 发现但不阻塞当前 hypothesis 的治理/标准化/兼容性问题应 Deferred，并记录：

- 问题；
- 当前影响；
- 为什么本轮不修；
- 触发处理的条件。

不要求建立独立 Debt 文档；优先记录在当前 Change、任务输出或现有项目记忆中，避免新增治理层。

## 8. 治理变更授权

只有用户当前任务明确要求调整治理、`AGENTS.md`、`rules/` 或治理检查器时，才允许修改本目录及绑定检查器。

普通功能开发即使发现规则不合理，也只能报告冲突，不得顺手改治理。

Maps 可在实现事实已核验且旧内容会误导时随代码同步；Runbooks 可在操作已真实执行
或由已验证自动化合同覆盖后随任务同步。两者均不得写计划为事实，也不产生部署或数据授权。
