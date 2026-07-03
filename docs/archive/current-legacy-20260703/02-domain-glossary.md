> 文档状态：CURRENT DESIGN BASELINE  
> 设计基线日期：2026-07-03  
> 设计确认截止日期：2026-07-03  
> 实现核对基线：ddca659b8c9d64b6a414da0b4bbd6f80f704aef1  
> 实现核对分支：main  
> 最近一致性检查日期：2026-07-03  
> 事实来源：代码库 + 项目负责人截至 2026-07-02 已确认的产品与架构要求  
> 维护要求：任何代码、配置、测试、部署或文档修改都必须同步更新相关当前设计文档，并新增 CHANGE 记录。  
> 注意：该代码基线用于设计核对，不代表已经满足合并 `main` 或生产发布条件。
> 对齐口径：`CURRENT` 表示已确认设计，不等同于代码已完成；代码未实现、未验证或生产表现不一致的内容，必须在 `18-code-doc-alignment.md` 标为 `KNOWN_GAP`。

# 02 领域术语表

本文件只统一术语，不定义权限或业务判断。业务允许与禁止以 `03-business-rules.md` 为准。

## 1. 产品与账户

| 术语 | 定义 |
|---|---|
| 趋势选股 | 对完整已发布的全市场 DSA 特征结果进行查询、筛选和排序 |
| 自选股 | 用户主动关注的股票；仅有效会员进入新监控 |
| 个股详情 | 聚合历史 Bar、实时 partial Bar、指标、节点、事件和备忘录的研究页面 |
| Plan | `plans` 表中的套餐定义和能力上限 |
| Subscription | 普通会员当前套餐、有效期、状态和权益快照；替代旧 Membership 业务模型 |
| 有效订阅 | `status=active` 且 `starts_at <= now < expires_at` |
| 冻结 | 订阅无效时保留数据但禁止核心业务读取、修改、监控和新投递 |
| 历史消息只读 | 可读取已经生成的消息，不生成新事件、新消息或新飞书投递 |

## 2. 策略与发布

| 术语 | 定义 |
|---|---|
| StrategyDefinition | 稳定策略身份，例如 `dsa_selector`、`watchlist_monitor` |
| StrategyVersion | 不可变策略版本，状态为 draft/released/archived |
| StrategyRun | 某策略版本在某业务日期的一次批量运行 |
| computable universe | 满足市场、状态和最小行情要求，必须生成特征结果的股票集合 |
| skipped | 不可计算且属于允许原因的股票，必须保存 reason code |
| failed | 本应计算但执行失败的股票，不得伪装为未命中或 skipped |
| StrategyResult | 某股票在某运行下的完整特征结果 |
| published run | 已通过完整性与质量门禁、可供普通用户读取的不可变批次 |
| `published_run_id` | 查询、筛选、回放和展示绑定的发布批次标识 |
| `partial_failed` | 部分股票执行失败的运行状态，不允许自动发布 |
| selector | 对股票集合生成批量特征的策略类型，不等于预筛选器 |
| monitor | 对有效会员自选股和最新完成 Bar 判断事件的策略类型 |
| DSA | Dynamic Swing Anchored VWAP 趋势特征体系 |
| Volume Node Cluster | 基于成交量分布识别节点、POC 和上下支撑压力区的算法 |
| `visual_segments` | 仅用于图表分段渲染，不用于筛选 |
| `factor_per_bar` | 因果时间序列，用于计算、查询和回测 |

## 3. 行情与任务

| 术语 | 定义 |
|---|---|
| completed bar | 已结束且通过时间边界判断的正式 Bar |
| partial bar | 由最新 1m 数据聚合的当前未完成周期 Bar，不写入完成 Bar 表 |
| data source | `db`、`pytdx`、`hybrid` 或 `degraded` |
| as_of | 本次行情或页面数据真实对应的最新时间 |
| MonitorEvaluation | 对某股票、策略版本和源 Bar 的一次评估记录 |
| StrategyEvent | 评估产生的稳定事件，携带源 Bar 和事件快照 |
| SchedulerJobRun | 调度任务的状态、心跳、租约、计数和错误记录 |
| WorkerHeartbeat | Worker 实例、版本和最近心跳 |
| run key | 任务业务幂等键 |

## 4. 消息与截图

| 术语 | 定义 |
|---|---|
| Outbox | 与业务事务一起写入的待处理消息记录 |
| NotificationMessage | 站内消息或外部通知内容实体 |
| MessageDelivery | 一条消息向一个明确渠道的一次投递 |
| message group | 同一业务消息的文字、图片等投递关联组 |
| CaptureJob | 生成个股详情截图的任务 |
| `partial_failed` 消息 | 文字与图片至少一项成功、至少一项失败 |

## 5. 文档状态

`CURRENT`、`EXPERIMENTAL`、`PLANNED`、`PAUSED`、`OPEN`、`DEPRECATED`、`KNOWN_GAP` 的定义以 `docs/README.md` 为准。
