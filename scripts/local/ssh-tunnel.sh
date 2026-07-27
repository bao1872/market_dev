#!/usr/bin/env bash
# 本地开发 SSH 隧道脚本：把远程共享 PostgreSQL / Redis 映射到本地端口。
#
# 用法:
#   scripts/local/ssh-tunnel.sh start   # 建立隧道
#   scripts/local/ssh-tunnel.sh status  # 检查隧道状态
#   scripts/local/ssh-tunnel.sh stop    # 停止隧道
#
# 约束:
# - 必须使用 ~/.ssh/config 中定义的 Host 别名 panji-prod，禁止命令行明文密码。
# - 使用默认 host key 校验（不得设置 StrictHostKeyChecking=no）。
# - 每次 start 只远程只读获取容器当前 IP，再建隧道。
# - 启动前校验 ssh -G 解析出的 HostName 必须是 43.136.118.82（盘迹腾讯云稳定服务器）。
# - 隧道 PID 写入 /tmp，不保存任何密钥。
# - 15432/16379 任一被占用即失败，避免误连其他服务。

set -euo pipefail

SSH_HOST="${PANJI_SSH_HOST:-panji-prod}"
EXPECTED_HOSTNAME="43.136.118.82"
POSTGRES_REMOTE_PORT=5432
REDIS_REMOTE_PORT=6379
POSTGRES_LOCAL_PORT=15432
REDIS_LOCAL_PORT=16379
PID_FILE="/tmp/panji-ssh-tunnel.pid"

log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

fail() {
    log "错误: $*" >&2
    exit 1
}

check_port_free() {
    local port="$1"
    if command -v lsof >/dev/null 2>&1; then
        if lsof -iTCP:"${port}" -sTCP:LISTEN >/dev/null 2>&1; then
            fail "本地端口 ${port} 已被占用"
        fi
    elif command -v netstat >/dev/null 2>&1; then
        if netstat -an 2>/dev/null | grep -E "127\.0\.0\.1[.:]${port}\s+.*LISTEN" >/dev/null 2>&1; then
            fail "本地端口 ${port} 已被占用"
        fi
    else
        if nc -z 127.0.0.1 "${port}" 2>/dev/null; then
            fail "本地端口 ${port} 已被占用"
        fi
    fi
}

get_container_ips() {
    # 远程只读命令：获取两个容器的当前 IP。
    # 注意：这里只读取 docker inspect，不修改任何远程状态。
    ssh "${SSH_HOST}" '
        set -e
        docker inspect -f "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}" trading-postgres 2>/dev/null || true
        docker inspect -f "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}" trading-redis 2>/dev/null || true
    '
}

verify_ssh_host() {
    # 校验 SSH Host 别名解析出的 HostName 必须是盘迹腾讯云稳定服务器。
    local resolved
    resolved="$(ssh -G "${SSH_HOST}" 2>/dev/null | awk '/^hostname /{print $2; exit}')"
    if [[ "${resolved}" != "${EXPECTED_HOSTNAME}" ]]; then
        fail "SSH Host '${SSH_HOST}' 解析为 '${resolved}'，期望 '${EXPECTED_HOSTNAME}'。请检查 ~/.ssh/config 中的 Host panji-prod 配置。"
    fi
    log "SSH Host 校验通过: ${SSH_HOST} -> ${resolved}"
}

start_tunnel() {
    log "使用 SSH Host 别名: ${SSH_HOST}"

    verify_ssh_host
    check_port_free "${POSTGRES_LOCAL_PORT}"
    check_port_free "${REDIS_LOCAL_PORT}"

    log "远程只读获取容器 IP ..."
    local ips=()
    while IFS= read -r line; do
        [[ -n "${line}" ]] && ips+=("${line}")
    done < <(get_container_ips)

    if [[ ${#ips[@]} -lt 2 ]]; then
        fail "无法从远程获取 trading-postgres / trading-redis 容器 IP"
    fi

    local pg_ip="${ips[0]}"
    local redis_ip="${ips[1]}"

    if [[ -z "${pg_ip}" ]]; then
        fail "trading-postgres 容器 IP 为空"
    fi
    if [[ -z "${redis_ip}" ]]; then
        fail "trading-redis 容器 IP 为空"
    fi

    log "PostgreSQL 容器 IP: ${pg_ip}"
    log "Redis 容器 IP: ${redis_ip}"

    # 如果已有 PID 文件，先尝试停止旧进程
    if [[ -f "${PID_FILE}" ]]; then
        local old_pid
        old_pid="$(cat "${PID_FILE}" 2>/dev/null || true)"
        if [[ -n "${old_pid}" ]] && kill -0 "${old_pid}" 2>/dev/null; then
            log "发现已有隧道进程 PID=${old_pid}，先停止"
            stop_tunnel
        else
            rm -f "${PID_FILE}"
        fi
    fi

    log "建立 SSH 隧道: 127.0.0.1:${POSTGRES_LOCAL_PORT} -> ${pg_ip}:${POSTGRES_REMOTE_PORT}"
    log "建立 SSH 隧道: 127.0.0.1:${REDIS_LOCAL_PORT} -> ${redis_ip}:${REDIS_REMOTE_PORT}"

    ssh -N -f \
        -L "127.0.0.1:${POSTGRES_LOCAL_PORT}:${pg_ip}:${POSTGRES_REMOTE_PORT}" \
        -L "127.0.0.1:${REDIS_LOCAL_PORT}:${redis_ip}:${REDIS_REMOTE_PORT}" \
        "${SSH_HOST}"

    # 获取最后一个后台 ssh 进程的 PID
    local ssh_pid
    ssh_pid="$!"
    if [[ -z "${ssh_pid}" ]] || ! kill -0 "${ssh_pid}" 2>/dev/null; then
        # 某些系统 ssh -f 后 $! 不是子进程，尝试通过 pgrep 找最近启动的到该 host 的 ssh
        ssh_pid="$(pgrep -n -f "ssh.*${SSH_HOST}" || true)"
    fi

    if [[ -z "${ssh_pid}" ]] || ! kill -0 "${ssh_pid}" 2>/dev/null; then
        fail "隧道进程启动失败，无法获取 PID"
    fi

    echo "${ssh_pid}" > "${PID_FILE}"
    log "隧道已启动，PID=${ssh_pid}，PID 文件=${PID_FILE}"

    sleep 1
    if ! status_tunnel >/dev/null 2>&1; then
        log "隧道进程存在，但端口尚未监听，等待中 ..."
        sleep 2
    fi

    status_tunnel
}

stop_tunnel() {
    if [[ -f "${PID_FILE}" ]]; then
        local pid
        pid="$(cat "${PID_FILE}" 2>/dev/null || true)"
        if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
            log "停止隧道进程 PID=${pid}"
            kill "${pid}" || true
            # 等待进程退出
            for _ in {1..10}; do
                if ! kill -0 "${pid}" 2>/dev/null; then
                    break
                fi
                sleep 0.5
            done
            if kill -0 "${pid}" 2>/dev/null; then
                log "普通终止失败，强制结束 PID=${pid}"
                kill -9 "${pid}" || true
            fi
        else
            log "PID 文件中的进程已不存在"
        fi
        rm -f "${PID_FILE}"
    else
        log "未找到 PID 文件，尝试搜索并停止到 ${SSH_HOST} 的 ssh 隧道进程"
        local pids
        pids="$(pgrep -f "ssh.*-L.*127\.0\.0\.1:${POSTGRES_LOCAL_PORT}.*${SSH_HOST}" || true)"
        if [[ -n "${pids}" ]]; then
            echo "${pids}" | while IFS= read -r p; do
                [[ -n "${p}" ]] || continue
                log "停止隧道进程 PID=${p}"
                kill "${p}" 2>/dev/null || true
            done
        else
            log "未找到运行中的隧道进程"
        fi
    fi
}

status_tunnel() {
    local pid=""
    if [[ -f "${PID_FILE}" ]]; then
        pid="$(cat "${PID_FILE}" 2>/dev/null || true)"
    fi

    local proc_ok=false
    if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
        proc_ok=true
    fi

    local pg_ok=false
    local redis_ok=false
    if command -v nc >/dev/null 2>&1; then
        nc -z 127.0.0.1 "${POSTGRES_LOCAL_PORT}" 2>/dev/null && pg_ok=true
        nc -z 127.0.0.1 "${REDIS_LOCAL_PORT}" 2>/dev/null && redis_ok=true
    elif command -v lsof >/dev/null 2>&1; then
        lsof -iTCP:"${POSTGRES_LOCAL_PORT}" -sTCP:LISTEN >/dev/null 2>&1 && pg_ok=true
        lsof -iTCP:"${REDIS_LOCAL_PORT}" -sTCP:LISTEN >/dev/null 2>&1 && redis_ok=true
    fi

    if [[ "${proc_ok}" == true && "${pg_ok}" == true && "${redis_ok}" == true ]]; then
        log "隧道运行中: PID=${pid}, PostgreSQL=127.0.0.1:${POSTGRES_LOCAL_PORT}, Redis=127.0.0.1:${REDIS_LOCAL_PORT}"
        return 0
    fi

    log "隧道未运行或端口未监听 (proc=${proc_ok}, pg=${pg_ok}, redis=${redis_ok})"
    return 1
}

main() {
    local cmd="${1:-}"
    case "${cmd}" in
        start)
            start_tunnel
            ;;
        stop)
            stop_tunnel
            ;;
        status)
            status_tunnel
            ;;
        *)
            echo "用法: $0 {start|stop|status}"
            echo "环境变量: PANJI_SSH_HOST（默认 panji-prod，取自 ~/.ssh/config）"
            exit 1
            ;;
    esac
}

main "$@"
