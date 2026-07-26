# 40 测试和质量

## 默认原则

按风险运行足够测试，不机械每次全量，也不允许无证据跳过。

## 常用门禁

```text
knowledge/docs checker
architecture checker
test allowlist
ruff
mypy
pytest
tsc
eslint
frontend build
contract tests
E2E
```

## 数据库测试

集成测试必须 PostgreSQL + 真实 Alembic，禁止 SQLite 冒充。

## 自动部署前

dev push 至少应完成快速检查：

- 文档/知识结构；
- 架构门禁；
- 后端受影响静态检查；
- 前端类型和 build；
- 变更分类。

大规模 pytest/Playwright 可在开发任务中先完成，不要求每次部署重复全部运行。

## 真实验收

以下必须在 CN/腾讯云：

- Docker、Nginx；
- 真实 DB 数据；
- Worker；
- after-close；
-飞书；
- Capture；
- 资源状态。

无测试数据写 BLOCKED，不写 PASS。
