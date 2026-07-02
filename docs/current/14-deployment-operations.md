> 文档状态：CURRENT DESIGN BASELINE  
> 基线日期：2026-07-02  
> 已核对代码基线：`6f5ae2cec6b24dbd1b7bf6f23477f5e6f5096822`（`refactor/access-v2-platform-recovery`）  
> 事实来源：代码库 + 项目负责人截至 2026-07-02 已确认的产品与架构要求  
> 维护要求：任何代码、配置、测试、部署或文档修改都必须同步更新相关当前设计文档，并新增 CHANGE 记录。  
> 注意：该代码基线用于设计核对，不代表已经满足合并 `main` 或生产发布条件。
> 对齐口径：`CURRENT` 表示已确认设计，不等同于代码已完成；代码未实现、未验证或生产表现不一致的内容，必须在 `18-code-doc-alignment.md` 标为 `KNOWN_GAP`。

# 14 部署与运维设计

## 1. 生产拓扑

- PostgreSQL 16：Compose 服务 `postgres`，命名 volume 持久化，不公开宿主机 5432；
- Redis：Compose 服务；
- Backend：FastAPI 容器，宿主机 8000；
- Frontend：Nginx 容器，宿主机 80；
- Python Workers：共享同一 backend Git SHA 镜像；
- Capture Worker：专用浏览器镜像；
- 所有应用通过 Compose 服务名 `postgres`、`redis` 连接。

## 2. Git 和分支

- 禁止直接在 `main` 修改；
- 每项变更使用独立分支并在 CHANGE 中登记；
- 合并候选从最新 `origin/main` 创建，使用 PR 和 merge commit；
- 合并前在候选分支重新运行全部门禁；
- 生产只部署已经合入 `main` 的 Commit。

## 3. 构建版本

Backend、Python Workers、Frontend 和 Capture 必须可追踪 Git SHA、Build Time 和应用版本。生产不接受 `unknown` 或 `dev` 标签。

## 4. 部署顺序

1. 验证当前分支为 `main`、工作树干净、HEAD 等于 `origin/main`；
2. 备份数据库并验证磁盘；
3. 构建 backend、frontend、capture；
4. 启动 postgres/redis 并等待 healthy；
5. 运行 Alembic upgrade head；
6. 启动 backend/frontend 和业务需要的 Workers；
7. 验证版本、健康、心跳、任务、行情、发布和投递。

`CORE_ONLY=1` 仅用于受控恢复。需要趋势选股时必须运行 strategy_batch/scheduler；需要飞书图片时必须运行 capture/outbox/delivery；不能把部分服务启动解释为完整业务可用。

## 5. 健康和验收

至少检查：

- PostgreSQL/Redis health 和 volume；
- Backend health/readiness/version；
- Frontend 200；
- Alembic 唯一 head；
- 所有应运行 Worker 的 heartbeat 和 Git SHA；
- 最新行情 `as_of`；
- DSA 完整 published run；
- Monitor eligibility；
- Outbox、文字 Delivery、图片 Capture/Delivery；
- 到期会员 403 和续期恢复；
- 外部 URL、防火墙和安全组。

## 6. 回滚

保留上一个成功镜像和数据库恢复点。代码与数据库回滚分开评估；不修改已执行历史 migration；不可逆迁移优先前向修复；回滚后验证 Schema、Worker 和旧代码兼容。

## 7. Secret 与日志

`/etc/market-dev/market.env` 权限为 600。部署脚本不得回显完整连接串或飞书密钥。日志包含 service、git_sha、run_id、run_key、instrument、source_bar_time、error_code 和 request_id，但不包含 Secret。
