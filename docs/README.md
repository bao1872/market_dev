> 文档状态：CURRENT DESIGN BASELINE  
> 基线日期：2026-07-02  
> 已核对代码基线：`6f5ae2cec6b24dbd1b7bf6f23477f5e6f5096822`（`refactor/access-v2-platform-recovery`）  
> 事实来源：代码库 + 项目负责人截至 2026-07-02 已确认的产品与架构要求  
> 维护要求：任何代码、配置、测试、部署或文档修改都必须同步更新相关当前设计文档，并新增 CHANGE 记录。  
> 注意：该代码基线用于设计核对，不代表已经满足合并 `main` 或生产发布条件。
> 对齐口径：`CURRENT` 表示已确认设计，不等同于代码已完成；代码未实现、未验证或生产表现不一致的内容，必须在 `18-code-doc-alignment.md` 标为 `KNOWN_GAP`。

# 盘迹（PanJi）项目文档入口

## 1. 文档目标

这套文档描述系统当前确认的产品、前端、后端、数据、API、权限、任务、参数、部署和测试设计。它不替代代码，也不保存旧设计历史。

文档系统分为：

1. `docs/current/`：当前有效设计，直接写当前应该如何运行；
2. `docs/changes/`：变化历史，记录为什么修改、修改前后行为、影响范围、分支、Commit 和测试；
3. `docs/archive/`：只保存已废弃或历史材料，不作为当前需求来源。

代码代表当前实际实现，文档代表当前确认设计。两者冲突时必须登记到 `18-code-doc-alignment.md`，结合最新 CHANGE、Git、测试、生产行为和项目负责人要求裁决，任何一方都不能被默认视为绝对正确。

## 2. 状态定义

| 状态 | 含义 |
|---|---|
| `CURRENT` | 当前确认并采用的设计；不单独代表代码已完成 |
| `EXPERIMENTAL` | 正在验证的临时设计，仍需完整记录 |
| `PLANNED` | 已确认但尚未完成实现 |
| `PAUSED` | 暂停实施，不应继续扩展 |
| `OPEN` | 尚未作出最终决定 |
| `DEPRECATED` | 已废弃，不得从旧代码、旧分支或旧注释恢复 |
| `KNOWN_GAP` | 当前确认设计与代码、测试、部署或生产表现不一致 |

## 2.1 代码对齐判定

本基线固定核对 `refactor/access-v2-platform-recovery` 的 `6f5ae2cec6b24dbd1b7bf6f23477f5e6f5096822`。对齐结论分两层：

1. **事实对齐**：文档准确记录该 Commit 的代码行为、已确认设计和已知差异；
2. **实现闭环**：只有 `18-code-doc-alignment.md` 对应条目关闭并有测试、CI 或生产验收证据后，才能声明代码已经符合设计。

因此，本包可以作为该 Commit 的最终文档基线，但不能被用来证明所有 `KNOWN_GAP` 已经修复。后续代码 Commit 变化时必须建立新的 CHANGE 并更新基线，不属于本版结论反复。

## 3. 当前设计文档地图

本项目在通用 00–15 基线基础上，保留三个项目专用拆分文档：领域术语、策略指标契约、代码文档对齐表。它们只能细化职责，不能复制其他文档的权威定义。

| 文档 | 唯一负责的事实域 |
|---|---|
| [00-project-overview.md](current/00-project-overview.md) | 项目定位、边界、技术栈和模块总览 |
| [01-product-requirements.md](current/01-product-requirements.md) | 当前 PRD、用户场景、功能状态和验收目标 |
| [02-domain-glossary.md](current/02-domain-glossary.md) | 术语名称和含义，不定义业务规则 |
| [03-business-rules.md](current/03-business-rules.md) | 业务判断、状态转换、资格、冻结和发布规则 |
| [04-business-workflows.md](current/04-business-workflows.md) | 端到端流程及成功、失败、重试和幂等 |
| [05-system-architecture.md](current/05-system-architecture.md) | 系统组成、部署单元、依赖方向和数据流 |
| [06-frontend-design.md](current/06-frontend-design.md) | 路由、页面职责、页面状态和前端数据边界 |
| [07-backend-design.md](current/07-backend-design.md) | API、Service、Repository、计算和 Worker 职责 |
| [08-data-model.md](current/08-data-model.md) | 实体、字段语义、约束、生命周期和迁移原则 |
| [09-api-contracts.md](current/09-api-contracts.md) | API 权限、请求响应、错误、分页、筛选和兼容 |
| [10-permissions-security.md](current/10-permissions-security.md) | 身份、角色、订阅资格、所有权、Token、Secret 和审计 |
| [11-jobs-integrations.md](current/11-jobs-integrations.md) | Worker、Scheduler、行情源、飞书、截图、重试和恢复 |
| [12-strategy-indicator-contracts.md](current/12-strategy-indicator-contracts.md) | DSA 与监控策略的输入、输出、完整性和展示契约 |
| [13-configuration-parameters.md](current/13-configuration-parameters.md) | 参数含义、当前值、机器事实源和可修改范围 |
| [14-deployment-operations.md](current/14-deployment-operations.md) | 容器、构建、迁移、部署、健康、回滚和运维 |
| [15-testing-acceptance.md](current/15-testing-acceptance.md) | 测试层级、关键回归和完成门禁 |
| [16-ui-design-system.md](current/16-ui-design-system.md) | 品牌、布局、组件、状态、图表和可访问性 |
| [17-open-decisions.md](current/17-open-decisions.md) | 仅保存尚未决定的问题，不保存已确认规则 |
| [18-code-doc-alignment.md](current/18-code-doc-alignment.md) | 已确认设计与当前实现之间的临时差异及关闭证据 |

## 4. 单一事实源

同一事实只能有一个负责文档：

- 功能范围：`01-product-requirements.md`
- 业务允许与禁止：`03-business-rules.md`
- 流程顺序：`04-business-workflows.md`
- 页面和路由：`06-frontend-design.md`
- 后端职责：`07-backend-design.md`
- 数据实体和约束：`08-data-model.md`
- API 字段和错误：`09-api-contracts.md`
- 权限资格：`10-permissions-security.md`
- Worker 和第三方：`11-jobs-integrations.md`
- 策略输出和完整性：`12-strategy-indicator-contracts.md`
- 参数值和所有权：`13-configuration-parameters.md`

其他文档只能链接引用，不能复制一套独立定义。

## 5. 修改前强制流程

任何代码、测试、配置、部署或文档修改前必须：

1. 阅读本文件和 `00-project-overview.md`；
2. 根据任务阅读相关设计文档；
3. 核对真实入口、调用链、数据表、Worker、第三方和测试；
4. 新建独立 Git 分支；
5. 先建立 CHANGE 记录；
6. 输出“当前功能、前端、后端、数据、API、任务、文档、冲突、修改范围、不修改范围、预计更新文档”的理解说明；
7. 发现冲突时先登记，不得直接扩大修改。

## 6. 修改后强制闭环

所有修改必须同时完成：

- 修改代码或配置；
- 修改测试；
- 将相关 `docs/current/` 直接改成新状态；
- 补全 CHANGE 记录并更新 CHANGELOG；
- 检查 `18-code-doc-alignment.md`；
- 运行文档、后端、前端、迁移和部署配置检查；
- 通过 PR 合并，禁止直接修改 `main`。

完成标准：

```text
代码实现
= 当前设计文档
= API 和数据契约
= 测试验证
= 部署配置
```
