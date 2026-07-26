# 30 权限与安全

## CURRENT

当前 main 的普通会员资格仍以 active user、member role、active subscription 时间窗口为核心。

Capability V2 在完成、合并和真实验证前只能标记 WIP。

## 后端

- 后端是权限真源；
- user_id 来自 JWT；
- 用户隔离必须测试；
- Capture Token 仅 Capture API；
- 管理员以 `is_admin` 为真源。

## 秘密

禁止提交或输出：

```text
DATABASE_URL
REDIS_URL
JWT_SECRET
SECRET_MASTER_KEY
WENCAI_COOKIE
飞书密钥
部署 SSH 私钥
生产 API Key
```

## GitHub 自动部署密钥

部署 SSH Key 必须：

- 专用；
- 不与个人 SSH 共用；
- 使用 forced command；
- 禁止 PTY、端口转发、agent 转发和 X11；
- 只能触发固定部署入口；
- 不能读取数据库秘密。
