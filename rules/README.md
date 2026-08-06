# 盘迹规则体系

`AGENTS.md` 是任务入口和最高安全边界，`rules/` 保存项目详细强制规则。
两者冲突时，以 `AGENTS.md` 第 8 节为最高边界，其余以更具体的规则为准。

## 通用执行主体合同

所有 IDE、编码助手和自动化 Agent 遵守同一套仓库规则。治理按实际操作定义，
不按 IDE、Agent、模型或客户端区分。规则中不得新增按工具命名的角色、能力矩阵、
模式切换表或工具专属状态值。

## 权威文件

| 文件 | 主题 |
|---|---|
| `00-core-governance.md` | 事实源优先级、修改闭环和最小报告 |
| `10-product-domain-invariants.md` | 产品、策略、DSA、自选、监控和飞书边界 |
| `20-market-data-indicators.md` | 行情、指标、快照、板块和因子合同 |
| `30-access-security.md` | Token、权限、Owner 账户和生产秘密 |
| `40-testing-quality.md` | 测试隔离、质量门禁、CI 和结论纪律 |
| `50-git-development-flow.md` | 分支、提交、推送和执行纪律 |
| `80-deployment-data-safety.md` | Migration、部署、生产访问和数据安全 |
| `81-remote-deployment-only.md` | 部署位置唯一性：本地仅开发/验证/控制，所有部署均在远程服务器执行 |
| `90-deprecated-forbidden.md` | 已废止入口和禁止恢复项 |

上述规则均为当前有效规则。未来方案、阶段迁移表和已经删除的流程不在有效规则中保留；
重要历史由 `docs/changes/` 和 Git 历史记录。

## 事实源

- `docs/prd/`：已确认需求和目标行为；
- `docs/maps/`：已核验当前实现和项目记忆；
- `docs/changes/`：重要演化及原因；
- `docs/runbooks/`：当前可重复操作步骤；
- 代码、数据、日志和运行状态：当前执行事实。

`sync/` 不是正式真源，不得成为运行时依赖。`docs/current/` 若在历史版本中出现，
仅视为 legacy，不得新增或修改。

## 维护约束

- 只有用户在当前任务明确要求调整治理体系时，才允许修改 `AGENTS.md`、`rules/`、治理检查器或治理测试；
- 规则只保留稳定、可执行的当前约束，不写当前 SHA、阶段进度或未来设计；
- PRD、Maps、Runbooks 分别需要用户在当前任务主动发起或明确确认，笼统文档授权和代码任务不得隐式覆盖；
- 需求变化由用户先发起 PRD 更新；候选实现只更新代码、测试、Migration、配置和对应 Change；
- 用户验收候选实现并明确授权后，才把已核验入口和运行事实写入 Maps，把真实走通的步骤写入 Runbooks；
- 普通 Bug 由 Git 历史记录，只有重要行为、契约或运行方式变化才新增一个 Change；
- 不新增未经用户确认的治理目录或重复权威文件。
