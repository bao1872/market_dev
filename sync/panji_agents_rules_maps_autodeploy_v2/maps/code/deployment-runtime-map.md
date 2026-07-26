# Deployment Runtime Map

## 代码目录

```text
/root/web_dev
/opt/panji-deploy
/opt/panji-live
```

## 自动链

```text
GitHub Actions
→ deploy SSH key
→ forced command
→ /usr/local/sbin/panji-deploy-gateway
→ /usr/local/lib/panji-deploy/panji-deploy-dev
```

## 现有项目入口

预计复用：

```text
scripts/sync_live_runtime.sh
scripts/deploy_live_runtime.sh
docker-compose.prod.yml
docker-compose.live.yml
```

实际名称和参数必须在实施时核对。

## 版本

```text
/opt/panji-live/RUNTIME_SHA
/api or /version
Docker labels/env
```
