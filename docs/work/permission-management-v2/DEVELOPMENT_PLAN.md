# 盘迹权限管理 V2 开发计划
## 基于邀请码的模块化授权

- 文档状态：可执行
- 版本：V1.0
- 日期：2026-07-25
- 输入合同：`盘迹权限管理_V2_PRD.md`
- 执行角色：TRAE是执行器，不是规划器

---

## 1. 执行原则

1. 业务规则以PRD为准，不由IDE临时决定；
2. 只修改权限、邀请码、自选额度、监控资格和相关UI；
3. 不改行情算法、Chart Snapshot、DSA、SMC和盘后编排；
4. 每阶段完成必要逻辑调试和最小验证，再进入下一阶段；
5. “接口能返回”不等于正确，必须验证权限矩阵、期限、并发和事务原子性；
6. 不运行无关全仓测试；
7. 不直接修改main，不修改历史migration；
8. 数据迁移先在隔离测试库验证；
9. 后端授权是唯一安全边界；
10. 任何旧数据无法确定时失败并报告，不猜默认值。

---

## 2. 开始条件

开始时只做一次仓库确认：

```bash
git branch --show-current
git rev-parse HEAD
git status --short
git diff --cached --stat
free -m
df -B1 /
```

要求：

- 独立分支，例如 `refactor/invite-capability-access-v2`；
- 工作区无用户未说明的tracked修改；
- 暂存区为空；
- 记录base SHA；
- 确认隔离测试数据库，不接触生产库。

读取并锁定真实代码入口：

```text
backend/app/services/access_control_service.py
backend/app/services/eligible_user_service.py
invite_codes / plans / subscriptions / user_watchlist_items 模型
邀请码管理与兑换 API
自选 CRUD API
行情/详情/选股 API 权限依赖
前端 AccessContext store/hook
管理员邀请码页面
行情页、详情页、导航与路由守卫
监控 Worker 调用链
```

文件名不同只允许定位真实入口，不允许改变PRD业务规则。

输出一次“现状映射表”后直接开发：

| 领域 | 当前入口 | V2修改点 |
|---|---|---|
| 邀请码创建 | 实际文件/函数 | 能力组合、额度、月份 |
| 兑换 | 实际文件/函数 | 原子创建grant |
| AccessContext | 实际文件/函数 | 三能力聚合 |
| 自选额度 | 实际文件/函数 | 事务内校验 |
| 监控资格 | 实际文件/函数 | 统一AccessContext |
| 前端 | 实际文件/组件 | 权限矩阵UI |

---

## 3. 阶段A：领域模型与migration

### A1. 新增能力常量

后端建立唯一常量/枚举：

```text
WATCHLIST_MANAGEMENT
MARKET_SCREENING
REVIEW_MANAGEMENT
```

禁止在API、Worker和前端散落不同字符串。

### A2. 数据模型

实现PRD中的：

- `invite_code_capabilities`
- `user_capability_grants`
- `invite_codes.duration_months/revoked_at/redeemed*`缺失字段

数据库约束必须落在migration和ORM两层：

- 能力键范围；
- 唯一键；
- 正整数；
- 自选额度与能力匹配；
- `expires_at > starts_at`；
- 来源唯一。

只新增前向migration，不改历史migration。

### A3. migration最小验证

在空测试库：

```text
upgrade head
→ 检查表、字段、索引、约束
→ downgrade本次revision
→ 再upgrade head
```

在含旧数据测试库：

```text
upgrade head
→ 旧记录数量不丢失
→ 新约束不破坏旧数据
```

### A4. 阶段门禁

通过后才能进入服务层：

- migration三步通过；
- ORM可创建三种能力；
- 非法额度、非法能力、非法期限由数据库拒绝；
- 无生产库操作。

---

## 4. 阶段B：AccessControlService单一真源

### B1. 新增聚合方法

实现：

```text
get_access_context(user_id, now)
has_capability(user_id, capability_key, now)
require_capability(...)
require_any_capability(...)
get_effective_watchlist_limit(user_id, now)
```

聚合规则严格按PRD：

- 管理员三能力全开、额度unlimited；
- 普通用户按当前有效grant；
- 每项能力独立最晚到期时间；
- 自选额度取当前有效grant最大值；
- 到期实时计算，不依赖cron更新状态。

### B2. 月份计算

建立唯一纯函数：

```text
add_calendar_months_asiashanghai(timestamp, months)
```

覆盖：

- 月中；
- 月末；
- 闰年；
- UTC入库；
- `[start,end)`边界。

禁止在兑换API中手写日期逻辑。

### B3. AccessContext缓存

若当前使用缓存：

- 缓存键包含用户；
- 创建grant、兑换、撤销或订阅迁移后精确失效；
- TTL不能让过期权限继续有效；
- TTL必须小于等于距离最近到期时间，或在读取时再次校验。

### B4. 阶段最小验证

只运行服务层定向测试：

- 三能力七组合；
- 管理员；
- 多grant；
- 独立到期；
- 自选额度max；
- 到期边界；
- 日历月边界；
- 缓存失效。

不得继续开发API，直到所有结果与PRD矩阵一致。

---

## 5. 阶段C：管理员邀请码创建

### C1. 请求与响应DTO

创建/更新管理API的DTO：

```text
capabilities
watchlist_stock_limit
duration_months
```

后端校验：

- 至少一个能力；
- 自选勾选与额度一致；
- 正整数和技术上限；
- 非自选额度必须null；
- 复盘权限可保存。

### C2. 创建事务

同一事务中：

1. 创建邀请码；
2. 写入能力子表；
3. 写审计记录；
4. 提交。

不得出现主表成功、能力子表缺失。

### C3. 管理列表

列表/详情返回：

- 权限摘要；
- 自选额度；
- 授权月份；
- 状态；
- 创建/兑换信息。

撤销只允许未兑换邀请码。

### C4. 最小调试

用真实Service和测试DB验证：

- 七种组合均可创建；
- 无权限、错误额度、0个月被拒绝；
- 未勾选自选却带额度被拒绝；
- 撤销后不可兑换；
- 已兑换后不可撤销或编辑。

---

## 6. 阶段D：邀请码兑换与续期

### D1. 兑换事务

严格实现：

```text
锁定邀请码
→ 校验状态
→ 读取能力配置
→ 每项能力独立计算base和expires_at
→ 创建grant
→ 标记邀请码已兑换
→ 精确失效AccessContext
→ 提交
```

### D2. 并发

必须使用行锁/CAS保证：

- 两个用户同时兑换同一码，只有一个成功；
- 同一用户重复提交，不能生成重复grant；
- 任一grant创建失败时邀请码仍保持未兑换。

### D3. 多次兑换

验证：

- 新能力立即增加；
- 已有能力延长；
- 多项能力按各自当前到期时间延长；
- 自选额度按有效grant最大值生效。

### D4. 阶段门禁

至少通过：

- 单次兑换；
- 并发兑换；
- 原子回滚；
- 多次兑换；
- 月末续期；
- 缓存刷新。

---

## 7. 阶段E：API授权接入

### E1. 基础行情列表

`watchlist_management OR market_screening`。

但响应必须区分能力：

- 自选-only不返回/不加载市场选股研究数据；
- market可获得完整选股数据；
- 不得只靠前端过滤敏感字段。

### E2. 行情和详情

以下必须要求`market_screening`：

- DSA/选股结果；
- 个股详情；
- Chart Snapshot；
- Bars；
- Indicators；
- 研究指标API。

### E3. 自选

以下必须要求`watchlist_management`：

- 自选列表；
- 添加/删除；
- 监控状态；
- 自选额度信息。

### E4. 复盘

建立统一依赖，但只接入真实存在的复盘API。不存在时不新建占位业务接口。

### E5. 错误合同

统一：

```text
401 未认证
403 CAPABILITY_REQUIRED
409 WATCHLIST_LIMIT_REACHED
409 INVITE_CODE_ALREADY_REDEEMED
409 INVITE_CODE_REVOKED
```

### E6. 最小验证

按测试计划的权限矩阵逐个请求核心端点。至少证明：

- watchlist-only无法访问详情；
- market-only无法访问自选；
- 手工URL/API无法绕过；
- admin正常；
- expired grant实时403。

---

## 8. 阶段F：自选额度与并发

### F1. 添加事务

在一个事务内完成：

```text
权限检查
→ 获取有效额度
→ 锁
→ 统计
→ 去重
→ 插入
```

顺序应兼容“重复添加同一股票”的幂等语义。

### F2. 达到额度

- 当前数=额度：拒绝新增；
- 删除始终允许；
- 降额后超限：保留、可删除、不可新增；
- 返回当前数量和额度。

### F3. 监控范围

`EligibleUserService`不得复制grant查询，应调用AccessControlService。

超限时：

```text
ORDER BY created_at ASC, id ASC
LIMIT effective_limit
```

超限股票不产生新监控事件。

### F4. 并发最小验证

- 额度1，两个不同股票并发添加，只成功一个；
- 同一股票并发添加不重复；
- 权限在请求过程中到期不能绕过；
- 无自选权限即使知道API也403。

---

## 9. 阶段G：前端管理端

### G1. 邀请码创建表单

实现：

- 三个checkbox；
- 自选额度输入；
- 月份输入；
- 客户端即时校验；
- 后端错误展示；
- 创建成功权限摘要。

前端校验只用于体验，不能替代后端。

### G2. 列表

显示权限、额度、月数、状态、创建/兑换信息。

不得提供编辑已创建权限配置。

### G3. 最小验证

组件/纯函数测试：

- checkbox组合；
- 自选字段启停和清空；
- 错误输入；
- DTO序列化；
- 响应渲染；
- 撤销按钮状态。

不做无关截图E2E。

---

## 10. 阶段H：前端用户端

### H1. AccessContext类型

前端类型严格对应后端，不从旧Subscription/Plan推导。

### H2. 导航和页面

- 行情：watchlist或market任一权限；
- 自选scope/按钮：watchlist；
- DSA/详情：market；
- review：功能存在且有权限时；
- 无权限显示明确说明。

### H3. 路由

- 详情链接仅market渲染；
- 手工访问详情显示403状态；
- API仍是最终边界；
- market-only不渲染自选入口；
- watchlist-only不渲染研究入口。

### H4. 额度UI

展示使用数/上限/超限提示，并按后端结果显示不参与监控的条目。

---

## 11. 阶段I：旧数据迁移

### I1. 迁移前报告

只读统计：

- 当前有效会员数；
- 过期会员数；
- 未兑换邀请码数；
- 可推导额度/期限数量；
- 无法推导清单。

### I2. 回填

建立幂等回填：

- active subscription → watchlist + market grants；
- unused legacy invite → capability配置；
- review不自动授予；
- 来源标记`legacy_subscription`或`legacy_invite`。

### I3. 失败策略

任何记录无法确定额度或期限：

- 不使用默认值；
- 不跳过后声称完成；
- 迁移失败并输出记录ID；
- 修正映射后重跑。

### I4. 核对

迁移前后逐项对账：

- 有效用户数；
- 到期时间；
- 自选额度；
- 未使用邀请码；
- 无用户失权；
- 无重复grant。

---

## 12. 阶段J：文档和记忆

只更新受影响文档：

```text
AGENTS.md
docs/current/00-product-business.md
docs/current/02-data-api-contracts.md
docs/current/03-jobs-integrations-operations.md
docs/current/04-frontend-ux.md
docs/current/05-testing-acceptance.md
docs/maps/api-route-map.md
docs/maps/backend-module-map.md
docs/maps/frontend-route-map.md
docs/maps/worker-job-map.md
docs/maps/test-coverage-map.md
docs/changes/CHANGELOG.md
docs/changes/records/CHANGE-*.md
docs/current/MANIFEST.md
```

稳定记忆应写入AGENTS：

- 三能力键；
- 自选包含盘中监控；
- 行情不包含自选；
- 仅自选不能进入详情；
- AccessControlService单一真源；
- 月份使用日历月；
- 权限到期实时推导；
- Worker不得复制权限规则。

不要把临时SHA、测试数量和开发过程写入长期规则。

---

## 13. 最终验证顺序

1. migration upgrade/downgrade/upgrade；
2. AccessControlService定向测试；
3. 邀请码创建/兑换/并发测试；
4. 自选额度并发测试；
5. API权限矩阵；
6. 监控资格测试；
7. 前端管理表单测试；
8. 前端导航/路由权限测试；
9. 旧数据迁移对账；
10. Ruff/Mypy/TS/ESLint修改文件；
11. docs consistency和architecture检查；
12. `git diff --check`。

只重跑失败项，修复后再运行一次相关组。

---

## 14. 提交拆分

建议保持可回滚：

1. `feat(access): capability grant data model and access service`
2. `feat(invites): configurable invite capabilities and redemption`
3. `feat(access): enforce capability matrix and watchlist quota`
4. `feat(frontend): capability-aware admin and user UX`
5. `docs(access): align permission v2 contracts and maps`

每次commit前精确`git add <files>`，禁止`git add .`。

---

## 15. 完成报告

TRAE最终一次报告：

1. 分支、base、HEAD；
2. 修改文件；
3. 数据模型与migration；
4. 邀请码事务；
5. AccessContext示例；
6. 权限矩阵实际结果；
7. 自选额度和并发结果；
8. Worker资格结果；
9. 旧数据迁移对账；
10. 测试命令及collected/passed/failed/skipped；
11. docs和AGENTS更新；
12. 资源占用；
13. 未解决问题；
14. 是否允许集成/部署。

除硬停止条件外，不逐步等待用户确认。
