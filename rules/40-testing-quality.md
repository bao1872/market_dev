# 40 测试与质量

> 来源：AGENTS.md §五、§七.20、§八、§六.6、§六.8、§七.8（测试部分）
> 状态：并行验证

## CHANGE 规则

每次修改必须新增 `docs/changes/records/CHANGE-YYYYMMDD-NNN.md` 并更新 `docs/changes/CHANGELOG.md`。

CHANGE 必填字段：

- 变更编号；
- 任务名称；
- 需求出处；
- 修改前/后行为；
- 影响模块；
- 修改文件；
- 文档更新；
- 测试证据；
- Git 分支；
- Git Commit；
- 数据库迁移；
- 配置变化；
- 风险；
- 遗留问题。

不存在"小改不用 CHANGE"。`tools/check_docs_consistency.py` 规则 12 强制校验 CHANGE 引用可达性。

## 文档目录与 CI 门禁

`tools/check_docs_consistency.py` 必须通过。

规则包括：

- MANIFEST 存在且含实现核对基线（40 位 SHA 且为 HEAD 祖先）；
- baseline 必须在 HEAD 的最近 50 个 commit 内；
- `docs/current/*.md` 与 `docs/maps/*.md` 存在；
- 本地 Markdown 链接有效；
- 无"待填写"占位符；
- `feishu_webhook` 不得回退为当前方案；
- open-decisions 不得把 Webhook vs Platform App 写回 OPEN；
- CHANGE 引用必须可达；
- ref/ 隔离文本扫描。

CI 必须失败若代码 SHA 变化后未同步 current/contracts/CHANGE/MANIFEST baseline。

## 质量门禁

```
Ruff   新增/修改 Python 文件零错误；历史债务由 tools/quality_baselines/ruff.json 管控
Mypy   新增 backend/app Python 生产文件零错误；历史债务由 tools/quality_baselines/mypy.json 管控
Docs   python tools/check_docs_consistency.py
Arch   python tools/check_architecture.py
Allow  python tools/check_test_allowlist.py
Sync   python tools/update_docs.py --check
```

禁止通过全局 ignore、批量 noqa、扩大 exclude、批量 `type: ignore` 或关闭检查掩盖新增问题。

前端：

- `tsc --noEmit`；
- `npm run lint`；
- `npm run build`；
- `npm run test:contract`；
- `npm run test:e2e`。

## 测试纪律

- 删除测试以适配错误实现：禁止；
- 修改 API 不检查前端调用：禁止；
- 修改数据模型不检查 migration：禁止；
- 修改 Worker 不检查幂等、心跳、重试：禁止；
- 把 Mock E2E 说成真实生产 E2E：禁止；
- 把 OPEN 问题写成最终结论：禁止；
- 把临时实验写成永久规则：禁止。

## ref/ 隔离测试

`ref/` 目录下所有文件仅供人工阅读参考，**禁止作为运行依赖**。

- 生产代码、测试、工具、构建脚本在运行时不得 `import` / `open` / `read` / `glob` `ref/` 目录下任何文件；
- SMC Pine parity 测试只读取 `backend/tests/fixtures/smc_pine/*.csv`；
- 禁止从 DB 重新取 bar 或依赖 `ref/` 导出脚本；
- `AGENTS.md` / `docs/current/*.md` / `docs/maps/*.md` 不得把 `ref/` 文件称为"真源"、"合同"、"fixture 生成器"或"运行依赖"；应称为"参考源（人工阅读）"；
- 算法真源必须是生产代码（如 `smc_pine_core`、`node_cluster_engine`、`indicator_contract`、`indicator_semantics`）。

## Migration 测试纪律

修改 migration 必须有 upgrade / downgrade / upgrade 验证。详见 `80-deployment-data-safety.md`。
