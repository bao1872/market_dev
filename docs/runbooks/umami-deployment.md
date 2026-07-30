# Umami 访客分析部署 Runbook

> [CHANGE-20260729-009] 替代 GoAccess，作为生产环境访客分析服务。
> 本地开发**不得**使用 Docker；仅在生产环境部署。

## 架构

```
nginx (frontend 容器)
  ├─ access.log /var/log/nginx/access.log combined 格式（保留 + logrotate 每 15 分钟轮转）
  ├─ /umami/ 反向代理到 umami:3000（剥离 /umami/ 前缀）
  └─ sub_filter 在 index.html 响应中动态注入：
       <script async src="/umami/script.js" data-website-id="${UMAMI_WEBSITE_ID}"></script>
       （Live Mount 模式下 dist 只读，用 sed 替换 nginx.conf 占位符）
umami 容器 (docker.umami.is/umami-software/umami:3.2)
  ├─ 复用 trading-postgres 容器
  ├─ 独立 umami 数据库和用户（DATABASE_URL=postgresql://umami:***@trading-postgres:5432/umami）
  ├─ 强随机 APP_SECRET
  └─ umami_data volume 持久化 /app/data
```

## 安全设计

1. **独立数据库**：`umami` 数据库和用户隔离于业务数据库 `bz_stock`，无权限互访
2. **强随机 APP_SECRET**：64 字符十六进制随机字符串
3. **Nginx 受控代理**：只代理 `/umami/` 前缀路径，不暴露 Umami 容器到公网
4. **Tracking script 仅 production 注入**：通过 `UMAMI_WEBSITE_ID` 环境变量控制，开发/capture 模式不注入
5. **Nginx access.log 保留**：标准 COMBINED 格式 + logrotate 轮转（不依赖 GoAccess）
6. **数据库密码不出现在仓库**：通过 `/etc/market-dev/umami.env` 注入，文件权限 600

## 部署步骤

### 1. 准备 Postgres 数据库和用户

```bash
ssh panji-prod

# 创建 umami 数据库和用户
docker exec -i trading-postgres psql -U postgres <<'EOF'
CREATE USER umami WITH PASSWORD 'STRONG_RANDOM_PASSWORD';
CREATE DATABASE umami OWNER umami;
GRANT ALL PRIVILEGES ON DATABASE umami TO umami;
EOF

# 验证
docker exec trading-postgres psql -U umami -d umami -c "SELECT current_user, current_database();"
```

### 2. 创建配置文件

```bash
# 生成强随机 APP_SECRET
APP_SECRET=$(openssl rand -hex 32)
echo "Generated APP_SECRET: $APP_SECRET"

# 创建 /etc/market-dev/umami.env（权限 600）
cat > /etc/market-dev/umami.env <<EOF
DATABASE_URL=postgresql://umami:STRONG_RANDOM_PASSWORD@trading-postgres:5432/umami
APP_SECRET=$APP_SECRET
TZ=Asia/Shanghai
EOF
chmod 600 /etc/market-dev/umami.env
```

### 3. 确认 docker-compose.prod.yml 已包含 umami 服务

文件位置：`docker-compose.prod.yml`

关键配置：
- `umami` 服务：`image: docker.umami.is/umami-software/umami:3.2`
- `env_file: /etc/market-dev/umami.env`
- `depends_on: postgres (condition: service_healthy)`
- `volumes: umami_data:/app/data`
- 顶层 `volumes:` 声明 `umami_data`（name: `trading-umami-data`）

### 4. 确认 nginx.conf 已配置 /umami/ 反向代理 + sub_filter

文件位置：`frontend/nginx.conf`

关键配置：
- `location /umami/`：反向代理到 `umami:3000`，`rewrite ^/umami/(.*) /$1 break;`
- `location = /index.html`：`sub_filter` 在 `</head>` 前注入 `<script async src="/umami/script.js" data-website-id="${UMAMI_WEBSITE_ID}"></script>`

### 5. 确认 docker-entrypoint.sh 已适配 Live Mount

文件位置：`frontend/docker-entrypoint.sh`

关键逻辑：
- 用 `sed` 替换 `/etc/nginx/conf.d/default.conf` 中的 `${UMAMI_WEBSITE_ID}` 占位符
- `UMAMI_WEBSITE_ID` 为空时占位符替换为空字符串

### 6. 部署

```bash
# 在生产服务器上执行（不在本机）
ssh panji-prod
cd /root/web_dev
git pull

# 重建并启动 umami 服务（不影响其他服务）
docker compose -f docker-compose.prod.yml --env-file /etc/market-dev/market.env up -d umami

# 等待 Umami 完成首次 Prisma migration
sleep 30

# 重启 frontend 使 nginx sub_filter 和 /umami/ 代理生效
docker compose -f docker-compose.prod.yml --env-file /etc/market-dev/market.env restart frontend
```

### 7. 首次初始化（添加 Website）

1. 通过 SSH 隧道访问 Umami Web UI：`ssh -L 13000:localhost:13000 panji-prod`（临时）或通过 nginx 代理访问 `http://panji-prod/umami/`
2. 默认账号：`admin` / `umami-admin`（首次登录后**立即修改密码**）
3. 添加 Website：
   - Name: `panji-prod`
   - Domain: `panji-prod`
4. 复制生成的 `Website ID`（UUID 格式，例如 `109c6241-d39e-47b0-a6f2-29a6bc15bd09`）
5. 写入 `/etc/market-dev/market.env`：
   ```bash
   echo "UMAMI_WEBSITE_ID=109c6241-d39e-47b0-a6f2-29a6bc15bd09" >> /etc/market-dev/market.env
   ```
6. 重启 frontend 容器使 nginx 配置生效：
   ```bash
   docker compose -f docker-compose.prod.yml --env-file /etc/market-dev/market.env restart frontend
   ```

### 8. 验证

```bash
# 检查 umami 容器状态
docker ps | grep umami

# 检查 umami 容器日志（应显示 Prisma migration 完成 + 服务启动）
docker logs trading-umami --tail 50

# 检查 nginx 配置已注入 website_id
docker exec trading-frontend grep umami /etc/nginx/conf.d/default.conf
# 预期：data-website-id="109c6241-d39e-47b0-a6f2-29a6bc15bd09"

# 检查 index.html 响应中是否包含 tracking script
curl -s http://localhost/index.html | grep -o 'umami/script.js[^"]*'
# 预期：/umami/script.js" data-website-id="109c6241-d39e-47b0-a6f2-29a6bc15bd09"

# 模拟浏览器访问触发 pageview（注意：必须用浏览器 User-Agent，否则 Umami 会识别为 bot 不计入）
curl -s -X POST http://localhost/umami/api/send \
  -H 'Content-Type: application/json' \
  -H 'x-umami-website-id: 109c6241-d39e-47b0-a6f2-29a6bc15bd09' \
  -H 'x-umami-hostname: panji-prod' \
  -H 'User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36' \
  -d '{"type":"event","payload":{"website":"109c6241-d39e-47b0-a6f2-29a6bc15bd09","name":"page-view","url":"http://panji-prod/market","referrer":"","hostname":"panji-prod","language":"zh-CN","screen":"1920x1080"}}'
# 预期：返回 {"cache":"...","sessionId":"...","visitId":"..."} 表示成功

# 验证数据库中是否有 pageview 记录
docker exec trading-postgres psql -U umami -d umami -c \
  "SELECT w.website_id, w.name, COUNT(we.event_id) AS event_count, MAX(we.created_at) AS last_event FROM website w LEFT JOIN website_event we ON w.website_id=we.website_id GROUP BY w.website_id, w.name ORDER BY event_count DESC NULLS LAST LIMIT 5;"
# 预期：event_count >= 1
```

### 9. 前端验证

1. 浏览器访问 `http://panji-prod/market`，登录后浏览页面
2. 在 Umami Web UI（`http://panji-prod/umami/`）查看实时访客和 pageview 数据
3. 确认 pageview 数量随浏览器访问增加

## 故障排查

### Umami 容器无法启动

```bash
# 检查 umami 容器日志
docker logs trading-umami --tail 100

# 常见原因：
# 1. DATABASE_URL 错误 → 检查 /etc/market-dev/umami.env
# 2. Postgres 用户/数据库未创建 → 重新执行步骤 1
# 3. Prisma migration 失败 → 删除 umami 数据库后重建：
#    docker exec trading-postgres psql -U postgres -c "DROP DATABASE umami; DROP USER umami;"
#    重新执行步骤 1 和 6
```

### Tracking script 未注入到 index.html

```bash
# 检查 nginx 配置中的占位符是否已替换
docker exec trading-frontend grep -o 'data-website-id=[^"]*"[^"]*"' /etc/nginx/conf.d/default.conf

# 检查 UMAMI_WEBSITE_ID 是否设置
docker exec trading-frontend env | grep UMAMI
# 应输出：UMAMI_WEBSITE_ID=109c6241-d39e-47b0-a6f2-29a6bc15bd09

# 检查 index.html 响应
curl -s http://localhost/index.html | grep umami
# 预期：<script async src="/umami/script.js" data-website-id="109c6241-d39e-47b0-a6f2-29a6bc15bd09"></script>
```

### Pageview 数量不增加

```bash
# 1. 检查请求是否到达 Umami API
docker logs trading-umami --tail 20 | grep -i 'api/send'

# 2. 检查请求是否被识别为 bot（默认排除 bot User-Agent）
# 必须用浏览器 User-Agent 测试，不能用 curl 默认 UA

# 3. 检查数据库中是否有记录
docker exec trading-postgres psql -U umami -d umami -c \
  "SELECT COUNT(*) FROM website_event WHERE website_id = '109c6241-d39e-47b0-a6f2-29a6bc15bd09';"

# 4. 检查 Website 配置中 domain 是否匹配
docker exec trading-postgres psql -U umami -d umami -c \
  "SELECT website_id, name, domain FROM website;"
```

### Nginx /umami/ 代理 502

```bash
# 1. 检查 umami 容器是否在运行
docker ps | grep umami

# 2. 检查 umami 是否监听 3000 端口
docker exec trading-umami netstat -tlnp 2>/dev/null | grep 3000 || \
docker exec trading-umami sh -c 'ss -tlnp 2>/dev/null | grep 3000 || netstat -tlnp 2>/dev/null | grep 3000'

# 3. 检查 docker 网络是否连通
docker exec trading-frontend ping -c 3 umami 2>&1 | head -5
```

## 回滚

```bash
# 停止 umami 服务（不影响其他服务）
docker compose -f docker-compose.prod.yml --env-file /etc/market-dev/market.env stop umami

# 如需完全移除
docker compose -f docker-compose.prod.yml --env-file /etc/market-dev/market.env rm -f umami
docker volume rm trading-umami-data

# 可选：删除 Postgres 中的 umami 数据库和用户
docker exec trading-postgres psql -U postgres -c "DROP DATABASE umami; DROP USER umami;"

# 移除 market.env 中的 UMAMI_WEBSITE_ID
sed -i '/^UMAMI_WEBSITE_ID=/d' /etc/market-dev/market.env

# 重启 frontend 使 nginx 配置生效（sub_filter 占位符为空，不注入 script）
docker compose -f docker-compose.prod.yml --env-file /etc/market-dev/market.env restart frontend
```

回滚后：
- 前端不再上报 pageview
- nginx access.log 仍正常记录（保留）
- 其他功能不受影响

## 相关文件

| 文件 | 职责 |
|---|---|
| `docker-compose.prod.yml` | umami 服务定义、umami_data 卷声明 |
| `frontend/nginx.conf` | `/umami/` 反向代理、`sub_filter` 注入 tracking script |
| `frontend/docker-entrypoint.sh` | `sed` 替换 `${UMAMI_WEBSITE_ID}` 占位符适配 Live Mount |
| `scripts/deploy_live_runtime.sh` | 容器启动列表包含 `umami`（替代 `goaccess`） |
| `/etc/market-dev/umami.env` | Umami DATABASE_URL + APP_SECRET + TZ（服务器侧，权限 600） |
| `/etc/market-dev/market.env` | `UMAMI_WEBSITE_ID` 注入到 frontend 容器 |

## 与 GoAccess 的差异

| 项 | GoAccess（已废弃） | Umami（当前） |
|---|---|---|
| 数据源 | nginx access.log 文件（COMBINED 格式） | 浏览器上报的 pageview event |
| 部署方式 | 解析日志文件生成 JSON 报告 | 独立 Web 服务，Prisma + Postgres 存储 |
| IP 匿名化 | `--anonymize-ip` 保留前 3 段 | Umami 内置匿名化 |
| 实时性 | 周期性生成报告 | 实时上报 |
| 访问方式 | `/admin/visitors` API 读取 JSON | `/umami/` Web UI + API |
| 失败原因 | nginx access.log 软链到 /dev/stdout，GoAccess 容器读不到文件 | 不依赖文件，直接接收 HTTP 上报 |

> `docs/runbooks/goaccess-deployment.md` 保留为历史记录，不再作为部署依据。`/admin/visitors` API 仍存在但返回 `data_source="empty"`（待后续清理或废弃）。
