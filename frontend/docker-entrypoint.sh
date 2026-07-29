#!/bin/sh
# [CHANGE-20260729-005 五.4] Frontend nginx 入口：启动 crond + nginx
# crond 运行 logrotate（每小时检查一次，由 logrotate.conf 的 daily/maxsize 双触发决定是否轮转）
set -e

# 启动 busybox crond（后台），负责定时执行 /etc/periodic/15min/logrotate-nginx
# crond 日志输出到 stderr，由 Docker json-file 收集
crond -b -l 8

# 执行 nginx 主进程（前台，保持容器存活）
exec nginx -g 'daemon off;'
