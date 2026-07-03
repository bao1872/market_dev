# 盘迹 PanJi 文档入口（v2 候选结构）

> 文档包状态：RESTRUCTURED DOCS CANDIDATE V2  
> 生成日期：2026-07-03  
> 生成基线：main `40dd2287f0962910d2e272c468b3e5054abddaaf`  
> 原 current docs 实现核对基线：`ddca659b8c9d64b6a414da0b4bbd6f80f704aef1`  
> 目标：让新机器、新对话、新 AI agent 能快速恢复项目上下文，并降低 docs 维护成本。  
> 注意：本包尚未应用到仓库。落库前必须同步修改 `tools/check_docs_consistency.py` 与相关测试。

## 1. 这套文档解决什么问题

旧结构把当前事实拆成 `docs/current/00-18` 共 19 个文件，每个文件重复维护相同头部、日期和实现基线；`README`、`current`、`open-decisions`、`alignment`、`CHANGE` 之间有重叠，维护成本过高。v2 的目标是：

1. 保留防止 AI 乱改的核心事实；
2. 把“当前设计”和“实现地图”分开；
3. 把历史过程放回 `changes/`；
4. 把未决问题放回 `current/open-decisions.md`；
5. 让新 agent 可以先读少量文件快速上手，再按模块深入。

## 2. 新文档分层

| 层级 | 目录/文件 | 用途 |
|---|---|---|
| 快速上手 | `AI-ONBOARDING.md` | 新对话、新机器、新 agent 第一入口 |
| 还原检查 | `RESTORE-CHECKLIST.md` | 判断 agent 是否真正理解项目 |
| 当前设计 | `current/` | 当前产品、架构、契约、运维和验收事实 |
| 实现地图 | `maps/` | 真实代码入口、页面、API、表、Worker、测试、部署映射 |
| 变更历史 | `changes/` | CHANGELOG 与每次变更记录 |
| 归档历史 | `archive/` | 已废弃旧文档和旧 current 拆分 |
| 维护规则 | `MAINTENANCE.md` | 哪些文件手写，哪些半自动，怎么更新 |
| 迁移说明 | `MIGRATION-MAP.md` | 旧 `00-18` 到新文件的映射 |

## 3. 新 agent 推荐阅读顺序

```text
1. docs/AI-ONBOARDING.md
2. docs/current/MANIFEST.md
3. docs/current/00-product-business.md
4. docs/current/01-system-architecture.md
5. docs/maps/backend-module-map.md
6. docs/maps/frontend-route-map.md
7. docs/maps/worker-job-map.md
8. docs/current/code-doc-alignment.md
9. 与当前任务相关的 maps/current 文件
```

## 4. 当前设计文档

| 文件 | 职责 |
|---|---|
| `current/MANIFEST.md` | 当前 docs 基线、事实源、状态定义、维护边界 |
| `current/00-product-business.md` | 产品定位、用户、业务边界、核心规则 |
| `current/01-system-architecture.md` | 系统拓扑、模块边界、依赖方向、数据流 |
| `current/02-data-api-contracts.md` | 数据实体、API 契约、权限、Capture Token |
| `current/03-jobs-integrations-operations.md` | Worker、任务、飞书、Capture、部署与运维 |
| `current/04-frontend-ux.md` | 路由、页面职责、状态、UI 设计原则 |
| `current/05-testing-acceptance.md` | 测试层级、CI 门禁、验收标准 |
| `current/open-decisions.md` | 真正未决的产品和架构问题 |
| `current/code-doc-alignment.md` | 当前设计与实现/生产之间尚未闭合的差异 |

## 5. 实现地图

| 文件 | 回答的问题 |
|---|---|
| `maps/backend-module-map.md` | 后端各业务模块在哪里、入口是什么、边界是什么 |
| `maps/frontend-route-map.md` | 前端路由、页面、守卫、API 依赖 |
| `maps/api-route-map.md` | 后端 router、业务能力、权限要求 |
| `maps/database-model-map.md` | 核心表、实体语义、生命周期 |
| `maps/worker-job-map.md` | WORKER_TYPE、服务名、任务、表、风险 |
| `maps/notification-flow-map.md` | 事件、消息、Outbox、Delivery、飞书、截图完整链路 |
| `maps/test-coverage-map.md` | 关键业务规则对应测试 |
| `maps/deployment-runtime-map.md` | Compose 服务、环境变量、健康检查、生产验证 |

## 6. 使用规则

1. current docs 描述“现在应该是什么”；
2. maps 描述“代码现在在哪里”；
3. changes 描述“为什么变成这样”；
4. alignment 只记录当前仍未闭合的差异；
5. open decisions 只记录未决，不存已决历史；
6. 不要把同一业务规则复制到 5 个文件；
7. 代码变更必须更新相关 current + map + CHANGE；
8. 如果只是文件移动，优先更新 maps，不要重写产品规则。
