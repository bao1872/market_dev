# 行情与个股体验 Map

核验状态：待重建  
最后核验日期：未核验  
核验分支：未核验  
核验提交：未核验  
核验范围：尚未基于最新代码完整核验  
对应 PRD：`../prd/40-market-stock-experience.md`  
事实所有权：前端路由、页面、组件、筛选排序状态、详情来源列表和图层清单

> 本文件必须基于真实代码、数据、日志或运行结果填写。不得根据 PRD 推测实现已经存在。

## 1. PRD 实现映射

| PRD 条款 | 当前实现入口 | 状态 | 验证证据 |
|---|---|---|---|
| MX-01 | `/market` 路由待核验 | 部分已知 | 未核验 |
| MX-02 | 主表格和 EventStatePanel 待核验 | 部分实现 | 未核验 |
| MX-03 | 行业/概念筛选待核验 | 部分实现 | 未核验 |
| MX-04 | 列表排序待核验 | 已知曾有偏差 | 未核验 |
| MX-05 | 来源列表上下文待核验 | 已知曾有偏差 | 未核验 |
| MX-10 | `/stock/:symbol` 待核验 | 部分已知 | 未核验 |
| MX-11 | 单一 K 线待核验 | 部分实现 | 未核验 |
| MX-12 | `indicatorLayerManifest` 待核验 | 部分实现 | 未核验 |
| MX-13 | 中文标签待核验 | 部分实现 | 未核验 |
| MX-14 | 管理调试路由待核验 | 部分已知 | 未核验 |
| MX-15 | 页面状态待核验 | 未核验 | 未核验 |

## 2. 路由

| 路由 | 页面组件 | 权限 | 数据入口 |
|---|---|---|---|
| `/market` | 待核验 | 待核验 | 待核验 |
| `/stock/:symbol` | 待核验 | 待核验 | 待核验 |
| `/admin/stocks/:id/debug` | 待核验 | 管理员 | 待核验 |

## 3. 行情页组件

| 组件 | 路径 | 状态拥有者 | API |
|---|---|---|---|
| 主表格 | 待核验 | 待核验 | 待核验 |
| 筛选器 | 待核验 | 待核验 | 待核验 |
| EventStatePanel | 待核验 | 待核验 | 待核验 |
| 行列表 | 待核验 | 待核验 | 待核验 |

## 4. 个股详情组件

| 组件 | 路径 | 数据来源 | 责任 |
|---|---|---|---|
| K 线 | 待核验 | 待核验 | 主图 |
| 图层控制 | 待核验 | `indicatorLayerManifest` 待核验 | 图层开关 |
| 来源列表 | 待核验 | 待核验 | 上下文导航 |
| 状态提示 | 待核验 | 待核验 | loading/empty/error 等 |

## 5. 状态所有权

重点核验：

- 行情筛选状态；
- 排序状态；
- 当前来源列表；
- 自选排序；
- 详情当前 symbol；
- 返回后的上下文；
- 图层开关；
- 权限状态。

| 状态 | 权威拥有者 | URL | Store | Local State |
|---|---|---|---|---|
| 筛选 | 待核验 | 待核验 | 待核验 | 待核验 |
| 排序 | 待核验 | 待核验 | 待核验 | 待核验 |
| 来源列表 | 待核验 | 待核验 | 待核验 | 待核验 |
| 图层 | 待核验 | 待核验 | 待核验 | 待核验 |

## 6. 与 PRD 的已知偏差

需重点重新验证：

- 筛选后进入详情，来源列表是否仍跳回自选；
- 行情页与详情页自选排序是否一致；
- 图层是否全部由统一清单控制；
- `?debug=1` 是否彻底移除。

## 7. 验证入口

以用户真实交互路径验证，不使用 IDE 截图代替行为核验。

## 8. 前端验证结果（Phase 5B-0）

**验证环境**：本地原生 Backend (port 8000) + Frontend (port 8008) + SSH 隧道（panji-prod 43.136.118.82）；admin token 认证；2026-07-27。

**验证方式**：HTTP 状态码 + API 响应 + 浏览器运行错误（不安装浏览器自动化依赖，不以截图为唯一证据）。

| 路由 | 页面加载 | 主要 API | 数据展示 | 权限 | 阻塞原因 |
|---|---|---|---|---|---|
| `/` | 失败（无限刷新） | - | - | 公开 | 本地 Vite 无 Nginx 前置，`LandingPage` `window.location.replace('/')` 触发循环；生产环境 Nginx 精确分流不受影响 |
| `/login` | OK | - | 登录表单 | 公开 | - |
| `/market` | OK | `/market/stocks` 200、`/market/boards` 200、`/market/status` 200 | 行情列表 | 需登录 | - |
| `/replay` | OK | `/strategies` 200 | 策略列表 | 需订阅 | - |
| `/stock/000001` | OK | `/api/v1/stocks/000001/context` 200、`/api/v1/instruments/{id}/bars` 200、`/indicators` 200、`/structural-factors` 200、`/temporal-features` 200、`/quote` 200、`/chart-snapshot` 200 | 个股详情 + K 线 + 指标 | 需订阅 | - |
| `/settings` | OK | `/me` 200、`/me/access` 200、`/me/membership` 404（admin 无订阅） | 用户设置 | 需登录 | - |
| `/messages` | OK | `/messages` 200、`/messages/unread-count` 200 | 消息列表 | 需登录 | - |
| `/admin/stocks/000001/debug` | OK | `/api/v1/admin/stocks/000001/debug` 200 | 调试面板 | 管理员 | - |

**重定向路由**（SPA 客户端重定向，HTTP 200）：`/overview`、`/watchlist`、`/screener`、`/admin/strategies`、`/admin/stock-debug/:symbol`、通配符 `*`。

**结论**：除 `/` 受本地 Vite 限制外，所有用户级和管理员路由均正常加载，主要 API 返回 200，数据展示正确，权限模型符合预期。详细 API 响应记录在 `docs/maps/80-system-runtime.md` §9。
