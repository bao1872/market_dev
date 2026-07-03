# Docs v2 迁移映射

> 目的：把旧 `docs/current/00-18` 的事实合并到 v2 结构，降低重复。

## 1. 旧文件到新文件映射

| 旧文件 | 新文件 |
|---|---|
| `00-project-overview.md` | `current/00-product-business.md` + `current/01-system-architecture.md` + `maps/backend-module-map.md` |
| `01-product-requirements.md` | `current/00-product-business.md` + `current/04-frontend-ux.md` |
| `02-domain-glossary.md` | `current/MANIFEST.md` 附录 + 各 current 文件内定义 |
| `03-business-rules.md` | `current/00-product-business.md` |
| `04-business-workflows.md` | `current/01-system-architecture.md` + `maps/notification-flow-map.md` + `maps/worker-job-map.md` |
| `05-system-architecture.md` | `current/01-system-architecture.md` + `maps/deployment-runtime-map.md` |
| `06-frontend-design.md` | `current/04-frontend-ux.md` + `maps/frontend-route-map.md` |
| `07-backend-design.md` | `current/01-system-architecture.md` + `maps/backend-module-map.md` |
| `08-data-model.md` | `current/02-data-api-contracts.md` + `maps/database-model-map.md` |
| `09-api-contracts.md` | `current/02-data-api-contracts.md` + `maps/api-route-map.md` |
| `10-permissions-security.md` | `current/02-data-api-contracts.md` |
| `11-jobs-integrations.md` | `current/03-jobs-integrations-operations.md` + `maps/worker-job-map.md` + `maps/notification-flow-map.md` |
| `12-strategy-indicator-contracts.md` | `current/00-product-business.md` + `current/02-data-api-contracts.md` |
| `13-configuration-parameters.md` | `current/MANIFEST.md` + `current/03-jobs-integrations-operations.md` |
| `14-deployment-operations.md` | `current/03-jobs-integrations-operations.md` + `maps/deployment-runtime-map.md` |
| `15-testing-acceptance.md` | `current/05-testing-acceptance.md` + `maps/test-coverage-map.md` |
| `16-ui-design-system.md` | `current/04-frontend-ux.md` |
| `17-open-decisions.md` | `current/open-decisions.md` |
| `18-code-doc-alignment.md` | `current/code-doc-alignment.md` |

## 2. 建议归档方式

落库 PR 中不要直接删除旧文档。建议移动到：

```text
docs/archive/current-v1-20260703/
```

保留旧文件名，便于追溯，但 README 明确它们不是当前事实源。

## 3. 落库时必须同步修改

```text
tools/check_docs_consistency.py
tools/tests/test_check_docs_consistency.py
docs/changes/CHANGELOG.md
docs/changes/records/CHANGE-20260703-016.md 或下一编号
```

## 4. 验收标准

- 新 docs 入口只指向 v2 current/maps；
- 旧 00-18 不再作为 current 事实源；
- docs consistency 通过；
- 所有本地链接有效；
- current/open-decisions 不含已决问题；
- current/code-doc-alignment 只保留仍未关闭差异；
- Trae 新对话按 `AI-ONBOARDING.md` 能完成 `RESTORE-CHECKLIST.md`。
