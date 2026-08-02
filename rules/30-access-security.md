# 30 访问与安全

> 来源：AGENTS.md 第 8 节与产品安全合同

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

## 受保护 Owner 账户

`8752028@qq.com` 为项目 Owner 账户，任何环境中禁止修改或删除其 email、password_hash、status、角色、权限和订阅，除非用户在当前任务中明确指定字段并授权。清理测试数据前必须先排除此邮箱。

## 生产秘密边界

- 部署 SSH Key 必须专用，仅限部署目标服务器；
- SSH Key 的权限必须限制在部署所需最小范围；
- 部署链路不读取数据库秘密；
- GitHub Actions secrets 不写入镜像环境变量；
- 生产数据库秘密只在服务器配置目录中存在，不进入 Git、日志或测试夹具。
