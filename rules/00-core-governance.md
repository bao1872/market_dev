# 00 核心治理

> 来源：AGENTS.md §一、§三、§四
> 状态：生效（Phase 2 激活）

## 修改闭环

任何修改必须形成闭环：读取文档入口 → 理解系统地图 → 核对真实代码 → 判断任务类型与文档影响 → 明确修改/不修改范围 → 修改代码/必要文档/测试 → 运行一致性检查 → 提交。只有重要业务规则、契约、主要结构或运行方式发生变化时才建立一个 CHANGE；普通 Bug 由 Git 历史记录。

完成标准（六者对齐）：代码实现 = 当前设计文档 = 系统地图 = API/数据契约 = 测试验证 = 部署配置。六者缺一不可。

## 事实源优先级

冲突时判断顺序（前者覆盖后者）：

1. 用户当前明确要求；
2. 当前工作分支的代码、数据、日志与运行事实；
3. `docs/prd/*.md`（已确认需求与目标行为）；
4. `docs/maps/*.md`（已核验当前实现）；
5. `docs/changes/INDEX.md` 及其指向的最新相关 Change；
6. 测试与 CI 结果；
7. 生产只读验证结果；
8. archive 历史文档；
9. 旧聊天记忆。

archive 和旧聊天不能覆盖 current。

> 注（2026-07-29 收口）：`docs/current/` 已标记为 legacy 只读，不再作为事实源优先级入口；
> 事实源以 `docs/prd/` 与 `docs/maps/` 为准。后续 `docs/current/` 将另行迁移。
> 历史 `reports/` 目录已删除，不再作为读取入口。

## 修改前最小报告

Trae 动手前必须输出：

- 任务目标；
- 分支和 base commit；
- 已读 `docs/prd` 与 `docs/maps`（`docs/current` 已 legacy 只读，不再要求读取）；
- 当前代码入口（前端/API/Service/Repository/Worker）；
- 涉及数据表；
- 测试覆盖规则；
- 文档与代码是否一致；
- 本次准备修改什么；
- 明确不修改什么；
- 预计更新哪些 `docs/maps`；
- 是否需要 CHANGE；需要时说明唯一文件。

发现冲突先列出，不得直接编码。

## 分层原则

- API：认证、参数、响应；
- Service：业务状态、事务、资格、幂等；
- Repository：数据访问；
- Kernel：计算；
- Adapter：外部系统；
- 前端：ViewModel 与展示，不重算后端业务。

## 单一代码源

- 所有正式修改必须提交 GitHub；
- 服务器运行目录不产生业务代码；
- 自动部署只部署 GitHub commit（自动部署本身为 PLANNED，未实现）。

## 时间和因果

- 业务时间 Asia/Shanghai；
- 历史和盘后必须 point-in-time；
- causal、confirmed_delay、hindsight、label 严格分离。
