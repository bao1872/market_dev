# 手动部署

自动部署失败或需重跑：

```bash
sudo /usr/local/sbin/panji-deploy-gateway <40-sha>
```

必须：

- SHA 属于 dev；
- 获取部署锁；
- 使用 `/opt/panji-deploy`；
- 验证 runtime；
- 记录 evidence。
