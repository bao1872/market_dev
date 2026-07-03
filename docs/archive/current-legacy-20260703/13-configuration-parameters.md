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

# 13 配置与参数基线

## 1. 参数所有权

| 参数类型 | 机器事实源 |
|---|---|
| 指标与行情根数 | `backend/app/constants/indicator_contract.py` |
| 策略输入、输出和版本 | `backend/app/strategy_assets/manifests/*.yaml` + StrategyVersion |
| 套餐和额度 | `plans` 表，通过 `plan_service.py` 查询 |
| 用户资格 | `access_control_service.py` / `eligible_user_service.py` |
| Worker 调度和轮询 | Worker 代码 + 生产环境变量 |
| 部署服务 | `docker-compose.prod.yml` |
| Secret | `/etc/market-dev/market.env` 或受限 Secret 系统 |

文档解释含义和当前口径，不形成第二套可执行常量。

## 2. 指标与行情参数

| 参数 | 当前确认值 | 可修改范围 |
|---|---:|---|
| DSA/日线最小历史 | 250 根 | 只能发布新策略版本 |
| 图表日线基础根数 | 250 根 | 代码契约变更 + 测试 |
| Node 主周期 | 1d / 250 根 | 新策略版本 |
| Node 低周期 | 15m / 3600 根 | 新策略版本 |
| 穿越判断 | 2 根已完成 1m Bar | 新策略版本 |
| BB 窗口/倍数 | 20 / 2.0 | 新策略版本 |
| 盘中业务时区 | Asia/Shanghai | 不允许用户修改 |

完整细值由机器事实源和自动生成参数文档给出。

## 3. DSA 资源预算

当前代码存在 100ms 单股预算，但该值已造成大规模失败风险，状态为 `KNOWN_GAP`，不是确认的生产发布参数。修复前不得通过放宽发布门禁隐藏问题。最终预算必须来自基准测试，修改时记录样本、分位数、安全系数和总运行上限。

## 4. 套餐

- 默认普通套餐代码：`observe_20`；
- 当前实验套餐：`observe_20`、`research_50`；
- 监控额度和 features 从 `plans` 表读取；
- admin 的 `plan_code=null`，无 Subscription；
- 前端不能硬编码套餐额度和 features。

商业价格和套餐是否长期保留仍为 OPEN，但当前权限执行必须与数据库套餐一致。

## 5. Worker 和环境

生产环境至少包含：

- `POSTGRES_USER`、`POSTGRES_PASSWORD`、`POSTGRES_DB`；
- `DATABASE_URL`、`REDIS_URL`；
- `JWT_SECRET`；
- `GIT_SHA`、`BUILD_TIME`；
- `FRONTEND_BASE_URL`、`CAPTURE_WORKER_URL`；
- 飞书管理员配置；
- 每个容器明确 `WORKER_TYPE`。

Secret 只记录名称，不记录真实值。

## 6. 行情聚合参数

实时缓存 TTL、外部源重试、可接受 freshness 和降级阈值必须在统一行情服务中集中配置。任何页面不得单独定义“最新”的判断。

## 7. 修改流程

参数变化必须明确来源，修改唯一事实源，删除重复硬编码，更新 StrategyVersion/Manifest/环境，更新本文档和自动生成文档，新增 CHANGE 和一致性测试；影响算法时不得覆盖已发布版本。
