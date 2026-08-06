# CHANGE-20260806-006 — 远程验证尝试强制资源清理

日期：2026-08-06
类型：governance + resource-safety + quality-gate
状态：`implemented_local_pending_user_acceptance`

## 1. 目标

远程测试和调试无论成功或失败，都不得长期占用服务器内存、容器槽位和磁盘。诊断能力从“保留运行现场”改为“先导出轻量证据，再立即精确清理”。

## 2. 新合同

- pass、fail、cancelled、interrupted、timeout 全部进入正式 cleanup。
- 自动清理本次尝试创建的验证容器、network、临时文件、验证专用镜像和精确命名验证数据库。
- 清理前导出 SHA、revision、测试摘要、关键日志与资源快照。
- 永不删除 `bz_stock`、共享 Volume、稳定运行容器、基础/受保护镜像或来源不明资源。
- 禁止全局 prune、Volume prune、模糊数据库匹配和批量 drop。
- 清理后复检磁盘、内存、容器/network 和验证数据库残留。
- 清理失败标记 `blocked_cleanup`，禁止继续创建下一套验证资源。

## 3. 修改范围

- `AGENTS.md`
- `rules/40-testing-quality.md`
- `rules/50-git-development-flow.md`
- `rules/80-deployment-data-safety.md`
- `rules/90-deprecated-forbidden.md`
- `tools/check_governance_rules.py`
- `tools/tests/test_check_governance_rules.py`

本次不修改 PRD、Maps 或 Runbooks。

## 4. 后续实现要求

本 Change 只建立治理合同。验证脚本中的 trap/finally、证据导出、精确 cleanup 和资源复检需要后续代码任务实现；在实现并真实远程验证前不得声称自动清理已运行生效。
