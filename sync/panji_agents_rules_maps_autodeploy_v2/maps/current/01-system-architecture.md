# 系统架构

```text
Browser
→ Nginx / React
→ FastAPI
→ PostgreSQL / Redis

Workers
→ 行情
→ 日历
→ DSA
→ 盘后
→ 监控
→ Outbox
→ Delivery
→ Capture
→ Watchdog
```

## SSOT

- PostgreSQL：正式业务状态；
- Redis：缓存、锁、短期协调；
- MDAS：行情；
- Canonical Service：指标四链；
- GitHub：应用代码；
- `/version` + `RUNTIME_SHA`：当前部署版本。

## 部署架构

```text
/root/web_dev
    CN 开发测试

GitHub dev
    push trigger

/opt/panji-deploy
    干净 checkout

/opt/panji-live
    当前运行
```
