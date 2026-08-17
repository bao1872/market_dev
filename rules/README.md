# 盘迹规则体系

`AGENTS.md` 是项目阶段、任务路由和最高安全边界的入口；`rules/` 保存长期有效、可执行的详细规则。

当前项目阶段由 `AGENTS.md` 定义。当前默认：

- `PROJECT_STAGE = EXPLORATION`
- `DEFAULT_MODE = FAST_ITERATION`

## 1. 设计原则

规则体系只解决三件事：

1. **保护结果可信度**：业务逻辑、代码逻辑、测试、数据安全、时间因果和真实运行证据不能因“探索模式”而降低。
2. **保护学习速度**：与当前 hypothesis slice 无关的 release-grade 完备性、全域治理、标准化和重型验证不得成为默认 blocker。
3. **按风险升级**：只有命中 Hardening Trigger 或真实高风险操作时，才启用完整发布、迁移、全域 closure 和 release certification。

规则不得把“工程形式完整”本身当作目标。

## 2. 权威文件

| 文件 | 主题 | Exploration 默认 |
|---|---|---|
| `00-core-governance.md` | 阶段路由、事实源、Hypothesis Slice、严重度、文档授权 | Always On |
| `20-market-data-computation.md` | MDAS、复权、点时、Canonical、DSA/SMC/Momentum/Chip、Chart、板块 | Always On |
| `25-engineering-implementation.md` | 通用工程实现规范（Cross-cutting Implementation Standard，不拥有产品/安全/测试/Git/部署合同） | Always On |
| `30-security-data-safety.md` | 账户、权限、秘密、真实业务数据安全 | Always On |
| `40-testing-quality.md` | 单元/合同/PG/真实数据/前端技术闭环的分层验证 | Always On |
| `50-git-development-flow.md` | dev-only、提交、推送、checkpoint、任务收尾 | Always On |
| `60-runtime-frontend-acceptance.md` | 真实 Runtime、API→前端绑定、代表性样本、人工产品验收 | Always On when applicable |
| `70-hardening-release.md` | full RTM、full closure、全面回归、release certification | Triggered Only |
| `80-deployment-migration.md` | 远程部署唯一性、Migration 风险分级、运行身份、资源安全 | Always On; heavy gates risk-based |
| `90-deprecated-forbidden.md` | 已废弃路径和永久禁止项 | Always On |
| `PROTECTED_GOVERNANCE_FILES.json` | 受保护治理变更域 | Always On |

### 2.1 Compatibility Alias

为避免历史 PRD/Map/Change/Runbook 链接在一次治理迁移中全部失效，以下旧文件名暂时保留为只读跳转文件，不再承载独立规则：

- `30-access-security.md` → `30-security-data-safety.md`
- `80-deployment-data-safety.md` → `80-deployment-migration.md`
- `81-remote-deployment-only.md` → `80-deployment-migration.md`

兼容文件不得新增业务、测试、部署或安全合同；新代码、新文档和新治理引用统一使用新文件名。

## 3. 冲突与优先级

### 3.1 目标行为

按以下顺序判断：

1. 用户当前明确要求；
2. `docs/prd/` 已确认需求；
3. 其他产品文档或历史 Change；
4. 旧聊天、archive。

代码当前怎么做，不能反向覆盖已确认 PRD。

### 3.2 当前实现事实

按以下顺序判断：

1. 当前分支代码；
2. 数据库、运行状态、日志、API/前端真实行为；
3. `docs/maps/`；
4. 最新相关 Change；
5. 历史材料。

测试结果是“实现证据”，不能覆盖需求定义。

### 3.3 规则冲突

- `AGENTS.md` 的基础安全边界最高；
- `rules/` 中更具体的安全/业务不变量覆盖更宽泛流程描述；
- Exploration 与 Hardening 的冲突，先按 `AGENTS.md` 的 Stage Router 选择模式；
- Correctness / Security / Data Safety / Forbidden 永远不能被 Exploration 降级。

## 4. Exploration 的默认 Definition of Done

一个 hypothesis iteration 只有在以下工程门槛全部满足后，才进入用户产品判断：

`PRD/业务逻辑 → 代码逻辑 → modified-scope unit → 必要 contract/integration → 真实 runtime → API → frontend binding`

然后由用户判断：

- 视觉表达是否合适；
- 研究价值是否成立；
- 产品假设是否值得继续；
- PRD 是否需要下一轮调整。

用户“看到了页面”不能代替工程正确性；工程测试通过也不能代替用户产品价值判断。

## 5. Hardening Trigger

仅在以下情况之一发生时启用 `70-hardening-release.md`：

- 用户明确要求上线、release、正式部署、公开 beta、付费使用或全面代码审计；
- destructive / irreversible Migration；
- 权限、安全、支付、凭据或不可恢复用户数据风险；
- 大规模真实数据 backfill / rewrite；
- 核心 Schema、长期外部 API 或兼容契约准备冻结；
- shared foundation 变更可能造成大范围不可恢复影响；
- 用户明确要求 full RTM / full closure / production-clone rehearsal / release certification。

普通功能迭代、算法假设调整、UI 表达修改、局部 Bug 不得仅因“已有验证设施”而自动进入 Hardening。

## 6. Deferred Debt

Exploration 发现但不阻塞当前 hypothesis 的治理/标准化/兼容性问题应 Deferred，并记录：

- 问题；
- 当前影响；
- 为什么本轮不修；
- 触发处理的条件。

不要求建立独立 Debt 文档；优先记录在当前 Change、任务输出或现有项目记忆中，避免新增治理层。

## 7. 治理变更授权

只有用户当前任务明确要求调整治理、`AGENTS.md`、`rules/` 或治理检查器时，才允许修改本目录及绑定检查器。

普通功能开发即使发现规则不合理，也只能报告冲突，不得顺手改治理。
