#!/bin/sh
# [CHANGE-20260729-005 五.4] Frontend nginx 入口：启动 crond + nginx
# crond 运行 logrotate（每小时检查一次，由 logrotate.conf 的 daily/maxsize 双触发决定是否轮转）
set -e

# [GoAccess 修复 2026-07-30] 删除 nginx:alpine 默认软链 /var/log/nginx/access.log -> /dev/stdout
# 否则即使 nginx.conf 配置 access_log /var/log/nginx/access.log main; 实际仍写到 stdout，
# GoAccess 容器挂载 nginx_logs 卷读不到文件。
rm -f /var/log/nginx/access.log /var/log/nginx/error.log

# [CHANGE-20260729-009] Umami tracking script 注入（nginx sub_filter 模式）
# - 不修改 dist/index.html（Live Mount 模式下 dist 只读挂载，无法写入）
# - 用 sed 把 nginx.conf 中的 ${UMAMI_WEBSITE_ID} 占位符替换为实际值
# - 替换后写入 /etc/nginx/conf.d/default.conf（镜像内置文件可写）
# - UMAMI_WEBSITE_ID 为空时占位符替换为空字符串，sub_filter 仍注入但 data-website-id 为空
NGINX_CONF=/etc/nginx/conf.d/default.conf
if [ -f "$NGINX_CONF" ]; then
  # 转义 / 避免与 sed 分隔符冲突（website_id 是 UUID 含 -，不含 /，可直接替换）
  sed -i "s|\${UMAMI_WEBSITE_ID}|${UMAMI_WEBSITE_ID:-}|g" "$NGINX_CONF"
  if [ -n "$UMAMI_WEBSITE_ID" ]; then
    echo "[entrypoint] Umami tracking script enabled (website_id=$UMAMI_WEBSITE_ID)"
  else
    echo "[entrypoint] UMAMI_WEBSITE_ID empty, Umami script will use empty data-website-id"
  fi
else
  echo "[entrypoint] WARN: $NGINX_CONF not found, skip Umami injection"
fi

# 启动 busybox crond（后台），负责定时执行 /etc/periodic/15min/logrotate-nginx
# crond 日志输出到 stderr，由 Docker json-file 收集
crond -b -l 8

# 执行 nginx 主进程（前台，保持容器存活）
exec nginx -g 'daemon off;'
