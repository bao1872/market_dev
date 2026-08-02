#!/usr/bin/env bash
# panji-deploy.sh — 盘迹开发部署唯一服务器端实现（Live Mount）
#
# 权威来源：
#   - rules/80-deployment-data-safety.md（部署与数据安全硬约束）
#   - docs/runbooks/development-deployment.md（操作步骤）
#   - docs/maps/80-system-runtime.md（已核验运行事实）
#
# 唯一运行模式：
#   docker-compose.prod.yml + docker-compose.live.yml（始终叠加）
#   镜像只提供运行环境；运行代码唯一来自 /opt/panji-live。
#   即使因依赖/Dockerfile 变化重建镜像，重建后仍以 prod+live 叠加启动。
#
# 职责（本脚本是服务器端唯一部署实现）：
#   fetch → checkout → 变更分类 → 环境镜像 build（仅必要时）→ sync →
#   migration（仅 migration_changed）→ restart → health → SHA 验证 → 状态记录 → 回滚
#
# 用法:
#   scripts/deploy/panji-deploy.sh <FULL_SHA> [--dry-run]
#
# 调用方：scripts/ops/panji-test-deploy（本地唯一用户入口，经 SSH 调用本脚本）
#
# 约束:
# - 必须在 panji-prod（43.136.118.82）上运行；
# - REPO_ROOT 默认 /root/web_dev；LIVE_ROOT 默认 /opt/panji-live；
# - 不 down -v，不删除 PostgreSQL/Redis Volume；
# - 不自动执行 bootstrap / Review run / pointer publish 等任何数据操作；
# - 依赖：git, docker compose, flock, rsync, curl, npm/node（前端构建）。

set -euo pipefail

REPO_ROOT="${PANJI_REPO_ROOT:-/root/web_dev}"
LIVE_ROOT="${PANJI_LIVE_ROOT:-/opt/panji-live}"
ENV_FILE="${PANJI_ENV_FILE:-/etc/market-dev/market.env}"
STATE_FILE="${PANJI_STATE_FILE:-/etc/market-dev/.panji-deploy-state}"
LOCK_FILE="${PANJI_LOCK_FILE:-/var/lock/panji-deploy.lock}"
MIN_DISK_GB="${PANJI_MIN_DISK_GB:-20}"
MAX_DISK_PCT="${PANJI_MAX_DISK_PCT:-82}"
MIN_MEM_MB="${PANJI_MIN_MEM_MB:-4096}"

# 唯一 Compose 组合：prod + live 始终叠加。禁止出现不叠加 live.yml 的变体。
COMPOSE_CMD="docker compose --env-file ${ENV_FILE} -f docker-compose.prod.yml -f docker-compose.live.yml"

# 所有复用 backend 代码的 Python 服务（Live Mount 挂载 /opt/panji-live/backend/app）
PYTHON_SERVICES=(
    backend
    worker-bars-scheduler
    worker-strategy-scheduler
    worker-calendar
    worker-monitor
    worker-strategy-batch
    worker-outbox
    worker-delivery
    worker-after-close
    worker-watchdog
    worker-capture
)

DRY_RUN=false
TARGET_SHA=""
PREVIOUS_SHA=""

# 变更分类标志（由 classify_changes 计算）
BACKEND_RUNTIME_CHANGED=false
FRONTEND_RUNTIME_CHANGED=false
MIGRATION_CHANGED=false
BACKEND_ENVIRONMENT_CHANGED=false
FRONTEND_ENVIRONMENT_CHANGED=false
CAPTURE_ENVIRONMENT_CHANGED=false

log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

fail() {
    log "错误: $*" >&2
    exit 1
}

usage() {
    echo "用法: $0 <FULL_SHA> [--dry-run]"
    echo "环境变量: PANJI_REPO_ROOT, PANJI_LIVE_ROOT, PANJI_ENV_FILE, PANJI_STATE_FILE, PANJI_LOCK_FILE"
    exit 1
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --dry-run)
                DRY_RUN=true
                shift
                ;;
            -h|--help)
                usage
                ;;
            -*)
                fail "未知选项: $1"
                ;;
            *)
                if [[ -z "${TARGET_SHA}" ]]; then
                    TARGET_SHA="$1"
                else
                    usage
                fi
                shift
                ;;
        esac
    done

    if [[ -z "${TARGET_SHA}" ]]; then
        usage
    fi
}

check_prerequisites() {
    log "检查前置条件..."

    [[ -d "${REPO_ROOT}" ]] || fail "仓库目录不存在: ${REPO_ROOT}"
    [[ -f "${ENV_FILE}" ]] || fail "环境文件不存在: ${ENV_FILE}"
    [[ -f "${REPO_ROOT}/docker-compose.prod.yml" ]] || fail "生产 Compose 文件不存在"
    [[ -f "${REPO_ROOT}/docker-compose.live.yml" ]] || fail "Live Mount Compose 文件不存在"

    command -v git >/dev/null 2>&1 || fail "缺少 git"
    command -v docker >/dev/null 2>&1 || fail "缺少 docker"
    command -v flock >/dev/null 2>&1 || fail "缺少 flock"
    command -v rsync >/dev/null 2>&1 || fail "缺少 rsync"
    command -v curl >/dev/null 2>&1 || fail "缺少 curl"

    if [[ -n "${PANJI_SSH_HOST:-}" ]]; then
        local resolved
        resolved="$(ssh -G "${PANJI_SSH_HOST}" 2>/dev/null | awk '/^hostname /{print $2; exit}')"
        [[ "${resolved}" == "43.136.118.82" ]] || fail "SSH Host '${PANJI_SSH_HOST}' 解析为 '${resolved}'，期望 43.136.118.82"
    fi
}

check_resource_budget() {
    log "检查资源预算（任何状态修改之前）..."

    [[ "${MIN_DISK_GB}" =~ ^[0-9]+$ && "${MIN_DISK_GB}" -ge 20 ]] \
        || fail "PANJI_MIN_DISK_GB 只能保持或提高 20 GB 下限"
    [[ "${MAX_DISK_PCT}" =~ ^[0-9]+$ && "${MAX_DISK_PCT}" -le 82 ]] \
        || fail "PANJI_MAX_DISK_PCT 只能保持或收紧 82% 上限"
    [[ "${MIN_MEM_MB}" =~ ^[0-9]+$ && "${MIN_MEM_MB}" -ge 4096 ]] \
        || fail "PANJI_MIN_MEM_MB 只能保持或提高 4096 MB 下限"

    local available_kb available_gb used_pct mem_kb mem_mb
    available_kb="$(df -Pk / | awk 'NR==2 {print $4}')"
    used_pct="$(df -Pk / | awk 'NR==2 {gsub(/%/, "", $5); print $5}')"
    if [[ -r /proc/meminfo ]]; then
        mem_kb="$(awk '/^MemAvailable:/ {print $2; exit}' /proc/meminfo)"
    elif command -v sysctl >/dev/null 2>&1; then
        mem_kb="$(( $(sysctl -n hw.memsize) / 1024 ))"
    else
        fail "无法读取 MemAvailable"
    fi
    [[ "${available_kb}" =~ ^[0-9]+$ && "${used_pct}" =~ ^[0-9]+$ && "${mem_kb}" =~ ^[0-9]+$ ]] \
        || fail "无法读取磁盘或内存预算"
    available_gb=$((available_kb / 1024 / 1024))
    mem_mb=$((mem_kb / 1024))

    [[ "${available_gb}" -ge "${MIN_DISK_GB}" ]] \
        || fail "根分区可用 ${available_gb} GB，低于 ${MIN_DISK_GB} GB"
    [[ "${used_pct}" -le "${MAX_DISK_PCT}" ]] \
        || fail "根分区使用率 ${used_pct}%，高于 ${MAX_DISK_PCT}%"
    [[ "${mem_mb}" -ge "${MIN_MEM_MB}" ]] \
        || fail "MemAvailable ${mem_mb} MB，低于 ${MIN_MEM_MB} MB"

    log "资源预算通过: disk_free=${available_gb}GB disk_used=${used_pct}% mem_available=${mem_mb}MB"
}

ensure_state_directory() {
    local state_dir
    state_dir="$(dirname "${STATE_FILE}")"
    if [[ "${DRY_RUN}" == "true" ]]; then
        log "[dry-run] 将确保 state 目录存在: ${state_dir}"
    elif [[ ! -d "${state_dir}" ]]; then
        mkdir -p "${state_dir}" || fail "无法创建 state 目录: ${state_dir}"
    fi
}

validate_sha() {
    log "验证 SHA: ${TARGET_SHA}"

    cd "${REPO_ROOT}"
    [[ "${TARGET_SHA}" =~ ^[0-9a-fA-F]{40}$ ]] || fail "必须提供 40 位完整 SHA"

    # dev 是唯一部署来源
    log "拉取 origin/dev 最新引用..."
    git fetch origin dev --no-tags 2>&1 | sed 's/^/  /' || fail "git fetch origin dev 失败"

    if ! git cat-file -e "${TARGET_SHA}^{commit}" 2>/dev/null; then
        fail "SHA 不存在或不是 commit: ${TARGET_SHA}"
    fi

    local full_sha
    full_sha="$(git rev-parse "${TARGET_SHA}^{commit}")"

    if ! git merge-base --is-ancestor "${full_sha}" origin/dev 2>/dev/null; then
        fail "SHA ${full_sha} 不是 origin/dev 的祖先，拒绝部署"
    fi

    TARGET_SHA="${full_sha}"
    log "SHA 验证通过（origin/dev 祖先，完整 SHA）: ${TARGET_SHA}"
}

check_working_tree() {
    log "检查工作区状态..."

    cd "${REPO_ROOT}"

    if [[ -n "$(git status --porcelain 2>/dev/null)" ]]; then
        fail "工作区不干净，拒绝部署。请手动处理未提交修改后再试。"
    fi
}

load_previous_state() {
    if [[ -f "${STATE_FILE}" ]]; then
        PREVIOUS_SHA="$(cat "${STATE_FILE}" 2>/dev/null || echo "")"
    else
        PREVIOUS_SHA=""
    fi
    log "上一次部署 SHA: ${PREVIOUS_SHA:-无（按首次 Live Mount 部署处理）}"
}

save_state() {
    local sha="$1"
    if [[ "${DRY_RUN}" == "false" ]]; then
        echo "${sha}" > "${STATE_FILE}"
        log "状态已记录: ${STATE_FILE} = ${sha}"
    else
        log "[dry-run] 将记录状态: ${STATE_FILE} = ${sha}"
    fi
}

# 变更分类：使用「上一部署 SHA → 目标 SHA」的差异，禁止使用 HEAD~1
# （一次部署可能跨多个 commit，HEAD~1 会漏判）。
classify_changes() {
    log "分类变更范围（${PREVIOUS_SHA:0:7}..${TARGET_SHA:0:7}）..."

    cd "${REPO_ROOT}"

    if [[ -z "${PREVIOUS_SHA}" ]]; then
        log "无上一次部署记录：按首次 Live Mount 部署处理（同步 backend + frontend）"
        BACKEND_RUNTIME_CHANGED=true
        FRONTEND_RUNTIME_CHANGED=true
        MIGRATION_CHANGED=true
        return
    fi

    if ! git cat-file -e "${PREVIOUS_SHA}^{commit}" 2>/dev/null; then
        log "上一部署 SHA ${PREVIOUS_SHA:0:7} 在本地不可解析：按首次 Live Mount 部署处理"
        BACKEND_RUNTIME_CHANGED=true
        FRONTEND_RUNTIME_CHANGED=true
        MIGRATION_CHANGED=true
        return
    fi

    local changed_files
    changed_files="$(git diff --name-only "${PREVIOUS_SHA}" "${TARGET_SHA}" 2>/dev/null || true)"

    if [[ -z "${changed_files}" ]]; then
        log "两次 SHA 之间无文件变化（仍将执行最终 SHA 与 Mount 核验）"
        return
    fi

    # backend 运行代码（Live Mount 同步范围）
    if echo "${changed_files}" | grep -qE '^backend/(app/|alembic/|alembic\.ini)'; then
        BACKEND_RUNTIME_CHANGED=true
    fi

    # frontend 运行代码（需要 build dist）
    if echo "${changed_files}" | grep -qE '^frontend/(src/|public/|index\.html|vite\.config|tsconfig)'; then
        FRONTEND_RUNTIME_CHANGED=true
    fi

    # migration
    if echo "${changed_files}" | grep -qE '^backend/alembic/versions/'; then
        MIGRATION_CHANGED=true
    fi

    # backend 运行环境（依赖 / Dockerfile / 系统依赖 → 需要 build backend 镜像）
    if echo "${changed_files}" | grep -qE '^backend/(Dockerfile|pyproject\.toml|pyproject\.lock|poetry\.lock|requirements.*\.txt)$'; then
        BACKEND_ENVIRONMENT_CHANGED=true
    fi

    # frontend 运行环境（依赖 / Dockerfile / Nginx / entrypoint → 需要 build frontend 镜像）
    if echo "${changed_files}" | grep -qE '^frontend/(Dockerfile|package\.json|package-lock\.json|nginx\.conf|docker-entrypoint\.sh|logrotate-nginx)'; then
        FRONTEND_ENVIRONMENT_CHANGED=true
    fi

    # capture 运行环境（浏览器环境 → 需要 build capture 镜像）
    if echo "${changed_files}" | grep -qE '^backend/Dockerfile\.capture$'; then
        CAPTURE_ENVIRONMENT_CHANGED=true
    fi

    log "  backend_runtime_changed=${BACKEND_RUNTIME_CHANGED}"
    log "  frontend_runtime_changed=${FRONTEND_RUNTIME_CHANGED}"
    log "  migration_changed=${MIGRATION_CHANGED}"
    log "  backend_environment_changed=${BACKEND_ENVIRONMENT_CHANGED}"
    log "  frontend_environment_changed=${FRONTEND_ENVIRONMENT_CHANGED}"
    log "  capture_environment_changed=${CAPTURE_ENVIRONMENT_CHANGED}"
}

run_cmd() {
    if [[ "${DRY_RUN}" == "true" ]]; then
        log "[dry-run] 将执行: $*"
    else
        log "执行: $*"
        "$@"
    fi
}

checkout_target() {
    log "检出目标 SHA..."
    cd "${REPO_ROOT}"
    run_cmd git fetch origin dev --no-tags
    run_cmd git checkout -f "${TARGET_SHA}"
    log "已检出: ${TARGET_SHA}"
}

# 仅构建确实受影响的镜像。镜像只提供运行环境，
# 构建完成后仍以 prod+live 叠加启动，运行代码仍来自 /opt/panji-live。
build_environment_images() {
    local images=()
    [[ "${BACKEND_ENVIRONMENT_CHANGED}" == "true" ]] && images+=(backend)
    [[ "${FRONTEND_ENVIRONMENT_CHANGED}" == "true" ]] && images+=(frontend)
    [[ "${CAPTURE_ENVIRONMENT_CHANGED}" == "true" ]] && images+=(worker-capture)

    if [[ ${#images[@]} -eq 0 ]]; then
        log "无运行环境变化，跳过镜像构建（普通代码变化不 build）"
        return 0
    fi

    log "运行环境变化，构建受影响镜像: ${images[*]}"
    cd "${REPO_ROOT}"
    run_cmd ${COMPOSE_CMD} build "${images[@]}"
}

build_frontend_dist() {
    log "构建前端 dist..."
    if [[ "${DRY_RUN}" == "true" ]]; then
        log "[dry-run] 在 ${REPO_ROOT}/frontend 执行 vite build（不构建 Docker 镜像）"
        return 0
    fi

    cd "${REPO_ROOT}/frontend"
    if [[ "${FRONTEND_ENVIRONMENT_CHANGED}" == "true" ]]; then
        log "前端依赖或构建环境变化，先执行 npm ci"
        npm ci
    fi
    if [[ -x "./node_modules/.bin/vite" ]]; then
        NODE_OPTIONS=--max-old-space-size=1024 ./node_modules/.bin/vite build
    else
        log "WARN: ./node_modules/.bin/vite 不存在，回退到 npm run build"
        NODE_OPTIONS=--max-old-space-size=1024 npm run build
    fi
    cd "${REPO_ROOT}"
}

sync_backend_runtime() {
    log "同步 backend 运行代码到 ${LIVE_ROOT}..."

    if [[ "${DRY_RUN}" == "true" ]]; then
        log "[dry-run] rsync backend/app, backend/alembic, backend/alembic.ini → ${LIVE_ROOT}/backend/"
        return 0
    fi

    mkdir -p "${LIVE_ROOT}/backend"

    rsync -a --delete \
        --exclude='__pycache__' \
        --exclude='*.pyc' \
        --exclude='.pytest_cache' \
        --exclude='.mypy_cache' \
        --exclude='.ruff_cache' \
        "${REPO_ROOT}/backend/app/" "${LIVE_ROOT}/backend/app/"

    rsync -a --delete \
        --exclude='__pycache__' \
        --exclude='*.pyc' \
        "${REPO_ROOT}/backend/alembic/" "${LIVE_ROOT}/backend/alembic/"

    rsync -a "${REPO_ROOT}/backend/alembic.ini" "${LIVE_ROOT}/backend/alembic.ini"

    log "backend 运行代码同步完成"
}

sync_frontend_runtime() {
    log "同步 frontend dist 到 ${LIVE_ROOT}..."

    if [[ "${DRY_RUN}" == "true" ]]; then
        log "[dry-run] rsync frontend/dist → ${LIVE_ROOT}/frontend/dist"
        return 0
    fi

    [[ -d "${REPO_ROOT}/frontend/dist" ]] || fail "frontend/dist 不存在，前端构建可能失败"

    mkdir -p "${LIVE_ROOT}/frontend"
    rsync -a --delete \
        --exclude='.gitkeep' \
        "${REPO_ROOT}/frontend/dist/" "${LIVE_ROOT}/frontend/dist/"
    # capture 静态目录是 frontend nginx 的嵌套挂载点，必须存在
    mkdir -p "${LIVE_ROOT}/frontend/dist/static/captures"

    log "frontend dist 同步完成"
}

write_runtime_sha() {
    log "写入 RUNTIME_SHA=${TARGET_SHA}..."

    if [[ "${DRY_RUN}" == "true" ]]; then
        log "[dry-run] 写入 ${LIVE_ROOT}/RUNTIME_SHA = ${TARGET_SHA}（完整 SHA）"
        return 0
    fi

    mkdir -p "${LIVE_ROOT}"
    local tmp
    tmp="$(mktemp)"
    printf '%s' "${TARGET_SHA}" > "${tmp}"
    rsync -a "${tmp}" "${LIVE_ROOT}/RUNTIME_SHA"
    rm -f "${tmp}"

    log "RUNTIME_SHA 已写入 ${LIVE_ROOT}/RUNTIME_SHA"
}

# 更新 market.env：BUILD_TIME 与 DEPLOYMENT_MODE=live。
# 唯一运行模式为 live，故 DEPLOYMENT_MODE 恒为 live。
# GIT_SHA 用于 docker-compose.prod.yml 的 image tag，仅在构建镜像时更新，
# 否则保持不变（避免 Compose 引用不存在的镜像 tag）。
update_env_file() {
    local update_git_sha="${1:-false}"
    log "原子更新 ${ENV_FILE}（DEPLOYMENT_MODE=live, update_git_sha=${update_git_sha}）..."

    local short_sha="${TARGET_SHA:0:7}"
    local build_time
    build_time="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

    if [[ "${DRY_RUN}" == "true" ]]; then
        if [[ "${update_git_sha}" == "true" ]]; then
            log "[dry-run] 将更新 ${ENV_FILE}: GIT_SHA=${short_sha}, BUILD_TIME=${build_time}, DEPLOYMENT_MODE=live"
        else
            log "[dry-run] 将更新 ${ENV_FILE}: BUILD_TIME=${build_time}, DEPLOYMENT_MODE=live（GIT_SHA 保持不变）"
        fi
        return 0
    fi

    local orig_owner orig_group orig_mode
    orig_owner="$(stat -c '%u' "${ENV_FILE}" 2>/dev/null || echo "0")"
    orig_group="$(stat -c '%g' "${ENV_FILE}" 2>/dev/null || echo "0")"
    orig_mode="$(stat -c '%a' "${ENV_FILE}" 2>/dev/null || echo "600")"

    local tmp_file
    tmp_file="$(mktemp "${ENV_FILE}.XXXXXX")" || fail "无法创建临时文件"
    cp "${ENV_FILE}" "${tmp_file}"

    if [[ "${update_git_sha}" == "true" ]]; then
        awk -v sha="${short_sha}" -v bt="${build_time}" '
            /^GIT_SHA=/ { print "GIT_SHA=" sha; next }
            /^BUILD_TIME=/ { print "BUILD_TIME=" bt; next }
            /^DEPLOYMENT_MODE=/ { print "DEPLOYMENT_MODE=live"; next }
            { print }
        ' "${tmp_file}" > "${tmp_file}.new" && mv "${tmp_file}.new" "${tmp_file}"
        if ! grep -q "^GIT_SHA=" "${tmp_file}"; then
            echo "GIT_SHA=${short_sha}" >> "${tmp_file}"
        fi
    else
        awk -v bt="${build_time}" '
            /^BUILD_TIME=/ { print "BUILD_TIME=" bt; next }
            /^DEPLOYMENT_MODE=/ { print "DEPLOYMENT_MODE=live"; next }
            { print }
        ' "${tmp_file}" > "${tmp_file}.new" && mv "${tmp_file}.new" "${tmp_file}"
    fi

    if ! grep -q "^BUILD_TIME=" "${tmp_file}"; then
        echo "BUILD_TIME=${build_time}" >> "${tmp_file}"
    fi
    if ! grep -q "^DEPLOYMENT_MODE=" "${tmp_file}"; then
        echo "DEPLOYMENT_MODE=live" >> "${tmp_file}"
    fi

    chmod "${orig_mode}" "${tmp_file}" 2>/dev/null || true
    chown "${orig_owner}:${orig_group}" "${tmp_file}" 2>/dev/null || true
    mv "${tmp_file}" "${ENV_FILE}" || fail "无法原子替换 ${ENV_FILE}"

    local verified_dm
    verified_dm="$(grep "^DEPLOYMENT_MODE=" "${ENV_FILE}" | cut -d= -f2)"
    [[ "${verified_dm}" == "live" ]] || fail "market.env DEPLOYMENT_MODE 验证失败: 期望 live, 实际 ${verified_dm}"

    log "已原子更新 ${ENV_FILE}: DEPLOYMENT_MODE=${verified_dm}"
}

compose_config_check() {
    log "校验 Compose 配置（prod + live 叠加）..."
    cd "${REPO_ROOT}"
    run_cmd ${COMPOSE_CMD} config --quiet
    log "Compose 配置校验通过"
}

# migration 仅在 migration_changed 时执行；失败时调用方不得重启应用服务。
run_migration() {
    log "执行 alembic upgrade head（使用目标 SHA 的 Live Mount 代码）..."
    cd "${REPO_ROOT}"
    run_cmd ${COMPOSE_CMD} run --rm --no-deps --no-build backend bash -c "cd /app && alembic upgrade head"
    log "migration 完成"
}

restart_services() {
    local services=("$@")
    if [[ ${#services[@]} -eq 0 ]]; then
        log "无需重启任何服务"
        return 0
    fi
    log "重启服务: ${services[*]}"
    cd "${REPO_ROOT}"
    run_cmd ${COMPOSE_CMD} up -d --force-recreate --no-build "${services[@]}"
}

deploy() {
    # 1. 运行环境镜像（仅受影响的）
    if [[ "${BACKEND_ENVIRONMENT_CHANGED}" == "true" \
        || "${FRONTEND_ENVIRONMENT_CHANGED}" == "true" \
        || "${CAPTURE_ENVIRONMENT_CHANGED}" == "true" ]]; then
        update_env_file true
        build_environment_images
    else
        update_env_file false
    fi

    # 2. 前端 dist（运行代码或运行环境变化都需要重新产出 dist）
    local need_frontend=false
    if [[ "${FRONTEND_RUNTIME_CHANGED}" == "true" || "${FRONTEND_ENVIRONMENT_CHANGED}" == "true" ]]; then
        need_frontend=true
        build_frontend_dist
        sync_frontend_runtime
    fi

    # 3. backend 运行代码
    local need_backend=false
    if [[ "${BACKEND_RUNTIME_CHANGED}" == "true" \
        || "${BACKEND_ENVIRONMENT_CHANGED}" == "true" \
        || "${CAPTURE_ENVIRONMENT_CHANGED}" == "true" \
        || "${MIGRATION_CHANGED}" == "true" ]]; then
        need_backend=true
        sync_backend_runtime
    fi

    # 4. RUNTIME_SHA 始终写入（是 runtime_git_sha 的唯一来源）
    write_runtime_sha

    compose_config_check

    # 5. migration（失败即返回，调用方不重启服务）
    if [[ "${MIGRATION_CHANGED}" == "true" ]]; then
        run_migration || return 1
    else
        log "migration_changed=false，跳过 alembic upgrade"
    fi

    # 6. 重启：Python 服务与 frontend 分别判定；postgres/redis/umami 永不重启
    local restart_list=()
    if [[ "${need_backend}" == "true" ]]; then
        restart_list+=("${PYTHON_SERVICES[@]}")
    fi
    if [[ "${need_frontend}" == "true" ]]; then
        restart_list+=(frontend)
    fi

    if [[ ${#restart_list[@]} -eq 0 ]]; then
        log "无运行代码变化，不重启任何服务（仅刷新 RUNTIME_SHA 与核验）"
    else
        restart_services "${restart_list[@]}"
    fi
}

# 部署成功判据（全部基于完整 SHA，不接受短 SHA）：
#   repo HEAD = RUNTIME_SHA = version.runtime_git_sha = 目标完整 SHA
#   deployment_mode = live
#   受影响容器 Mounts 包含 /opt/panji-live
verify_deployment() {
    if [[ "${DRY_RUN}" == "true" ]]; then
        log "[dry-run] 计划核验:"
        log "[dry-run]   repo HEAD = ${TARGET_SHA}"
        log "[dry-run]   ${LIVE_ROOT}/RUNTIME_SHA = ${TARGET_SHA}"
        log "[dry-run]   /v1/version runtime_git_sha = ${TARGET_SHA}（完整 SHA）"
        log "[dry-run]   /v1/version deployment_mode = live"
        log "[dry-run]   /v1/health, /v1/health/ready 返回 200"
        log "[dry-run]   backend 与 Python 容器 Mounts 包含 ${LIVE_ROOT}"
        return 0
    fi

    log "核验部署结果..."

    # 1. repo HEAD
    cd "${REPO_ROOT}"
    local repo_head
    repo_head="$(git rev-parse HEAD)"
    if [[ "${repo_head}" != "${TARGET_SHA}" ]]; then
        log "repo HEAD 不匹配: 期望 ${TARGET_SHA}, 实际 ${repo_head}"
        return 1
    fi
    log "repo HEAD 一致: ${repo_head}"

    # 2. RUNTIME_SHA 文件
    local file_sha
    file_sha="$(cat "${LIVE_ROOT}/RUNTIME_SHA" 2>/dev/null || echo "")"
    if [[ "${file_sha}" != "${TARGET_SHA}" ]]; then
        log "RUNTIME_SHA 不匹配: 期望 ${TARGET_SHA}, 实际 ${file_sha:-空}"
        return 1
    fi
    log "RUNTIME_SHA 一致: ${file_sha}"

    # 3. health
    local max_wait=60
    local waited=0
    while [[ ${waited} -lt ${max_wait} ]]; do
        if curl -sf http://127.0.0.1:8000/v1/health >/dev/null 2>&1; then
            break
        fi
        log "等待 backend /v1/health... (${waited}/${max_wait})"
        sleep 2
        waited=$((waited + 2))
    done
    if [[ ${waited} -ge ${max_wait} ]]; then
        log "/v1/health 未通过（超时 ${max_wait}s）"
        return 1
    fi
    log "/v1/health 通过"

    # 4. ready
    waited=0
    while [[ ${waited} -lt ${max_wait} ]]; do
        if curl -sf http://127.0.0.1:8000/v1/health/ready >/dev/null 2>&1; then
            break
        fi
        log "等待 backend /v1/health/ready... (${waited}/${max_wait})"
        sleep 2
        waited=$((waited + 2))
    done
    if [[ ${waited} -ge ${max_wait} ]]; then
        log "/v1/health/ready 未通过（超时 ${max_wait}s）"
        return 1
    fi
    log "/v1/health/ready 通过"

    # 5. version：完整 runtime_git_sha + deployment_mode=live
    local version_json runtime_sha deployment_mode
    version_json="$(curl -sf http://127.0.0.1:8000/v1/version 2>/dev/null || echo "")"
    [[ -n "${version_json}" ]] || { log "/v1/version 不可达"; return 1; }

    runtime_sha="$(echo "${version_json}" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("runtime_git_sha",""))' 2>/dev/null || echo "")"
    deployment_mode="$(echo "${version_json}" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("deployment_mode",""))' 2>/dev/null || echo "")"

    if [[ "${runtime_sha}" != "${TARGET_SHA}" ]]; then
        log "version.runtime_git_sha 不匹配（要求完整 SHA）: 期望 ${TARGET_SHA}, 实际 ${runtime_sha:-空}"
        return 1
    fi
    log "version.runtime_git_sha 一致: ${runtime_sha}"

    if [[ "${deployment_mode}" != "live" ]]; then
        log "deployment_mode 不是 live: 实际 ${deployment_mode:-空}"
        return 1
    fi
    log "deployment_mode = live"

    # 6. Mounts 包含 /opt/panji-live
    local backend_mounts
    backend_mounts="$(docker inspect trading-backend --format '{{range .Mounts}}{{.Source}} {{end}}' 2>/dev/null || echo "")"
    if [[ "${backend_mounts}" != *"${LIVE_ROOT}"* ]]; then
        log "backend 容器 Mounts 不含 ${LIVE_ROOT}（未实际运行 Live Mount）"
        return 1
    fi
    log "backend 容器 Mounts 包含 ${LIVE_ROOT}"

    if [[ "${FRONTEND_RUNTIME_CHANGED}" == "true" || "${FRONTEND_ENVIRONMENT_CHANGED}" == "true" ]]; then
        local frontend_mounts
        frontend_mounts="$(docker inspect trading-frontend --format '{{range .Mounts}}{{.Source}} {{end}}' 2>/dev/null || echo "")"
        if [[ "${frontend_mounts}" != *"${LIVE_ROOT}/frontend/dist"* ]]; then
            log "frontend 容器 Mounts 不含 ${LIVE_ROOT}/frontend/dist"
            return 1
        fi
        log "frontend 容器 Mounts 包含 ${LIVE_ROOT}/frontend/dist"
    fi

    # 7. 关键容器与 Scheduler 单实例
    local required=(trading-backend trading-frontend trading-redis trading-postgres)
    for c in "${required[@]}"; do
        if ! docker ps --format '{{.Names}}' | grep -qx "${c}"; then
            log "关键容器未运行: ${c}"
            return 1
        fi
    done
    log "关键容器检查通过"

    local scheduler_names=(trading-worker-bars-scheduler trading-worker-strategy-scheduler trading-worker-calendar)
    for s in "${scheduler_names[@]}"; do
        local count
        count="$(docker ps --format '{{.Names}}' | grep -cx "${s}" || true)"
        if [[ "${count}" -ne 1 ]]; then
            log "Scheduler ${s} 实例数量异常: ${count}（期望 1）"
            return 1
        fi
    done
    log "Scheduler 单实例检查通过"

    return 0
}

rollback() {
    log "!!! 部署失败，执行回滚 !!!"

    if [[ -z "${PREVIOUS_SHA}" ]]; then
        log "无 previous SHA 记录，无法自动回滚代码。请手动处理。"
        return 1
    fi

    log "回滚到 previous SHA: ${PREVIOUS_SHA}"

    cd "${REPO_ROOT}"
    run_cmd git checkout -f "${PREVIOUS_SHA}"

    local saved_target_sha="${TARGET_SHA}"
    TARGET_SHA="${PREVIOUS_SHA}"
    if [[ "${BACKEND_ENVIRONMENT_CHANGED}" == "true" \
        || "${FRONTEND_ENVIRONMENT_CHANGED}" == "true" \
        || "${CAPTURE_ENVIRONMENT_CHANGED}" == "true" ]]; then
        update_env_file true
    else
        update_env_file false
    fi
    sync_backend_runtime
    if [[ "${FRONTEND_RUNTIME_CHANGED}" == "true" || "${FRONTEND_ENVIRONMENT_CHANGED}" == "true" ]]; then
        build_frontend_dist
        sync_frontend_runtime
    fi
    write_runtime_sha
    TARGET_SHA="${saved_target_sha}"

    cd "${REPO_ROOT}"
    run_cmd ${COMPOSE_CMD} up -d --force-recreate --no-build \
        "${PYTHON_SERVICES[@]}" frontend

    log "回滚完成（已恢复到 ${PREVIOUS_SHA}）"
}

cleanup_resources() {
    log "执行部署后受控清理..."
    run_cmd docker builder prune -f
    run_cmd docker image prune -f
    run_cmd docker container prune -f
}

main() {
    parse_args "$@"
    check_prerequisites
    check_resource_budget

    (
        flock -n 200 || fail "另一个部署正在进行中"

        ensure_state_directory
        validate_sha
        check_working_tree
        load_previous_state
        classify_changes

        checkout_target

        if ! deploy; then
            rollback
            fail "部署失败并已回滚（migration 失败时不会重启应用服务）"
        fi

        if ! verify_deployment; then
            rollback
            fail "部署核验失败并已回滚"
        fi

        cleanup_resources

        save_state "${TARGET_SHA}"

        log "部署成功: ${TARGET_SHA}"
        log "  deployment_mode=live"
        log "  repo HEAD = RUNTIME_SHA = version.runtime_git_sha = ${TARGET_SHA}"

    ) 200>"${LOCK_FILE}"
}

main "$@"
