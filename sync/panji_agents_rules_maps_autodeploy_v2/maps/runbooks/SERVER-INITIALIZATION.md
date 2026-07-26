# 服务器一次性初始化

目标：

```text
/root/web_dev
/opt/panji-deploy
/opt/panji-live
/usr/local/lib/panji-deploy
/usr/local/sbin/panji-deploy-gateway
```

## 用户

创建专用 `panji-deploy`，不用于人工登录。

## SSH

authorized_keys 使用：

```text
restrict,command="/usr/local/sbin/panji-deploy-gateway" ssh-ed25519 AAAA...
```

如系统不支持 `restrict`，显式添加：

```text
no-agent-forwarding,no-port-forwarding,no-pty,no-user-rc,no-X11-forwarding
```

## 权限

deploy 用户只允许触发 gateway。gateway 自己验证 SHA 和分支。

实际 Docker/文件权限由服务器初始化时按当前环境最小化配置。
