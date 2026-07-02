> 文档状态：CURRENT DESIGN BASELINE  
> 基线日期：2026-07-02  
> 已核对代码基线：`6f5ae2cec6b24dbd1b7bf6f23477f5e6f5096822`（`refactor/access-v2-platform-recovery`）  
> 事实来源：代码库 + 项目负责人截至 2026-07-02 已确认的产品与架构要求  
> 维护要求：任何代码、配置、测试、部署或文档修改都必须同步更新相关当前设计文档，并新增 CHANGE 记录。  
> 注意：该代码基线用于设计核对，不代表已经满足合并 `main` 或生产发布条件。
> 对齐口径：`CURRENT` 表示已确认设计，不等同于代码已完成；代码未实现、未验证或生产表现不一致的内容，必须在 `18-code-doc-alignment.md` 标为 `KNOWN_GAP`。

# 06 前端设计

## 1. 技术边界

前端使用 React、TypeScript、React Router。前端负责页面、交互、DTO 到 ViewModel、图表和页面状态，不重新实现后端权限、套餐、DSA、Node Cluster、发布门禁或监控资格。

## 2. 路由与守卫

| 路由 | 守卫 | 页面职责 |
|---|---|---|
| `/` | Public | 门户与内测申请 |
| `/login` | Public | 登录和邀请码注册 |
| `/subscription-expired` | Authenticated | 套餐状态和续期 |
| `/membership-expired` | Redirect | 兼容重定向到 canonical 路由 |
| `/overview` | Subscriber/Admin | 服务总览 |
| `/screener` | Subscriber/Admin | 趋势选股 |
| `/watchlist` | Subscriber/Admin | 自选和监控状态 |
| `/stock/:symbol` | Subscriber/Admin 或专用 Capture Token | 个股研究和截图 |
| `/messages` | Authenticated | 历史消息；到期用户只读 |
| `/settings` | Authenticated | 账户安全、续期必需设置和通知渠道 |
| `/admin/*` | Admin | 管理功能 |

页面刷新后必须重新调用 `/me/access`，不能永久相信本地缓存的 `subscription_active`。

## 3. 认证与错误

- 401：清除普通登录 Token 和用户状态，跳转登录；
- 403：保留登录状态，显示无权限或跳转续期页；
- Capture Token 使用独立存储键和独立请求路径，不能成为普通 API Token；
- 普通 Access Token 和 Capture Token 不能互相覆盖。

## 4. 数据层

```text
API Client → Domain Adapter → ViewModel → Page/Component
```

字段、单位、时区、错误和空值转换集中在 Adapter；多个页面不得复制同一业务转换。

## 5. 页面设计

### 5.1 趋势选股

- 固定读取最新完整 `published` 批次；
- 默认 `metric_filters=[]`，不带隐式筛选；
- 分页大小不等于计算股票数量；
- 展示 source_total、filtered_total、成功、失败、跳过和覆盖率；
- 批次不完整时显示红色阻断状态，不伪装为正常发布；
- `current_trend` 是展示概念，后端真实字段为 `dsa_dir_bars`。

### 5.2 我的自选

- 展示代码、名称、实时/最新价格、节点、POC、最近事件；
- 新增、删除和恢复后刷新服务器状态；
- 到期用户不加载列表，直接进入续期路径；
- 已存在、软删除和额度不足使用不同提示。

### 5.3 个股详情

- K 线、指标和截图共享同一行情快照；
- 展示 `as_of`、数据源、是否 partial 和降级提示；
- DSA 与 Node Cluster 开关在图表顶部；
- 页面不能把陈旧数据库数据显示成“实时”；
- 截图区有明确 render-ready 标志。

### 5.4 消息与飞书

- 消息显示股票、事件时间、详情入口；
- 文字和图片显示独立状态；
- `partial_failed` 显示失败步骤和“仅重试图片”；
- 未创建图片或截图 Worker 不可用时不显示整体成功。

### 5.5 管理页面

- 所有按钮必须调用真实 API；
- 启用、禁用、授予、续期、撤销、改套餐和重试都有确认、loading、错误和刷新；
- 不得用本地 state 或 Toast 模拟保存成功。

## 6. 页面状态

所有页面统一支持 loading、refreshing、empty、error、partial、permission、success。刷新保留已有内容；错误显示可重试动作和诊断 ID；数据新鲜度和业务时间始终可见。
