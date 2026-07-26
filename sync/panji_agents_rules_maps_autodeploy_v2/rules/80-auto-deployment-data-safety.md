# 80 自动部署与数据安全

## 默认

```text
push dev → GitHub Actions → 受限 SSH → 固定脚本 → 自动部署
```

## 自动允许

- docs/rules/maps：不重启；
- frontend source/public：frontend live；
- backend app：Python live；
- 前后端共同变化：联合 live；
- 普通测试文件：不单独部署。

## 自动阻塞

- Alembic migration；
- Dockerfile；
- pyproject/package lock；
- Compose；
- Nginx；
- 服务器环境变量合同；
- 未识别高风险文件。

阻塞后由 TRAE CN 评估使用 image/manual/migration 流程。

## 数据库

普通部署不得：

- 删除数据库；
- drop/truncate 核心行情；
- 删除 volume；
- 重建数据目录；
- 为解决 migration 清空重来。

## 备份

测试期不默认执行大体积 `pg_dump`。只有用户明确要求或高风险数据操作单独决定。

## Docker

禁止：

```text
docker compose down -v
docker image prune -a
并行全量构建
删除受保护基础镜像
```

## 失败

- 部署前记录 previous runtime SHA；
- 应用部署失败自动或手动回滚代码；
- migration 不自动回滚；
- 代码回滚不等于数据回滚。
