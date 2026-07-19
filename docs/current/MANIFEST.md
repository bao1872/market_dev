# Current Docs Manifest

> 文档状态：CURRENT DESIGN BASELINE  
> 实现核对基线：`18049da1c0487120c3ebebba711ab37a225b6b37`
> v2 文档包生成基线：`18049da1c0487120c3ebebba711ab37a225b6b37`
> 原 current docs 历史基线：`ddca659b8c9d64b6a414da0b4bbd6f80f704aef1`（归档参考，不参与一致性检查）  
> 设计基线日期：2026-07-05  
> 当前事实源：代码 + 已合并 PR + 生产只读审计 + 项目负责人确认  
> 注意：该文件是 v2 唯一基线头；其他 current 文档不再重复基线字段。

## 1. 文档状态定义

| 状态 | 含义 |
|---|---|
| CURRENT | 当前确认采用的设计，不单独代表实现已完成 |
| EXPERIMENTAL | 已进入验证但尚未最终定型 |
| PLANNED | 已确认但尚未实现 |
| OPEN | 尚未作出决定 |
| DEPRECATED | 已废弃，不得从旧代码或旧文档恢复 |
| KNOWN_GAP | 当前设计与代码、测试、部署或生产表现尚未一致 |
| CLOSED | 差异已有代码、测试、CI 或生产证据闭合 |

## 2. 单一事实源规则

| 事实类型 | 当前文件 |
|---|---|
| 产品定位、用户、业务规则 | `00-product-business.md` |
| 系统拓扑、模块边界、数据流 | `01-system-architecture.md` |
| 数据实体、API、权限、安全 | `02-data-api-contracts.md` |
| Worker、任务、飞书、Capture、部署 | `03-jobs-integrations-operations.md` |
| 前端路由、页面、UI 状态 | `04-frontend-ux.md` |
| 测试、CI、验收 | `05-testing-acceptance.md` |
| 研究特征矩阵与因果口径 | `06-research-feature-matrix.md` |
| Atomic Fact Contract V1 个股状态观察 | `07-atomic-fact-contract-v1.md` |
| 指标计算合同（业务含义/输入/语义/输出/调用方/版本） | `08-indicator-calculation-contracts.md` |
| 未决问题 | `open-decisions.md` |
| 当前差异 | `code-doc-alignment.md` |
| 真实代码入口 | `../maps/*.md` |
| 历史原因 | `../changes/records/*.md` |

## 3. 当前关键事实

- 产品名：盘迹 PanJi；
- 阶段：产品探索与内部验证；
- 技术栈：React + FastAPI + PostgreSQL + Redis + Docker Compose + 多 Worker；
- 用户模型：访客、有效会员、到期/无订阅会员、管理员、系统 Worker；
- 策略：生产仅 `dsa_selector` 与 `watchlist_monitor`；
- 飞书：Platform App only，Webhook 已永久删除；
- Capture：专用路由、专用 token、独立 worker；
- 生产审计：13 个服务 Up，飞书图文曾成功一次，但 partial_failed/仅重试图片生产 E2E 仍需验证；
- Worker heartbeat 僵尸清理已生产验证（ALIGN-023 CLOSED）：worker-watchdog 部署后 38 条 stale running 自动清理为 stopped。

## 4. 修改原则

1. 每次代码变更至少更新一个 CHANGE；
2. 只更新受影响 current/map 文件，不允许机械修改全部文件；
3. 如果代码与 current 不一致，先登记 alignment；
4. 如果只是历史原因，放 changes，不放 current；
5. 如果只是代码位置，放 maps，不放产品规则；
6. 不引入 OpenSpec，不改 monorepo，不全盘重构。
