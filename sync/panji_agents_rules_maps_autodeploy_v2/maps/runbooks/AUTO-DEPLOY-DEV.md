# dev Push 自动部署

## GitHub

```text
push dev
→ quick-ci
→ deploy
```

## Server

```text
forced SSH command
→ gateway $SHA
→ validate
→ flock
→ /opt/panji-deploy checkout
→ classify
→ live deploy
→ verify
```

## Deploy modes

```text
none
frontend_live
python_live
combined_live
blocked
```

## Blocked

```text
migration
dependencies
Dockerfile
Compose
Nginx
env contract
unknown
```

Blocked 不意味着失败，只表示由 CN 继续。
