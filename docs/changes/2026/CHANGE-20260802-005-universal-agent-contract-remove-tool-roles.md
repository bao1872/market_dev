# CHANGE-20260802-005 治理去角色化：删除工具专属规则，收敛为通用执行主体合同

- **日期**：2026-08-02
- **类型**：governance（治理结构变化，业务代码零改动）
- **影响面**：`AGENTS.md`、`rules/`、治理检查器、相关 PRD / Maps / Runbook 引用
- **状态**：生效（规则与检查器已改；无需部署，无需 migration）

## 1. 变更前

治理体系按**工具**划分角色，存在两个工具专属规则文件：

| 文件 | 内容 |
|---|---|
| `rules/60-trae-work.md` | TRAE Work 角色边界、能力矩阵、dev 提交约束 |
| `rules/70-trae-cn.md` | TRAE CN 多模式职责（开发/测试/观察/部署/排障/紧急修复模式）、一轮闭环模式、固定十步执行顺序、固定 ledger 路径、工具专属最终状态值 |

由此产生的问题：

1. **同一操作因工具不同而规则不同**——例如"能否连生产""能否部署"取决于工具名称，而非操作本身的风险；
2. **规则重复与漂移**——测试纪律、CI 读取、部署证据要求在角色文件与 `40/50/80` 中各写一份，且已出现相互冲突（如 CI 是否为部署前置条件）；
3. **无法覆盖新工具**——任何未被列名的 IDE / Agent 处于规则真空；
4. **一次性任务规则被固化为长期规则**——十步执行顺序、ledger 路径、闭环模式本质是某轮任务的操作流程，不具备长期价值。

## 2. 变更后

### 2.1 唯一通用合同

> 所有 IDE、编码助手和自动化 Agent 遵守同一套仓库规则。
> 治理按**实际操作**定义，不按 TRAE CN、TRAE Work、CodeBuddy、Codex 或其他工具区分。

声明位置：

- `AGENTS.md` §0「通用执行主体合同」（路由层最高声明）；
- `rules/50-git-development-flow.md`「通用执行主体合同」（详细条款）；
- `rules/README.md` 顶部（目录级约束）。

执行主体的权限边界由两项决定：**用户在本轮授予的授权** + `AGENTS.md` §8 基础安全边界，
与工具名称无关。

### 2.2 删除的文件

| 文件 | 处置 |
|---|---|
| `rules/70-trae-cn.md` | 删除 |
| `rules/60-trae-work.md` | 删除（审计确认同为工具专属角色规则） |

### 2.3 有长期价值规则的归属

| 原内容 | 新位置 | 编号 |
|---|---|---|
| 测试必须进入正式测试文件，禁止临时命令充当验收 | `40-testing-quality.md` | TQ-94 |
| 失败重跑上限 2 次 | `40-testing-quality.md` | TQ-95 |
| 禁止用未验证结论冒充事实 | `40-testing-quality.md` | TQ-96 |
| 页面验收三类证据（URL / Console / Network） | `40-testing-quality.md` | TQ-97 |
| 成功判定三要素（pointer + 版本 + 真实数据证据） | `40-testing-quality.md` | TQ-98 |
| CI 结论读取纪律（精确 SHA、降级查询、PG 不得 skipped） | `40-testing-quality.md` | TQ-99 |
| 提交与推送纪律（fetch / 祖先校验 / ff-only / 禁 force push / 精确 add） | `50-git-development-flow.md` | 通用执行主体合同 |
| 高风险操作按操作本身授权 | `50-git-development-flow.md` | 通用执行主体合同 |
| 生产只读核验默认姿势 | `80-deployment-data-safety.md` | DS-95 |
| 部署后必须留存证据 | `80-deployment-data-safety.md` | DS-96 |
| Migration 人工门禁 | `80-deployment-data-safety.md` | DS-97 |
| 禁止临时脚本代替代码修复 | `80-deployment-data-safety.md` | DS-98 |

### 2.4 明确**未**迁移的内容（禁止恢复）

以下属于工具专属或一次性任务设计，**不迁移**到任何通用文件，并写入
`rules/90-deprecated-forbidden.md` 禁止恢复清单：

- 开发 / 测试 / 观察 / 部署 / 排障 / 紧急修复模式（工具运行模式表）；
- 一轮闭环模式；
- 固定十步执行顺序；
- 固定 ledger 路径（原 `/tmp/trae_review_*_ledger.md`）；
- 某次具体业务任务规则；
- CI 全绿后部署（与 CHANGE-20260802-003 冲突：CI 不是部署门禁）；
- 自动部署 PLANNED（与 CHANGE-20260802-003 冲突：自动部署已从治理体系移除）；
- IDE 专属最终状态值（如 `CLOSURE_PASSED` 强制枚举）。

原 `rules/80-deployment-data-safety.md` 中"Compact 恢复后必须读取 `/tmp/trae_review_*_ledger.md`"
的固定 ledger 路径要求同步删除，改为「以 `docs/maps/80-system-runtime.md` §2 为权威参数，
经 `panji-prod-preflight` 校验后继续」。

## 3. 防回潮检查

`tools/check_governance_rules.py` 新增两项检查：

- `check_universal_agent_contract()`：`AGENTS.md` 与 `50-git-development-flow.md`
  必须显式声明通用合同关键语句；
- `check_no_tool_specific_roles()`：
  1. `rules/60-trae-work.md`、`rules/70-trae-cn.md` 不得恢复；
  2. `rules/` 任意文件中出现 `TRAE CN` / `TRAE Work` / `CodeBuddy` / `Codex` / `Cursor` / `Copilot`
     用于定义角色或能力即判违规（"不按工具区分"的说明句豁免）；
  3. 上述 §2.4 八类被禁设计以正则扫描拦截。

负面清单段落（标题含"禁止恢复 / 已废弃 / 使用约束"等）内的行豁免——
这类段落必须写出被禁设计的名称才能禁止它。

同时删除 `check_trae_work_required_phrases()`（依赖已删除文件）
与 `check_autodeploy_still_planned()`（自动部署不再是 PLANNED 项）。

## 4. 受影响的引用（已同步）

| 文件 | 修改 |
|---|---|
| `AGENTS.md` | §1 必读入口移除两个角色文件；新增 §0 通用执行主体合同 |
| `rules/README.md` | 索引表移除两行；新增通用合同段；PLANNED 移除自动部署；使用约束新增去角色化条款 |
| `rules/90-deprecated-forbidden.md` | 新增「工具专属角色规则」禁止恢复条目；多分支条目去工具名 |
| `rules/80-deployment-data-safety.md` | 移除 `70-trae-cn.md` 交叉引用；删除固定 ledger 路径要求 |
| `docs/prd/70-review.md` | 引用改指 `40-testing-quality.md` TQ-97 / TQ-98 |
| `docs/prd/80-system-runtime.md` | 去工具名表述 |
| `docs/runbooks/after-close-recovery.md` | 成功判定三要素引用改指 TQ-98 |

`docs/changes/` 中的历史 Change 保留原文不改——旧事实由 Git 历史与 Change 记录保存，
当前 rules 不保留旧工具角色设计。

## 5. 验证

| 项 | 结果 |
|---|---|
| `tools/check_governance_rules.py` 部署与治理检查 | PASS |
| `tools/check_docs_consistency.py` | PASS |
| `tools/check_architecture.py` | PASS |
| `scripts/deploy/panji-deploy.test.sh` | 34 通过 / 0 失败 |
| `scripts/ops/test-panji-test-deploy-contracts.sh` | 第二轮 9 通过 / 0 失败 |
| 负向测试 | 工具角色文件、未来状态、自动 CI、旧部署入口、重复 Change 五类注入均被拒绝 |

本轮不涉及业务代码、数据库与运行服务器，`needs_deploy = false`。
