#!/bin/sh
# [CHANGE-20260729-005 五.4] Frontend nginx 入口：启动 crond + nginx
# crond 运行 logrotate（每小时检查一次，由 logrotate.conf 的 daily/maxsize 双触发决定是否轮转）
set -e

# [GoAccess 修复 2026-07-30] 删除 nginx:alpine 默认软链 /var/log/nginx/access.log -> /dev/stdout
# 否则即使 nginx.conf 配置 access_log /var/log/nginx/access.log main; 实际仍写到 stdout，
# GoAccess 容器挂载 nginx_logs 卷读不到文件。
rm -f /var/log/nginx/access.log /var/log/nginx/error.log

# [CHANGE-20260729-009] Umami tracking script 注入（仅 production + UMAMI_WEBSITE_ID 非空）
# - 开发/capture 模式不注入（不统计）
# - 注入位置：index.html 的 </head> 前
# - script src=/umami/script.js 由 nginx 反向代理到 umami:3000
# - data-website-id 由 market.env 的 UMAMI_WEBSITE_ID 提供
INDEX_HTML=/usr/share/nginx/html/index.html
if [ -n "$UMAMI_WEBSITE_ID" ] && [ -f "$INDEX_HTML" ]; then
  if ! grep -q "umami/script.js" "$INDEX_HTML"; then
    # 用 sed 在 </head> 前插入 script tag（兼容 BusyBox sed）
    SCRIPT_TAG="<script async src=\"/umami/script.js\" data-website-id=\"$UMAMI_WEBSITE_ID\"></script>"
    sed "s|</head>|  $SCRIPT_TAG\n</head>|" "$INDEX_HTML" > "$INDEX_HTML.tmp" && mv "$INDEX_HTML.tmp" "$INDEX_HTML"
    echo "[entrypoint] Umami tracking script injected (website_id=$UMAMI_WEBSITE_ID)"
  else
    echo "[entrypoint] Umami tracking script already exists, skip injection"
  fi
else
  echo "[entrypoint] UMAMI_WEBSITE_ID empty or index.html missing, skip Umami injection"
fi

# 启动 busybox crond（后台），负责定时执行 /etc/periodic/15min/logrotate-nginx
# crond 日志输出到 stderr，由 Docker json-file 收集
crond -b -l 8

# 执行 nginx 主进程（前台，保持容器存活）
exec nginx -g 'daemon off;'
