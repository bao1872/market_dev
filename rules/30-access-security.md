# 30 访问与安全

> 来源：AGENTS.md §七.7、§六.7、§六.10
> 状态：并行验证

## Capture Token 隔离

Capture Token 只能访问 Capture API。

- 不能访问普通用户 API；
- 不能污染普通 Access Token。

## 权限隔离

修改权限必须检查用户隔离。

- active / expired / no-sub / admin 角色必须显式区分；
- 到期用户保留历史数据，但不能读取、修改、监控或产生新投递（与产品域不变量一致）。

## 生产环境账户密码

未经用户明确授权，禁止修改生产环境账户密码。

## 生产秘密边界（PLANNED）

> 提议中，尚未在 `AGENTS.md` 确立。

- 部署 SSH Key 必须专用，仅限部署目标服务器；
- SSH Key 必须配合 forced command 限制；
- 部署链路不读取数据库秘密；
- GitHub Actions secrets 不写入镜像环境变量；
- 生产数据库秘密只在服务器配置目录中存在，不进入 Git。

> Phase 1 注：本节为未来自动部署阶段提议，当前未实现。
