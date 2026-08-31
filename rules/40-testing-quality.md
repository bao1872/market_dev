# 40 测试与质量

## 1. 总原则

**Fast Iteration 不是少测试。**

Exploration 只减少与当前改动无关的测试和 release ceremony；受影响业务逻辑的测试仍是硬门。

任何代码修改必须回答：

1. 业务逻辑有没有做对？
2. 代码逻辑有没有实现对？
3. 哪些测试足以证明当前修改？
4. 是否还需要真实数据 / PG / API / Frontend 技术证据？

测试是 evidence，不因其存在而天然成为 truth。测试必须证明生产合同，而不是用手写
fixture 或复制算法建立第二套看似合理的合同。

### 1.1 Production Contract Reuse

涉及 artifact、serialization、lineage、status、identity、version、readiness 或 publication 的测试，
优先复用正式 encoder、decoder、repository、domain factory 和 identity helper。

禁止手抄生产 payload/schema 后用同一手抄结构断言成功。只有 malformed/legacy payload
本身是测试目标时，才允许直接构造非生产输入，并必须明确标注该目的。

## 2. 默认验证层级

### T0 — Syntax / Static

按修改范围运行：

- Python：Ruff、py_compile；生产 Python 变更按现有项目策略运行 Mypy；
- Frontend：TypeScript / lint；
- Governance/Docs：仅在修改相关文件时运行对应 checker。

### T1 — Modified-Scope Unit

**每个代码变更默认必须运行。**

只跑与修改代码直接相关的 unit tests，不默认全仓。

算法、状态机、filter、flatten/read-model、API schema、错误处理等变化必须有对应 unit coverage。

### T2 — Contract

当修改以下内容时运行：

- API request/response；
- DTO / schema；
- frontend service/hook contract；
- persistence shape；
- canonical/read-model contract；
- domain status mapping。

### T3 — Targeted Business Chain

当修改业务链时，验证当前 vertical slice 的关键调用关系。

例如 H1：

`daily bars → Core → first pyramid → persistence → API → frontend binding`

只验证当前链，不自动扩大到 Board/Review/Chip/Auction。

### T4 — Representative Real-Data Sampling

算法/数据行为变化时，使用有意设计的代表性真实样本。

默认建议：

- 25–60 只股票；
- strong / weak / range；
- high / low volatility；
- 关键 SMC / momentum case；
- edge / insufficient history；
- 用户熟悉、可人工判断的股票。

不是纯随机抽样。

### T5 — Targeted PG / Integration

只有真实 PostgreSQL 语义、SQL、ORM、transaction、migration、publication、pointer 或跨服务持久化需要证明时运行。

不因“有数据库表”就自动跑全套 PG。

### T6 — Full PURE_UNIT

**非默认。**

仅在以下情况启用：

- shared foundation；
- broad schema/contract；
- 大范围核心算法公共层；
- 编排大重构；
- milestone / hardening；
- modified-scope 无法合理界定。

### T7 — Remote PG / Synthetic E2E

用于证明：

- Migration；
- 真实 PostgreSQL 行为；
- 多服务/编排；
- publication/pointer；
- 当前 slice 的必要端到端。

Exploration 只运行需要的 profile/gate，不默认 full-closure。

#### T7 安全合同（Always-On）

- **PG 测试必须 self-contained**：每个 PG 测试必须在自身 transaction/fixture 中创建最小完整前置数据，不依赖共享业务库既有状态；
- **PG 测试只能由 `panji-verify-python` 容器经 `verify_exec.py` 运行**：禁止 host 直接跑 psql/alembic，禁止 `docker cp` 注入源码，禁止任意 shell/pytest 参数注入；
- **验证数据 100% synthetic**：标准验证使用 synthetic 数据，不读取 `bz_stock` 作为测试 fixture；
- **远程 PG test 必须 fail-closed 确认 DB identity**：`current_database()` 必须是 `bz_stock_verify_<sha>`，不是 `bz_stock`。

当前注册远程验证计划：

- `targeted-pg`：Exploration 默认 PostgreSQL 合同证据；
- `migration-roundtrip`：Migration 专项；
- `full-closure`：Hardening/Release。

禁止通过任意 shell/pytest 参数注入绕过已注册 plan/profile。

### T8 — Release Regression

只在 `70-hardening-release.md` 启用。

## 3. 默认 Exploration 测试链

通常：

`T0 → T1 → [T2] → [T3] → [T4] → [T5/T7 only if required] → Runtime/Frontend`

方括号表示按风险选择。

禁止默认追加：

- full PURE_UNIT；
- full Synthetic Closure；
- production clone；
- full regression；
- 与当前 slice 无关的 E2E。

## 4. 本地与远程测试模式

### 本地 / CI

默认：

`PURE_UNIT_TEST=1`

- 不连接 PostgreSQL；
- 不联网的纯单元优先；
- 可运行静态、合同和前端测试。

### 远程 PostgreSQL

真实 PG test 使用：

`PANJI_REMOTE_VERIFY_DB_TEST=1`

只允许连接 `panji-prod` 上正式创建的 `bz_stock_verify_<sha>`。

必须 fail-closed 确认：

- `APP_ENV=verification`；
- `current_database()` 是验证库；
- 不是 `bz_stock`。

禁止恢复共享业务库 pytest。

## 5. Marker

需要真实 PostgreSQL 的测试显式：

`@pytest.mark.postgres`

依赖外部数据源的测试显式：

`@pytest.mark.external_data`

marker 不能作为失败免责机制；如果是代码断言错误仍必须修。

## 6. 前端测试

前端变更按范围选择：

### 纯样式/布局

- targeted component test（如已有）；
- TypeScript / lint；
- build（当变更可能影响构建）。

### 数据绑定 / API

必须验证：

- endpoint；
- request params；
- HTTP status；
- response schema；
- hook/service mapping；
- component consumed fields；
- loading/error/unavailable 行为。

### 产品逻辑展示

如果页面状态会影响用户业务判断，必须使用真实 API/真实数据做远程技术闭环。

不要求 IDE 替用户判断“页面好不好看”或“理论有没有价值”。

## 7. API 变更纪律

修改 API 时必须：

- 检查所有实际前端调用；
- 检查 response field rename/type/nullability；
- 更新相应 contract test；
- 验证至少一个真实/targeted response；
- 禁止后端 PASS 但前端读旧字段。

## 8. 算法测试纪律

算法修改必须同时考虑：

- 正向 case；
- 反向/无信号 case；
- 边界历史；
- point-in-time；
- 前缀不变性（如适用）；
- no future leakage；
- canonical determinism；
- representative real-data sampling（当产品假设依赖算法输出）。

## 9. Worker / Orchestrator

修改 Worker/编排时检查：

- 幂等；
- heartbeat；
- retry；
- lease/fencing；
- partial/failure status；
- resume；
- 不得 false-green。

只审当前修改涉及的任务，不自动审所有 Worker。

## 10. Migration 测试

修改 Migration 时必须：

- 静态审查 upgrade/downgrade；
- 在允许的远程验证库进行 upgrade/downgrade/upgrade；
- 检查约束对存量 fixture/测试数据的影响。

是否需要生产数据 clone 由 `80-deployment-migration.md` 的风险等级决定，不是所有 migration 默认要求。

## 11. 失败分类与纪律

测试失败后，在修改生产逻辑前必须分类：

- `STALE_TEST`：测试仍断言已废弃或错误合同；
- `INVALID_FIXTURE`：fixture 不满足生产 reader、identity、FK、时间或可见性合同；
- `RUNTIME_BUG`：生产 owner 在有效输入和真实路径下行为错误；
- `INFRA_BUG`：runner、环境、依赖、数据库身份或资源故障；
- `UNKNOWN`：证据不足。

`UNKNOWN` 不允许直接修改生产行为。分类必须由生产 owner、真实 reader/decoder、调用链和
运行证据支持，不能仅根据失败测试的期望推断。

- 删除测试以适配错误实现：禁止；
- 修改断言来掩盖业务错误：禁止；
- 未运行写成 PASS：禁止；
- 本地 modified-scope 失败时禁止进入部署；
- 服务器 smoke 不能冒充 unit/regression；
- Mock E2E 不能冒充真实数据 E2E；
- 局部成功不能冒充整体成功。

## 12. 重跑纪律

同一测试集默认最多运行两次：

1. 首次真实运行；
2. 针对明确根因修复后的复跑。

第二次仍失败应 STOP 并报告 first real blocker，除非用户明确要求继续诊断。

## 13. 证据资格

“正式证据”要与结论范围匹配。

### Exploration

最低要求：

- exact commit SHA；
- test command / test set；
- PASS/FAIL；
- runtime target date / sample；
- 关键 API/DB/前端绑定证据。

### Hardening

按 `70-hardening-release.md` 增加：

- repo/runtime SHA；
- DB revision；
- full gate；
- resource identity；
- complete release evidence。

Exploration 不得为了证明一个局部 hypothesis 自动要求 Hardening 证据包。

### 13.1 Gate Truthfulness Contract

正式 Gate 必须回答 required contract 是否实际执行，而不只回答 pytest 是否 exit 0。

状态固定为 `passed`、`failed`、`skipped`、`deselected`、`not_registered`、
`not_run`、`blocked`。

Required evidence 只有实际收集、执行并通过才可记为 `passed`。测试文件存在、未失败、被
marker 排除、未注册、未运行或依赖 Gate 被阻塞，都不能计入 closure。

`scripts/verify/evidence_manifest.json` 是正式远程 Gate 的 contract -> selector -> gate 声明 owner。
选择必须显式、无 glob、无动态全仓 discovery。验证框架必须输出逐 contract 的机器证据。

## 14. 质量工具

修改相关文件时，适用的 checker 必须通过。

不得通过：

- 全局 ignore；
- 批量 `noqa`；
- 扩大 exclude；
- 批量 `type: ignore`；
- 删除测试；
- 放宽 checker；

来适配错误实现。

不要求每个普通代码任务都运行与本次修改无关的 docs/governance checker。

## 15. ref/ 隔离

`ref/` 仅供人工参考。

生产代码、测试、工具、构建脚本不得运行时读取 `ref/`。

正式算法 fixture 必须位于正式测试 fixture 目录。

## 16. Evidence Promotion

当真实 Source / Runtime 调查确认了一个：

* 稳定；
* 会影响实现正确性；
* 未来可能回归；

的外部事实时，SHOULD 将该事实升级为可持续验证证据。

优先路径：

```text
Real Evidence
→ representative fixture / regression
→ durable repository evidence
```

例如：

* provider response shape；
* timestamp format；
* null behavior；
* ordering behavior；
* API field semantics。

禁止仅依赖：

* 聊天记录；
* IDE 报告；
* 临时 probe 输出；

作为长期唯一证据。

当 Stable External Fact 满足：

* 能够被确定性表达；
* 会影响未来实现判断；
* 存在真实回归风险；

SHOULD 优先通过 representative fixture + regression / contract test 沉淀为 durable repository evidence。

如果该事实不适合 fixture 表达，则应选择与其性质匹配的 test、contract、benchmark、runtime assertion 或其他正式证据（例如 provider rate limit、真实网络 latency、DB planner behavior、runtime resource limit、外部服务 availability semantics）。

不得仅依赖聊天记录、IDE 报告或临时 probe 作为长期唯一事实来源。
