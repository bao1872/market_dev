# 盘迹权限管理 V2 核心测试验证计划
## 基于邀请码的模块化授权

- 文档状态：可执行
- 版本：V1.0
- 日期：2026-07-25
- 测试目标：证明权限、期限、额度、事务和监控结果正确
- 非目标：通过大量无关测试制造“看起来稳定”

---

## 1. 测试原则

1. 调试通不等于业务正确；
2. 每项测试必须对应PRD规则和可观察结果；
3. 后端授权必须独立于前端；
4. 时间、并发和事务必须使用可控输入；
5. 不依赖当前真实时间，测试注入`now`或冻结时间；
6. 不连接生产数据库；
7. 不通过修改数据版本号伪造migration状态；
8. 不删除失败测试以适配实现；
9. 只运行直接受影响测试及最小回归；
10. 每个失败必须判断是代码错误、测试错误还是旧合同已废弃。

---

## 2. 测试层级

| 层级 | 目标 |
|---|---|
| 纯函数 | 月份计算、权限聚合、额度选择 |
| Service | AccessContext、创建邀请码、兑换、续期 |
| Repository/DB | 约束、事务、并发、migration |
| API | 401/403/409、权限矩阵、DTO |
| Worker | 监控用户资格和额度范围 |
| Frontend | 表单、导航、按钮、路由状态 |
| Migration | 旧用户和旧邀请码无损迁移 |

不做与本需求无关的行情算法、SMC、截图、完整盘后流程或全仓E2E。

---

## 3. 固定测试数据

建立用户：

```text
admin
user_none
user_watchlist
user_market
user_review
user_watchlist_market
user_watchlist_review
user_market_review
user_all
user_expired
user_multi_grant
```

建立核心股票5只，自选额度场景：

```text
limit=1
limit=3
current_count=0/1/3/5
```

时间基准：

```text
T0 = 2026-01-31 10:00:00 Asia/Shanghai
T1 = 2026-02-28 10:00:00 Asia/Shanghai
```

---

## 4. 月份与时间测试

### TIME-001 普通月份

- 输入：2026-07-25 10:00 + 1月
- 期望：2026-08-25 10:00

### TIME-002 月末

- 输入：2026-01-31 10:00 + 1月
- 期望：2026-02-28 10:00

### TIME-003 闰年

- 输入：2028-01-31 + 1月
- 期望：2028-02-29

### TIME-004 多月

- 输入：2026-11-30 + 3月
- 期望：2027-02-28

### TIME-005 有效边界

- `now=starts_at`：有效；
- `now=expires_at-1微秒`：有效；
- `now=expires_at`：无效。

### TIME-006 时区

- 计算在Asia/Shanghai；
- 入库UTC；
- 读回后有效区间不偏移。

禁止断言“N个月=N×30天”。

---

## 5. 数据库约束和migration

### DB-001 空库升级

- upgrade head成功；
- 新表、字段、索引存在；
- revision正确。

### DB-002 downgrade/upgrade

- downgrade本revision；
- 新对象移除；
- 再upgrade成功。

### DB-003 非法能力

插入未知能力键，数据库拒绝。

### DB-004 自选额度约束

- watchlist limit=null：拒绝；
- watchlist limit=0/-1：拒绝；
- market/review带limit：拒绝；
- 正整数：成功。

### DB-005 时间约束

`expires_at <= starts_at`被拒绝。

### DB-006 来源唯一

同一`source_type+source_id+capability`不能重复。

### DB-007 历史migration保护

确认没有修改旧migration内容。

---

## 6. AccessContext权限组合

对七种非空组合逐一验证：

| 测试ID | watchlist | market | review |
|---|---:|---:|---:|
| ACC-001 | 1 | 0 | 0 |
| ACC-002 | 0 | 1 | 0 |
| ACC-003 | 0 | 0 | 1 |
| ACC-004 | 1 | 1 | 0 |
| ACC-005 | 1 | 0 | 1 |
| ACC-006 | 0 | 1 | 1 |
| ACC-007 | 1 | 1 | 1 |

每项断言：

- `active`准确；
- `expires_at`为该能力最晚有效到期；
- 不存在的能力为false/null；
- 不相互隐式包含。

### ACC-008 无权限

三项均false。

### ACC-009 管理员

三项true，自选unlimited。

### ACC-010 已过期

grant存在但三项结果按时间失效。

### ACC-011 多grant

同能力两个有效grant：

- 到期时间取最大；
- 自选额度取最大；
- 撤销grant不参与；
- 未来/过期grant按时间正确处理。

### ACC-012 缓存边界

授权创建、兑换、撤销后立即刷新；到期后缓存不能继续返回有效。

---

## 7. 邀请码创建

### INV-CREATE-001 七种组合

七种非空组合均成功，数据库子表数量正确。

### INV-CREATE-002 未选权限

返回422/稳定业务错误，不创建主表。

### INV-CREATE-003 自选无额度

拒绝，事务无残留。

### INV-CREATE-004 非自选带额度

拒绝。

### INV-CREATE-005 非法数字

- 月份0、负数、非整数、超过技术上限；
- 额度0、负数、非整数、超过技术上限；
- 全部拒绝。

### INV-CREATE-006 不可编辑

创建后不能改变权限、额度、月份。

### INV-CREATE-007 撤销

未兑换可撤销；已兑换不可撤销为未使用状态。

---

## 8. 兑换与原子性

### REDEEM-001 首次兑换

- 邀请码标记兑换；
- 每项能力一条grant；
- source指向邀请码；
- 时间和额度正确；
- AccessContext立即变化。

### REDEEM-002 同码重复

第二次返回409，不新增grant。

### REDEEM-003 两用户并发兑换

两个并发事务争抢同一码：

- 恰好一个成功；
- 一个409；
- 邀请码只有一个redeemed_by；
- grant只属于成功用户。

### REDEEM-004 部分失败回滚

模拟第二项grant插入失败：

- 无任何grant；
- 邀请码仍未兑换；
- 无脏缓存。

### REDEEM-005 新能力

用户已有watchlist，兑换market：

- watchlist不改变；
- market立即生效；
- 两项到期独立。

### REDEEM-006 同能力续期

当前到期E，兑换N个月：

```text
new expires_at = add_months(E, N)
```

starts_at=now，立即生效。

### REDEEM-007 多能力不同到期

一张码包含watchlist+market；用户两项原到期不同：

- 分别从各自最晚到期延长；
- 不使用统一expires_at覆盖。

### REDEEM-008 额度升级

当前limit=10，兑换limit=30：

- 立即有效limit=30；
- 期限正确延长。

### REDEEM-009 低额度续期

当前高额度仍有效，兑换低额度：

- 高额度有效期内仍取max；
- 高额度到期后按剩余有效grant计算。

---

## 9. API权限矩阵

核心端点分组：

```text
BASIC_MARKET_LIST
SCREENING_RESULTS
STOCK_DETAIL
CHART_SNAPSHOT
BARS_INDICATORS
WATCHLIST_READ
WATCHLIST_WRITE
MONITOR_STATUS
REVIEW_API(若存在)
ADMIN_INVITE
```

### AUTH-001 未登录

所有私有端点401。

### AUTH-002 watchlist-only

- basic market list：200；
- watchlist read/write：200；
- monitor status：200；
- screening/detail/chart/bars/indicators：403；
- 手工URL不能绕过。

### AUTH-003 market-only

- basic list/screening/detail/chart/bars/indicators：200；
- watchlist和monitor：403。

### AUTH-004 review-only

- review API：200（若真实存在）；
- market/watchlist：403。

### AUTH-005 组合权限

权限取并集，不丢失能力。

### AUTH-006 过期

令`now=expires_at`，对应API立即403。

### AUTH-007 管理员

管理和用户能力正常，不能因为无grant被拒绝。

### AUTH-008 reason_code

403/409返回稳定reason_code，前端可可靠处理。

### AUTH-009 敏感字段

watchlist-only的基础行情列表不返回仅market用户可见的选股研究数据；不能只在前端隐藏。

---

## 10. 自选额度与并发

### WL-001 未达额度

count=2，limit=3，添加成功，count=3。

### WL-002 达到额度

count=3，limit=3，新增409：

```text
WATCHLIST_LIMIT_REACHED
current_count=3
limit=3
```

### WL-003 删除

超限或达到额度时删除仍成功。

### WL-004 无权限

即使limit字段异常存在，无watchlist能力仍403。

### WL-005 最后名额并发

limit=1，两个不同股票并发添加：

- 只成功一个；
- 最终count=1；
- 另一个409。

### WL-006 同股票并发

不产生重复行；结果符合现有幂等合同。

### WL-007 降额超限

count=5，高额度grant到期后limit=3：

- 5条仍在；
- 不能新增；
- 可以删除；
- `over_limit=true`；
- 前3条进入监控；
- 后2条标记不参与监控。

### WL-008 稳定顺序

多次查询监控范围顺序一致：

```text
created_at ASC, id ASC
```

---

## 11. Worker和消息

### MON-001 有效自选用户

进入EligibleUserService结果。

### MON-002 仅行情用户

不进入监控，即使数据库中遗留自选行。

### MON-003 到期用户

到期后下一轮不进入监控，不产生新事件/Outbox。

### MON-004 超额用户

只处理额度内前N只。

### MON-005 AccessControlService单一真源

架构测试或spy证明Worker调用统一服务，不复制grant SQL和到期逻辑。

### MON-006 历史保留

权限失效不删除历史自选、事件和消息。

---

## 12. 前端管理端

### FE-ADMIN-001 勾选自选

显示额度字段并要求正整数。

### FE-ADMIN-002 取消自选

清空并禁用额度字段。

### FE-ADMIN-003 至少一个权限

无勾选不能提交。

### FE-ADMIN-004 DTO

提交字段与后端契约一致，无旧Plan推导。

### FE-ADMIN-005 列表摘要

权限、额度、月份、状态、兑换信息正确。

### FE-ADMIN-006 不可编辑

已创建码无权限编辑入口。

---

## 13. 前端用户端

### FE-USER-001 watchlist-only

- 行情入口存在；
- 自选scope/按钮存在；
- 详情链接不存在或不可用；
- DSA/研究入口不渲染；
- 直接URL显示无权限。

### FE-USER-002 market-only

- 行情、筛选、详情存在；
- 自选scope/按钮和监控入口不渲染。

### FE-USER-003 组合权限

并集正确。

### FE-USER-004 expired

刷新AccessContext后入口消失，直接API403状态正确展示。

### FE-USER-005 额度

显示使用数/上限；超限提示和不参与监控标记正确。

### FE-USER-006 review

功能未上线时不展示虚假页面；AccessContext仍保留权限。

---

## 14. 旧数据迁移验证

### MIG-001 有效会员数量对账

迁移前有效会员数 = 迁移后拥有watchlist+market有效grant的用户数。

### MIG-002 到期时间

抽样及全量SQL对账，expires_at不变。

### MIG-003 自选额度

每个用户新limit等于旧真实配额。

### MIG-004 旧未兑换邀请码

可推导记录全部生成watchlist+market配置；review=false。

### MIG-005 无法推导

迁移明确失败并列出记录，不赋默认值。

### MIG-006 幂等

回填脚本运行两次不新增重复grant。

### MIG-007 已兑换历史

不重新生成可兑换邀请码，不改变原兑换人。

---

## 15. 安全与越权

### SEC-001 伪造前端状态

修改前端store不能访问无权API。

### SEC-002 他人资源

有watchlist权限仍不能读写他人的自选。

### SEC-003 普通用户管理API

403。

### SEC-004 邀请码日志

日志不输出完整邀请码。

### SEC-005 缓存串用户

用户A权限不能污染用户B。

---

## 16. 最小回归范围

只运行直接相关既有测试：

- 登录/JWT基础鉴权；
- AccessContext；
- 邀请码创建与兑换；
- 自选CRUD；
- EligibleUserService；
- monitor资格；
- 行情列表权限；
- stock detail/chart API权限；
- 管理端邀请码组件；
- 用户导航/路由权限。

不运行：

- SMC算法parity；
- Chart Snapshot行情计算正确性；
- 全量DSA；
- 盘后编排完整流程；
- Capture截图；
- 飞书发送；
- 全仓Playwright；
- 全Docker构建。

除非本次代码实际修改了这些模块。

---

## 17. 质量门禁

后端：

```text
ruff：修改文件零错误
mypy：修改生产文件零新增错误
pytest：本计划测试0失败
migration：upgrade/downgrade/upgrade通过
```

前端：

```text
tsc --noEmit：0错误
ESLint：修改文件0错误
权限定向测试：0失败
```

文档：

```text
check_docs_consistency.py
check_architecture.py
update_docs.py --check
git diff --check
```

---

## 18. 通过标准

允许进入集成必须同时满足：

1. 七种权限组合全部正确；
2. 后端API矩阵无越权；
3. 邀请码并发兑换只有一个成功；
4. 兑换部分失败完全回滚；
5. 多次兑换独立续期；
6. 日历月边界正确；
7. 自选额度并发不超限；
8. Worker只处理有效自选权限和额度内股票；
9. 旧数据迁移对账无损；
10. 前端显示与后端AccessContext一致；
11. 无生产数据库操作；
12. docs和代码一致；
13. 所有核心测试0失败。

“页面能打开”“接口返回200”“调试没有报错”均不能单独作为通过依据。

---

## 19. 最终测试报告格式

```text
1. 测试分支/commit
2. 测试数据库和migration revision
3. 权限组合矩阵结果
4. 月份边界结果
5. 邀请码创建/兑换/并发结果
6. 自选额度/并发结果
7. API授权矩阵
8. Worker监控范围
9. 前端权限矩阵
10. 旧数据迁移对账
11. collected/passed/failed/skipped
12. 未解决问题
13. 是否允许集成
```

不得用“基本通过”代替具体结果。
