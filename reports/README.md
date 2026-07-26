# reports/ 长期报告管理体系

> 状态：生效（2026-07-26 建立，CHANGE-20260726-003）
> 主归属规则：`rules/40-testing-quality.md`
> 检查器：`tools/check_reports.py`

---

## 1. 目录定位

`reports/` 保存：

- TRAE Work 任务执行报告；
- TRAE CN 开发、测试、部署和验收报告；
- 项目治理审计报告；
- 测试和故障调查结果；
- 部署、回滚和运行验证证据；
- 用户明确要求长期保留的任务结论。

`reports/` 不保存：

- 产品正式定义；
- 当前架构真源；
- 业务规则真源；
- API 合同真源；
- 数据库合同真源；
- 密码、Token、SSH 私钥、数据库凭据；
- 数据库备份；
- 大型日志、构建产物和二进制文件。

正式事实仍属于：

- `AGENTS.md`
- `rules/`
- `docs/current/`
- `docs/maps/`
- `docs/changes/records/CHANGE-*.md`
- 真实代码和测试

报告与正式事实冲突时，以正式事实源为准，并在后续 CHANGE 中修正。

`reports/` 不是产品、架构或业务事实真源。

---

## 2. 文件命名

统一使用：

```
REPORT-YYYYMMDD-NNN-任务短名称.md
```

例如：

```
REPORT-20260726-001-governance-phase1.md
REPORT-20260726-002-governance-phase2.md
REPORT-20260727-001-stock-detail-fix.md
```

规则：

- 日期使用 Asia/Shanghai 日期；
- NNN 为当天三位流水号；
- 创建前扫描当天已有报告，使用下一个空闲编号；
- 任务短名称只使用小写英文、数字和连字符；
- 不使用 `final-final` / `new` / `latest2` 等不可追踪名称。

---

## 3. 存放位置

正在进行或刚完成、仍可能需要继续处理的报告：

```
reports/current/
```

任务结束并被后续工作取代后：

```
reports/archive/YYYY/MM/
```

不要在每次任务后立即归档。`current/` 只保留当前仍有后续价值的有限报告。

---

## 4. LATEST.md

`reports/LATEST.md` 必须始终是一个简短索引，不复制整份报告。

内容必须包含：

- 最新报告 ID；
- 报告标题；
- 状态；
- 创建时间；
- 工作环境；
- 分支；
- Base SHA；
- End SHA；
- 完整报告相对路径；
- 对应 CHANGE；
- 一句话结论。

示例：

```
# Latest Report

- Report: REPORT-20260726-002
- Title: Project Governance Phase 2
- Status: COMPLETED
- Created: 2026-07-26 23:10 Asia/Shanghai
- Environment: TRAE Work
- Branch: trae/agent-xxxx
- Base SHA: abc1234
- End SHA: def5678
- Path: reports/current/REPORT-20260726-002-governance-phase2.md
- CHANGE: CHANGE-20260726-003
- Summary: rules 已正式生效，自动部署仍为 PLANNED。
```

`LATEST.md` 指向的文件必须真实存在。`LATEST.md` 是 AI 读取最新任务状态的固定入口。

---

## 5. INDEX.md

`INDEX.md` 是报告索引，按日期倒序维护。

每份报告只占一行：

```
| Report ID | Date | Type | Status | Title | Environment | Commit | Path |
```

要求：

- 新报告插入最上方；
- 不复制完整报告内容；
- 不删除历史索引；
- 报告归档后更新 Path；
- 同一个 Report ID 不允许重复。

---

## 6. 报告状态

只允许：

- `COMPLETED`
- `PARTIAL`
- `BLOCKED`
- `FAILED`
- `SUPERSEDED`

不能使用含糊状态，例如：

- `basically done`
- `likely passed`
- `almost complete`
- `should be fine`

---

## 7. 对话输出规则

TRAE 完成任务后，对话中只输出：

1. 状态；
2. 完整报告路径；
3. commit SHA；
4. push 结果；
5. 仍存在的 blocker；
6. 是否需要用户决策。

不得再把几百行完整报告直接输出到对话。
不得只给 `file:///workspace` 链接。
必须提供仓库相对路径。

---

## 8. 必须包含的字段

每份报告必须使用 `reports/templates/TASK-REPORT-TEMPLATE.md` 模板，包含固定 15 个章节：

0. Report Metadata
1. User Request
2. Scope（Included / Excluded）
3. Starting State
4. Actions Performed
5. Files Changed
6. Behavior Before and After
7. Validation
8. Git Operations
9. Deployment Status
10. Database and Migration
11. Risks and Known Gaps
12. Blockers and User Decisions
13. Next Recommended Action
14. Final Summary

每次报告必须包含 Base SHA、End SHA、检查结果、Git、部署、数据库和 Known Gaps。

未提交、未 push 的报告不能描述为远程可读取。

---

## 9. 历史报告迁移

历史报告（如 `sync/outbox/*.md`）迁移到 `reports/archive/YYYY/MM/`，规则：

1. 不改写原始报告内容；
2. 可以在文件顶部增加 `Legacy Report Metadata`；
3. 状态、原路径、迁移日期必须保留；
4. 在 `INDEX.md` 建立索引；
5. 不把历史报告设置为 `LATEST`；
6. 迁移后清理空的 `sync/outbox/`；
7. `sync/README.md` 中删除"使用 outbox 保存长期报告"的描述；
8. 不删除 sync 中其他中转材料。

---

## 10. AI 读取入口

用户要求"查看最新 TRAE 报告"时，AI 优先读取：

```
reports/LATEST.md
```

再读取其中 `Path` 字段指向的完整报告。
