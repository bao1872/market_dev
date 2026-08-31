# 盘迹 Maps

## 1. 定位

`docs/maps/` 是盘迹“当前实现实际上怎样工作”的项目记忆层。

Map 负责记录：

- 主要入口；
- 权威实现；
- 模块和调用关系；
- 数据与状态流；
- 当前实现状态；
- 验证证据；
- 与 PRD 的偏差；
- 已废弃路径；
- 尚未核验问题。

Map 不重新定义需求。

## 2. 与 PRD 的成对关系

领域 Map 与 PRD 使用相同编号：

| Map | 对应 PRD |
|---|---|
| `00-system-overview.md` | `../prd/00-product-scope.md` |
| `10-market-data.md` | `../prd/10-market-data.md` |
| `20-quant-model.md` | `../prd/20-quant-model.md` |
| `30-after-close.md` | `../prd/30-after-close.md` |
| `40-market-stock-experience.md` | `../prd/40-market-stock-experience.md` |
| `50-watchlist-intraday.md` | `../prd/50-watchlist-intraday.md` |
| `60-permissions-admin.md` | `../prd/60-permissions-admin.md` |
| `70-review.md` | `../prd/70-review.md` |
| `80-system-runtime.md` | `../prd/80-system-runtime.md` |
| `90-system-wide-implementation.md` | `../prd/90-system-wide-requirements.md` |

`technical/` 记录多个领域共同引用的技术事实，不单独定义产品需求。

## 3. 核验状态

- `已核验`：关键代码关系和必要运行证据已确认；
- `部分核验`：只确认部分路径，未核验范围已列出；
- `待重建`：现有内容明显过时，不能作为开发依据。

不使用“草案”描述 Map。

## 4. PRD 实现映射

每份领域 Map 必须包含：

| PRD 条款 | 当前实现入口 | 状态 | 验证证据 |
|---|---|---|---|

状态建议使用：

- 已实现并核验；
- 已实现但未运行核验；
- 部分实现；
- 未实现；
- 与 PRD 不一致；
- 未核验。

## 5. 更新规则

当以下内容变化时更新 Map：

- 主要入口；
- 权威实现位置；
- 模块职责；
- 数据流；
- 状态拥有者；
- API 契约；
- 表和关键字段关系；
- Scheduler、Worker 或发布链路；
- 前端路由和主要导航状态；
- 权限判断入口；
- 运行服务关系。

局部函数内部修复、样式和文案调整通常不更新 Map，除非旧 Map 会误导后续开发。

Map 是当前实现事实层，不采用独立审批作为事实同步前置条件。当代码任务已经核验了入口、
owner、数据流、契约或运行事实，且旧 Map 会误导后续开发时，应在同一任务同步对应 Map。
同步不得把计划、假设、未运行结果或 PRD 目标写成当前事实，也不构成部署或数据操作授权。

## 6. 技术地图

| 文件 | 事实所有权 |
|---|---|
| `technical/codebase-modules.md` | 仓库目录、模块职责和依赖边界 |
| `technical/data-storage.md` | PostgreSQL、Redis、表、Key 和数据所有权 |
| `technical/backend-api.md` | 后端入口、路由、Schema、Service 和调用方 |
| `technical/observability-debugging.md` | 日志、任务状态、健康检查和调试入口 |

## 7. 重要原则

1. 只记录已核验事实。
2. 计划不能写成当前实现。
3. 不确定时明确写“未核验”。
4. 不复制大段源码。
5. PRD 变化后，Map 可暂时记录与新 PRD 的偏差。
6. 不得修改 PRD 迁就错误实现。
