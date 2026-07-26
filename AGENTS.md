# 盘迹项目 AGENTS（入口与规则路由器）

适用项目：`market_dev` / 盘迹 PanJi
核心目标：防止 AI/Trae 在新对话、新机器、新分支中误解当前系统，防止已确认业务逻辑被旧代码、旧文档或旧记忆还原。

> 本文件是项目入口、规则路由器和最高安全边界。详细业务和技术规则在 `rules/`，当前项目事实在 `docs/current/`，代码地图在 `docs/maps/`，变更历史在 `docs/changes/`。

---

## 一、最高原则

任何修改必须形成闭环：读取文档入口 → 理解系统地图 → 核对真实代码 → 建立 CHANGE → 明确修改/不修改范围 → 修改代码/文档/测试 → 运行一致性检查 → PR → 人工 Review 后合并。

完成标准（六者对齐）：代码实现 = 当前设计文档 = 系统地图 = API/数据契约 = 测试验证 = 部署配置。六者缺一不可。

用户当前明确指令优先级最高。能力或权限范围不明确时，必须先询问用户，不得自行假设。

---

## 二、状态边界

- **CURRENT**：当前生产/正式生效的事实与规则。`docs/current/` 与已激活的 `rules/` 条款属于 CURRENT。
- **WIP**：进行中的开发工作，未达到 CURRENT。必须在 CHANGE 与分支中明确标记。
- **PLANNED**：未来阶段提议，尚未实施，不得描述为已生效。包括自动部署、`/opt/panji-deploy`、forced-command SSH、GitHub 部署 secrets、Capability V2、尚未在腾讯云建设的目录和脚本。

---

## 三、必读顺序

任何任务开始前必须按以下顺序读取：

1. `AGENTS.md`（本文件）；
2. `rules/README.md` + 对应 `rules/*.md`；
3. `docs/current/MANIFEST.md` + 对应 `docs/current/*.md`；
4. 对应 `docs/maps/*.md`；
5. 对应 `docs/changes/records/CHANGE-*.md`；
6. 对应 `docs/runbooks/*.md`（运维任务时）。

`docs/` 顶级目录只允许：`current/` `maps/` `changes/` `archive/` `contracts/` `decisions/` `runbooks/` `acceptance/` `evidence/` `work/`（`docs/` 根 `.md` 文件不受限）。

`sync/` 是临时中转站，不是正式真源，不得作为运行时依赖。

---

## 四、事实源优先级

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

---

## 五、修改流程

Trae 动手前必须输出：任务目标 / 分支和 base commit / 已读 docs/current 与 docs/maps / 当前代码入口（前端/API/Service/Repository/Worker）/ 涉及数据表 / 测试覆盖规则 / 文档与代码是否一致 / 本次准备修改什么 / 明确不修改什么 / 预计更新哪些 docs/current 与 docs/maps / 预计新增哪个 CHANGE。发现冲突先列出，不得直接编码。详见 `rules/00-core-governance.md`。

---

## 六、CHANGE 规则

每次修改必须新增 `docs/changes/records/CHANGE-YYYYMMDD-NNN.md` 并更新 `docs/changes/CHANGELOG.md`。不存在"小改不用 CHANGE"。必填字段与规则见 `rules/40-testing-quality.md`。`tools/check_docs_consistency.py` 规则 12 强制校验 CHANGE 引用可达性。

---

## 七、规则索引（rules/）

详细强制规则按主题拆分在 `rules/`：

| 文件 | 主题 |
|---|---|
| `rules/00-core-governance.md` | 事实源优先级、修改闭环、修改前最小报告 |
| `rules/10-product-domain-invariants.md` | 产品边界、策略、DSA、自选与监控、飞书 |
| `rules/20-market-data-indicators.md` | MDAS、复权、Node Cluster、SMC、AFC、Canonical、ChartSnapshot、板块同步、因子版本 |
| `rules/30-access-security.md` | Capture Token、权限隔离、生产秘密 |
| `rules/40-testing-quality.md` | CHANGE 必填、CI 门禁、质量门禁、测试纪律、ref 隔离测试 |
| `rules/50-git-development-flow.md` | 分支、PR、提交安全、执行模式、继续执行 |
| `rules/60-trae-work.md` | TRAE Work 角色边界与分支模型 |
| `rules/70-trae-cn.md` | TRAE CN 多模式职责 |
| `rules/80-deployment-data-safety.md` | Migration、不备份、Docker 镜像保护、Live Mount |
| `rules/85-server-directory-boundaries.md` | 三目录职责（PLANNED 部分） |
| `rules/90-deprecated-forbidden.md` | 禁止行为清单、废弃项、禁止恢复项 |
| `rules/AGENTS-MIGRATION-MAP.md` | AGENTS 章节 → rules 映射表 |

---

## 八、TRAE Work 分支模型

TRAE Work 使用系统生成的 `trae/agent-*` 内部分支工作，**不固定直接工作在 dev 分支**，**不允许切换分支**。

- `origin/dev` 是统一开发基线；
- 开始任务时必须 `git fetch origin dev` 并确认 `origin/dev` 是当前 HEAD 的祖先（`git merge-base --is-ancestor origin/dev HEAD` 退出码为 0）；
- 若 `origin/dev` 已前进、不是当前 HEAD 祖先，必须停止并报告，不得自行 merge、rebase 或覆盖；
- 完成后使用 `git push origin HEAD:dev` 以 fast-forward 方式推送当前 HEAD 到远程 dev；
- **只允许 fast-forward；禁止 force push**；
- 禁止 `git add -A` / `git add .` / `git add -u`；必须精确 `git add <file>`。

详见 `rules/60-trae-work.md`。

---

## 九、TRAE CN 能力边界

TRAE CN 保留开发、测试、部署、验收和运维能力，可按需切换模式（开发/测试/观察/手动部署/排障/紧急修复）。对 TRAE Work、TRAE CN 权限范围不确定时先询问用户。详见 `rules/70-trae-cn.md`。

---

## 十、最高风险禁止项

以下为不可放松的最高安全边界（详见 `rules/90-deprecated-forbidden.md` 与 `rules/80-deployment-data-safety.md`）：

- 不删除数据库卷、不执行 `docker compose down -v`；
- 不执行 `docker image prune -a`，不主动删除 `node:20-alpine`；
- 不修改已发布历史 migration；
- 不泄露秘密（Token、SSH 私钥、数据库连接、密码）；
- 不把 Work Preview 当腾讯云真实验收；
- 不绕过 `check_docs_consistency.py` / `check_architecture.py` / `check_test_allowlist.py` / `check_governance_rules.py`；
- 不为通过检查扩大 ignore、批量 noqa、批量 `type: ignore` 或关闭检查；
- 不 `force push` 已共享分支；
- 生产代码/测试/工具/构建脚本运行时不 `import`/`open`/`read`/`glob` `ref/` 目录；
- 不恢复 `feishu_webhook` / 多策略组合 / SMC FVG / 个股详情行情双源。

---

## 十一、质量门禁

```
Ruff   新增/修改 Python 文件零错误；历史债务由 tools/quality_baselines/ruff.json 管控
Mypy   新增 backend/app Python 生产文件零错误；历史债务由 tools/quality_baselines/mypy.json 管控
Docs   python tools/check_docs_consistency.py
Arch   python tools/check_architecture.py
Allow  python tools/check_test_allowlist.py
Gov    python tools/check_governance_rules.py
Sync   python tools/update_docs.py --check
```

前端：`tsc --noEmit`、`npm run lint`、`npm run build`、`npm run test:contract`、`npm run test:e2e`。

---

## 十二、完成报告格式

当前分支 / Base Commit / Head Commit；一、修改前理解（产品行为/系统地图/代码入口/文档依据/冲突）；二、实际修改（代码/docs/current/docs/maps/docs/changes/tools/测试）；三、一致性检查（current/maps/CHANGE/CHANGELOG/archive 是否更新）；四、验证（执行命令/测试结果/CI 状态）；五、剩余问题（Known Gap/OPEN/需要生产验证）。

---

## 十三、变更历史索引

完整变更历史见 `docs/changes/CHANGELOG.md`（按日期顺序的简短摘要）与 `docs/changes/records/CHANGE-YYYYMMDD-NNN.md`（每条变更的完整记录）。任何对历史变更的疑问必须查阅对应 record，不得凭旧聊天记忆或 archive 推断。
