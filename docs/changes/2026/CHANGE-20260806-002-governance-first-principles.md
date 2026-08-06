# CHANGE-20260806-002 治理体系第一性原理收敛

- 变更编号：CHANGE-20260806-002
- 任务名称：治理体系冲突审计与授权、验证、运行边界收敛
- 需求出处：用户明确要求审查并调整治理体系，同时要求只有用户明确指令时才能修改治理类文档
- 修改前行为：三平面运行模型与“只关心开发阶段、禁止描述其他阶段”互相冲突；本地 Runbook 同时禁止和要求连接 `bz_stock`；CI 非自动门禁被过度解释为相关失败也不影响部署；普通任务可以顺带修改治理文件
- 修改后行为：治理变更必须有用户当前任务明确授权；运行模型统一为本地开发、远程隔离验证、远程稳定运行；本地禁止业务库写入；高风险变更必须以同一 SHA 完成相应远程验证；CI 未运行不自动阻断，但相关失败证据必须处理
- 影响模块：AGENTS、核心治理、测试质量、部署安全、禁止项、系统运行 PRD/Map、开发 Runbook、治理检查器
- 修改文件：`AGENTS.md`、`rules/README.md`、`rules/00-core-governance.md`、`rules/40-testing-quality.md`、`rules/80-deployment-data-safety.md`、`rules/81-remote-deployment-only.md`、`rules/90-deprecated-forbidden.md`、`docs/prd/80-system-runtime.md`、`docs/maps/80-system-runtime.md`、`docs/runbooks/local-development.md`、`docs/runbooks/development-deployment.md`、治理检查器及测试
- 文档更新：删除 Runbook 中任务状态段；网络身份回归 Map + preflight；不新增治理目录或规则文件
- 测试证据：`tools/check_docs_consistency.py` 通过；`tools/check_governance_rules.py` 通过；治理纯单元 `31 passed`；`git diff --check` 通过
- Git 分支：`dev`
- Git Commit：未提交
- 数据库迁移：无
- 配置变化：无
- 风险：当前尚未提供经核验的本地只读数据库凭据，因此真实业务数据本地调试仍不可作为默认路径
- 遗留问题：远程验证与稳定运行仍共享物理主机，资源和故障域隔离按现有 DS-110/111 门禁治理
- 状态：`implemented_local`；`remote_verified=not_required_for_governance_only`；`deployed=false`
