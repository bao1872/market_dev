> 文档状态：CURRENT DESIGN BASELINE  
> 基线日期：2026-07-02  
> 已核对代码基线：`6f5ae2cec6b24dbd1b7bf6f23477f5e6f5096822`（`refactor/access-v2-platform-recovery`）  
> 事实来源：代码库 + 项目负责人截至 2026-07-02 已确认的产品与架构要求  
> 维护要求：任何代码、配置、测试、部署或文档修改都必须同步更新相关当前设计文档，并新增 CHANGE 记录。  
> 注意：该代码基线用于设计核对，不代表已经满足合并 `main` 或生产发布条件。
> 对齐口径：`CURRENT` 表示已确认设计，不等同于代码已完成；代码未实现、未验证或生产表现不一致的内容，必须在 `18-code-doc-alignment.md` 标为 `KNOWN_GAP`。

# 00 项目概述

## 1. 产品身份

- 品牌：**盘迹**（PanJi）
- 当前形态：面向 A 股个人投资者的多用户研究、全市场特征计算、自选股盘中监控和消息投递平台
- 当前阶段：产品探索与内部验证
- 核心路径：**发现机会 → 验证机会 → 持续追踪**

## 2. 目标用户

| 用户 | 当前目标 |
|---|---|
| 未登录访客 | 理解产品能力并申请内测 |
| 有效普通会员 | 查看趋势特征、管理自选、研究个股、接收站内和飞书通知 |
| 到期或无有效订阅会员 | 登录、查看账户与套餐、续期、维护账户安全、只读查看历史消息 |
| 管理员 | 管理用户、邀请码、订阅、策略版本、任务、投递和审计日志 |
| 系统 Worker | 更新行情、计算全市场特征、监控完成 Bar、生成事件、投递消息和截图 |

## 3. 当前核心能力

| 能力 | 设计状态 | 当前确认设计 | 对 `6f5ae2c` 的实现核对 |
|---|---|---|---|
| 公开门户与内测申请 | `CURRENT` | 落地页展示产品价值并收集申请 | 未发现本轮阻断差异，仍需 E2E 验收 |
| 邀请码注册与订阅 | `EXPERIMENTAL` | 使用 `admin`/`member`、`plans`、`subscriptions`；商业定价仍可调整 | 部分实现；管理员完整订阅管理仍是 `KNOWN_GAP` |
| 趋势选股 | `CURRENT` | 对可计算全市场股票生成特征，不在计算阶段筛选；用户只在已完整发布结果上查询 | `KNOWN_GAP`：部分接口缺少有效订阅校验，残缺批次仍可能发布 |
| 自选股 | `CURRENT` | 有效会员添加后自动进入监控；订阅到期时数据保留但冻结访问与监控 | `KNOWN_GAP`：读写接口未全部接入统一订阅依赖，代码存在新旧额度函数未收口 |
| 盘中监控 | `CURRENT` | 仅处理已完成 1 分钟 Bar，并按用户资格、策略版本、股票和源 Bar 幂等 | `KNOWN_GAP`：eligible user 在 Monitor/Recipient/Outbox/Delivery 全链路仍需验证 |
| 个股详情 | `CURRENT` | 展示历史已完成 Bar 与实时聚合尾部、指标、节点、备忘录和飞书分享 | `KNOWN_GAP`：图表场景尚未统一补齐数据库尾部和盘中 partial Bar |
| 消息中心 | `CURRENT` | 查看历史消息和投递状态；到期用户仅保留历史消息只读能力 | 部分实现；到期只读和投递资格仍需端到端验证 |
| 飞书通知 | `CURRENT` | 文字与图片是同一消息组的两个独立投递结果，允许部分成功和仅重试图片 | `KNOWN_GAP`：生产仅收到文字，图片失败状态和独立重试尚未闭环 |
| 管理后台 | `CURRENT` | 管理用户、邀请码、订阅、策略、任务、投递和审计日志；无实现的按钮不得假成功 | `KNOWN_GAP`：已存在邀请码、列表和部分审计；用户启停、授予/续期/撤销/改套餐仍不完整 |
| 多策略组合 | `DEPRECATED` | 当前生产版本不支持，未来必须作为独立实验重新设计 | 与当前确认方向一致 |
| 自动交易和资金管理 | 不在范围 | 不连接券商账户，不下单，不管理资产，不保证收益 | 与当前代码边界一致 |

## 4. 系统边界

盘迹负责行情准备、数据质量、全市场特征计算、发布批次、自选监控、事件消息、个股研究、飞书投递和任务可观察性。

盘迹不负责自动下单、投资收益承诺、将单一指标包装成确定性买卖信号，也不允许普通用户修改生产算法参数。

## 5. 技术栈总览

- 前端：React、TypeScript、React Router、Nginx
- 后端：FastAPI、SQLAlchemy Async、Pydantic
- 数据库：PostgreSQL 16 容器，持久化到 Compose volume
- 缓存与协调：Redis 容器
- 后台执行：统一 Python Worker 入口，以不同 `WORKER_TYPE` 独立运行
- 策略资产：Python 算法 + YAML Manifest + 不可变 StrategyVersion
- 外部数据：Pytdx 行情、Mootdx 交易日历
- 通知：飞书 Webhook / 飞书平台应用
- 截图：Capture Worker + 浏览器渲染
- 部署：Docker Compose

## 6. 代码主入口

| 领域 | 事实源 |
|---|---|
| 前端路由 | `frontend/src/App.tsx` |
| 后端应用 | `backend/app/main.py` |
| Worker | `backend/app/worker.py` |
| 策略 Manifest | `backend/app/strategy_assets/manifests/` |
| 指标基础参数 | `backend/app/constants/indicator_contract.py` |
| 套餐定义 | `plans` 表，由 Alembic 初始化，通过 `plan_service.py` 查询 |
| 权限解析 | `backend/app/services/access_control_service.py` |
| 可监控用户资格 | `backend/app/services/eligible_user_service.py` |
| ORM 模型 | `backend/app/models/` |
| 生产编排 | `docker-compose.prod.yml` |
| 自动文档 | `tools/update_docs.py` |

## 7. 探索阶段原则

探索允许改变，但不能无痕改变。已确认设计写入当前文档；未决问题进入 `17-open-decisions.md`；设计与实现差异进入 `18-code-doc-alignment.md`；每次改变必须有独立分支、CHANGE、测试和 PR。
