# 30 访问、安全与真实数据保护

本文件永远生效，Exploration 不降低安全门槛。

## 1. Capture Token

Capture Token 只能访问 Capture API。

不得：

- 访问普通用户 API；
- 冒充普通 Access Token；
- 扩展为管理员通用凭据。

## 2. 用户与权限隔离

任何权限相关修改必须检查至少：

- active；
- expired；
- no-subscription；
- admin / owner；
- 普通用户之间的数据隔离。

不得因为测试方便绕过用户隔离。

## 3. 受保护 Owner

受保护 Owner 账户的 email、password_hash、status、角色、权限和订阅不得被自动修改或删除。

清理测试数据必须显式排除 Owner。

只有用户在当前任务明确指定字段并授权时，才允许修改受保护 Owner。

## 4. 秘密

禁止：

- 把数据库密码、JWT、SSH 私钥、第三方 token 提交到 Git；
- 把真实密码写入日志、截图、命令输出或测试 fixture；
- 用环境变量 dump / debug endpoint 暴露秘密；
- 在镜像中固化生产秘密。

服务器秘密只存在正式配置位置。

## 5. 真实业务数据库

`bz_stock` 是共享真实业务数据。

### Read

只读调查可以在明确任务范围内进行，但必须：

- 先确认连接身份；
- 明确 SQL 是只读；
- 不以只读结果冒充新代码已经验证。

### Write

任何对 `bz_stock` 的：

- Migration；
- INSERT / UPDATE / DELETE；
- publish / pointer switch；
- backfill；
- producer 重跑；
- 数据修复；

都属于真实业务写入，需要用户当前任务明确授权或已批准的 plan-scoped authorization。

## 6. 不可恢复数据

禁止：

- 删除唯一业务数据副本；
- 删除 PostgreSQL / Redis 持久 Volume；
- 清空共享 Redis；
- 用模糊资源名批量清理；
- 为通过测试或磁盘门槛删除业务历史数据。

## 7. 测试数据不得冒充真实数据

Synthetic / Mock / Fixture 可以证明代码合同，但不得被描述为真实市场业务结果。

真实产品假设判断必须使用真实市场数据或明确授权的真实样本。

## 8. 安全优先升级

遇到以下任一情况，即使当前为 Exploration，也必须提高流程强度：

- destructive migration；
- 用户权限；
- 密码/token；
- 不可恢复写入；
- 大规模真实数据重写；
- 无明确回滚路径的 schema 变更。

具体升级路径见 `70-hardening-release.md` 与 `80-deployment-migration.md`。
