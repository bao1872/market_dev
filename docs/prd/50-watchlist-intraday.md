# 自选与盘中监控 PRD

状态：草案  
最后确认日期：2026-07-28  
对应 Map：`../maps/50-watchlist-intraday.md`  
需求所有权：自选管理、盘中监控、异常信息和转发角色

## 1. 自选管理

### WI-01 自选能力

获得自选管理权限的用户可以添加、删除和查看自己的自选股票。

### WI-02 数量限制

自选股票数量由管理员在权限或邀请码中自由设置，不限制为固定套餐档位。

### WI-03 排序一致

行情列表、自选列表和个股详情来源列表使用一致的自选排序语义。

### WI-04 用户隔离

不同用户的自选数据必须隔离。

### WI-05 自选成员 canonical owner 与单一变更源（CHANGE-20260815-004）

所有入口（全局股票搜索、个股详情、自选 workspace、行情页自选行内入口）对自选成员的 add/remove 必须复用**同一个 canonical persisted 自选成员 owner**：

- persisted canonical membership 是唯一 source of truth；
- client cache / optimistic state 不得成为独立事实源；
- mutation 完成后客户端状态必须与 canonical persisted membership 收敛；
- 所有入口使用同一 mutation contract。

禁止任何页面维护 page-local duplicate 自选 state 作为第二 membership source of truth。具体后端 owner、模型、endpoint 与前端 hook 名称见 CHANGE-20260815-004 §3 Code Owner Map 与对应 Map。

自选范围的 base universe 由该 canonical membership 决定（见 MX-08）。

Watchlist Management 权限与 watchlist quantity limit 的语义 owner 为 PRD 60（PA-02 / PV2 系列）：limit 属 entitlement，由后端在 add 时 enforce（达上限返回错误），前端仅展示与交互；permission / invite / limit 的完整闭环见 PRD 60，不在本 PRD 重新定义。

## 2. 盘中监控

### WI-10 权限归属

盘中监控属于自选管理权限，不属于行情管理权限。

### WI-11 监控对象

盘中监控以用户有权管理的自选股票为主要对象。

### WI-12 信息定位

盘中监控提供异常和状态变化信息，不直接替用户下结论。

### WI-13 异常标记

志愿者或转发人员可以标记异常，但不得代表盘迹给出确定性交易结论。

### WI-14 信息收益

参与盘中测试或转发的人员可以第一时间接触盘中异常信息和系统测试结果。

### WI-15 盘中与盘后分离

盘中状态不得无意覆盖盘后正式发布结果。盘中计算和盘后正式结果应有明确来源和时间语义。

## 3. 验收标准

- 无自选权限的用户不能使用盘中监控。
- 行情管理权限不自动获得自选和盘中能力。
- 自选数量限制由管理员配置并被后端强制执行。
- 盘中信息能够区分事实、异常标记和结论。
- Map 能指向自选存储、排序、监控任务、消息入口和权限判断。

## 4. 监控事件类别稳定条款（CHANGE-20260728-010）

### WI-20 仅两类触发事件

盘中监控 `watchlist_monitor` 只保留两类触发事件：

- **结构**：SMC 的 BOS、CHoCH、EQH、EQL、Order Block first touch；
- **筹码共识**：`node_cluster_touch`。

布林带（Bollinger）不再触发盘中监控事件，不再参与盘中状态合并、事件统计、通知文案与事件到指标视图的映射。布林带算法本体、盘后 Bollinger 计算、个股详情页布林带图层工具栏不在本条款禁止范围。

### WI-21 任一事件固定生成组合图

任一结构事件或筹码共识事件触发时，每个事件独立生成一张图片；该图片固定同时展示“结构 + 筹码共识”两类指标，不再按事件类别选择单指标视图。

事件类别只决定：

- 触发焦点（`focus_event`）；
- 事件文字内容；
- 概览统计归类（结构 / 筹码共识）。

事件类别不再决定截图图层组合。截图视图固定为“结构 + 筹码共识”组合值。

### WI-22 事件文字与图片语义分离

事件文字只描述实际触发事件（结构事件描述结构位、内部/摆动、方向、形成时间等可用字段；筹码共识事件描述节点触碰）。

图片同时展示结构图层与筹码共识图层，与事件文字内容解耦：结构事件触发的图片也展示筹码共识，筹码共识事件触发的图片也展示结构。

### WI-23 历史兼容

- 历史 `CaptureJob.indicator_view` 字段及历史数据保留，仅做读取兼容；
- 历史 `node_cluster` / `smc` / `bollinger` 枚举值保留读取兼容；
- 新业务只写入固定组合值，不再写入单指标视图；
- 不删除历史数据，不新增数据库 migration。

### WI-24 不新增常驻资源

不新增常驻 Worker、数据库 migration、依赖、Compose 或部署脚本。本轮改造只调整监控器内部委托、监控配置 manifest、批次服务事件归类与文案、Capture Preset、API 转发与前端弹窗。
