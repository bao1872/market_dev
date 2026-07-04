# 04 前端、路由与 UX

## 1. 前端职责

前端使用 React、TypeScript、React Router。前端负责页面、交互、DTO 到 ViewModel、图表和页面状态。

前端不得重新实现：

```text
后端权限
套餐额度
DSA 算法
Node Cluster 算法
发布门禁
监控资格
```

## 2. 路由与守卫

| 路由 | 守卫 | 页面 |
|---|---|---|
| `/` | Public | 门户 |
| `/login` | Public | 登录/注册 |
| `/subscription-expired` | Authenticated | 续期 |
| `/membership-expired` | Redirect | 兼容跳转 |
| `/capture/stock/:symbol` | Capture Token | 截图专用页面 |
| `/overview` | Subscriber/Admin | 服务总览 |
| `/screener` | Subscriber/Admin | 趋势选股 |
| `/watchlist` | Subscriber/Admin | 我的自选 |
| `/stock/:symbol` | Subscriber/Admin | 个股详情 |
| `/messages` | Authenticated | 历史消息 |
| `/settings` | Authenticated | 账户和通知渠道 |
| `/admin/*` | Admin | 管理页面 |

刷新后必须重新调用 `/me/access`，不能永久相信本地缓存。

## 3. 页面职责

### 趋势选股

- 固定读取最新完整 published 批次；
- 默认无隐式筛选；
- 展示 source_total、filtered_total、成功、失败、跳过和覆盖率；
- 批次不完整显示阻断，不伪装正常。

### 我的自选

- 展示股票、价格、涨跌幅、上下节点、POC、最近事件；
- 新增/删除/恢复后刷新服务器状态；
- 到期用户不加载列表，进入续期路径；
- 已存在、软删除、额度不足提示不同。

### 个股详情

- K 线、指标和截图共享行情快照；
- 展示 as_of、数据源、partial、degraded；
- DSA 与 Node 图层可开关；
- 截图区设置 render-ready 标志；
- 按 timeframe 请求对应根数（1d=250、15m=4000、1h=1200、1w=260、1mo=120、1m=2），与 Node Cluster / indicator_contract 对齐；
- 实时报价通过 `mergeRealtimeQuoteIntoBars` 合并到最后一根 K 线用于显示：1d 保留日期语义并跨日追加实时 bar，intraday（15m/1h 等）使用 `quote.update_time`；`baseBars` 仍用于指标计算，避免污染算法输入；
- 顶部报价条优先使用实时报价，fallback 到最后一根 bar。

### 消息与飞书

- 消息显示股票、事件时间、详情入口；
- 文字和图片显示独立状态；
- partial_failed 展示失败步骤和仅重试图片；
- Worker 不可用时不显示整体成功。

### 管理页面

- 所有按钮调用真实 API；
- 启用、禁用、授予、续期、撤销、改套餐、重试都有 loading/error/refresh；
- 禁止用本地 state 或 Toast 模拟成功。

## 4. UI 状态

所有页面统一支持：loading、refreshing、empty、error、partial、permission、success。

行情、策略结果、任务和消息页面必须显示真实数据时间。图表不连虚假线，partial Bar 有视觉区别。

## 5. 视觉原则

深色、专业、研究型；不夸张承诺收益；上涨红、下跌绿，同时用文字或形状辅助，避免只依赖颜色。图表提供文本摘要，可访问性不能丢。
