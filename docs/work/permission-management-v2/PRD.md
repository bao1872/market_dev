# 盘迹权限管理 V2 PRD
## 基于邀请码的模块化授权

- 文档状态：待开发确认
- 版本：V1.0
- 日期：2026-07-25
- 适用项目：盘迹 PanJi / `market_dev`
- 目标读者：产品负责人、后端、前端、测试、TRAE 执行器

---

## 1. 背景与问题

当前权限以统一会员/订阅为主，无法在创建邀请码时按业务模块精确授权，也无法对自选股数量进行邀请码级配置。随着盘迹拆分为自选、行情选股、复盘三类能力，继续使用“有会员权限/无会员权限”的单一判断会产生以下问题：

1. 无法只发放某一模块；
2. 行情权限与自选、盘中监控被错误捆绑；
3. 自选额度只能依赖固定套餐，管理员不能按用户自由配置；
4. 复盘功能尚未上线，但缺少提前保存和识别授权的能力；
5. 前端隐藏与后端授权容易出现不一致；
6. 多次兑换、续期、额度变化和到期后的行为缺少统一规则。

本次改造建立“邀请码配置能力 → 兑换生成能力授权 → AccessContext 聚合 → API、页面和 Worker 统一执行”的单一权限链。

---

## 2. 第一性原理

### 2.1 权限是能力，不是页面开关

权限必须决定用户是否可以执行某类业务操作。前端显示只是权限结果的表现，不能作为安全边界。

### 2.2 权限、期限、额度是三个独立维度

- 权限：用户能否使用某项能力；
- 期限：该能力在哪个时间区间有效；
- 额度：能力有效时允许使用的资源数量。

不得用一个 `is_member` 同时替代三个维度。

### 2.3 后端是唯一授权真源

所有 API、Worker、消息生成和额度校验均通过统一的 `AccessControlService` 获取有效权限。前端只消费 `/me/access` 返回结果，不自行推导。

### 2.4 授权必须可审计

每项权限必须能够回答：

- 来源于哪个邀请码；
- 何时兑换；
- 何时开始、何时到期；
- 配置的额度是多少；
- 是否被撤销；
- 当前为什么有效或无效。

---

## 3. 目标与非目标

### 3.1 本期目标

1. 管理员创建邀请码时自由勾选：
   - 自选管理；
   - 行情选股；
   - 复盘管理。
2. 勾选自选管理时填写任意正整数的股票上限，不使用固定档位。
3. 邀请码按月配置授权期限。
4. 用户兑换后按能力生成独立授权。
5. 支持用户多次兑换邀请码，并按能力独立续期。
6. 后端统一拦截 API，前端统一控制导航、路由和按钮。
7. 自选权限与盘中监控绑定；行情权限不自动包含自选和监控。
8. 复盘权限先完成数据、接口和 AccessContext 支持，不虚构未开发业务。
9. 迁移现有有效会员和未兑换邀请码，不损失已有使用权。

### 3.2 非目标

- 不开发复盘业务本身；
- 不引入支付、自动续费和账单；
- 不改造管理员角色体系；
- 不改变行情算法、DSA、SMC、Chart Snapshot 或盘后编排；
- 不删除历史 Plan/Subscription 数据；
- 不在本期增加邀请码批量多人使用；
- 不增加用户自助修改权限或额度。

---

## 4. 权限定义

统一能力键：

```text
watchlist_management
market_screening
review_management
```

### 4.1 自选管理 `watchlist_management`

包含：

- 查看自己的自选列表；
- 添加和移除自选；
- 查看自选相关监控状态；
- 自选股盘中监控；
- 自选触发的新消息和投递；
- 在额度内维护自选股票。

不包含：

- 完整个股详情；
- K 线、指标和完整行情研究；
- DSA 选股结果；
- 复盘。

#### 自选权限的行情页边界

仅有自选权限的用户可以进入“行情”页面的列表视图，用于搜索股票、查看基础行信息和管理自选；可以查看自己的自选 scope。

但必须限制：

- 股票行不能进入完整个股详情；
- 不加载需要 `market_screening` 的 DSA/研究指标；
- 后端详情、K 线和指标接口返回 403；
- 不能通过手工 URL 绕过。

### 4.2 行情选股 `market_screening`

包含：

- 查看全市场行情列表；
- 搜索、行业、概念和列筛选；
- 查看系统发布的行情选股/DSA 结果；
- 打开个股详情；
- 查看 K 线、Chart Snapshot 和指标；
- 使用行情研究功能。

不包含：

- 自选列表；
- 添加/删除自选；
- 自选额度；
- 盘中监控；
- 自选监控消息生成；
- 复盘。

仅有行情权限时，前端不得显示自选 scope、加入自选按钮和盘中监控入口；后端自选 API 必须返回 403。

### 4.3 复盘管理 `review_management`

本期只完成：

- 邀请码配置；
- 授权持久化；
- `/me/access` 返回；
- 后端权限依赖；
- 将来复盘路由/API可直接接入。

复盘功能未上线前：

- 不创建虚假复盘页面；
- 不因拥有该权限而暴露不存在的入口；
- 权限数据必须保留，未来上线后直接生效。

### 4.4 管理员

`is_admin=true`：

- 默认拥有三项能力；
- 自选额度为 unlimited；
- 不依赖邀请码；
- 仍需通过统一 AccessContext 返回，不允许前端自行猜测管理员权限。

---

## 5. 权限组合矩阵

| 权限组合 | 基础行情列表 | DSA/选股 | 个股详情/K线 | 自选列表/操作 | 盘中监控 | 复盘授权 |
|---|---:|---:|---:|---:|---:|---:|
| 仅自选 | 是 | 否 | 否 | 是 | 是 | 否 |
| 仅行情 | 是 | 是 | 是 | 否 | 否 | 否 |
| 仅复盘 | 否 | 否 | 否 | 否 | 否 | 是 |
| 自选+行情 | 是 | 是 | 是 | 是 | 是 | 否 |
| 自选+复盘 | 是 | 否 | 否 | 是 | 是 | 是 |
| 行情+复盘 | 是 | 是 | 是 | 否 | 否 | 是 |
| 三项全部 | 是 | 是 | 是 | 是 | 是 | 是 |
| 无有效权限 | 否 | 否 | 否 | 否 | 否 | 否 |

复盘未上线期间，“复盘授权=是”只代表权限已保存，不代表已有可访问页面。

---

## 6. 邀请码业务规则

### 6.1 创建字段

管理员创建邀请码时提交：

```json
{
  "capabilities": {
    "watchlist_management": true,
    "market_screening": false,
    "review_management": false
  },
  "watchlist_stock_limit": 30,
  "duration_months": 3
}
```

### 6.2 校验规则

1. 至少勾选一个能力；
2. `duration_months` 必须为正整数；
3. 勾选自选时，`watchlist_stock_limit` 必须为正整数；
4. 未勾选自选时，`watchlist_stock_limit` 必须为 `null`；
5. 自选额度不使用预设档位；
6. 技术安全上限使用后端常量，不能由前端单独限制；默认建议：
   - `MAX_WATCHLIST_STOCK_LIMIT=100000`
   - `MAX_DURATION_MONTHS=120`
7. 邀请码创建后，权限配置不可编辑；需要变化时撤销未使用邀请码并新建；
8. 邀请码维持当前单人、单次兑换语义；
9. 已兑换邀请码不能再次兑换；
10. 被撤销邀请码不能兑换。

### 6.3 期限语义

`duration_months` 表示兑换后授予能力的有效月数，不等于 `N×30天`。

使用日历月运算：

```text
基准时间 + N个月
```

规则：

- 2026-07-25 10:00 + 1个月 = 2026-08-25 10:00；
- 2026-01-31 + 1个月 = 2026-02-28；
- 闰年按实际月末处理；
- 使用 timezone-aware 时间；
- 业务月运算以 `Asia/Shanghai` 计算，再以 UTC 入库；
- 有效区间采用 `[starts_at, expires_at)`，到达 `expires_at` 即失效。

邀请码自身是否存在“兑换截止日”沿用现有行为；本 PRD 不新增第二套邀请码截止规则。

---

## 7. 多次兑换与续期规则

用户允许兑换多个不同的邀请码。

每项能力独立计算，不能用同一个统一到期日覆盖三项权限。

兑换某项能力时：

```text
base = max(now, 该能力当前最晚有效 expires_at)
new_grant.starts_at = now
new_grant.expires_at = add_calendar_months(base, duration_months)
```

含义：

- 没有该能力：从当前兑换时间开始；
- 已拥有该能力：在当前最晚到期日后继续延长；
- 新授权立即生效，同时延长有效期；
- 一张邀请码包含多项能力时，每项能力独立计算到期时间。

### 7.1 自选额度叠加

任一时刻：

```text
effective_watchlist_limit
= 所有当前有效 watchlist_management grant 的 limit_value 最大值
```

低额度邀请码不能立即降低正在生效的更高额度。

高额度授权到期后，如果有效额度下降且已有自选数超过新额度：

- 不删除自选数据；
- 用户仍可查看和删除；
- 禁止继续新增；
- 盘中监控只覆盖按 `created_at ASC, id ASC` 排序的前 N 只；
- 超出额度的条目标记“超出当前额度，不参与监控”；
- 用户减少到额度内后恢复正常。

---

## 8. 数据模型

### 8.1 `invite_codes`

保留现有邀请码表及单次兑换字段，新增或确认：

| 字段 | 类型 | 说明 |
|---|---|---|
| `duration_months` | integer | 授权月数，>0 |
| `revoked_at` | timestamptz nullable | 未兑换邀请码撤销时间 |
| `redeemed_by_user_id` | FK nullable | 兑换用户 |
| `redeemed_at` | timestamptz nullable | 兑换时间 |

邀请码状态由字段推导：

```text
available / redeemed / revoked
```

不得同时依赖多个可漂移的状态字段。

### 8.2 `invite_code_capabilities`

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | PK | |
| `invite_code_id` | FK | 邀请码 |
| `capability_key` | varchar/check | 三个固定能力键之一 |
| `limit_value` | integer nullable | 仅自选能力使用 |
| `created_at` | timestamptz | |

约束：

- `UNIQUE(invite_code_id, capability_key)`；
- 自选能力 `limit_value > 0`；
- 非自选能力 `limit_value IS NULL`。

### 8.3 `user_capability_grants`

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | PK | |
| `user_id` | FK | 用户 |
| `capability_key` | varchar/check | 能力键 |
| `limit_value` | integer nullable | 自选额度 |
| `source_type` | varchar | 本期固定 `invite_code`，迁移可用 `legacy_subscription` |
| `source_id` | varchar/UUID | 来源记录 |
| `starts_at` | timestamptz | 开始时间 |
| `expires_at` | timestamptz | 结束时间 |
| `revoked_at` | timestamptz nullable | 撤销时间 |
| `created_at` | timestamptz | |
| `created_by` | FK nullable | 管理员/系统 |

约束：

- `expires_at > starts_at`；
- `UNIQUE(source_type, source_id, capability_key)`；
- 自选能力 `limit_value > 0`；
- 其他能力 `limit_value IS NULL`。

有效状态实时推导：

```text
revoked_at IS NULL
AND starts_at <= now
AND expires_at > now
```

不得依赖每日任务更新 `active/expired` 状态。

---

## 9. AccessContext 契约

`GET /me/access` 至少返回：

```json
{
  "is_admin": false,
  "capabilities": {
    "watchlist_management": {
      "active": true,
      "expires_at": "2026-10-25T14:00:00Z"
    },
    "market_screening": {
      "active": false,
      "expires_at": null
    },
    "review_management": {
      "active": true,
      "expires_at": "2026-11-01T00:00:00Z"
    }
  },
  "limits": {
    "watchlist_stock_limit": 30,
    "watchlist_current_count": 12,
    "watchlist_over_limit": false
  }
}
```

规则：

- 每项能力单独返回状态和到期时间；
- `watchlist_stock_limit=null` 表示无自选权限；
- 管理员返回 unlimited 的明确字段，不使用魔法大数；
- 前端不得从 Subscription、Plan 或邀请码自行重算；
- AccessContext 必须由 `AccessControlService` 一次聚合生成。

---

## 10. 后端授权边界

### 10.1 统一依赖

提供统一依赖或服务方法：

```text
require_capability(capability_key)
require_any_capability(capability_keys)
get_access_context(user_id)
```

### 10.2 API矩阵

| API/能力 | 所需权限 |
|---|---|
| 基础行情列表、股票搜索 | `watchlist_management OR market_screening` |
| DSA/趋势选股结果 | `market_screening` |
| 个股详情、Chart Snapshot、Bars、Indicators | `market_screening` |
| 自选列表、添加、删除 | `watchlist_management` |
| 自选监控状态 | `watchlist_management` |
| 盘中监控用户资格 | `watchlist_management` |
| 复盘 API | `review_management` |
| 管理邀请码 | `is_admin` |

拒绝响应：

- 未认证：401；
- 已认证但无能力：403，稳定 `reason_code=CAPABILITY_REQUIRED`；
- 自选达到额度：409，`reason_code=WATCHLIST_LIMIT_REACHED`；
- 邀请码已使用：409，`reason_code=INVITE_CODE_ALREADY_REDEEMED`；
- 邀请码已撤销：409，`reason_code=INVITE_CODE_REVOKED`。

### 10.3 自选额度并发安全

添加自选必须在同一数据库事务中：

1. 锁定用户有效授权或使用等价 advisory lock；
2. 获取有效额度；
3. 统计当前有效自选；
4. 判断是否可新增；
5. 插入；
6. 提交。

两个并发请求不能同时绕过最后一个名额。

---

## 11. 邀请码兑换事务

兑换必须在单事务中完成：

1. `SELECT ... FOR UPDATE` 锁定邀请码；
2. 校验邀请码存在、未撤销、未兑换；
3. 读取邀请码能力配置；
4. 对每项能力计算独立 `expires_at`；
5. 创建 `user_capability_grants`；
6. 写入 `redeemed_by_user_id` 和 `redeemed_at`；
7. 提交；
8. 精确失效该用户 AccessContext 缓存。

任一步失败全部回滚。不得出现“邀请码已使用但授权未创建”或“部分能力创建成功”的状态。

---

## 12. 盘中监控与消息

`EligibleUserService` 必须使用统一 AccessContext：

- 只有有效 `watchlist_management` 用户进入监控；
- 最多选择有效额度 N 只股票；
- 超额时按稳定顺序选择前 N 只；
- 权限到期后不再产生新监控事件、Outbox 和投递；
- 不删除历史自选、历史事件和历史消息；
- 历史消息读取继续沿用现有消息权限，本期不额外改变。

行情权限不得让用户自动进入盘中监控。

---

## 13. 管理端交互

### 13.1 创建邀请码

```text
权限配置
☐ 自选管理
   自选股票上限 [      ] 只
☐ 行情选股
☐ 复盘管理

授权期限 [   ] 个月
[生成邀请码]
```

交互规则：

- 至少勾选一个；
- 勾选自选后显示并启用额度；
- 取消自选时清空额度；
- 额度和月份只接受正整数；
- 后端错误必须展示稳定原因；
- 创建成功后显示不可变的权限摘要。

### 13.2 邀请码列表

显示：

- 邀请码；
- 三项权限摘要；
- 自选额度；
- 授权月数；
- available/redeemed/revoked；
- 创建人、创建时间；
- 兑换人、兑换时间；
- 操作：复制、查看、撤销未使用邀请码。

不得允许编辑已生成邀请码的权限配置。

---

## 14. 用户端交互

### 14.1 导航

- 行情入口：自选或行情任一权限有效时显示；
- 自选 scope 和操作：仅自选权限；
- DSA/选股、详情入口：仅行情权限；
- 复盘入口：功能上线后，且复盘权限有效时显示。

### 14.2 直接URL访问

前端路由守卫应展示无权限状态，但后端 API 仍必须独立返回403。

### 14.3 自选额度展示

自选用户可看到：

```text
已使用 12 / 30
```

超额时显示：

```text
当前 35 只，额度 30 只。请移除至少 5 只；超额股票暂不参与盘中监控。
```

---

## 15. 旧数据迁移

### 15.1 现有有效会员

为每个当前有效会员生成 `legacy_subscription` 来源授权：

- `watchlist_management`：有效，额度取当前真实 Plan/Subscription 配额；
- `market_screening`：有效；
- `review_management`：不自动授予；
- `starts_at`、`expires_at`沿用当前有效订阅；
- 无法推导额度或期限时迁移失败并报告，禁止默认猜值。

### 15.2 现有未兑换邀请码

按当前邀请码关联的 Plan/Subscription 配置迁移：

- 自选 + 行情；
- 复盘=false；
- 自选额度取当前配置；
- 月数取当前真实期限；
- 无法确定时标记为不可兑换并输出清单，不静默赋默认值。

### 15.3 兼容期

- `AccessControlService`以新grant为真源；
- 旧Plan/Subscription保留用于历史和迁移核对；
- 迁移闭合前不得删除旧字段或表；
- 不允许新旧两套权限判断长期并行。

---

## 16. 审计与可观察性

至少记录：

- 管理员创建/撤销邀请码；
- 用户兑换邀请码；
- 生成的能力和到期时间；
- 自选额度拒绝；
- API能力拒绝；
- 监控因权限或额度跳过。

日志不得输出完整邀请码，只显示末4位或哈希。

---

## 17. 验收标准

1. 管理员可自由组合三项权限；
2. 自选额度为管理员填写的正整数；
3. 邀请码按日历月生成授权；
4. 多次兑换按能力独立续期；
5. 七种有效权限组合均符合矩阵；
6. 所有敏感API均有后端能力拦截；
7. 仅自选用户不能进入详情；
8. 仅行情用户看不到自选和监控；
9. 自选额度并发添加无法超限；
10. 权限到期无需定时任务即可实时失效；
11. 监控只处理有效自选权限和额度内股票；
12. 兑换事务不会产生部分成功；
13. 旧有效会员和旧未兑换邀请码完成可核对迁移；
14. 复盘权限能保存、返回和校验，但不虚构业务页面；
15. docs、API契约、测试和代码一致。

---

## 18. 明确禁止

- 不得只在前端隐藏；
- 不得把行情权限自动等同自选权限；
- 不得让自选权限打开完整详情；
- 不得使用 `N×30天`代替日历月；
- 不得把三项权限压成一个统一到期日；
- 不得在Worker里复制权限判断；
- 不得在邀请码兑换后修改原邀请码配置；
- 不得直接改历史migration；
- 不得通过默认值掩盖旧数据迁移失败；
- 不得为了通过测试放宽403或额度校验。
