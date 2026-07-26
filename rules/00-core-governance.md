# 00 核心治理

> 来源：AGENTS.md §一、§三、§四 + 提议
> 状态：并行验证（§一/§三/§四 部分）；PLANNED（分层、单一代码源、时间和因果 提议部分）

## 修改闭环

任何修改必须形成闭环：读取文档入口 → 理解系统地图 → 核对真实代码 → 建立 CHANGE → 明确修改/不修改范围 → 修改代码/文档/测试 → 运行一致性检查 → PR → 人工 Review 后合并。

完成标准（六者对齐）：代码实现 = 当前设计文档 = 系统地图 = API/数据契约 = 测试验证 = 部署配置。六者缺一不可。

## 事实源优先级

冲突时判断顺序（前者覆盖后者）：

1. 用户当前明确要求；
2. 当前 main 代码；
3. `docs/current/MANIFEST.md`；
4. `docs/current/*.md`；
5. `docs/maps/*.md`；
6. 最新 `docs/changes/records/*.md`；
7. 测试与 CI 结果；
8. 生产只读验证结果；
9. archive 历史文档；
10. 旧聊天记忆。

archive 和旧聊天不能覆盖 current。

> Phase 1 注：本优先级与 `AGENTS.md` §三完全一致。`sync/` 草案提议将 `rules/` 提到第 3 位，但本阶段不采用，保留 `docs/current/MANIFEST.md` 第 3 位。

## 修改前最小报告

Trae 动手前必须输出：

- 任务目标；
- 分支和 base commit；
- 已读 `docs/current` 与 `docs/maps`；
- 当前代码入口（前端/API/Service/Repository/Worker）；
- 涉及数据表；
- 测试覆盖规则；
- 文档与代码是否一致；
- 本次准备修改什么；
- 明确不修改什么；
- 预计更新哪些 `docs/current` 与 `docs/maps`；
- 预计新增哪个 CHANGE。

发现冲突先列出，不得直接编码。

## 分层原则（PLANNED）

> 提议中，尚未在 `AGENTS.md` 确立。

- API：认证、参数、响应；
- Service：业务状态、事务、资格、幂等；
- Repository：数据访问；
- Kernel：计算；
- Adapter：外部系统；
- 前端：ViewModel 与展示，不重算后端业务。

## 单一代码源（PLANNED）

> 提议中，尚未在 `AGENTS.md` 确立。

- 所有正式修改必须提交 GitHub；
- 服务器运行目录不产生业务代码；
- 自动部署只部署 GitHub commit。

## 时间和因果（PLANNED）

> 提议中，尚未在 `AGENTS.md` 确立。

- 业务时间 Asia/Shanghai；
- 历史和盘后必须 point-in-time；
- causal、confirmed_delay、hindsight、label 严格分离。
