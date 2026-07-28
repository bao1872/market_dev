# GoAccess 访问统计部署 Runbook

> [Gate5] 生产环境部署 GoAccess + Nginx 日志解析 + /admin/visitors API。
> 本地开发**不得**使用 Docker；仅在生产环境部署。

## 架构

```
nginx (frontend 容器)
  └─ access.log (/var/log/nginx/access.log, COMBINED 格式)
       │ (共享卷 nginx_logs, 只读挂载)
       ▼
goaccess 容器 (allin1/goaccess:1.7.2)
  ├─ 解析 access.log (--log-format=COMBINED)
  ├─ IP 匿名化 (--anonymize-ip, 保留前 3 段末段为 0)
  ├─ 保留最近 30 天 (--keep-last=30)
  └─ 输出 JSON 报告 (/srv/goaccess/report.json)
       │ (共享卷 goaccess_reports, 只读挂载到 backend)
       ▼
backend 容器
  └─ GET /admin/visitors (读取 /srv/goaccess/report.json)
       │
       ▼
frontend (/admin/visitors 页面, admin only)
```

## 安全设计

1. **IP 匿名化**：GoAccess `--anonymize-ip` 保留 IPv4 前 3 段，末段为 0（如 `192.168.1.0`）
2. **敏感参数脱敏**：backend `_sanitize_path()` 在展示时将 `token/jwt/password/key/secret/api_key/access_token` 参数值替换为 `***`
3. **admin only**：`/admin/visitors` 端点通过 `require_roles("admin")` 鉴权
4. **只读挂载**：GoAccess 容器对 nginx_logs 卷只读挂载，不修改日志
5. **日志轮转**：Docker json-file 驱动，单容器上限 50m × 5 = 250MB

## 部署步骤

### 1. 确认 docker-compose.prod.yml 已包含 goaccess 服务

文件位置：`docker-compose.prod.yml`

关键配置：
- `frontend` 服务挂载 `nginx_logs:/var/log/nginx`
- `goaccess` 服务只读挂载 `nginx_logs:/var/log/nginx:ro`
- `goaccess` 服务挂载 `goaccess_reports:/srv/goaccess`
- `backend` 服务只读挂载 `goaccess_reports:/srv/goaccess:ro`
- 共享卷 `nginx_logs` 和 `goaccess_reports` 在 `volumes:` 顶层声明

### 2. 确认 nginx.conf 已启用 access_log

文件位置：`frontend/nginx.conf`

```nginx
server {
    listen 80;
    server_name _;

    # [Gate5] 访问日志：标准 COMBINED 格式
    access_log /var/log/nginx/access.log combined;
    error_log /var/log/nginx/error.log;
    ...
}
```

### 3. 部署

```bash
# 在生产服务器上执行（不在本机）
ssh panji-prod

# 拉取最新代码
cd /path/to/market_dev
git pull

# 重建并启动 goaccess 服务（不影响其他服务）
docker compose -f docker-compose.prod.yml --env-file /etc/market-dev/market.env up -d goaccess

# 重启 frontend 使 nginx.conf 生效（已含 access_log 配置）
docker compose -f docker-compose.prod.yml --env-file /etc/market-dev/market.env restart frontend

# 重启 backend 使 /admin/visitors 路由生效
docker compose -f docker-compose.prod.yml --env-file /etc/market-dev/market.env restart backend
```

### 4. 验证

```bash
# 检查 goaccess 容器状态
docker ps | grep goaccess

# 检查报告文件是否生成（等待 5 分钟首次生成）
docker exec trading-backend ls -la /srv/goaccess/
# 预期：report.json 存在

# 检查报告内容
docker exec trading-backend cat /srv/goaccess/report.json | head -50

# 测试 API（替换 <TOKEN> 为 admin 用户 token）
curl -H "Authorization: Bearer <TOKEN>" http://localhost/api/v1/admin/visitors
# 预期：返回 JSON，data_source="goaccess_json"
```

### 5. 前端验证

1. 访问 `https://your-domain/admin/visitors`
2. 确认页面显示三个时间窗口切换（今日/7日/30日）
3. 确认 PV/UV、热门页面、来源、状态码、设备/浏览器、时段趋势均有数据
4. 确认生成时间显示在页头右侧
5. 切换时间窗口验证数据切换

## 故障排查

### 报告文件不存在（data_source="empty"）

```bash
# 检查 goaccess 容器是否运行
docker ps | grep goaccess

# 检查 goaccess 容器日志
docker logs trading-goaccess --tail 50

# 检查 nginx access.log 是否存在
docker exec trading-frontend ls -la /var/log/nginx/
```

### JSON 解析失败（data_source="error"）

```bash
# 检查 report.json 内容是否合法
docker exec trading-backend cat /srv/goaccess/report.json | python3 -m json.tool

# 检查 goaccess 错误日志
docker exec trading-backend cat /srv/goaccess/goaccess-error.log
```

### API 返回 403

- 确认请求头包含 `Authorization: Bearer <TOKEN>`
- 确认 token 对应用户角色为 `admin`（`user_roles.role = 'admin'`）

## 本地开发

本地开发**不**启动 GoAccess 容器（用户硬约束：不 Docker）。

- `/admin/visitors` API 返回 `data_source="empty"` + 空数据
- 前端页面展示空态："GoAccess 报告未生成"
- 这是预期行为，不影响其他功能

如需本地测试有数据的场景，可手动创建 `/srv/goaccess/report.json` 文件：

```bash
# 仅本地测试用，不写入生产
mkdir -p /srv/goaccess
cat > /srv/goaccess/report.json <<'EOF'
{
  "data": {
    "visitors": {"total": 10, "data": [{"data": "192.168.1.0", "hits": 5, "percent": 50.0}]},
    "requests": {"total": 50, "data": [{"data": "/market", "hits": 30, "percent": 60.0}]}
  },
  "generated_at": "2026-07-28T10:00:00"
}
EOF
```

## 回滚

```bash
# 停止 goaccess 服务（不影响其他服务）
docker compose -f docker-compose.prod.yml --env-file /etc/market-dev/market.env stop goaccess

# 如需完全移除
docker compose -f docker-compose.prod.yml --env-file /etc/market-dev/market.env rm -f goaccess
docker volume rm trading-goaccess-reports trading-nginx-logs
```

回滚后：
- `/admin/visitors` API 返回 `data_source="empty"`
- 前端页面展示空态
- 其他功能不受影响

## 相关文件

| 文件 | 职责 |
|---|---|
| `docker-compose.prod.yml` | goaccess 服务定义、共享卷声明 |
| `frontend/nginx.conf` | access_log 配置（COMBINED 格式） |
| `backend/app/api/admin_visitors.py` | `/admin/visitors` API 端点 |
| `backend/app/schemas/visitors.py` | VisitorReport/VisitorSummary Schema |
| `frontend/src/pages/AdminVisitorsPage.tsx` | 访问统计页面 |
| `frontend/src/api/endpoints.ts` | VisitorReport 类型 + getAdminVisitors |
| `frontend/src/hooks/useApi.ts` | useAdminVisitors hook |
