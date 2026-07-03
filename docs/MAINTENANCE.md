# 文档维护规则

## 1. 文档职责

| 类型 | 文件 | 维护方式 |
|---|---|---|
| 全局基线 | `current/MANIFEST.md` | 人工维护；每次 docs 架构变化更新 |
| 产品/业务 | `current/00-product-business.md` | 人工维护；只写当前确认规则 |
| 架构 | `current/01-system-architecture.md` | 人工维护；只写稳定边界 |
| 数据/API | `current/02-data-api-contracts.md` | 人工维护 + 代码核对；不替代 OpenAPI |
| 任务/集成/运维 | `current/03-jobs-integrations-operations.md` | 人工维护 + 生产审计证据 |
| 前端/UX | `current/04-frontend-ux.md` | 人工维护 + 路由核对 |
| 测试 | `current/05-testing-acceptance.md` | 人工维护 + CI 核对 |
| 实现地图 | `maps/*.md` | 半自动维护；代码结构变更必须同步 |
| 历史 | `changes/records/*.md` | 每个变更一条，不回写 current 历史 |
| Alignment | `current/code-doc-alignment.md` | 只记录仍未闭合的差异 |

## 2. 基线规则

v2 不再要求每个 current 文件都重复 `实现核对基线`。唯一基线写在：

```text
current/MANIFEST.md
```

落库时必须同步修改：

```text
tools/check_docs_consistency.py
tools/tests/test_check_docs_consistency.py
```

新的 docs consistency 应检查：

1. `current/MANIFEST.md` 存在且有完整 40 位基线；
2. 基线是当前 HEAD 的祖先；
3. current/maps 关键文件存在；
4. current 文档不得把 `feishu_webhook` 写回当前方案；
5. open decisions 不得把 Webhook vs Platform App 写回未决；
6. 本地链接有效；
7. 无 `待填写`；
8. `MIGRATION-MAP.md` 覆盖旧 00-18 文档。

## 3. 什么时候更新哪些文件

### 产品规则变化

更新：

```text
current/00-product-business.md
current/open-decisions.md（如果关闭或新增未决）
current/code-doc-alignment.md（如果代码未同步）
changes/records/CHANGE-*.md
```

### API 或数据模型变化

更新：

```text
current/02-data-api-contracts.md
maps/api-route-map.md
maps/database-model-map.md
相关测试
CHANGE
```

### Worker/飞书/Capture 变化

更新：

```text
current/03-jobs-integrations-operations.md
maps/worker-job-map.md
maps/notification-flow-map.md
maps/deployment-runtime-map.md
CHANGE
```

### 前端页面变化

更新：

```text
current/04-frontend-ux.md
maps/frontend-route-map.md
frontend contract tests
CHANGE
```

### 纯代码移动

通常只更新：

```text
maps/*.md
CHANGE
```

不要重写产品规则。

## 4. 不要做的事

- 不要把 CHANGE 历史复制进 current；
- 不要让 Alignment 变成所有 TODO 的万能表；
- 不要在 6 个 current 文件里重复同一条规则；
- 不要每次修改都更新所有文件；
- 不要把旧 00-18 current 文档继续作为当前事实源。
