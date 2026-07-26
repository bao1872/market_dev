# REPORT-YYYYMMDD-NNN — 任务标题

> 复制本模板到 `reports/current/` 并按规则命名：`REPORT-YYYYMMDD-NNN-任务短名称.md`
> 详细规则见 `reports/README.md`。

---

## 0. Report Metadata

- Report ID:
- Status:
- Report Type:
- Environment:
- Created At:
- Branch:
- Upstream/Base:
- Base SHA:
- Implementation SHA:
- Report Published Through SHA:
- CHANGE:
- Related Task:
- Previous Report:
- Supersedes:

---

## 1. User Request

准确记录用户本次要求，不自行扩大范围。

---

## 2. Scope

### Included

### Excluded

---

## 3. Starting State

- 当前分支
- 当前 HEAD
- origin/dev
- 工作区状态
- 已知前置问题

---

## 4. Actions Performed

按执行顺序列出实际动作。

不得记录计划但未执行的动作。

---

## 5. Files Changed

| File | Action | Purpose |
|---|---|---|

必须列完整文件，不得只写目录名称。

---

## 6. Behavior Before and After

### Before

### After

如果只是文档治理，明确写"无运行时行为变化"。

---

## 7. Validation

| Command or Check | Result | Exit Code | Notes |
|---|---|---|---|

要求：

- 写实际执行命令；
- 写真实退出码；
- FAIL 不得描述为 PASS；
- 环境阻塞标记 `BLOCKED_ENVIRONMENT`；
- 预先存在问题必须给出证据，不能只声称"与本次无关"。

---

## 8. Git Operations

- Implementation Commit:
- Metadata Commit:
- Push Target:
- Push Result:
- origin/dev After Implementation Push:
- origin/dev After Metadata Push:
- Force Push Used: NO

---

## 9. Deployment Status

明确选择：

- NOT_REQUESTED
- NOT_ENABLED
- NOT_DEPLOYED
- DEPLOYED
- VERIFIED
- FAILED

不得因为 push 成功就声称腾讯云已经部署。

---

## 10. Database and Migration

- Database Accessed:
- Migration Created:
- Migration Executed:
- Backup Created:
- Volume Modified:

默认全部为 NO，除非真实执行。

---

## 11. Risks and Known Gaps

必须记录未解决问题。

---

## 12. Blockers and User Decisions

只记录真正需要用户决定的事项。

---

## 13. Next Recommended Action

只给一个明确的下一步，不写泛泛建议。

---

## 14. Final Summary

使用 5—10 行概括：

- 做了什么；
- 没做什么；
- 验证结果；
- commit；
- push；
- 下一步。
