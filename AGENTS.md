# 盘迹项目开发与文档一致性规则 v2

适用项目：`market_dev` / 盘迹 PanJi\
适用阶段：探索期产品 + 可运行生产验证期\
核心目标：防止 AI/Trae 在新对话、新机器或新分支中误解当前系统，防止已确认业务逻辑被旧代码、旧文档或旧记忆还原。

***

## 一、最高原则

任何修改必须形成闭环：

```
读取文档入口
→ 理解当前系统地图
→ 核对真实代码入口
→ 建立 CHANGE
→ 明确修改范围和不修改范围
→ 修改代码/文档/测试
→ 运行一致性检查
→ 创建 PR
→ 人工 Review 后合并

```

完成标准不是“代码能跑”，而是：

```
代码实现
= 当前设计文档
= 系统地图
= API / 数据契约
= 测试验证
= 部署配置

```

***

## 二、当前文档结构

当前真实文档结构以 v2 为准：

```
docs/
  README.md
  AI-ONBOARDING.md
  RESTORE-CHECKLIST.md
  MAINTENANCE.md
  MIGRATION-MAP.md
  SOURCE-SNAPSHOT.md

  current/
    MANIFEST.md
    00-product-business.md
    01-system-architecture.md
    02-data-api-contracts.md
    03-jobs-integrations-operations.md
    04-frontend-ux.md
    05-testing-acceptance.md
    open-decisions.md
    code-doc-alignment.md

  maps/
    backend-module-map.md
    frontend-route-map.md
    api-route-map.md
    database-model-map.md
    worker-job-map.md
    notification-flow-map.md
    test-coverage-map.md
    deployment-runtime-map.md

  changes/
    CHANGELOG.md
    records/

  archive/
    current-legacy-20260703/

```

旧规则中 `docs/current/00-18` 的结构已经废弃。旧 current 文档只作为历史归档，不再作为当前事实源。

***

## 三、文档职责

### 1. `docs/README.md`

文档系统入口。说明文档地图、阅读顺序、事实源边界和修改流程。

### 2. `docs/AI-ONBOARDING.md`

新对话、新机器、新 agent 必须先读。用于快速恢复项目上下文。

任何 Trae/Codex/ChatGPT 任务开始前，必须先读取：

```
docs/AI-ONBOARDING.md
docs/current/MANIFEST.md

```

### 3. `docs/RESTORE-CHECKLIST.md`

用于判断新环境是否已经正确理解项目。不得跳过。

### 4. `docs/current/MANIFEST.md`

当前设计基线唯一总入口。\
全局基线字段只维护在这里，不再要求每个 current 文档重复维护。

必须包含：

```
设计基线日期
设计确认截止日期
实现核对基线
实现核对分支
最近一致性检查日期

```

### 5. `docs/current/*.md`

只描述当前有效设计，不保存历史。

职责：

```
00-product-business.md              产品定位、用户、核心业务边界
01-system-architecture.md           系统架构、模块边界、技术栈、部署单元
02-data-api-contracts.md            数据模型、API、权限、安全、参数契约
03-jobs-integrations-operations.md  Worker、调度、飞书、Capture、Outbox、部署运维
04-frontend-ux.md                   路由、页面、前端状态、UI 规则
05-testing-acceptance.md            测试层级、验收门禁、CI 策略
open-decisions.md                   真正未决的问题
code-doc-alignment.md               当前代码/文档/生产表现不一致的 Known Gap

```

### 6. `docs/maps/*.md`

这是系统实现地图，不是 PRD。

必须帮助新 agent 回答：

```
代码在哪里？
哪个页面调哪个 API？
哪个 API 调哪个 Service？
哪个 Worker 处理哪个任务？
哪些表保存核心状态？
哪些测试覆盖关键规则？
生产服务如何运行？

```

maps 可以半自动维护，但不能胡编。

### 7. `docs/changes/`

只保存变更历史。\
历史设计、旧方案、修复过程、证据链都放这里，不放 current。

### 8. `docs/archive/`

只保存历史归档，不作为当前事实源。\
旧 `docs/current/00-18` 已归档后，不得再作为修改依据。

***

## 四、事实源优先级

当代码、文档、历史 CHANGE、生产表现冲突时，不得擅自判断谁一定正确。

判断顺序：

```
1. 用户当前明确要求
2. 当前 main 代码
3. docs/current/MANIFEST.md
4. docs/current/*.md
5. docs/maps/*.md
6. 最新 docs/changes/records/*.md
7. 测试与 CI 结果
8. 生产只读验证结果
9. archive 历史文档
10. 旧聊天记忆

```

archive 和旧聊天不能覆盖 current。

***

## 五、每次修改前必须读取

### 通用必读

```
docs/AI-ONBOARDING.md
docs/current/MANIFEST.md
docs/RESTORE-CHECKLIST.md

```

### 修改产品/业务

```
docs/current/00-product-business.md
docs/current/02-data-api-contracts.md
docs/maps/api-route-map.md
docs/maps/database-model-map.md

```

### 修改后端

```
docs/current/01-system-architecture.md
docs/current/02-data-api-contracts.md
docs/maps/backend-module-map.md
docs/maps/api-route-map.md
docs/maps/database-model-map.md

```

### 修改前端

```
docs/current/04-frontend-ux.md
docs/maps/frontend-route-map.md
docs/maps/api-route-map.md

```

### 修改 Worker / 飞书 / Capture / Outbox / Delivery

```
docs/current/03-jobs-integrations-operations.md
docs/maps/worker-job-map.md
docs/maps/notification-flow-map.md
docs/maps/deployment-runtime-map.md

```

### 修改测试 / CI

```
docs/current/05-testing-acceptance.md
docs/maps/test-coverage-map.md

```

### 修改部署

```
docs/current/03-jobs-integrations-operations.md
docs/maps/deployment-runtime-map.md
docker-compose.prod.yml

```

***

## 六、修改前理解说明

Trae 在动手前必须输出：

```
1. 当前任务目标
2. 当前分支和 base commit
3. 已阅读哪些 docs/current 文件
4. 已阅读哪些 docs/maps 文件
5. 当前代码真实入口
6. 当前前端入口
7. 当前 API 入口
8. 当前 Service / Repository / Worker 入口
9. 当前涉及哪些数据表
10. 当前测试覆盖哪些规则
11. 文档和代码是否一致
12. 本次准备修改什么
13. 明确不修改什么
14. 预计更新哪些 docs/current
15. 预计更新哪些 docs/maps
16. 预计新增哪个 CHANGE

```

如果发现冲突，先列出冲突，不得直接编码。

***

## 七、CHANGE 规则

每次修改必须新增：

```
docs/changes/records/CHANGE-YYYYMMDD-NNN.md

```

并更新：

```
docs/changes/CHANGELOG.md

```

CHANGE 必须包含：

```
变更编号
任务名称
需求出处
修改前行为
修改后行为
影响模块
修改文件
文档更新
测试证据
Git 分支
Git Commit
数据库迁移
配置变化
风险
遗留问题

```

不存在“小改不用 CHANGE”。

***

## 八、current 文档修改规则

`docs/current/` 只写当前状态，不写流水账。

错误写法：

```
以前是 A，现在改成 B。

```

正确写法：

```
当前系统使用 B。

```

历史 A 放入 CHANGE 或 archive。

***

## 九、maps 修改规则

如果修改了真实代码结构，必须检查对应 `docs/maps/`。

例如：

```
新增/删除 API              → api-route-map.md
新增/移动 Service          → backend-module-map.md
新增/移动页面              → frontend-route-map.md
改表、字段、约束            → database-model-map.md
改 Worker / 调度           → worker-job-map.md
改飞书、Capture、Outbox     → notification-flow-map.md
改测试覆盖                 → test-coverage-map.md
改 compose / 部署服务       → deployment-runtime-map.md

```

maps 不能变成理想设计，必须描述真实实现位置。

***

## 十、禁止行为

Trae 和开发者禁止：

```
1. 未读 AI-ONBOARDING 和 MANIFEST 就修改；
2. 根据旧 docs/current/00-18 修改当前系统；
3. 根据 archive 恢复旧设计；
4. 根据旧聊天记忆覆盖 current；
5. 只改代码不改文档；
6. 只改 current 不改 CHANGE；
7. 改代码结构不更新 maps；
8. 只更新文档不核对真实代码；
9. 复制旧实现形成第二条路径；
10. 在前端重新实现后端业务规则；
11. 删除测试以适配错误实现；
12. 修改 API 不检查前端调用；
13. 修改数据模型不检查 migration；
14. 修改 Worker 不检查幂等、心跳、重试；
15. 修改权限不检查用户隔离；
16. 把 Mock E2E 说成真实生产 E2E；
17. 把 OPEN 问题写成最终结论；
18. 把临时实验写成永久规则；
19. 直接修改 main；
20. force push 已共享分支；
21. 为通过检查削弱 check_docs_consistency.py。

```

***

## 十一、docs consistency 硬规则

`tools/check_docs_consistency.py` 必须匹配 v2 文档结构。

必须检查：

```
1. docs/current/MANIFEST.md 存在；
2. MANIFEST 包含实现核对基线；
3. 实现核对基线是 40 位 SHA；
4. SHA 是真实 commit；
5. SHA 是当前 HEAD 祖先；
6. docs/current/*.md 存在；
7. docs/maps/*.md 存在；
8. 本地 Markdown 链接有效；
9. 不存在 待填写 占位符；
10. current 文档不得把 feishu_webhook 写成当前方案；
11. open-decisions 不得把 Webhook vs Platform App 写回 OPEN；
12. archive 旧文档不参与 baseline 一致性检查。

```

禁止把检查改成摆设。

***

## 十二、盘迹项目硬规则

### 1. 产品边界

盘迹是 A 股研究、全市场特征计算、自选股盘中监控和消息投递平台。

不做：

```
自动交易
券商账户连接
资金管理
收益承诺
单一指标买卖信号
普通用户修改生产算法参数

```

### 2. 策略规则

当前生产只保留：

```
dsa_selector
watchlist_monitor

```

多策略组合已废弃，不得从旧代码或旧文档恢复。

### 3. DSA 规则

DSA 对全市场 computable universe 计算特征。\
不得在计算阶段按方向、强弱、matched、用户筛选提前删除股票。

发布必须满足严格完整性门禁。\
`partial_failed` 不得发布。

### 4. 自选和监控

有效会员添加自选后自动进入盘中监控。\
不创建 MonitoringPlan。\
到期用户保留历史数据，但不能读取、修改、监控或产生新投递。

### 5. Node Cluster

固定契约：

```
1d = 250 根日线
15m = 250 * 16 = 4000 根
1m = 2 根已完成 Bar

```

图表显示数量、指标输出数量、Node 内部输入数量必须分离。

### 6. 飞书

唯一接入方式：

```
feishu_platform_app

```

禁止恢复：

```
feishu_webhook
FEISHU_WEBHOOK
独立管理员飞书 App
独立管理员接收人配置

```

管理员内测申请通知必须复用管理员用户自己的 active `feishu_platform_app` NotificationChannel。

### 7. Capture Token

Capture Token 只能访问 Capture API。\
不能访问普通用户 API。\
不能污染普通 Access Token。

### 8. Outbox target\_channel\_id

`notification.message.created` payload 含 `target_channel_id` 时，属于用户主动触发/手动指定渠道通知，跳过 `eligible_user_service`。

无 `target_channel_id` 的自动通知仍必须走用户资格过滤。

### 9. Migration

不得修改已发布历史 migration。\
只允许新增前向 migration。\
修改 migration 必须有 upgrade/downgrade/upgrade 验证。

### 10. Alignment

未经测试、CI 或真实运行证据，不得关闭 `code-doc-alignment.md` 条目。\nMock 不能替代真实生产 E2E。

### 11. 测试期部署不备份数据库

测试期部署默认不备份数据库；除非用户明确说“先备份数据库”，否则禁止 `pg_dump` / 大体积备份，禁止写入 `/root/backups` 或 `/root/web_dev/backups`。当前物理机磁盘紧张，优先节省硬盘；有问题直接定位修复。

### 12. Docker 镜像保护

`node:20-alpine` 是受保护基础镜像，拉取很慢。禁止主动删除 `node:20-alpine`。\
禁止 `docker image prune -a`。\
除非明确升级 Node 版本或镜像损坏，否则不要删除 `node:20-alpine`。\
普通清理只允许 `docker builder prune -f`、`docker image prune -f`、`docker container prune -f`。

***

## 十三、质量门禁

### Ruff

新增或修改的 Python 文件必须 Ruff 零错误。\
全仓 Ruff 历史债务由 `tools/quality_baselines/ruff.json` 管控。\
禁止通过全局 ignore、批量 noqa、扩大 exclude 掩盖新增问题。

### Mypy

新增 backend/app Python 生产文件必须 mypy 零错误。\
全仓 mypy 历史债务由 `tools/quality_baselines/mypy.json` 管控。\
禁止批量 `type: ignore` 或关闭检查掩盖新增问题。

### 文档检查

每次 PR 至少运行：

```
python tools/check_docs_consistency.py
python tools/check_architecture.py
python tools/check_test_allowlist.py
python tools/update_docs.py --check

```

***

## 十四、分支和 PR

每个变更使用独立分支：

```
fix/<topic>
feat/<topic>
docs/<topic>
refactor/<topic>
chore/<topic>
experiment/<topic>

```

禁止直接改 main。

PR 必须说明：

```
1. 当前系统原来如何运行；
2. 本次为什么修改；
3. 修改了哪些代码；
4. 修改了哪些 docs/current；
5. 修改了哪些 docs/maps；
6. 新增哪个 CHANGE；
7. 是否改变 API；
8. 是否改变数据模型；
9. 是否改变 Worker 或第三方集成；
10. 测试结果；
11. 是否仍有 Known Gap；
12. 是否需要生产验证。

```

***

## 十五、完成报告格式

Trae 完成后必须输出：

```
当前分支：
Base Commit：
Head Commit：

一、修改前理解
- 当前产品行为：
- 当前系统地图依据：
- 当前代码入口：
- 当前文档依据：
- 当前冲突：

二、实际修改
- 代码文件：
- docs/current：
- docs/maps：
- docs/changes：
- tools：
- 测试：

三、一致性检查
- current 是否更新：
- maps 是否更新：
- CHANGE 是否新增：
- CHANGELOG 是否更新：
- archive 是否只作历史参考：
- 是否存在未登记冲突：

四、验证
- 执行命令：
- 测试结果：
- CI 状态：

五、剩余问题
- Known Gap：
- OPEN：
- 需要生产验证：

```

***

## 十六、最终规则

任何修改都必须满足：

```
当前设计文档
+ 系统实现地图
+ 真实代码
+ 测试
+ CHANGE
+ PR

```

六者缺一不可。

如果只是代码变了，文档没变，不算完成。\
如果只是文档变了，代码没核对，不算完成。\
如果 maps 过期，新对话会误解项目，也不算完成。
