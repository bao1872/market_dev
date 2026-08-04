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
#   fetch → checkout → 上一 SHA 四级解析 → 首次 Live Mount 检测 → 变更分类 →
#   环境镜像 build（仅 environment_changed，且按同一 GIT_SHA tag 组整体构建）→ sync →
#   migration（仅 migration_changed，且早于任何重启）→ restart → health →
#   SHA 与 Mount 验证 → 状态记录 → 分级失败处理
#
# 构建策略：
#   普通代码变化零构建（Live Mount 直接生效）；
#   任意 environment_changed → backend/frontend/worker-capture 作为同一 GIT_SHA tag 组整体构建；
#   构建后仍以 prod+live 叠加启动，运行代码仍唯一来自 /opt/panji-live。
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

# [CHANGE-20260804 / DS-102] 强制 Compose 串行：禁止并行拉起多容器（瞬时内存峰值不可控）。
# 必须在每次 COMPOSE_CMD 执行前导出，覆盖任何并行默认值。
export COMPOSE_PARALLEL_LIMIT=1

# [CHANGE-20260804 / DS-103] 关键命令外层超时（秒），可在环境变量覆盖收紧。
TIMEOUT_NPM_CI_SECONDS="${PANJI_TIMEOUT_NPM_CI_SECONDS:-900}"
TIMEOUT_VITE_BUILD_SECONDS="${PANJI_TIMEOUT_VITE_BUILD_SECONDS:-900}"
TIMEOUT_DOCKER_BUILD_SECONDS="${PANJI_TIMEOUT_DOCKER_BUILD_SECONDS:-2400}"
TIMEOUT_COMPOSE_UP_SECONDS="${PANJI_TIMEOUT_COMPOSE_UP_SECONDS:-600}"
TIMEOUT_MIGRATION_SECONDS="${PANJI_TIMEOUT_MIGRATION_SECONDS:-600}"
TIMEOUT_HEALTH_WAIT_SECONDS="${PANJI_TIMEOUT_HEALTH_WAIT_SECONDS:-120}"
TIMEOUT_RSYNC_SECONDS="${PANJI_TIMEOUT_RSYNC_SECONDS:-600}"

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
PREVIOUS_SHA_SOURCE=""

# 外层自举前记录的完整 SHA（由 panji-test-deploy 经环境变量传入），
# 仅作为"上一真实运行 SHA"的最终 fallback。
BOOTSTRAP_PREVIOUS_SHA="${PANJI_BOOTSTRAP_PREVIOUS_SHA:-}"

# 变更分类标志（由 classify_changes 计算）
BACKEND_RUNTIME_CHANGED=false
FRONTEND_RUNTIME_CHANGED=false
MIGRATION_CHANGED=false
BACKEND_ENVIRONMENT_CHANGED=false
FRONTEND_ENVIRONMENT_CHANGED=false
CAPTURE_ENVIRONMENT_CHANGED=false

# 首次 Live Mount 部署：核心应用容器尚未挂载 LIVE_ROOT。
# 需要强制建立挂载，但**不得**因此把 migration_changed 设为 true。
FIRST_LIVE_DEPLOY=false

# 部署执行状态机（用于区分 migration 失败与重启后失败两类回滚路径）
SERVICES_RESTARTED=false
FAILURE_STAGE=""
MIGRATION_ATTEMPTED=false
MIGRATION_SUCCEEDED=false
IMAGES_BUILT=false

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

# 一个 SHA 只有同时满足「40 位十六进制」与「本地可解析为 commit」才可用作基线。
_is_resolvable_sha() {
    local sha="${1:-}"
    [[ "${sha}" =~ ^[0-9a-fA-F]{40}$ ]] || return 1
    git -C "${REPO_ROOT}" cat-file -e "${sha}^{commit}" 2>/dev/null
}

# 读取当前运行 backend 的 /v1/version 中的 SHA 字段。
# 优先顺序：runtime_git_sha → image_git_sha → git_sha。
# 任一字段若为 7 位短 SHA，尝试在仓库中唯一解析为完整 40 位 SHA。
_resolve_version_sha() {
    local version_json field val resolved
    version_json="$(curl -sf http://127.0.0.1:8000/v1/version 2>/dev/null || echo "")"
    [[ -n "${version_json}" ]] || return 0

    for field in runtime_git_sha image_git_sha git_sha; do
        val="$(echo "${version_json}" \
            | python3 -c "import sys,json; print(json.load(sys.stdin).get('${field}',''))" 2>/dev/null || echo "")"
        [[ -n "${val}" ]] || continue

        # 完整 40 位 SHA 直接采用
        if [[ "${val}" =~ ^[0-9a-fA-F]{40}$ ]]; then
            echo "${val}"
            return 0
        fi
        # 7 位短 SHA：在仓库中唯一解析
        if [[ "${val}" =~ ^[0-9a-fA-F]{7}$ ]]; then
            resolved="$(git -C "${REPO_ROOT}" rev-parse --quiet --verify "${val}^{commit}" 2>/dev/null || echo "")"
            if [[ -n "${resolved}" && "${resolved}" =~ ^[0-9a-fA-F]{40}$ ]]; then
                echo "${resolved}"
                return 0
            fi
            log "  version.${field}=${val} 在仓库中无法唯一解析，跳过"
        fi
    done
    return 0
}

# 读取当前 trading-backend 镜像 tag 中的 SHA。
# 项目镜像 tag 既可能是 40 位完整 SHA（如 ...:<GIT_SHA> 或 ...:sha-<40位>），
# 也可能是 7 位短 SHA（如 ...:<7位>）；两种都需解析为唯一完整 commit 才采用。
_resolve_image_tag_sha() {
    local tag candidate resolved
    tag="$(docker inspect trading-backend --format '{{.Config.Image}}' 2>/dev/null || echo "")"
    [[ -n "${tag}" ]] || return 0

    # 优先尝试 40 位完整 SHA：必须能在当前仓库解析为 commit。
    if [[ "${tag}" =~ ([0-9a-fA-F]{40}) ]]; then
        candidate="${BASH_REMATCH[1]}"
        if _is_resolvable_sha "${candidate}"; then
            echo "${candidate}"
            return 0
        fi
        log "  镜像 tag ${tag} 含 40 位 SHA ${candidate} 但仓库中不可解析，跳过"
    fi

    # 退而尝试 7 位短 SHA：必须通过 git 唯一解析为完整 40 位 commit。
    # 无法解析或存在歧义（多个匹配）时拒绝使用，不得猜测或直接采用 7 位值。
    if [[ "${tag}" =~ ([0-9a-fA-F]{7}) ]]; then
        candidate="${BASH_REMATCH[1]}"
        resolved="$(git -C "${REPO_ROOT}" rev-parse --quiet --verify "${candidate}^{commit}" 2>/dev/null || echo "")"
        if [[ -n "${resolved}" && "${resolved}" =~ ^[0-9a-fA-F]{40}$ ]]; then
            echo "${resolved}"
            return 0
        fi
        log "  镜像 tag ${tag} 的 7 位短 SHA ${candidate} 无法唯一解析，跳过"
    fi
    return 0
}

# 上一真实运行 SHA 解析。
# 关键约束：外层 panji-test-deploy 已把服务器 checkout 到 TARGET_SHA，
# 因此**禁止**把当前 repo HEAD 当作上一部署 SHA（否则 diff 为空、漏判 migration/环境变化）。
#
# 首次 Live Mount（核心容器尚未挂载 LIVE_ROOT）：
#   当前真实运行版本应优先于任何 repo 状态读取：
#     1. 当前 trading-backend /v1/version（runtime/image/git_sha）
#     2. 当前 trading-backend 镜像 tag 中的 SHA
#     3. 外层自举前的完整 SHA（PANJI_BOOTSTRAP_PREVIOUS_SHA）
#     4. 仍无法确认 → 停止并报告 previous_runtime_sha_unknown
#
# 已处于 Live Mount：
#     1. 部署状态文件
#     2. /opt/panji-live/RUNTIME_SHA
#     3. version.runtime_git_sha（当前运行版本，非 repo HEAD）
#     4. PANJI_BOOTSTRAP_PREVIOUS_SHA
#
# 短 SHA 仅在仓库中能唯一解析为完整 commit 时才允许使用。
resolve_previous_runtime_sha() {
    log "解析上一真实运行 SHA..."
    PREVIOUS_SHA=""
    PREVIOUS_SHA_SOURCE=""

    if [[ "${FIRST_LIVE_DEPLOY}" == "true" ]]; then
        log "  [首次 Live Mount] 优先读取当前真实运行版本"

        # 1. 当前运行 backend /v1/version
        local vsha
        vsha="$(_resolve_version_sha)"
        if _is_resolvable_sha "${vsha}"; then
            PREVIOUS_SHA="${vsha}"
            PREVIOUS_SHA_SOURCE="running_version"
        fi

        # 2. 当前镜像 tag 中的 SHA
        if [[ -z "${PREVIOUS_SHA}" ]]; then
            local isha
            isha="$(_resolve_image_tag_sha)"
            if _is_resolvable_sha "${isha}"; then
                PREVIOUS_SHA="${isha}"
                PREVIOUS_SHA_SOURCE="running_image_tag"
            fi
        fi

        # 3. 外层自举前完整 SHA（最终 fallback）
        if [[ -z "${PREVIOUS_SHA}" && -n "${BOOTSTRAP_PREVIOUS_SHA}" ]]; then
            if _is_resolvable_sha "${BOOTSTRAP_PREVIOUS_SHA}"; then
                PREVIOUS_SHA="${BOOTSTRAP_PREVIOUS_SHA}"
                PREVIOUS_SHA_SOURCE="bootstrap_previous_sha"
            else
                log "  BOOTSTRAP_PREVIOUS_SHA 不可解析: ${BOOTSTRAP_PREVIOUS_SHA:-空}"
            fi
        fi

        # 4. 仍无法确认 → 停止
        if [[ -z "${PREVIOUS_SHA}" ]]; then
            log "!!! 无法确认当前真实运行 SHA（首次 Live Mount），拒绝部署"
            log "结论: previous_runtime_sha_unknown"
            PREVIOUS_SHA_SOURCE="unknown_runtime"
            return 1
        fi

        log "上一真实运行 SHA: ${PREVIOUS_SHA}（来源: ${PREVIOUS_SHA_SOURCE}）"
        return 0
    fi

    # 已处于 Live Mount：状态文件 / RUNTIME_SHA / 运行版本 / 自举前 SHA
    log "  [已 Live Mount] 按状态文件 → RUNTIME_SHA → 运行版本 → 自举前 SHA 解析"

    # 1. 部署状态文件
    if [[ -f "${STATE_FILE}" ]]; then
        local candidate
        candidate="$(tr -d '[:space:]' < "${STATE_FILE}" 2>/dev/null || echo "")"
        if _is_resolvable_sha "${candidate}"; then
            PREVIOUS_SHA="${candidate}"
            PREVIOUS_SHA_SOURCE="state_file"
        else
            log "  状态文件存在但内容不可解析: ${candidate:-空}"
        fi
    fi

    # 2. /opt/panji-live/RUNTIME_SHA
    if [[ -z "${PREVIOUS_SHA}" && -f "${LIVE_ROOT}/RUNTIME_SHA" ]]; then
        local candidate
        candidate="$(tr -d '[:space:]' < "${LIVE_ROOT}/RUNTIME_SHA" 2>/dev/null || echo "")"
        if _is_resolvable_sha "${candidate}"; then
            PREVIOUS_SHA="${candidate}"
            PREVIOUS_SHA_SOURCE="runtime_sha_file"
        else
            log "  RUNTIME_SHA 存在但内容不可解析: ${candidate:-空}"
        fi
    fi

    # 3. 当前运行版本（非 repo HEAD）
    if [[ -z "${PREVIOUS_SHA}" ]]; then
        local vsha
        vsha="$(_resolve_version_sha)"
        if _is_resolvable_sha "${vsha}"; then
            PREVIOUS_SHA="${vsha}"
            PREVIOUS_SHA_SOURCE="running_version"
        fi
    fi

    # 4. 外层自举前完整 SHA
    if [[ -z "${PREVIOUS_SHA}" && -n "${BOOTSTRAP_PREVIOUS_SHA}" ]]; then
        if _is_resolvable_sha "${BOOTSTRAP_PREVIOUS_SHA}"; then
            PREVIOUS_SHA="${BOOTSTRAP_PREVIOUS_SHA}"
            PREVIOUS_SHA_SOURCE="bootstrap_previous_sha"
        else
            log "  BOOTSTRAP_PREVIOUS_SHA 不可解析: ${BOOTSTRAP_PREVIOUS_SHA:-空}"
        fi
    fi

    if [[ -z "${PREVIOUS_SHA}" ]]; then
        PREVIOUS_SHA_SOURCE="unknown_baseline"
        log "上一真实运行 SHA: 无法解析（未知基线）"
    else
        log "上一真实运行 SHA: ${PREVIOUS_SHA}（来源: ${PREVIOUS_SHA_SOURCE}）"
    fi
    return 0
}

# 首次 Live Mount 部署识别：任一核心应用容器未挂载 LIVE_ROOT 即为 true。
# 只影响「是否强制同步与重建以建立挂载」，不影响 migration 判定。
detect_first_live_deploy() {
    log "检测 Live Mount 是否已建立..."

    if ! command -v docker >/dev/null 2>&1; then
        log "  docker 不可用，按首次 Live Mount 部署处理"
        FIRST_LIVE_DEPLOY=true
        return 0
    fi

    local container mounts
    for container in trading-backend trading-frontend; do
        mounts="$(docker inspect "${container}" --format '{{range .Mounts}}{{.Source}} {{end}}' 2>/dev/null || echo "")"
        if [[ -z "${mounts}" ]]; then
            log "  容器 ${container} 不存在或无法 inspect → 首次 Live Mount 部署"
            FIRST_LIVE_DEPLOY=true
            return 0
        fi
        if [[ "${mounts}" != *"${LIVE_ROOT}"* ]]; then
            log "  容器 ${container} 未挂载 ${LIVE_ROOT} → 首次 Live Mount 部署"
            FIRST_LIVE_DEPLOY=true
            return 0
        fi
    done

    FIRST_LIVE_DEPLOY=false
    log "  backend 与 frontend 均已挂载 ${LIVE_ROOT}（非首次 Live Mount 部署）"
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

    # 首次未知基线：四级解析全部失败，无法算 diff，只能全量同步。
    # 此时 migration 状态同样未知，必须执行 alembic upgrade head（幂等）。
    if [[ -z "${PREVIOUS_SHA}" ]]; then
        log "上一部署 SHA 不可解析（${PREVIOUS_SHA_SOURCE}）：全量同步 + migration"
        BACKEND_RUNTIME_CHANGED=true
        FRONTEND_RUNTIME_CHANGED=true
        MIGRATION_CHANGED=true
        return
    fi

    local changed_files
    changed_files="$(git diff --name-only "${PREVIOUS_SHA}" "${TARGET_SHA}" 2>/dev/null || true)"

    if [[ -z "${changed_files}" ]]; then
        log "两次 SHA 之间无文件变化"
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

# 首次 Live Mount 部署强制覆盖：必须完整同步 Python 与前端运行代码并重建挂载，
# 否则容器内不存在 /opt/panji-live 内容。
# 明确边界：FIRST_LIVE_DEPLOY 只提升运行代码同步范围，**不得**据此设置 migration_changed。
apply_first_live_deploy_override() {
    if [[ "${FIRST_LIVE_DEPLOY}" != "true" ]]; then
        return 0
    fi

    log "首次 Live Mount 部署：强制全量同步 backend + frontend 运行代码以建立挂载"
    log "  （migration_changed 保持由差异判定决定，不因首次挂载而强制执行）"
    BACKEND_RUNTIME_CHANGED=true
    FRONTEND_RUNTIME_CHANGED=true

    log "  first_live_deploy=${FIRST_LIVE_DEPLOY}"
    log "  backend_runtime_changed=${BACKEND_RUNTIME_CHANGED}"
    log "  frontend_runtime_changed=${FRONTEND_RUNTIME_CHANGED}"
    log "  migration_changed=${MIGRATION_CHANGED}"
}

run_cmd() {
    if [[ "${DRY_RUN}" == "true" ]]; then
        log "[dry-run] 将执行: $*"
    else
        log "执行: $*"
        "$@"
    fi
}

# [CHANGE-20260804 / DS-103] 统一长命令外层超时。
# 用法: run_with_timeout <stage> <seconds> -- <cmd...>
# 超时/失败时返回非 0，由调用方既有的失败路径处理（写 failure_stage、释放锁、不重试）。
run_with_timeout() {
    local stage="$1"
    local seconds="$2"
    shift 2
    [[ "$1" == "--" ]] && shift

    if [[ "${DRY_RUN}" == "true" ]]; then
        log "[dry-run] ${stage}: 将执行(限时 ${seconds}s): $*"
        return 0
    fi

    if ! timeout --kill-after=30 "${seconds}" "$@"; then
        FAILURE_STAGE="${stage}"
        log "错误: ${stage} 执行超时(>${seconds}s)或失败，failure_stage=${stage}"
        return 1
    fi
    return 0
}

checkout_target() {
    log "检出目标 SHA..."
    cd "${REPO_ROOT}"
    run_cmd git fetch origin dev --no-tags
    run_cmd git checkout -f "${TARGET_SHA}"
    log "已检出: ${TARGET_SHA}"
}

# 是否存在任意运行环境级变化。
environment_changed() {
    [[ "${BACKEND_ENVIRONMENT_CHANGED}" == "true" \
        || "${FRONTEND_ENVIRONMENT_CHANGED}" == "true" \
        || "${CAPTURE_ENVIRONMENT_CHANGED}" == "true" ]]
}

# 环境镜像构建策略（与 docker-compose.prod.yml 的 image tag 约定保持一致）：
#   backend / frontend / worker-capture 三个镜像共用同一个 GIT_SHA tag。
#   因此只要发生任意 environment_changed，就必须把三者作为**同一个 tag 组**整体构建，
#   否则新 GIT_SHA 下会出现未构建的镜像 tag，Compose 启动即失败。
# 普通代码变化（无 environment_changed）零构建：Live Mount 直接生效。
# 构建完成后仍以 prod+live 叠加启动，运行代码仍唯一来自 /opt/panji-live。
ENV_IMAGE_TAG_GROUP=(backend frontend worker-capture)

build_environment_images() {
    if ! environment_changed; then
        log "无运行环境变化，跳过镜像构建（普通代码变化零构建）"
        return 0
    fi

    log "运行环境变化，按同一 GIT_SHA tag 组整体构建镜像: ${ENV_IMAGE_TAG_GROUP[*]}"
    log "  触发项: backend_env=${BACKEND_ENVIRONMENT_CHANGED} frontend_env=${FRONTEND_ENVIRONMENT_CHANGED} capture_env=${CAPTURE_ENVIRONMENT_CHANGED}"
    cd "${REPO_ROOT}"
    # [CHANGE-20260804 / DS-102] 逐服务串行构建（COMPOSE_PARALLEL_LIMIT=1 兜底），
    # 避免并发构建放大磁盘/CPU/内存峰值。任一服务构建失败即整体失败。
    for svc in "${ENV_IMAGE_TAG_GROUP[@]}"; do
        run_with_timeout "docker_build_${svc}" "${TIMEOUT_DOCKER_BUILD_SECONDS}" -- \
            ${COMPOSE_CMD} build "${svc}" || return 1
    done
    IMAGES_BUILT=true
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
        run_with_timeout "npm_ci" "${TIMEOUT_NPM_CI_SECONDS}" -- npm ci || return 1
    fi
    if [[ -x "./node_modules/.bin/vite" ]]; then
        run_with_timeout "vite_build" "${TIMEOUT_VITE_BUILD_SECONDS}" -- \
            env NODE_OPTIONS=--max-old-space-size=1024 ./node_modules/.bin/vite build || return 1
    else
        log "WARN: ./node_modules/.bin/vite 不存在，回退到 npm run build"
        run_with_timeout "vite_build" "${TIMEOUT_VITE_BUILD_SECONDS}" -- \
            env NODE_OPTIONS=--max-old-space-size=1024 npm run build || return 1
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

# RUNTIME_SHA 是**单文件 bind mount** 源。
# 单文件挂载在容器启动时绑定的是 inode，一旦通过
#   「写临时文件 → mv/rename 覆盖」或「rsync 覆盖」
# 更新，源文件 inode 会改变，容器内看到的仍是旧 inode 的旧内容。
# 因此这里必须**原地写入**（truncate + write 同一个 inode），不得 rename/rsync。
write_runtime_sha() {
    local sha_file="${LIVE_ROOT}/RUNTIME_SHA"
    log "写入 RUNTIME_SHA=${TARGET_SHA}（原地写入，保持 inode）..."

    if [[ "${DRY_RUN}" == "true" ]]; then
        log "[dry-run] 原地写入 ${sha_file} = ${TARGET_SHA}（完整 SHA，保持 inode）"
        return 0
    fi

    mkdir -p "${LIVE_ROOT}"

    if [[ ! -e "${sha_file}" ]]; then
        # 首次不存在：创建文件本身（此后该 inode 即为挂载源，不得再替换）
        : > "${sha_file}" || fail "无法创建 ${sha_file}"
        chmod 644 "${sha_file}" 2>/dev/null || true
        log "  RUNTIME_SHA 首次创建: ${sha_file}"
    fi

    [[ -f "${sha_file}" ]] || fail "${sha_file} 不是普通文件，拒绝写入"

    local inode_before inode_after
    inode_before="$(stat -c '%i' "${sha_file}" 2>/dev/null || echo "")"

    # `> file` 是 truncate + 原地写，保持同一 inode。
    printf '%s' "${TARGET_SHA}" > "${sha_file}" || fail "无法原地写入 ${sha_file}"

    inode_after="$(stat -c '%i' "${sha_file}" 2>/dev/null || echo "")"
    if [[ -n "${inode_before}" && "${inode_before}" != "${inode_after}" ]]; then
        fail "RUNTIME_SHA inode 发生变化（${inode_before} → ${inode_after}），单文件挂载已失效"
    fi

    # 回读校验：必须等于完整 40 位 SHA
    local readback
    readback="$(tr -d '[:space:]' < "${sha_file}" 2>/dev/null || echo "")"
    if [[ "${readback}" != "${TARGET_SHA}" ]]; then
        fail "RUNTIME_SHA 回读校验失败: 期望 ${TARGET_SHA}, 实际 ${readback:-空}"
    fi

    log "RUNTIME_SHA 原地写入并回读通过: ${sha_file} (inode=${inode_after:-unknown})"
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

# migration 仅在 migration_changed 时执行，且**必须早于任何服务重启**。
# 失败时调用方不得重启应用服务，也不得 force-recreate 任何容器。
run_migration() {
    log "执行 alembic upgrade head（使用目标 SHA 的 Live Mount 代码）..."
    MIGRATION_ATTEMPTED=true
    cd "${REPO_ROOT}"
    if ! run_with_timeout "migration" "${TIMEOUT_MIGRATION_SECONDS}" -- \
        ${COMPOSE_CMD} run --rm --no-deps --no-build backend bash -c "cd /app && alembic upgrade head"; then
        MIGRATION_SUCCEEDED=false
        log "migration 执行失败或超时"
        return 1
    fi
    MIGRATION_SUCCEEDED=true
    log "migration 完成"
}

# [CHANGE-20260804 / DS-102] 按固定波次重启，禁止一次性 up -d 交出全部 Python 服务：
#   波次1 backend → 健康/就绪检查；波次2 frontend；波次3 Scheduler 单实例；
#   波次4 普通 Worker 小批次；波次5 after-close/watchdog；波次6 capture。
#   数据服务（postgres/redis/umami）永不进入普通重启列表。
restart_services() {
    local services=("$@")
    if [[ ${#services[@]} -eq 0 ]]; then
        log "无需重启任何服务"
        return 0
    fi
    log "按波次重启服务: ${services[*]}"
    cd "${REPO_ROOT}"

    # 标记必须在实际发起重启之前置位：一旦 up 开始，容器状态即可能已被改变。
    SERVICES_RESTARTED=true

    _wave_up() {
        local wave_name="$1"; shift
        local wave_services=("$@")
        if [[ ${#wave_services[@]} -eq 0 ]]; then
            return 0
        fi
        log "  波次 [${wave_name}]: ${wave_services[*]}"
        run_with_timeout "compose_up_${wave_name}" "${TIMEOUT_COMPOSE_UP_SECONDS}" -- \
            ${COMPOSE_CMD} up -d --force-recreate --no-build "${wave_services[@]}" || return 1
    }

    _wave_backend=()
    _wave_frontend=()
    _wave_scheduler=()
    _wave_workers=()
    _wave_recovery=()
    _wave_capture=()

    for s in "${services[@]}"; do
        case "${s}" in
            backend) _wave_backend+=("${s}") ;;
            frontend) _wave_frontend+=("${s}") ;;
            worker-bars-scheduler|worker-strategy-scheduler|worker-calendar)
                _wave_scheduler+=("${s}") ;;
            worker-capture) _wave_capture+=("${s}") ;;
            worker-after-close|worker-watchdog) _wave_recovery+=("${s}") ;;
            worker-monitor|worker-strategy-batch|worker-outbox|worker-delivery)
                _wave_workers+=("${s}") ;;
            postgres|redis|umami)
                log "  跳过数据服务（永不重启）: ${s}"
                ;;
            *)
                log "  WARN: 未识别服务放入普通 worker 波次: ${s}"
                _wave_workers+=("${s}") ;;
        esac
    done

    _wave_up backend "${_wave_backend[@]}" || return 1
    if [[ ${#_wave_backend[@]} -gt 0 ]]; then
        _wait_health
    fi
    _wave_up frontend "${_wave_frontend[@]}" || return 1
    _wave_up scheduler "${_wave_scheduler[@]}" || return 1
    if [[ ${#_wave_scheduler[@]} -gt 0 ]]; then
        _check_scheduler_single_instance
    fi
    _wave_up workers "${_wave_workers[@]}" || return 1
    _wave_up recovery "${_wave_recovery[@]}" || return 1
    _wave_up capture "${_wave_capture[@]}" || return 1
    return 0
}

# 等待 backend /v1/health 就绪（限时）。
_wait_health() {
    local health_url="http://localhost:8000/v1/health"
    local deadline=$(( $(date +%s) + TIMEOUT_HEALTH_WAIT_SECONDS ))
    while :; do
        if curl -s -o /dev/null -w '%{http_code}' "${health_url}" 2>/dev/null | grep -q 200; then
            log "  backend health 就绪"
            return 0
        fi
        if [[ "$(date +%s)" -ge "${deadline}" ]]; then
            FAILURE_STAGE="health_wait"
            log "错误: backend health 等待超时(>${TIMEOUT_HEALTH_WAIT_SECONDS}s)"
            return 1
        fi
        sleep 3
    done
}

# Scheduler 波次后校验三个 Scheduler 均为单实例。
_check_scheduler_single_instance() {
    local failed=false
    for container in trading-worker-bars-scheduler trading-worker-strategy-scheduler trading-worker-calendar; do
        local count
        count="$(docker ps --filter "name=${container}" --format '{{.Names}}' | wc -l | tr -d ' ')"
        if [[ "${count}" != "1" ]]; then
            log "错误: Scheduler 单实例校验失败 ${container} count=${count}"
            failed=true
        fi
    done
    if [[ "${failed}" == "true" ]]; then
        FAILURE_STAGE="scheduler_single_instance"
        return 1
    fi
    log "  Scheduler 单实例校验通过"
    return 0
}

RESTARTED_PYTHON=false
RESTARTED_FRONTEND=false

deploy() {
    # 1. 运行环境镜像：任意 environment_changed → 按同一 GIT_SHA tag 组整体构建。
    #    无 environment_changed → 零构建，GIT_SHA 保持不变。
    FAILURE_STAGE="environment_images"
    if environment_changed; then
        update_env_file true
    else
        update_env_file false
    fi
    # build_environment_images 自带 environment_changed 守卫：
    # 无环境变化时只记录「零构建」决策，不执行任何 build。
    build_environment_images

    # 2. 前端 dist（运行代码 / 运行环境 / 首次 Live Mount 都需要重新产出 dist）
    FAILURE_STAGE="frontend_build"
    local need_frontend=false
    if [[ "${FRONTEND_RUNTIME_CHANGED}" == "true" || "${FRONTEND_ENVIRONMENT_CHANGED}" == "true" ]]; then
        need_frontend=true
        build_frontend_dist
        sync_frontend_runtime
    fi

    # 3. backend 运行代码
    FAILURE_STAGE="backend_sync"
    local need_backend=false
    if [[ "${BACKEND_RUNTIME_CHANGED}" == "true" \
        || "${BACKEND_ENVIRONMENT_CHANGED}" == "true" \
        || "${CAPTURE_ENVIRONMENT_CHANGED}" == "true" \
        || "${MIGRATION_CHANGED}" == "true" ]]; then
        need_backend=true
        sync_backend_runtime
    fi

    # 4. RUNTIME_SHA 始终原地写入（是 runtime_git_sha 的唯一来源）
    FAILURE_STAGE="runtime_sha"
    write_runtime_sha

    FAILURE_STAGE="compose_config"
    compose_config_check

    # 5. migration —— 必须早于任何服务重启。
    #    失败时立即返回，main() 走 migration 专用失败路径（不重启、不 force-recreate）。
    if [[ "${MIGRATION_CHANGED}" == "true" ]]; then
        FAILURE_STAGE="migration"
        run_migration || return 1
    else
        log "migration_changed=false，跳过 alembic upgrade"
    fi

    # 6. 重启：Python 服务与 frontend 分别判定；postgres/redis/umami 永不重启
    FAILURE_STAGE="restart"
    local restart_list=()
    if [[ "${need_backend}" == "true" ]]; then
        restart_list+=("${PYTHON_SERVICES[@]}")
        RESTARTED_PYTHON=true
    fi
    if [[ "${need_frontend}" == "true" ]]; then
        restart_list+=(frontend)
        RESTARTED_FRONTEND=true
    fi

    if [[ ${#restart_list[@]} -eq 0 ]]; then
        log "无运行代码变化，不重启任何服务（仅刷新 RUNTIME_SHA 与核验）"
    else
        restart_services "${restart_list[@]}"
    fi

    FAILURE_STAGE=""
    return 0
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
        log "[dry-run]   trading-backend Mounts 包含 ${LIVE_ROOT}"
        log "[dry-run]   trading-frontend Mounts 包含 ${LIVE_ROOT}/frontend/dist"
        log "[dry-run]   全部 ${#PYTHON_SERVICES[@]} 个 Python 服务 Mounts 包含 ${LIVE_ROOT}"
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

    # 6. Mount 核验
    # 6a. 无条件核验：trading-backend 必须包含 ${LIVE_ROOT}，
    #     trading-frontend 必须包含 ${LIVE_ROOT}/frontend/dist。
    #     无论本轮是否触发前端重建，都必须核验，否则无法发现挂载缺失。
    local backend_mounts
    backend_mounts="$(docker inspect trading-backend --format '{{range .Mounts}}{{.Source}} {{end}}' 2>/dev/null || echo "")"
    if [[ "${backend_mounts}" != *"${LIVE_ROOT}"* ]]; then
        log "trading-backend Mounts 不含 ${LIVE_ROOT}（未实际运行 Live Mount）"
        return 1
    fi
    log "trading-backend Mounts 包含 ${LIVE_ROOT}"

    local frontend_mounts
    frontend_mounts="$(docker inspect trading-frontend --format '{{range .Mounts}}{{.Source}} {{end}}' 2>/dev/null || echo "")"
    if [[ "${frontend_mounts}" != *"${LIVE_ROOT}/frontend/dist"* ]]; then
        log "trading-frontend Mounts 不含 ${LIVE_ROOT}/frontend/dist（未实际运行 Live Mount）"
        return 1
    fi
    log "trading-frontend Mounts 包含 ${LIVE_ROOT}/frontend/dist"

    # 6b. 全量 Python 服务核验：这 11 个服务共用同一份 Live Mount backend 代码。
    #     只核验 backend 一个容器会漏掉 worker 未挂载/未重启的情况。
    if [[ "${RESTARTED_PYTHON}" == "true" || "${FIRST_LIVE_DEPLOY}" == "true" ]]; then
        log "核验全部 ${#PYTHON_SERVICES[@]} 个 Python 服务的 Live Mount..."
        local svc container svc_mounts
        for svc in "${PYTHON_SERVICES[@]}"; do
            container="trading-${svc}"
            svc_mounts="$(docker inspect "${container}" --format '{{range .Mounts}}{{.Source}} {{end}}' 2>/dev/null || echo "")"
            if [[ -z "${svc_mounts}" ]]; then
                log "Python 服务容器不存在或无法 inspect: ${container}"
                return 1
            fi
            if [[ "${svc_mounts}" != *"${LIVE_ROOT}"* ]]; then
                log "Python 服务容器 Mounts 不含 ${LIVE_ROOT}: ${container}"
                return 1
            fi
        done
        log "全部 ${#PYTHON_SERVICES[@]} 个 Python 服务 Mounts 均包含 ${LIVE_ROOT}"
    else
        log "本轮未重启 Python 服务且非首次 Live Mount，跳过 worker 级 Mount 全量核验"
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

    # [CHANGE-20260804 / DS-104] 部署后资源复检：任一失败即判部署失败。
    post_deploy_resource_check || return 1

    return 0
}

# [CHANGE-20260804 / DS-104] 部署后资源验收：
#   主机磁盘/内存/swap、容器 OOMKilled/RestartCount、Compose 限制实际生效、stats 高水位。
#   任一失败即部署失败，不得写成功状态文件。
post_deploy_resource_check() {
    log "部署后资源复检..."

    # 1. 主机资源（复用 check_resource_budget 的阈值，但允许只读采集不修改）
    local available_kb available_gb used_pct mem_kb mem_mb
    available_kb="$(df -Pk / | awk 'NR==2 {print $4}')"
    used_pct="$(df -Pk / | awk 'NR==2 {gsub(/%/, "", $5); print $5}')"
    mem_kb="$(awk '/^MemAvailable:/ {print $2; exit}' /proc/meminfo 2>/dev/null || echo 0)"
    available_gb=$((available_kb / 1024 / 1024))
    mem_mb=$((mem_kb / 1024))
    log "  host: disk_free=${available_gb}GB disk_used=${used_pct}% mem_available=${mem_mb}MB"
    if [[ "${available_gb}" -lt "${MIN_DISK_GB}" || "${used_pct}" -gt "${MAX_DISK_PCT}" || "${mem_mb}" -lt "${MIN_MEM_MB}" ]]; then
        FAILURE_STAGE="post_deploy_host_resource"
        log "错误: 部署后主机资源跌破阈值"
        return 1
    fi

    # 2. 关键容器 OOMKilled / RestartCount / 限制生效
    local check_failed=false
    for container in trading-backend trading-worker-after-close trading-worker-capture; do
        local oom restart
        oom="$(docker inspect -f '{{.State.OOMKilled}}' "${container}" 2>/dev/null || echo "unknown")"
        restart="$(docker inspect -f '{{.RestartCount}}' "${container}" 2>/dev/null || echo "unknown")"
        log "  ${container}: oom_killed=${oom} restart_count=${restart}"
        if [[ "${oom}" == "true" ]]; then
            log "错误: ${container} OOMKilled"
            check_failed=true
        fi
        if [[ "${restart}" != "unknown" && "${restart}" -gt 3 ]]; then
            log "错误: ${container} 异常 RestartCount=${restart}"
            check_failed=true
        fi
    done

    # 3. Compose 限制实际生效（Memory/PidsLimit/NanoCpus 非 0）
    local mem_cfg pids_cfg cpu_cfg
    mem_cfg="$(docker inspect -f '{{.HostConfig.Memory}}' trading-backend 2>/dev/null || echo 0)"
    pids_cfg="$(docker inspect -f '{{.HostConfig.PidsLimit}}' trading-backend 2>/dev/null || echo 0)"
    cpu_cfg="$(docker inspect -f '{{.HostConfig.NanoCpus}}' trading-backend 2>/dev/null || echo 0)"
    log "  trading-backend limits: mem_limit_bytes=${mem_cfg} pids_limit=${pids_cfg} nano_cpus=${cpu_cfg}"
    if [[ "${mem_cfg}" == "0" || "${pids_cfg}" == "0" || "${cpu_cfg}" == "0" ]]; then
        log "错误: Compose 资源限制未实际生效（Memory/PidsLimit/NanoCpus 存在 0）"
        check_failed=true
    fi

    # 4. stats 高水位（只读采集，供 Map 记录与后续收紧预算）
    log "  docker stats 高水位（no-stream）:"
    docker stats --no-stream --format '    stats {{.Name}} mem_usage={{.MemUsage}} mem_pct={{.MemPerc}}' 2>/dev/null \
        | sed -n '1,30p' || true

    if [[ "${check_failed}" == "true" ]]; then
        FAILURE_STAGE="post_deploy_container_resource"
        return 1
    fi

    log "部署后资源复检通过"
    return 0
}

# 把 repo / Live Mount 文件 / RUNTIME_SHA / market.env 恢复到 PREVIOUS_SHA。
# 只做「文件层」恢复，不触碰任何容器，也不触碰数据库。
restore_files_to_previous_sha() {
    log "恢复代码与运行文件到 previous SHA: ${PREVIOUS_SHA}（来源: ${PREVIOUS_SHA_SOURCE}）"

    cd "${REPO_ROOT}"
    run_cmd git checkout -f "${PREVIOUS_SHA}"

    local saved_target_sha="${TARGET_SHA}"
    TARGET_SHA="${PREVIOUS_SHA}"

    if environment_changed; then
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
    log "文件层已恢复到 ${PREVIOUS_SHA}"
}

# migration 失败专用路径。
# 硬约束：
#   - 此刻服务**尚未重启**，容器仍运行 previous SHA 的代码；
#   - 因此绝不执行 docker compose up / --force-recreate，避免把失败状态推给容器；
#   - 只恢复文件层，使 Live Mount 与 RUNTIME_SHA 回到 previous SHA；
#   - 数据库状态未知，**不得**声称数据库已回滚。
handle_migration_failure() {
    log "!!! migration 失败：进入 migration 专用失败路径 !!!"
    log "服务未重启（services_restarted=${SERVICES_RESTARTED}），不执行任何容器重建"

    if [[ -z "${PREVIOUS_SHA}" ]]; then
        log "无 previous SHA，无法恢复文件层，请手动处理"
    else
        restore_files_to_previous_sha
    fi

    log "migration_attempted=${MIGRATION_ATTEMPTED} migration_succeeded=${MIGRATION_SUCCEEDED}"
    log "数据库状态未知：本脚本没有也不会自动回滚数据库 schema 或数据"
    log "结论: migration_failed_requires_inspection"
}

# 服务已重启后（health / SHA 核验失败）才允许做容器级回滚。
rollback() {
    log "!!! 部署失败，执行容器级回滚 !!!"
    log "failure_stage=${FAILURE_STAGE} services_restarted=${SERVICES_RESTARTED}"

    if [[ -z "${PREVIOUS_SHA}" ]]; then
        log "无 previous SHA 记录，无法自动回滚代码。请手动处理。"
        return 1
    fi

    restore_files_to_previous_sha

    cd "${REPO_ROOT}"
    run_cmd ${COMPOSE_CMD} up -d --force-recreate --no-build \
        "${PYTHON_SERVICES[@]}" frontend

    log "回滚完成（已恢复到 ${PREVIOUS_SHA}）"
}

# 资源清理边界：
#   - 普通 Live Mount 代码部署（本轮未构建任何镜像）→ 完全不清理，
#     因为没有产生新的 build cache 或悬空镜像，清理只会误伤无关资源；
#   - 仅当本轮确实构建了环境镜像时，才做受控范围清理（builder + 悬空 + 旧 SHA 精确回收）。
# 永久禁止：docker image prune -a / docker system prune -a / docker volume prune /
#           container prune / 删除 node:20-alpine / 删除 PostgreSQL 或 Redis Volume。
# [CHANGE-20260804 / DS-105] 旧 SHA 业务镜像精确回收：保留当前/上一成功/rollback/基础镜像，
# 按完整 SHA 组删除，禁止按模糊名或创建时间删除。

# 读取根分区可用空间（MB），用于清理前后磁盘证据。
_disk_free_mb() {
    df -Pk / | awk 'NR==2 {print $4}' 2>/dev/null || echo 0
}

cleanup_resources() {
    if [[ "${IMAGES_BUILT}" != "true" ]]; then
        log "本轮未构建任何镜像（images_built=false），跳过资源清理"
        log "  普通 Live Mount 代码部署不做 builder 缓存清理，也不清理无关容器/镜像"
        return 0
    fi

    local disk_before_mb disk_after_mb
    disk_before_mb="$(_disk_free_mb)"

    log "本轮构建了环境镜像，执行受控范围清理（builder + 悬空 + 旧 SHA 精确回收）..."
    log "cleanup_disk_before_mb=${disk_before_mb}"

    run_cmd docker builder prune -f
    run_cmd docker image prune -f

    # 构造保留集合：当前运行 SHA、上一成功部署 SHA、rollback 标签、基础镜像。
    local keep_sha=""
    for candidate in "${TARGET_SHA}" "${PREVIOUS_SHA}"; do
        if [[ -n "${candidate}" ]]; then
            keep_sha="${keep_sha} ${candidate}"
        fi
    done
    log "  旧 SHA 回收：保留 SHA 集合 =${keep_sha}，及所有 *-rollback 标签与基础镜像"

    local reclaimed=()
    # 枚举 market-dev-{backend,capture,frontend}:<sha> 的完整 SHA 组，仅在整组不在保留集合时删除。
    while IFS= read -r repo_tag; do
        [[ -n "${repo_tag}" ]] || continue
        # repo_tag 形如 market-dev/backend:abcdef... 或 market-dev-backend:abc（取决于本地构建命名）
        local short
        short="${repo_tag##*/}"
        local sha
        sha="${short##*:}"
        # 跳过非 SHA 形式（rollback / dev / latest 等标签，以及基础镜像/其他项目镜像）
        if ! [[ "${sha}" =~ ^[0-9a-f]{7,40}$ ]]; then
            continue
        fi
        if [[ "${keep_sha}" == *"${sha}"* ]]; then
            continue
        fi
        # 仅当该 SHA 的三个业务镜像（backend/capture/frontend）都指向同一且均不在保留集合时才整组删
        local backend_tag="market-dev-backend:${sha}"
        local capture_tag="market-dev-capture:${sha}"
        local frontend_tag="market-dev-frontend:${sha}"
        local b_count c_count f_count
        b_count="$(docker images -q "${backend_tag}" 2>/dev/null | wc -l | tr -d ' ')"
        c_count="$(docker images -q "${capture_tag}" 2>/dev/null | wc -l | tr -d ' ')"
        f_count="$(docker images -q "${frontend_tag}" 2>/dev/null | wc -l | tr -d ' ')"
        if [[ "${b_count}" != "0" && "${c_count}" != "0" && "${f_count}" != "0" ]]; then
            log "  回收旧 SHA 镜像组: ${sha} (backend/capture/frontend)"
            run_cmd docker rmi "${backend_tag}" "${capture_tag}" "${frontend_tag}" >/dev/null 2>&1 || true
            reclaimed+=("${sha}")
        fi
    done < <(docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null)

    disk_after_mb="$(_disk_free_mb)"
    log "cleanup_disk_after_mb=${disk_after_mb}"
    log "  回收旧 SHA 组数: ${#reclaimed[@]} 组"

    # [CHANGE-20260804 / DS-104] 清理后再复检一次资源，失败返回 1 由 main 判部署失败。
    post_deploy_resource_check || return 1
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
        # 顺序约束（P0 修复）：
        #   必须先用当前真实运行状态解析上一 SHA，再分类变化，最后 checkout。
        #   禁止在进入 checkout 目标 SHA 之后才解析——否则会把 TARGET_SHA 当作上一 SHA。
        detect_first_live_deploy
        resolve_previous_runtime_sha || {
            # 首次 Live Mount 且无法确认当前真实运行 SHA：停止，不部署。
            fail "previous_runtime_sha_unknown：首次 Live Mount 无法确认当前运行版本，拒绝部署"
        }
        classify_changes
        apply_first_live_deploy_override

        checkout_target

        if ! deploy; then
            # 区分两类失败路径：
            #   migration 失败（服务未重启）→ 只恢复文件，不动容器；
            #   其他阶段失败 → 按是否已重启决定容器级回滚。
            if [[ "${FAILURE_STAGE}" == "migration" ]]; then
                handle_migration_failure
                fail "migration_failed_requires_inspection：migration 失败，服务未重启，数据库状态需人工确认"
            fi

            if [[ "${SERVICES_RESTARTED}" == "true" ]]; then
                rollback
                fail "部署失败（阶段: ${FAILURE_STAGE}）并已执行容器级回滚"
            fi

            if [[ -n "${PREVIOUS_SHA}" ]]; then
                restore_files_to_previous_sha
            fi
            fail "部署失败（阶段: ${FAILURE_STAGE}），服务未重启，已恢复文件层"
        fi

        if ! verify_deployment; then
            # 核验发生在重启之后，属于容器级回滚场景。
            rollback
            fail "部署核验失败并已回滚"
        fi

        if ! cleanup_resources; then
            # 清理后资源复检失败（OOM / 资源跌破阈值 / 限制未生效）→ 判部署失败。
            rollback
            fail "部署后清理与资源复检失败（failure_stage=${FAILURE_STAGE}）并已回滚"
        fi

        save_state "${TARGET_SHA}"

        log "部署成功: ${TARGET_SHA}"
        log "  deployment_mode=live"
        log "  first_live_deploy=${FIRST_LIVE_DEPLOY}"
        log "  previous_sha=${PREVIOUS_SHA:-none} (source=${PREVIOUS_SHA_SOURCE})"
        log "  migration_attempted=${MIGRATION_ATTEMPTED} migration_succeeded=${MIGRATION_SUCCEEDED}"
        log "  images_built=${IMAGES_BUILT} services_restarted=${SERVICES_RESTARTED}"
        log "  repo HEAD = RUNTIME_SHA = version.runtime_git_sha = ${TARGET_SHA}"

    ) 200>"${LOCK_FILE}"
}

main "$@"
