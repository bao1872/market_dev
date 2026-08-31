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
COMPOSE_RUNTIME_CHANGED=false

# 首次 Live Mount 部署：核心应用容器尚未挂载 LIVE_ROOT。
# 需要强制建立挂载，但**不得**因此把 migration_changed 设为 true。
FIRST_LIVE_DEPLOY=false

# 部署执行状态机（用于区分 migration 失败与重启后失败两类回滚路径）
SERVICES_RESTARTED=false

# [E2.1 P1-A §3] 真实 runtime mutation 阶段 owner。
#
# SERVICES_RESTARTED 只表示**容器是否已 restart/recreate**，它无法回答
# env / live files / RUNTIME_SHA 是否已经被改写。旧 failure handler 正是
# 用 `SERVICES_RESTARTED == true` 的**反面**去推断"文件被改过需要恢复"，
# 导致在任何 mutation 都还没开始的失败（例如 rollback owner 解析失败）
# 下仍主动执行 restore_files_to_previous_sha，凭空制造出
# market.env / live rsync / RUNTIME_SHA 三处 mutation。
#
# 因此这里建立独立、source-backed 的阶段状态，由真实 mutation 点推进，
# 供 failure handler 做正确分派：
#   none       —— 尚无任何 runtime mutation（pre-mutation failure）
#   files      —— env/live files/RUNTIME_SHA 已 mutation，容器未 restart
#   containers —— 容器已 recreate/restart
MUTATION_STAGE="none"
FAILURE_STAGE=""
MIGRATION_ATTEMPTED=false
MIGRATION_SUCCEEDED=false
IMAGES_BUILT=false

# [E2.1 P1-C] supervisor-drain 进程 fence owner
#   AFTER_CLOSE_WAS_RUNNING   —— 进入部署时 worker-after-close 是否 running
#   AFTER_CLOSE_FENCE_OWNED   —— 本次 deploy 是否真正 stop -t -1 停掉了 running worker
#                                  （仅 owned 的 worker 才在成功/rollback 后由本 deploy 恢复）
#   AFTER_CLOSE_PICKUP_FENCED —— 线性化点已建立：容器 EXITED/missing 且 after_close running==0
AFTER_CLOSE_WAS_RUNNING=false
AFTER_CLOSE_FENCE_OWNED=false
AFTER_CLOSE_PICKUP_FENCED=false

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

# [RESOURCE_GATE_ORDER_DEBT] 把原 check_resource_budget 拆成两个语义独立的 owner：
#
#   check_static_resource_budget —— 在任何生产状态变更之前可静态判定：
#       · 阈值配置健全性（MIN_DISK_GB / MAX_DISK_PCT / MIN_MEM_MB 不得被调低）
#       · 主机磁盘余量（合法生产安全约束，不得在脚本侧放宽）
#       不判断当前 MemAvailable >= 4096：那是"部署工作集 headroom"，必须先让
#       supervisor-drain fence 释放 worker-after-close 之后才算数。
#
#   check_deployment_memory_headroom —— 部署临界区内存余量：
#       必须在 fence 之后、首笔 runtime mutation 之前检查。fence 释放的 ~942MB anon
#       才是本次部署真实可用 working set。MIN_MEM_MB 继续作为保守部署 headroom 参数，
#       本轮不降低、也不重新定义为稳态 host 空闲下限（那是不同概念）。
check_static_resource_budget() {
    log "检查静态资源预算（任何状态修改之前，不含 MemAvailable 稳态门槛）..."

    [[ "${MIN_DISK_GB}" =~ ^[0-9]+$ && "${MIN_DISK_GB}" -ge 20 ]] \
        || fail "PANJI_MIN_DISK_GB 只能保持或提高 20 GB 下限"
    [[ "${MAX_DISK_PCT}" =~ ^[0-9]+$ && "${MAX_DISK_PCT}" -le 82 ]] \
        || fail "PANJI_MAX_DISK_PCT 只能保持或收紧 82% 上限"
    [[ "${MIN_MEM_MB}" =~ ^[0-9]+$ && "${MIN_MEM_MB}" -ge 4096 ]] \
        || fail "PANJI_MIN_MEM_MB 只能保持或提高 4096 MB 下限（部署 headroom，非稳态 host 空闲下限）"

    local available_kb available_gb used_pct
    available_kb="$(df -Pk / | awk 'NR==2 {print $4}')"
    used_pct="$(df -Pk / | awk 'NR==2 {gsub(/%/, "", $5); print $5}')"
    [[ "${available_kb}" =~ ^[0-9]+$ && "${used_pct}" =~ ^[0-9]+$ ]] \
        || fail "无法读取磁盘预算"
    available_gb=$((available_kb / 1024 / 1024))

    [[ "${available_gb}" -ge "${MIN_DISK_GB}" ]] \
        || fail "根分区可用 ${available_gb} GB，低于 ${MIN_DISK_GB} GB"
    [[ "${used_pct}" -le "${MAX_DISK_PCT}" ]] \
        || fail "根分区使用率 ${used_pct}%，高于 ${MAX_DISK_PCT}%"

    log "静态资源预算通过: disk_free=${available_gb}GB disk_used=${used_pct}%"
}

# 部署工作集内存 headroom：fence 之后、首笔 runtime mutation 之前检查。
# PANJI_MOCK_MEM_AVAILABLE_KB 仅供契约测试注入（模拟 fence 前后不同 MemAvailable），
# 生产环境不设置时走真实 /proc/meminfo（Linux）或 sysctl hw.memsize（macOS）。
# 注意：本函数不足门槛时 return 1（不 fail），以便 deploy() 走 failure matrix
# 恢复被本 deploy fenced 的 worker-after-close。
check_deployment_memory_headroom() {
    local mem_kb mem_mb
    if [[ -n "${PANJI_MOCK_MEM_AVAILABLE_KB:-}" ]]; then
        # 测试 seam 仅在 dry-run / 正式契约测试下允许覆盖真实 MemAvailable。
        # 真实部署中若存在该变量，视为环境泄漏，fail-closed 拒绝使用，强制读取真实 /proc/meminfo。
        if [[ "${DRY_RUN}" != "true" ]]; then
            log "检测到 PANJI_MOCK_MEM_AVAILABLE_KB，但当前为真实部署（非 dry-run）；测试 seam 不能覆盖真实 MemAvailable，拒绝使用"
            return 1
        fi
        mem_kb="${PANJI_MOCK_MEM_AVAILABLE_KB}"
    elif [[ -r /proc/meminfo ]]; then
        mem_kb="$(awk '/^MemAvailable:/ {print $2; exit}' /proc/meminfo)"
    elif command -v sysctl >/dev/null 2>&1; then
        mem_kb="$(( $(sysctl -n hw.memsize) / 1024 ))"
    else
        log "错误: 无法读取 MemAvailable"
        return 1
    fi
    [[ "${mem_kb}" =~ ^[0-9]+$ ]] || { log "错误: 无法读取 MemAvailable"; return 1; }
    mem_mb=$((mem_kb / 1024))

    log "deployment headroom: mem_available=${mem_mb}MB min=${MIN_MEM_MB}MB (after fence, before first runtime mutation)"

    [[ "${mem_mb}" -ge "${MIN_MEM_MB}" ]] || {
        log "DEPLOYMENT_MEMORY_HEADROOM_INSUFFICIENT=true (mem_available=${mem_mb}MB < min=${MIN_MEM_MB}MB)"
        return 1
    }
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

    # [REVIEW-V2] Application runtime Compose config change detection.
    # Compose (prod / live overlay) is runtime topology/config SSOT. A changed Compose file
    # MUST recreate app containers — otherwise RUNTIME_SHA advances while containers keep the
    # previous container config (FIRST_BLOCKER = COMPOSE_ONLY_RUNTIME_CONFIG_NOT_APPLIED_BY_DEPLOY).
    # Classified as application runtime mutation: does NOT set *_ENVIRONMENT_CHANGED (no image build)
    # and does NOT set MIGRATION_CHANGED (no alembic).
    if echo "${changed_files}" | grep -qE '^docker-compose\.(prod|live)\.yml$'; then
        COMPOSE_RUNTIME_CHANGED=true
    fi

    log "  backend_runtime_changed=${BACKEND_RUNTIME_CHANGED}"
    log "  frontend_runtime_changed=${FRONTEND_RUNTIME_CHANGED}"
    log "  migration_changed=${MIGRATION_CHANGED}"
    log "  backend_environment_changed=${BACKEND_ENVIRONMENT_CHANGED}"
    log "  frontend_environment_changed=${FRONTEND_ENVIRONMENT_CHANGED}"
    log "  capture_environment_changed=${CAPTURE_ENVIRONMENT_CHANGED}"
    log "  compose_runtime_changed=${COMPOSE_RUNTIME_CHANGED}"
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

    # [deploy-fix 2026-08-05] 服务器主机无宿主 npm：/usr/local/bin/node 是
    # trae-cn 内嵌单文件二进制，不带 npm/corepack。前端 dist 必须在远端生成且
    # 禁止 docker cp，故在 node:20-alpine 容器内执行 npm ci + vite build，
    # 产物落到 ${REPO_ROOT}/frontend/dist（与 frontend/Dockerfile 同一基础镜像，
    # 同为远端服务器上生成，非本地 dist）。
    local node_image="node:20-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293"
    cd "${REPO_ROOT}/frontend"
    if [[ "${FRONTEND_ENVIRONMENT_CHANGED}" == "true" ]]; then
        log "前端依赖或构建环境变化，先执行 npm ci（node:20-alpine 容器内）"
        run_with_timeout "npm_ci" "${TIMEOUT_NPM_CI_SECONDS}" -- \
            docker run --rm -v "${REPO_ROOT}/frontend:/app" -w /app \
            -e NPM_CONFIG_REGISTRY=https://registry.npmmirror.com \
            "${node_image}" npm ci || return 1
    fi
    run_with_timeout "vite_build" "${TIMEOUT_VITE_BUILD_SECONDS}" -- \
        docker run --rm -v "${REPO_ROOT}/frontend:/app" -w /app \
        -e NODE_OPTIONS=--max-old-space-size=1024 \
        "${node_image}" npm run build || return 1
    cd "${REPO_ROOT}"
}

sync_backend_runtime() {
    log "同步 backend 运行代码到 ${LIVE_ROOT}..."

    if [[ "${DRY_RUN}" == "true" ]]; then
        log "[dry-run] rsync backend/app, backend/alembic, backend/alembic.ini → ${LIVE_ROOT}/backend/"
        return 0
    fi

    # [E2.1 P1-A §6] 由此 mutator 自己推进 mutation stage（dry-run 已提前 return）。
    _mark_files_mutated

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

    # [E2.1 P1-A §6] 由此 mutator 自己推进 mutation stage（dry-run 已提前 return）。
    _mark_files_mutated

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

    # [E2.1 P1-A §6] 由此 mutator 自己推进 mutation stage（dry-run 已提前 return）。
    _mark_files_mutated

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

    # [E2.1 P1-A §6] 由此 mutator 自己推进 mutation stage（dry-run 已提前 return）。
    _mark_files_mutated

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
    # [deploy-fix 2026-08-05] `docker compose run` 不支持 --no-build（仅 up/build 支持）。
    # 本阶段环境镜像已由 build_environment_images 按同一 GIT_SHA tag 组构建，
    # compose run 直接复用已构建 backend 镜像 + Live Mount 挂载运行迁移代码。
    if ! run_with_timeout "migration" "${TIMEOUT_MIGRATION_SECONDS}" -- \
        ${COMPOSE_CMD} run --rm --no-deps backend bash -c "cd /app && alembic upgrade head"; then
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
        _wait_health || return 1
    fi
    _wave_up frontend "${_wave_frontend[@]}" || return 1
    _wave_up scheduler "${_wave_scheduler[@]}" || return 1
    if [[ ${#_wave_scheduler[@]} -gt 0 ]]; then
        _check_scheduler_single_instance || return 1
    fi
    _wave_up workers "${_wave_workers[@]}" || return 1
    _wave_up recovery "${_wave_recovery[@]}" || return 1
    _wave_up capture "${_wave_capture[@]}" || return 1
    return 0
}

# [ROUND2 / P1-B] Compose-only 运行时配置对账：不 --force-recreate，交给 Docker
# Compose 自身判断哪些服务配置变化并仅重创那些。与 restart_services 的 force-recreate
# 语义解耦（后者仍用于真正的代码/环境/Migration 重启）。不重启数据服务。
reconcile_compose_runtime() {
    local services=("$@")
    if [[ ${#services[@]} -eq 0 ]]; then
        log "无需 Compose 配置对账"
        return 0
    fi
    # 标记必须在首次实际发起 compose up 之前置位：一旦 up 开始，容器状态即可能已被改变。
    # 与 restart_services 相同的安全语义，避免部分 Compose 变更后命令失败时
    # 被误分类为「服务未重启」（main 据此决定容器级回滚 vs 仅文件恢复）。
    SERVICES_RESTARTED=true
    log "Compose 配置对账（不强制重建，由 Compose 自行判断变更服务）: ${services[*]}"
    cd "${REPO_ROOT}"
    run_with_timeout "compose_reconcile" "${TIMEOUT_COMPOSE_UP_SECONDS}" -- \
        ${COMPOSE_CMD} up -d --no-build "${services[@]}" || return 1
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

# =============================================================================
# [DEPLOY-GATE] 活跃盘后长任务 fail-closed 门禁（ref/guide.md REOPEN NARROW RUNTIME-SAFETY FIX）
# =============================================================================
# 目标：当本次部署会变更 backend runtime / 重启 Python 服务（含 force-recreate
# worker-after-close）时，在任何 live runtime mutation 之前，检查 worker-after-close
# 当前是否拥有活跃的 long-running business item。若活跃 → 立即拒绝部署（fail-closed），
# 避免 Docker stop_grace_period 到期后 SIGKILL 仍在执行的 chip/bootstrap/auction/after-close
# 任务，造成 ownership 不清的 running job。
#
# 活跃定义（来自 SchedulerJobRun 当前状态机真值，非容器/心跳/日志推断）：status='running'。
#   queued / resume_queued 不是活跃执行，不机械阻塞（见 guide ACTIVE DEFINITION）。
#
# 该 job_name 集合 = 所有在 worker-after-close 进程内执行的业务任务，但按
# 部署中断语义分治（[REVIEW-V2 / DEPLOY-GATE-REINTRODUCES-CHIP-PRIORITY-INVERSION]）：
#
# BLOCKING_AFTER_CLOSE_JOB_NAMES（强制阻塞类，运行即 fail-closed）：
#   - after_close_orchestrator   # mandatory after-close / Review 链路
#   - review_bootstrap
#   - auction_final
#   - auction_open_confirmation
#   PRD30 AC-14/AC-14b、PRD31 PC-03/PC-30：不得为部署中断正在运行的强制盘后/Review 任务。
#
# PREEMPTIBLE_AFTER_CLOSE_JOB_NAMES（可抢占增强类，运行不阻塞部署，仅记录）：
#   - after_close_chip_consensus # Chip 是 enhancement，不阻塞 stock_core/board/Review；
#                                其执行可随 worker-after-close 重建而中断，并复用
#                                resume_queued 重跑已成功的标的（FIX C 复用既有语义）。
# 本回合只豁免 Chip；review_bootstrap/auction_final/auction_open_confirmation 的中断语义
# 不在本 blocker 范围内，不得自动豁免。
#
# 注意：本门禁只读、fail-fast，绝不等待业务任务完成（FIX C），也绝不扩大
#   PANJI_APP_HEAVY_STOP_GRACE / PANJI_TIMEOUT_COMPOSE_UP_SECONDS 为业务任务超时（FIX D）。
BLOCKING_AFTER_CLOSE_JOB_NAMES=(
    after_close_orchestrator
    review_bootstrap
    auction_final
    auction_open_confirmation
)
PREEMPTIBLE_AFTER_CLOSE_JOB_NAMES=(
    after_close_chip_consensus
)

# 本次部署是否会变更 backend runtime / 重启 Python 服务（含 worker-after-close）。
# 仅在为 true 时才启用活跃盘后任务门禁；frontend-only 部署不受无关活跃任务阻塞（FIX F CASE 6）。
_backend_runtime_will_mutate() {
    # [test-hook] 合同测试可强制视为 backend runtime 将变更（否则需真实 git diff）
    [[ "${PANJI_MOCK_BACKEND_RUNTIME_CHANGED:-0}" == "1" ]] && return 0
    [[ "${BACKEND_RUNTIME_CHANGED}" == "true" \
        || "${BACKEND_ENVIRONMENT_CHANGED}" == "true" \
        || "${CAPTURE_ENVIRONMENT_CHANGED}" == "true" \
        || "${MIGRATION_CHANGED}" == "true" \
        || "${COMPOSE_RUNTIME_CHANGED}" == "true" ]]
}

# 从 ENV_FILE 读取 compose 的 POSTGRES_USER / POSTGRES_DB（未定义时回退默认 bz / bz_stock）。
_pg_conn_var() {
    local key="$1"
    local default="$2"
    grep -E "^${key}=" "${ENV_FILE}" | head -n1 | cut -d= -f2- | tr -d '[:space:]' \
        || echo "${default}"
}

# [DEPLOY-GATE] 只读活跃盘后任务检查。
# - 无业务写入，无直接状态变更。
# - 查询失败（无法连接/解析 psql 失败）→ fail-closed 拒绝部署。
# - 强制阻塞类（BLOCKING_AFTER_CLOSE_JOB_NAMES）存在 status='running' 的活跃任务
#   → 打印结构化证据并 fail（ACTIVE_AFTER_CLOSE_JOB_BLOCKS_DEPLOY）。
# - 可抢占增强类（PREEMPTIBLE_AFTER_CLOSE_JOB_NAMES，仅 after_close_chip_consensus）
#   运行时不阻塞部署（PRD30 AC-14b / PRD31 PC-30：Chip 是 enhancement，不阻塞
#   stock_core/board/Review），仅记录 PREEMPTIBLE_ENHANCEMENT_ACTIVE 并继续。

# 将数组名引用的 job_name 列表拼成 SQL IN 字面量：'a','b',...
_job_name_filter() {
    local -n _arr="$1"
    local quoted=() name
    for name in "${_arr[@]}"; do
        quoted+=("'${name}'")
    done
    IFS=,; echo "${quoted[*]}"
}

_state_filter() {
    local -n _arr="$1"
    local quoted=() st
    for st in "${_arr[@]}"; do
        quoted+=("'${st}'")
    done
    IFS=,; echo "${quoted[*]}"
}

# [E2.1 P1-B] 两套状态必须严格区分，不得混为同一个名字：
#
# WORKER_CLAIMABLE_STATES
#   = worker 主循环**会主动领取**的状态（claim 语义）
#   权威 owner：backend/app/worker.py::_after_close_poll_once
#     WHERE job_name='after_close_orchestrator'
#       AND status IN ('queued','resume_queued')
#   本列表不得独立演化，由 scripts/ops/test-panji-test-deploy-contracts.sh
#   用结构化读取 worker.py 锁定。
#
# DEPLOYMENT_CONFLICT_STATES
#   = 部署临界区内**不允许存在**的状态（冲突语义）
#   = WORKER_CLAIMABLE_STATES ∪ { running }
#   running 不是"可被领取"，而是"正在执行"：部署不得杀掉它，
#   因此它同样构成部署冲突（§14 DO NOT KILL REAL PROGRESS）。
#
# 背景：旧门禁只查 status='running'，但 worker 在容器起来的第一个 5s 轮询
# 就会 claim queued / resume_queued —— 部署把 worker 拉起等于授权它捡走积压
# 任务（2026-08-29 生产事故：积压 43 小时的 queued 任务在部署后 9 秒被领取）。
WORKER_CLAIMABLE_STATES=(queued resume_queued)
DEPLOYMENT_CONFLICT_STATES=(running queued resume_queued)

guard_active_after_close_jobs() {
    if ! _backend_runtime_will_mutate; then
        log "backend runtime 不变化（不重启 Python 服务），跳过活跃盘后任务门禁"
        return 0
    fi

    log "部署将变更 backend runtime / 重启 Python 服务，检查 worker-after-close 活跃长任务..."

    local pg_user pg_db
    pg_user="$(_pg_conn_var "POSTGRES_USER" "bz")"
    pg_db="$(_pg_conn_var "POSTGRES_DB" "bz_stock")"
    if [[ -z "${pg_user}" || -z "${pg_db}" ]]; then
        fail "ACTIVE_AFTER_CLOSE_JOB_GATE_UNAVAILABLE: 无法从 ${ENV_FILE} 读取 POSTGRES_USER/POSTGRES_DB，拒绝部署（fail-closed）"
    fi

    # 构造 IN 列表：'a','b',...
    local blocking_filter
    blocking_filter="$(_job_name_filter BLOCKING_AFTER_CLOSE_JOB_NAMES)"
    local state_filter
    state_filter="$(_state_filter DEPLOYMENT_CONFLICT_STATES)"

    # [E2.1 P1-B §9] pre-deploy visibility：按状态分别统计，给 operator 足够上下文
    local count_out=""
    if ! count_out="$(docker exec trading-postgres psql -U "${pg_user}" -d "${pg_db}" \
        -tA -F ':' \
        -c "SELECT status, count(*) FROM scheduler_job_runs \
            WHERE status IN (${state_filter}) AND job_name IN (${blocking_filter}) \
            GROUP BY status ORDER BY status" 2>/dev/null)"; then
        fail "ACTIVE_AFTER_CLOSE_JOB_GATE_UNAVAILABLE: 无法统计 scheduler_job_runs（psql 失败），拒绝部署（fail-closed）"
    fi
    log "PRE_DEPLOY_PENDING_VISIBILITY（见 DEPLOYMENT_CONFLICT_STATES / WORKER_CLAIMABLE_STATES）:"
    local _st _n _s _c
    for _st in "${DEPLOYMENT_CONFLICT_STATES[@]}"; do
        _n=0
        while IFS=: read -r _s _c; do
            [[ "${_s}" == "${_st}" ]] && _n="${_c}"
        done <<< "${count_out}"
        log "  ${_st}_COUNT=${_n}"
    done

    local blocking_out=""
    if ! blocking_out="$(docker exec trading-postgres psql -U "${pg_user}" -d "${pg_db}" \
        -tA -F ' | ' \
        -c "SELECT id, job_name, business_date, status, heartbeat_at \
            FROM scheduler_job_runs \
            WHERE status IN (${state_filter}) AND job_name IN (${blocking_filter}) \
            ORDER BY started_at" 2>/dev/null)"; then
        fail "ACTIVE_AFTER_CLOSE_JOB_GATE_UNAVAILABLE: 无法查询 scheduler_job_runs（psql 失败），拒绝部署（fail-closed）"
    fi

    if [[ -n "${blocking_out}" ]]; then
        log "!!! 检测到 worker-after-close 待处理/正在执行的任务（仅可见性；由 supervisor-drain fence 处理，不阻塞部署） !!!"
        log "DEPLOYMENT_PENDING_AFTER_CLOSE_VISIBLE=TRUE"
        log "worker 可领取状态（claimable）: ${WORKER_CLAIMABLE_STATES[*]}"
        log "部署冲突状态（conflict）: ${DEPLOYMENT_CONFLICT_STATES[*]}"
        log "说明：queued / resume_queued 由 supervisor-drain fence 容忍（fence 后不得被 claim，"
        log "      fence 前已合法 claim 的可 running→terminal）；running 由 fence 经 stop -t -1 自然 drain。"
        log "[E2.1 §11] 部署工具**不会** kill / cancel / reset 正在推进的业务任务。"
        log "blocking_active_jobs:"
        while IFS= read -r row; do
            [[ -n "${row}" ]] && log "  ${row}"
        done <<< "${blocking_out}"
    fi

    # (2) 可抢占增强类（仅 Chip）：运行不阻塞部署，仅记录并继续（不 fail）。
    local preempt_filter
    preempt_filter="$(_job_name_filter PREEMPTIBLE_AFTER_CLOSE_JOB_NAMES)"
    local preempt_out=""
    if ! preempt_out="$(docker exec trading-postgres psql -U "${pg_user}" -d "${pg_db}" \
        -tA -F ' | ' \
        -c "SELECT id, job_name, business_date, heartbeat_at \
            FROM scheduler_job_runs \
            WHERE status = 'running' AND job_name IN (${preempt_filter}) \
            ORDER BY started_at" 2>/dev/null)"; then
        fail "ACTIVE_AFTER_CLOSE_JOB_GATE_UNAVAILABLE: 无法查询 scheduler_job_runs（psql 失败），拒绝部署（fail-closed）"
    fi

    if [[ -n "${preempt_out}" ]]; then
        log "PREEMPTIBLE_ENHANCEMENT_ACTIVE"
        log "Chip(after_close_chip_consensus) 为增强类任务，可随 worker-after-close 重建而中断并重试，不阻塞部署"
        log "preemptible_active_jobs:"
        while IFS= read -r row; do
            [[ -n "${row}" ]] && log "  ${row}"
        done <<< "${preempt_out}"
    fi

    log "无强制阻塞盘后长任务，继续部署"
}

# ---------------------------------------------------------------------------
# [E2.1 P1-C] supervisor-drain 进程 fence —— 单 owner 进程 fence
#
# 唯一 after-close pickup actor 是 worker-after-close（单 service / 单进程 / 单 poll loop），
# 其 claim 已 inline FOR UPDATE SKIP LOCKED（worker.py::_after_close_poll_once）。
# SIGTERM 已令 _shutdown=True 停止新 claim，当前 job 自然 drain 到 terminal（非 kill）。
# 故 fence 只需在 backend runtime mutation 前 `stop -t -1` 停掉整个进程（覆盖 60s grace，
# 永不 SIGKILL），并等待容器 EXITED/missing 且 after_close running==0（线性化点）。
# 不依赖 admission 表/锁/migration。queued/resume_queued 允许留队（fence 后不得被 claim，
# fence 前已合法 claim 的可 running→terminal）。
# ---------------------------------------------------------------------------

_after_close_container_status() {
    # 输出 running / exited / created / missing / unknown
    local name="trading-worker-after-close"
    local s
    s="$(docker inspect -f '{{.State.Status}}' "${name}" 2>/dev/null)" || { printf 'missing'; return 0; }
    case "${s}" in
        running|exited|created) printf '%s' "${s}" ;;
        paused|restarting) printf 'running' ;;   # 视为活跃，需要 stop
        *) printf 'unknown' ;;
    esac
}

_after_close_running_count() {
    # after_close running 任务数（queued/resume_queued 不算）
    local pg_user pg_db
    pg_user="$(_pg_conn_var "POSTGRES_USER" "bz")"
    pg_db="$(_pg_conn_var "POSTGRES_DB" "bz_stock")"
    [[ -n "${pg_user}" && -n "${pg_db}" ]] || return 1
    local out
    if ! out="$(docker exec trading-postgres psql -U "${pg_user}" -d "${pg_db}" \
        -tA -F ':' \
        -c "SELECT count(*) FROM scheduler_job_runs \
            WHERE status = 'running' AND job_name IN ($(_job_name_filter BLOCKING_AFTER_CLOSE_JOB_NAMES))" 2>/dev/null)"; then
        return 1
    fi
    printf '%s' "${out}" | head -1
}

_after_close_container_exited_or_missing() {
    local status
    status="$(_after_close_container_status)" || return 1
    [[ "${status}" == "exited" || "${status}" == "missing" ]]
}

_fence_after_close_worker() {
    # 线性化点 = 容器 EXITED/missing 且 after_close running==0（非 SIGTERM）。
    # running → stop -t -1（无限 graceful，覆盖 60s grace，绝不 SIGKILL），FENCE_OWNED=true。
    # exited/created/missing → WAS_RUNNING=false（owned=false）。
    # 无 fixed business timeout：仅 deploy 级 fail-closed 看门狗，绝不 SIGKILL。
    local status
    status="$(_after_close_container_status)" || { log "FENCE_INSPECT_FAILED=true"; return 1; }
    case "${status}" in
        running)
            AFTER_CLOSE_WAS_RUNNING=true
            log "[E2.1 P1-C] fencing worker-after-close: stop -t -1（graceful drain，绝不 SIGKILL）"
            if ! ${COMPOSE_CMD} stop -t -1 worker-after-close; then
                log "AFTER_CLOSE_FENCE_STOP_FAILED=true"; return 1
            fi
            AFTER_CLOSE_FENCE_OWNED=true
            ;;
        exited|created|missing)
            AFTER_CLOSE_WAS_RUNNING=false
            AFTER_CLOSE_FENCE_OWNED=false
            ;;
        *) log "FENCE_UNKNOWN_WORKER_STATE=${status}"; return 1 ;;
    esac

    # 等待容器 EXITED/missing（无 fixed business timeout；deploy 级看门狗仅在 truly stuck 时
    # fail-closed，绝不 SIGKILL）。
    local waited=0
    while ! _after_close_container_exited_or_missing; do
        if [[ ${waited} -ge ${PANJI_FENCE_MAX_WAIT_SECONDS:-1800} ]]; then
            log "AFTER_CLOSE_FENCE_WAIT_TIMEOUT=true（deploy 级看门狗，未 SIGKILL）"; return 1
        fi
        sleep 5
        waited=$((waited + 5))
    done

    # 线性化点：容器已退出且 after_close running==0
    local n
    n="$(_after_close_running_count 2>/dev/null)" || { log "FENCE_RUNNING_COUNT_UNAVAILABLE=true"; return 1; }
    if [[ "${n:-0}" != "0" ]]; then
        log "AFTER_CLOSE_FENCE_RUNNING_REMAINS=${n}"; return 1
    fi
    AFTER_CLOSE_PICKUP_FENCED=true
    log "AFTER_CLOSE_PICKUP_FENCED=true（worker-after-close 已 drained，after_close running=0）"
}

_restore_after_close_pickup_if_owned() {
    # 仅本 deploy 真正 stop -t -1 停掉的 worker 才恢复（owned-aware）。
    # 进入部署时 worker 原本 stopped/missing 则成功/rollback 都不得擅自 up。
    [[ "${AFTER_CLOSE_FENCE_OWNED}" == "true" ]] || { log "AFTER_CLOSE_RESTORE_SKIPPED_NOT_OWNED=true"; return 0; }
    log "[E2.1 P1-C] restoring worker-after-close（本 deploy 自己 fenced 的 worker）"
    if ! ${COMPOSE_CMD} up -d --force-recreate worker-after-close; then
        log "AFTER_CLOSE_PICKUP_RESTORE_FAILED=true"; log "MANUAL_INTERVENTION_REQUIRED=true"; return 1
    fi
    local waited=0
    while [[ "$(_after_close_container_status)" != "running" ]]; do
        if [[ ${waited} -ge ${PANJI_FENCE_MAX_WAIT_SECONDS:-1800} ]]; then
            log "AFTER_CLOSE_RESTORE_WAIT_TIMEOUT=true"; return 1
        fi
        sleep 5
        waited=$((waited + 5))
    done
    AFTER_CLOSE_FENCE_OWNED=false
    # 状态机一致性：恢复成功后 worker 正在运行（容器非 EXITED/missing、running!=0），
    # 故 PICKUP_FENCED 必须回到 false，否则该变量会与真实容器状态背离（状态机缺口）。
    AFTER_CLOSE_PICKUP_FENCED=false
    log "AFTER_CLOSE_PICKUP_FENCED=false（worker 已恢复运行，after_close running!=0）"
    log "AFTER_CLOSE_PICKUP_RESTORED=true"
}

# ---------------------------------------------------------------------------
# [E2.1 P1-A] Pre-deploy runtime manifest —— Live-Mount composite rollback owner
#
# 当前是 Live-Mount 运行时，**运行中的 runtime 由多个 owner 复合决定**：
#   A. repo / live-mounted 代码 identity
#   B. RUNTIME_SHA（Live Mount 运行时标识）
#   C. 每个受影响服务的 container runtime identity（immutable image content ID，
#      **不是** mutable tag —— Phase E 已实测运行镜像组可能既非 target 也非
#      previous deploy SHA，会被 cleanup_resources() 精确回收）
#   D. effective compose runtime definition digest
#
# 因此 "git checkout PREVIOUS_SHA" **不等于**恢复部署前真实 runtime。
# 必须在任何 destructive runtime mutation 之前一次性解析并固化上述 owner；
# 任一项 mandatory owner 无法解析 → STOP BEFORE MUTATION，不允许
# "先部署、失败后再想办法"，也禁止通过 latest / 当前 mutable tag /
# 重新 build 旧源码来猜 rollback target。
# ---------------------------------------------------------------------------
PRE_DEPLOY_MANIFEST_FILE=""
PRE_DEPLOY_RUNTIME_OWNER_RESOLVED=false

_write_manifest() {
    printf '%s=%s\n' "$1" "$2" >>"${PRE_DEPLOY_MANIFEST_FILE}"
}

# [E2.1 P1-A §4] 在 checkout_target **之前**捕获 repo 派生的 PRE_DEPLOY owner。
#
# PRE_DEPLOY_REPO_SHA 与 PRE_DEPLOY_COMPOSE_DIGEST 都从 REPO_ROOT 读取
# （git rev-parse HEAD / compose config）。一旦 checkout 到 TARGET_SHA，读到的就是
# candidate(B)，而不是真正 mutation 前的 old runtime(A)。manifest 会在「回滚依据」
# 的名义下记录 B，而 verify_rollback_owner 是对着 manifest 比对的 —— 于是 rollback
# 会**假通过**，这比没有 manifest 更危险。
#
# 这里只捕获、**不做** fail-closed 判定：判定仍由 deploy() 内的
# resolve_pre_deploy_runtime_owner 统一负责。若在 checkout 之前就 fail 退出，
# deploy() 根本不会执行，会把构建 / 同步 / 门禁 / 回滚全部短路掉。
capture_pre_checkout_repo_owners() {
    PRE_CHECKOUT_REPO_SHA="$(cd "${REPO_ROOT}" && git rev-parse HEAD 2>/dev/null || true)"
    # 取不到时优雅留空，交给 resolve 的显式 missing 判定，避免 set -e 中断。
    PRE_CHECKOUT_COMPOSE_DIGEST="$(
        cd "${REPO_ROOT}" && ${COMPOSE_CMD} config 2>/dev/null | sha256sum | awk '{print $1}'
    )" || PRE_CHECKOUT_COMPOSE_DIGEST=""
    log "pre-checkout owner 捕获: repo_sha=${PRE_CHECKOUT_REPO_SHA:-<none>}"
    log "pre-checkout owner 捕获: compose_digest=${PRE_CHECKOUT_COMPOSE_DIGEST:-<none>}"
}

resolve_pre_deploy_runtime_owner() {
    PRE_DEPLOY_MANIFEST_FILE="${PRE_DEPLOY_MANIFEST_FILE:-$(mktemp)}"
    : >"${PRE_DEPLOY_MANIFEST_FILE}"

    local missing=()
    local repo_sha runtime_sha service image_id compose_digest

    # A. repo code identity
    #    必须取 checkout **之前**捕获的 old runtime，而不是当前（已 checkout 到
    #    TARGET_SHA 的）candidate。见 capture_pre_checkout_repo_owners。
    repo_sha="${PRE_CHECKOUT_REPO_SHA:-}"
    # [E2.1 P1-A §2] 捕获值为空 = mandatory owner missing，FAIL CLOSED。
    # 严禁 fallback 到 `git rev-parse HEAD`：本函数在 checkout_target **之后**运行，
    # 此时 HEAD 已是 candidate(B)。fallback 会让 B 冒充 PRE_DEPLOY A，而
    # verify_rollback_owner 是对着 manifest 比对的 —— 将产生 false-green 回滚。
    [[ -z "${repo_sha}" ]] && missing+=("PRE_DEPLOY_REPO_SHA")
    _write_manifest PRE_DEPLOY_REPO_SHA "${repo_sha}"

    # B. RUNTIME_SHA
    #    语义分层：
    #      - LIVE_ROOT 目录都不存在 → **无前 live runtime**（如 dry-run、尚未挂载），
    #        没有可回滚对象，属合法无前状态，不强制；
    #      - LIVE_ROOT 存在但 RUNTIME_SHA 缺失 → 异常，必须 fail-closed；
    #      - 首次 Live Mount → 合法无前状态。
    runtime_sha=""
    if [[ -f "${LIVE_ROOT}/RUNTIME_SHA" ]]; then
        runtime_sha="$(tr -d '[:space:]' <"${LIVE_ROOT}/RUNTIME_SHA")"
    fi
    if [[ -d "${LIVE_ROOT}" && ! -f "${LIVE_ROOT}/RUNTIME_SHA" \
        && "${FIRST_LIVE_DEPLOY}" != "true" ]]; then
        missing+=("PRE_DEPLOY_RUNTIME_SHA")
    fi
    if [[ ! -d "${LIVE_ROOT}" ]]; then
        _write_manifest PRE_DEPLOY_NO_PRIOR_LIVE_RUNTIME true
    fi
    _write_manifest PRE_DEPLOY_RUNTIME_SHA "${runtime_sha}"

    # C. per-service immutable container runtime identity
    #    首次 Live Mount 时容器尚不存在，属合法无前状态。
    local services=("${PYTHON_SERVICES[@]}" frontend)
    for service in "${services[@]}"; do
        image_id="$(docker inspect "${service}" --format '{{.Image}}' 2>/dev/null || true)"
        if [[ -z "${image_id}" && "${FIRST_LIVE_DEPLOY}" != "true" ]]; then
            missing+=("PRE_DEPLOY_IMAGE_ID:${service}")
        fi
        _write_manifest "PRE_DEPLOY_IMAGE_ID:${service}" "${image_id}"
        # 同时记录 compose 会解析的 **镜像引用**（repo:tag，如 market-dev-backend:<sha>）。
        # content ID 是 source of truth，但 compose 用 tag 寻址，rollback 必须能把
        # 该 tag 重新钉到捕获的 content ID（禁止 latest / candidate / 重建旧源码）。
        local image_ref=""
        image_ref="$(docker inspect "${service}" --format '{{.Config.Image}}' 2>/dev/null || true)"
        _write_manifest "PRE_DEPLOY_IMAGE_REF:${service}" "${image_ref}"
    done

    # D. effective compose runtime definition digest（不用文件 mtime）
    compose_digest=""
    # 取不到时必须**优雅**地留空并交给下面的显式 missing 判定处理；若让命令替换以
    # 非零退出，会在 set -e 下直接中断脚本，从而绕过 fail-closed 报告。
    #    同理必须取 checkout 之前的 compose 定义（compose 文件来自 repo 树）。
    compose_digest="${PRE_CHECKOUT_COMPOSE_DIGEST:-}"
    # [E2.1 P1-A §2] 同理严禁 fallback：compose 文件来自 repo 树，此刻已是
    # candidate(B) 的定义，fallback 会把 DIGEST_B 记为 PRE_DEPLOY owner。
    [[ -z "${compose_digest}" ]] && missing+=("PRE_DEPLOY_COMPOSE_DIGEST")
    _write_manifest PRE_DEPLOY_COMPOSE_DIGEST "${compose_digest}"

    if [[ "${#missing[@]}" -gt 0 ]]; then
        log "!!! PRE-DEPLOY ROLLBACK OWNER 无法完整解析，STOP BEFORE MUTATION !!!"
        log "ROLLBACK_OWNER_RESOLVED_BEFORE_MUTATION=FAIL"
        log "缺失 mandatory owner:"
        local m
        for m in "${missing[@]}"; do log "  ${m}"; done
        return 1
    fi

    PRE_DEPLOY_RUNTIME_OWNER_RESOLVED=true
    log "ROLLBACK_OWNER_RESOLVED_BEFORE_MUTATION=PASS"
    log "pre-deploy runtime manifest:"
    sed 's/^/  /' "${PRE_DEPLOY_MANIFEST_FILE}"
    return 0
}

# rollback() 完成调用 ≠ rollback successful。必须由独立 verify owner 判定。
verify_rollback_owner() {
    local rc=0
    local key expected actual

    if [[ -z "${PRE_DEPLOY_MANIFEST_FILE}" || ! -f "${PRE_DEPLOY_MANIFEST_FILE}" ]]; then
        log "ROLLBACK_STATUS=FAILED: 无 pre-deploy manifest，无法验证恢复"
        return 1
    fi

    # A. repo / live-mounted 代码 identity
    key="PRE_DEPLOY_REPO_SHA"
    expected="$(sed -n "s/^${key}=//p" "${PRE_DEPLOY_MANIFEST_FILE}")"
    actual="$(cd "${REPO_ROOT}" && git rev-parse HEAD 2>/dev/null || true)"
    if [[ -n "${expected}" && "${actual}" != "${expected}" ]]; then
        log "ROLLBACK_STATUS=FAILED: repo/live 代码 identity 未恢复（expected=${expected} actual=${actual}）"
        rc=1
    fi

    # B. RUNTIME_SHA
    key="PRE_DEPLOY_RUNTIME_SHA"
    expected="$(sed -n "s/^${key}=//p" "${PRE_DEPLOY_MANIFEST_FILE}")"
    actual=""
    [[ -f "${LIVE_ROOT}/RUNTIME_SHA" ]] && actual="$(tr -d '[:space:]' <"${LIVE_ROOT}/RUNTIME_SHA")"
    if [[ -n "${expected}" && "${actual}" != "${expected}" ]]; then
        log "ROLLBACK_STATUS=FAILED: RUNTIME_SHA 未恢复（expected=${expected} actual=${actual}）"
        rc=1
    fi

    # D. effective compose runtime definition digest
    #    恢复机制是文件层恢复（compose 文件属于 repo/live 文件 owner），
    #    因此这里重新计算 digest 并与 manifest 对称比对。
    key="PRE_DEPLOY_COMPOSE_DIGEST"
    expected="$(sed -n "s/^${key}=//p" "${PRE_DEPLOY_MANIFEST_FILE}")"
    actual="$(
        cd "${REPO_ROOT}" && ${COMPOSE_CMD} config 2>/dev/null | sha256sum | awk '{print $1}'
    )" || actual=""
    if [[ -n "${expected}" && "${actual}" != "${expected}" ]]; then
        log "ROLLBACK_STATUS=FAILED: effective compose runtime definition 未恢复（expected=${expected} actual=${actual}）"
        rc=1
    fi

    # C. per-service container runtime identity
    while IFS= read -r line; do
        local service exp act
        service="${line#PRE_DEPLOY_IMAGE_ID:}"
        service="${service%%=*}"
        exp="${line#*=}"
        [[ -z "${exp}" ]] && continue
        act="$(docker inspect "${service}" --format '{{.Image}}' 2>/dev/null || true)"
        if [[ "${act}" != "${exp}" ]]; then
            log "ROLLBACK_STATUS=FAILED: ${service} 容器运行时未恢复（expected=${exp} actual=${act}）"
            rc=1
        fi
    done < <(grep '^PRE_DEPLOY_IMAGE_ID:' "${PRE_DEPLOY_MANIFEST_FILE}")

    if [[ "${rc}" -eq 0 ]]; then
        log "ROLLBACK_STATUS=SUCCESS"
    else
        log "ROLLBACK_STATUS=FAILED"
        log "MANUAL_INTERVENTION_REQUIRED=TRUE"
    fi
    return "${rc}"
}

# [E2.1 P1-A §6] 真实 runtime mutation 阶段的线性化标记。
#
# 只能由**真正执行写入**的 mutator 自己推进。禁止在任何预检 / 分类 / 构建 /
# 纯检查步骤之前提前标 files —— 否则这些步骤失败时，failure handler 会误判
# "文件已被改动、需要恢复"，从而主动执行 restore，凭空制造出 env / live rsync /
# RUNTIME_SHA 三处 mutation。这正是 P1-A 要闭合的缺陷，提前标记等于把它换个位置
# 重新引入。
#
# 因此：
#   build_environment_images / build_frontend_dist 属**构建**，不推进 stage；
#   update_env_file / sync_* / write_runtime_sha 属**文件层写入**，推进到 files；
#   restart / recreate 属**容器层写入**，推进到 containers。
_mark_files_mutated() {
    if [[ "${MUTATION_STAGE}" == "none" ]]; then
        MUTATION_STAGE="files"
        log "  [mutation] 进入文件层 runtime mutation 区间（stage=files）"
    fi
}

_mark_containers_mutated() {
    if [[ "${MUTATION_STAGE}" != "containers" ]]; then
        MUTATION_STAGE="containers"
        log "  [mutation] 进入容器层 runtime mutation 区间（stage=containers）"
    fi
}

deploy() {
    # 0. 活跃盘后任务 fail-closed 门禁（FIX B）：
    #    仅在 backend runtime 会变更时启用，并在任何 live runtime mutation
    #    （update_env_file / sync_backend_runtime / write_runtime_sha / migration / restart）
    #    以及替换 /opt/panji-live/backend/app 之前执行。活跃任务 → 立即停止部署。
    FAILURE_STAGE="active_job_gate"
    guard_active_after_close_jobs

    # [RESOURCE_GATE_ORDER_DEBT] 部署内存 headroom 必须在 supervisor-drain fence 之后、
    # 首笔 runtime mutation 之前检查。fence 释放 worker-after-close 的 ~942MB anon 后，
    # 才是本次部署真实可用的 working set；不得在任何 fence 之前用 MemAvailable 门槛阻止部署
    # （那会阻止本可安全回收内存的部署）。MIN_MEM_MB 仍是保守部署 headroom，本轮不降低。
    if _backend_runtime_will_mutate; then
        _fence_after_close_worker || return 1
        FAILURE_STAGE="deployment_memory_headroom"
        check_deployment_memory_headroom || return 1
    else
        # frontend-only / 非 backend runtime 变更：不 fence worker（不得停止生产服务），
        # 但首笔内存密集 mutation（frontend build/dist）前仍需 headroom 门槛。
        FAILURE_STAGE="deployment_memory_headroom"
        check_deployment_memory_headroom || return 1
    fi

    # [E2.1 P1-A] 在任何 destructive runtime mutation 之前固化 rollback owner。
    #    必须早于 update_env_file / build / sync / write_runtime_sha /
    #    migration / restart；任一 mandatory owner 无法解析即 STOP（fail-closed）。
    FAILURE_STAGE="pre_deploy_rollback_owner"
    # 主流程已在 checkout_target 之前完成捕获（见 main 中 §4 捕获点），此处仅作
    # 防御性兜底：已解析则跳过，避免重复捕获把 candidate 当成 PRE_DEPLOY owner。
    if [[ "${PRE_DEPLOY_RUNTIME_OWNER_RESOLVED}" != "true" ]]; then
        resolve_pre_deploy_runtime_owner || return 1
    fi

    # 1. 运行环境镜像：任意 environment_changed → 按同一 GIT_SHA tag 组整体构建。
    #    注意：MUTATION_STAGE 不在这里提前推进——构建属**非文件层写入**，
    #    真正的 mutation stage 由各 mutator 自己推进（见 _mark_files_mutated）。
    #    无 environment_changed → 零构建，GIT_SHA 保持不变。
    # [E2.1 P1-C] 线性化点门禁：紧邻第一笔 runtime mutation（update_env_file）之前，
    # 再次确认 AFTER_CLOSE_PICKUP_FENCED=true（容器已 EXITED 且 after_close running==0）。
    # queued/resume_queued 允许留队，不作为 blocker。
    if _backend_runtime_will_mutate; then
        if [[ "${AFTER_CLOSE_PICKUP_FENCED}" != "true" ]]; then
            fail "AFTER_CLOSE_PICKUP_NOT_FENCED: 部署临界区未建立，拒绝 runtime mutation（fail-closed）"
        fi
    fi

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
        # [E2.1 P1-C] supervisor-drain fence 已在 deploy() 开头建立（早于 migration），
        # 无需 first-install bootstrap acquire；migration 后重启会由 owned-aware restore 处理。
    else
        log "migration_changed=false，跳过 alembic upgrade"
    fi

    # 6. 重启：Python 服务与 frontend 分别判定；postgres/redis/umami 永不重启
    #
    # [DEPLOY ACTIVE-JOB GATE — NARROW TOCTOU CLOSURE] 在每个可能
    # restart / recreate worker-after-close 的 runtime action 之前，复用同一
    # guard_active_after_close_jobs 做 fresh fail-closed 门禁。第一道 guard 在 deploy()
    # 开头（任何 live mutation 之前）已执行；此处对每个实际 action 分别再加 final guard，
    # 以缩小 gate→action 之间的 admission TOCTOU 窗口（本次真实 8/25 reprocess 暴露此窗口）。
    #
    # 注意语义差异（避免误述）：
    #   - restart_services 使用 docker compose up -d --force-recreate（会强制重建受影响服务）；
    #   - reconcile_compose_runtime 使用 up -d --no-build（不 --force-recreate），
    #     但 Compose 可能因配置变化 recrecreate/restart 受影响服务，故同样需要 final guard。
    # 若本次只有一种 runtime action，则不会产生无意义第三次检查。
    # guard 自身已在 backend runtime 不变化时直接放行（frontend-only 不被无关活跃任务阻塞）。
    FAILURE_STAGE="restart"
    local restart_list=()
    if [[ "${need_backend}" == "true" ]]; then
        # [E2.1 P1-C] worker-after-close 由 owned-aware restore 处理，不在此通用重启中 recreate
        local _py_filtered=()
        local _s
        for _s in "${PYTHON_SERVICES[@]}"; do
            [[ "${_s}" == "worker-after-close" ]] && continue
            _py_filtered+=("${_s}")
        done
        restart_list+=("${_py_filtered[@]}")
        RESTARTED_PYTHON=true
    fi
    if [[ "${need_frontend}" == "true" ]]; then
        restart_list+=(frontend)
        RESTARTED_FRONTEND=true
    fi

    # [ROUND2 / P1-B] Compose 运行配置变化（prod/live overlay）属纯配置对账，
    # 不应 --force-recreate（否则会强制重建即便配置未变的容器，且可能因镜像缺失而失败）。
    # 交给 reconcile_compose_runtime：docker compose up -d --no-build <services>，
    # 由 Compose 自身判断哪些服务配置变化并仅重创那些（postgres/redis/umami 永不重启）。
    # 不触发镜像构建（STEP 5）、不触发 Migration（STEP 6）。
    if [[ "${COMPOSE_RUNTIME_CHANGED}" == "true" ]]; then
        RESTARTED_PYTHON=true
        RESTARTED_FRONTEND=true
        log "  Compose 运行配置变化：应用容器将以新 Compose 配置对账（PYTHON_SERVICES + frontend，不 force-recreate）"
    fi

    # 代码/环境/Migration 运行变化：restart_services（force-recreate）为权威路径。
    if [[ ${#restart_list[@]} -gt 0 ]]; then
        # final guard：紧邻 restart_services（--force-recreate）之前，复用同一 owner。
        # 显式 || return 1，保证门禁失败（存在活跃 after-close 强制任务）即 fail-closed 阻止重启，
        # 不依赖外层 set -e。
        FAILURE_STAGE="active_job_gate_pre_restart"
        if [[ "${AFTER_CLOSE_PICKUP_FENCED}" != "true" ]]; then
            fail "AFTER_CLOSE_PICKUP_NOT_FENCED: 部署临界区未建立，拒绝重启 worker-after-close（fail-closed）"
        fi
        FAILURE_STAGE="restart"
        # [E2.1 P1-A §6] 容器级 mutation 起点：此后失败必须走容器级精确回滚。
        _mark_containers_mutated
        restart_services "${restart_list[@]}" || return 1
    fi

    # Compose 运行配置变化（prod/live overlay）属纯配置对账，必须单独应用：
    # 即使 restart_list 已覆盖部分服务，仍可能有 backend/worker-after-close 等
    # Compose-only 变更未被 restart 命中。reconcile 使用 up -d --no-build（无
    # --force-recreate），Compose 会跳过已是最新的服务、只应用剩余 config-only 差异。
    if [[ "${COMPOSE_RUNTIME_CHANGED}" == "true" ]]; then
        # final guard：紧邻 reconcile_compose_runtime 之前，复用同一 owner。
        # reconcile 不 --force-recreate，但 Compose 可能因配置变化 recrecreate/restart 受影响服务，
        # 故仍需 final guard 拦截 reconcile 前新接纳的活跃 after-close 任务。
        # 显式 || return 1，保证门禁失败即 fail-closed 阻止配置对账。
        FAILURE_STAGE="active_job_gate_pre_reconcile"
        if [[ "${AFTER_CLOSE_PICKUP_FENCED}" != "true" ]]; then
            fail "AFTER_CLOSE_PICKUP_NOT_FENCED: 部署临界区未建立，拒绝 Compose 对账（fail-closed）"
        fi
        FAILURE_STAGE="compose_reconcile"
        local _recon_filtered=()
        local _rs
        for _rs in "${PYTHON_SERVICES[@]}"; do
            [[ "${_rs}" == "worker-after-close" ]] && continue
            _recon_filtered+=("${_rs}")
        done
        reconcile_compose_runtime "${_recon_filtered[@]}" frontend || return 1
    elif [[ ${#restart_list[@]} -eq 0 ]]; then
        log "无运行代码变化，不重启任何服务（仅刷新 RUNTIME_SHA 与核验）"
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

    # [test-seam, dry-run only] 强制最终稳态资源复检失败：仅当 after-close worker 已恢复运行
    # （即处于 main() 成功路径的最终复检阶段）时触发，用于验证最终健康失败后的 re-fence + rollback。
    # 真实部署不受影响（DRY_RUN!=true 时跳过）。
    if [[ "${DRY_RUN}" == "true" && "${PANJI_MOCK_POST_DEPLOY_FAIL_FINAL:-0}" == "1" ]]; then
        local _ws
        _ws="$(cat "${PANJI_MOCK_WORKER_STATE:-/tmp/panji_worker_state}" 2>/dev/null || echo "")"
        if [[ "${_ws}" == "running" ]]; then
            log "PANJI_MOCK_POST_DEPLOY_FAIL_FINAL: 强制最终稳态资源复检失败（仅 dry-run 测试）"
            FAILURE_STAGE="post_deploy_resource"
            return 1
        fi
    fi

    # 1. 主机资源 OBSERVATION（不是稳态 host MemAvailable>=4096 门槛）。
    #    deployment headroom 已在 fence 之后单独检查；此处仅观测 host 内存，
    #    不因此判失败（避免把"worker 被 fence 停掉时的临时状态"误判为稳态失败）。
    #    若未来确有 catastrophic host memory 阈值，应复用已有权威阈值；本 slice 不新设数字。
    local available_kb available_gb used_pct mem_kb mem_mb
    available_kb="$(df -Pk / | awk 'NR==2 {print $4}')"
    used_pct="$(df -Pk / | awk 'NR==2 {gsub(/%/, "", $5); print $5}')"
    mem_kb="$(awk '/^MemAvailable:/ {print $2; exit}' /proc/meminfo 2>/dev/null || echo 0)"
    available_gb=$((available_kb / 1024 / 1024))
    mem_mb=$((mem_kb / 1024))
    log "  host observation: disk_free=${available_gb}GB disk_used=${used_pct}% mem_available=${mem_mb}MB (observed, not a steady-state gate)"
    if [[ "${available_gb}" -lt "${MIN_DISK_GB}" || "${used_pct}" -gt "${MAX_DISK_PCT}" ]]; then
        FAILURE_STAGE="post_deploy_host_resource"
        log "错误: 部署后主机磁盘资源跌破阈值"
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

# [E2.1 P1-A §6/§7] 用捕获的 **immutable image content ID** 精确恢复每个受影响服务的运行时。
#
# compose 用 repo:tag 寻址（`market-dev-backend:${GIT_SHA}`），但 tag 是可变的；
# source of truth 必须是部署前捕获的 immutable content ID。因此这里把 compose
# 会解析的 image ref 重新 `docker tag` 钉到该 content ID。
#
# 明令禁止的 rollback target（§6）：
#   latest / 当前 mutable tag / 重新 build previous source / "compose up 后希望它碰巧还是旧 image"
#
# 逐服务处理，不得假设所有服务共用同一个 image（§7：backend/worker/frontend 可能不同）。
_pin_predeploy_image_refs() {
    [[ -n "${PRE_DEPLOY_MANIFEST_FILE}" && -f "${PRE_DEPLOY_MANIFEST_FILE}" ]] || return 0

    # [E2.1 P1-A §9] shared image_ref 冲突必须先判定，再决定如何恢复。
    #
    # 允许：A. service 级精确恢复（各服务 ref 互不相同，可逐个钉回）
    #       B. pre-mutation / pre-restore fail closed
    # 禁止：顺序 docker tag 同一个 ref —— 最后一个会静默覆盖前面所有服务，
    #       使 rollback 表面上成功、实际只恢复了其中一个服务。
    #
    # 因此：同一 ref 对应多个不同 immutable image_id 时，无法确定唯一正确目标，
    # 选 B：立即 fail closed，不做任何 tag。
    local -A ref_to_id=()
    local conflict=0
    while IFS= read -r line; do
        service="${line#PRE_DEPLOY_IMAGE_REF:}"
        service="${service%%=*}"
        image_ref="${line#*=}"
        [[ -n "${image_ref}" ]] || continue
        image_id="$(sed -n "s/^PRE_DEPLOY_IMAGE_ID:${service}=//p" "${PRE_DEPLOY_MANIFEST_FILE}" | head -1)"
        [[ -n "${image_id}" ]] || continue

        if [[ -n "${ref_to_id[${image_ref}]:-}" && "${ref_to_id[${image_ref}]}" != "${image_id}" ]]; then
            log "!! shared image_ref 冲突：ref=${image_ref} 同时对应 ${ref_to_id[${image_ref}]} 与 ${image_id}（service=${service}）!!"
            conflict=1
            continue
        fi
        ref_to_id["${image_ref}"]="${image_id}"
    done < <(grep '^PRE_DEPLOY_IMAGE_REF:' "${PRE_DEPLOY_MANIFEST_FILE}")

    if [[ "${conflict}" -ne 0 ]]; then
        log "ROLLBACK_STATUS=FAILED: shared image_ref 冲突，无法确定唯一 immutable 恢复目标"
        log "  未执行任何 docker tag —— 禁止用一个服务覆盖另一个服务的镜像引用"
        return 1
    fi

    local rc=0
    while IFS= read -r line; do
        service="${line#PRE_DEPLOY_IMAGE_REF:}"
        service="${service%%=*}"
        image_ref="${line#*=}"
        [[ -n "${image_ref}" ]] || continue
        image_id="$(sed -n "s/^PRE_DEPLOY_IMAGE_ID:${service}=//p" "${PRE_DEPLOY_MANIFEST_FILE}" | head -1)"
        [[ -n "${image_id}" ]] || continue

        if ! docker tag "${image_id}" "${image_ref}" >/dev/null 2>&1; then
            log "!! 无法把 ${service} 钉回 pre-deploy immutable image（id=${image_id} ref=${image_ref}）!!"
            rc=1
            continue
        fi
        log "  已恢复 ${service} 镜像引用 ${image_ref} → immutable image ${image_id}"
    done < <(grep '^PRE_DEPLOY_IMAGE_REF:' "${PRE_DEPLOY_MANIFEST_FILE}")

    return "${rc}"
}

# 服务已重启后（health / SHA 核验失败）才允许做容器级回滚。
rollback() {
    log "!!! 部署失败，执行容器级回滚 !!!"
    log "failure_stage=${FAILURE_STAGE} services_restarted=${SERVICES_RESTARTED}"

    if [[ -z "${PREVIOUS_SHA}" ]]; then
        log "无 previous SHA 记录，无法自动回滚代码。请手动处理。"
        return 1
    fi

    # 文件层恢复同时恢复了 compose 定义（compose 文件属于 repo/live 文件 owner），
    # 这就是 PRE_DEPLOY_COMPOSE_DIGEST 的恢复机制（§8）。
    restore_files_to_previous_sha

    # [E2.1 P1-A §6/§7] 逐服务把 image ref 钉回 pre-deploy immutable content ID；
    # 任一项失败即中止，不允许"compose up 后希望它碰巧还是旧 image"。
    if ! _pin_predeploy_image_refs; then
        log "!! pre-deploy immutable image owner 恢复失败，回滚中止（保持 fail-closed）!!"
        return 1
    fi

    cd "${REPO_ROOT}"
    # [E2.1 P1-C] worker-after-close 由 owned-aware restore 处理，回滚不擅自 recreate
    # （避免启动原本停着的 worker）
    local _rb_filtered=()
    local _rb
    for _rb in "${PYTHON_SERVICES[@]}"; do
        [[ "${_rb}" == "worker-after-close" ]] && continue
        _rb_filtered+=("${_rb}")
    done
    run_cmd ${COMPOSE_CMD} up -d --force-recreate --no-build \
        "${_rb_filtered[@]}" frontend

    # [E2.1 P1-A] rollback() 完成调用 ≠ rollback successful。
    # 必须由独立 verify owner 逐项核对 pre-deploy manifest 后才允许声称成功；
    # 验证失败必须保持 fail-closed，不得打印"回滚完成"后继续。
    if verify_rollback_owner; then
        log "回滚完成（已恢复到 ${PREVIOUS_SHA}）"
        return 0
    fi
    log "!!! 回滚验证未通过，保持 fail-closed，不声称回滚完成 !!!"
    return 1
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

# [E2.1 P1-A §8] 保护 manifest 记住的 immutable image owner 不被 cleanup 删除。
#
# 从 capture 到可能发生的 rollback 之间，cleanup_resources 会执行：
#   - `docker image prune -f`：删除 **dangling**（无 tag 引用）镜像；
#   - 旧 SHA 镜像组回收 `docker rmi market-dev-*:<sha>`。
# 此时容器已被重建到新镜像，manifest 记住的旧 image 若只被一个不在保留集合里的
# tag 引用（甚至完全没有 tag），就会被删除。那样 rollback 会拿着一个**已不存在**
# 的 image ID 去恢复，而 verify 只会看到 mismatch —— 等于 manifest 记住了一个
# 不可恢复的目标。
#
# 因此这里给每个 captured immutable owner 打上稳定的回滚保护标签，
# 使其不再是 dangling，从而不会被 prune 回收。
protect_pre_deploy_image_owners() {
    [[ -n "${PRE_DEPLOY_MANIFEST_FILE}" && -f "${PRE_DEPLOY_MANIFEST_FILE}" ]] || return 0

    local line service image_id
    while IFS= read -r line; do
        service="${line#PRE_DEPLOY_IMAGE_ID:}"
        service="${service%%=*}"
        image_id="${line#*=}"
        [[ -n "${image_id}" ]] || continue
        # 打稳定保护标签：source of truth 仍是 immutable content ID。
        run_cmd docker tag "${image_id}" \
            "panji-rollback-keep:${service}-${image_id#sha256:}" >/dev/null 2>&1 || true
    done < <(grep '^PRE_DEPLOY_IMAGE_ID:' "${PRE_DEPLOY_MANIFEST_FILE}")
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
    # [E2.1 P1-A §8] 顺序至关重要：必须先保护 captured immutable owner，
    # 再做任何 destructive cleanup —— prune 会删除 dangling image，而此时
    # 容器已重建到新镜像，旧 owner 可能已无任何 tag 引用。
    protect_pre_deploy_image_owners

    run_cmd docker image prune -f

    # 构造保留集合：当前运行 SHA、上一成功部署 SHA、rollback 标签、基础镜像。
    local keep_sha=""
    for candidate in "${TARGET_SHA}" "${PREVIOUS_SHA}"; do
        if [[ -n "${candidate}" ]]; then
            keep_sha="${keep_sha} ${candidate}"
        fi
    done
    # [E2.1 P1-A §8] 除 SHA 保留集合外，还必须按 **content ID** 保护 manifest
    # 记住的 immutable owner：SHA tag 与 image content 并非一一对应
    # （tag 可被重新指向别的 content），因此只有 content ID 才是可靠判据。
    KEEP_IMAGE_IDS=""
    if [[ -n "${PRE_DEPLOY_MANIFEST_FILE}" && -f "${PRE_DEPLOY_MANIFEST_FILE}" ]]; then
        while IFS= read -r _line; do
            _id="${_line#*=}"
            [[ -n "${_id}" ]] && KEEP_IMAGE_IDS="${KEEP_IMAGE_IDS} ${_id}"
        done < <(grep '^PRE_DEPLOY_IMAGE_ID:' "${PRE_DEPLOY_MANIFEST_FILE}")
    fi
    log "  旧 SHA 回收：保留 SHA 集合 =${keep_sha}，及所有 *-rollback 标签与基础镜像"
    log "  受保护的 immutable rollback owner: ${KEEP_IMAGE_IDS}"

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
            # [E2.1 P1-A §8] 若该组镜像正是 manifest 记住的 immutable owner，
            # 删除它会让 rollback 指向一个已被删除的 image → 必须跳过。
            local group_id
            group_id="$(docker images -q --no-trunc "${backend_tag}" 2>/dev/null | head -1)"
            if [[ -n "${group_id}" && "${KEEP_IMAGE_IDS}" == *"${group_id}"* ]]; then
                log "  跳过回收（captured rollback owner 受保护）: ${sha}"
                continue
            fi
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
    check_static_resource_budget

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

        # [E2.1 P1-A §4] PRE_DEPLOY owner 的**唯一**捕获点，必须早于 checkout_target。
        #
        # 原因：PRE_DEPLOY_REPO_SHA 与 PRE_DEPLOY_COMPOSE_DIGEST 都从 REPO_ROOT 读取
        # （git rev-parse HEAD / compose config）。一旦 checkout 到 TARGET_SHA，
        # 读到的就是 candidate(B) 而不是真正 mutation 前的 old runtime(A)。
        # 那样 manifest 会在「回滚依据」的名义下把 B 记成旧版本，rollback 随即失效
        # ——比没有 manifest 更危险，因为它会让 verify 假通过。
        #
        # 其余 owner（RUNTIME_SHA / container image identity）读的是 live runtime，
        # 不受 checkout 影响。
        #
        # 注意：这里只**捕获** repo 派生 owner，不做 fail-closed 判定 —— 判定仍在
        # deploy() 内由 resolve_pre_deploy_runtime_owner 统一负责。若在 checkout
        # 之前就 fail 退出，deploy() 根本不会执行，会把构建 / 同步 / 门禁 / 回滚
        # 全部短路掉。
        capture_pre_checkout_repo_owners

        checkout_target

        if ! deploy; then
            # [E2.1 P1-A §4] failure handler matrix：由**真实 mutation 阶段**分派，
            # 不再用 `SERVICES_RESTARTED == true` 的反面粗粒度推断"文件需要恢复"
            # ——那会在任何 mutation 都还没发生的失败下凭空制造 restore mutation。
            #
            #   migration  → 既有特殊合同：不 recreate 容器，不 false-claim DB rollback
            #   none       → pre-mutation failure：零 restore、零 runtime mutation
            #   files      → 仅恢复文件/live/RUNTIME_SHA owner，不动容器
            #   containers → 精确容器级回滚（per-service immutable image owner）
            if [[ "${FAILURE_STAGE}" == "migration" ]]; then
                handle_migration_failure
                fail "migration_failed_requires_inspection：migration 失败，服务未重启，数据库状态需人工确认"
            fi

            case "${MUTATION_STAGE}" in
                none)
                    # pre-mutation failure：没有任何 runtime 状态被改写，
                    # 因此**不存在**可恢复对象；主动 restore 反而会制造 mutation。
                    log "部署在任何 runtime mutation 之前失败（mutation_stage=none）："
                    log "  market.env / live files / RUNTIME_SHA / 容器 均未改动"
                    log "  不执行任何文件层恢复，也不执行容器级回滚"
                    # [E2.1 P1-C] 未发生任何 runtime mutation，释放本次 own pause（无 restart，安全）。
                    _restore_after_close_pickup_if_owned
                    fail "部署失败（阶段: ${FAILURE_STAGE}），mutation_stage=none，未产生任何 runtime mutation"
                    ;;
                files)
                    if [[ -n "${PREVIOUS_SHA}" ]]; then
                        restore_files_to_previous_sha
                    fi
                    # [E2.1 P1-C] 文件层已恢复、服务未重启：释放本次 own pause。
                    _restore_after_close_pickup_if_owned
                    fail "部署失败（阶段: ${FAILURE_STAGE}），服务未重启，已恢复文件层"
                    ;;
                containers)
                    if rollback; then
                        # [E2.1 P1-C] 回滚验证通过：释放本次 own pause。
                        _restore_after_close_pickup_if_owned
                        fail "部署失败（阶段: ${FAILURE_STAGE}）并已执行容器级回滚"
                    fi
                    # [E2.1 P1-C] 回滚验证未通过：本 deploy fenced 的 worker 不自动恢复，
                    # 保持停止（MANUAL_INTERVENTION_REQUIRED）。
                    log "AFTER_CLOSE_PICKUP_RESTORE_SKIPPED_ROLLBACK_FAILED=true"
                    fail "部署失败（阶段: ${FAILURE_STAGE}）回滚验证未通过，worker-after-close 保持停止（MANUAL_INTERVENTION_REQUIRED）"
                    ;;
                *)
                    fail "部署失败（阶段: ${FAILURE_STAGE}），未知 mutation_stage=${MUTATION_STAGE}，保持 fail-closed"
                    ;;
            esac
        fi

        if ! verify_deployment; then
            # 核验发生在重启之后，属于容器级回滚场景。
            if rollback; then
                _restore_after_close_pickup_if_owned
            else
                log "AFTER_CLOSE_PICKUP_RESTORE_SKIPPED_ROLLBACK_FAILED=true"
            fi
            fail "部署核验失败并已回滚"
        fi

        if ! cleanup_resources; then
            # 清理后资源复检失败（OOM / 资源跌破阈值 / 限制未生效）→ 判部署失败。
            if rollback; then
                _restore_after_close_pickup_if_owned
            else
                log "AFTER_CLOSE_PICKUP_RESTORE_SKIPPED_ROLLBACK_FAILED=true"
            fi
            fail "部署后清理与资源复检失败（failure_stage=${FAILURE_STAGE}）并已回滚"
        fi

        # [RESOURCE_GATE_ORDER_DEBT] 在 save_state / 宣布成功之前，先恢复本 deploy 拥有的
        # worker-after-close（最终稳态必须包含它在运行）；恢复失败则不得宣布成功（CASE F）。
        if ! _restore_after_close_pickup_if_owned; then
            fail "worker-after-close 恢复失败，不宣布部署成功（MANUAL_INTERVENTION_REQUIRED）"
        fi

        # 最终稳态 runtime 资源健康（DS-104）：此时 worker 已恢复运行，检查的是真实稳态，
        # 而非 fence 停掉 worker 时的临时状态。
        if ! post_deploy_resource_check; then
            FAILURE_STAGE="final_steady_state_resource"
            # [E3 CASE H] 最终稳态健康失败 → 回滚 backend runtime mutation 前必须重新建立
            # supervisor-drain fence。否则 live-mounted 文件被回滚改写时，candidate after-close
            # Python 进程仍在运行，形成混合 runtime（P1-C supervisor-drain 要消灭的情况）。
            # frontend-only 未 fence 过 worker，无需重新 fence。
            if _backend_runtime_will_mutate; then
                if ! _fence_after_close_worker; then
                    log "MANUAL_INTERVENTION_REQUIRED=true（最终稳态资源失败且无法重新 fence after-close worker）"
                    fail "最终稳态资源健康复检失败，且无法重新建立 after-close 盘后任务门禁（failure_stage=${FAILURE_STAGE}）"
                fi
            fi
            if rollback; then
                # 回滚成功：在 OLD runtime 上恢复 worker；restore 会清掉 PICKUP_FENCED，使状态与
                # 真实容器（running）一致。若 restore 失败，保持 fail-closed + 人工干预。
                if ! _restore_after_close_pickup_if_owned; then
                    log "MANUAL_INTERVENTION_REQUIRED=true（回滚成功但无法恢复 OLD after-close worker）"
                fi
            else
                # 回滚失败：保持 fenced 状态，等待人工干预，绝不在 worker 仍 running 时静默放过。
                log "回滚失败，after-close worker 保持 fenced 状态（MANUAL_INTERVENTION_REQUIRED=true）"
            fi
            fail "最终稳态资源健康复检失败（failure_stage=${FAILURE_STAGE}）"
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
