#!/usr/bin/env bash
# panji-deploy.sh — 盘迹生产环境自动部署脚本
#
# 设计目标：
# - 接收精确 SHA，验证其属于 origin/main；
# - 工作区不干净即失败；
# - 按变更范围分类部署，默认使用 Live Mount，不重建镜像；
# - 不 down -v，不删除 PostgreSQL/Redis Volume，不自动 migration；
# - 记录 previous/last-good SHA，失败可回滚代码和应用容器；
# - 支持 --dry-run，只输出计划不执行变更。
#
# 用法:
#   scripts/deploy/panji-deploy.sh <SHA> [--dry-run]
#   scripts/deploy/panji-deploy.sh --dry-run <SHA>
#
# 示例:
#   ssh panji-prod '/usr/local/bin/panji-deploy.sh abc1234'
#
# 约束:
# - 必须在 panji-prod（43.136.118.82）上运行；
# - REPO_ROOT 默认 /root/web_dev；
# - LIVE_ROOT 默认 /opt/panji-live；
# - 依赖：git, docker compose, flock, rsync, curl, npm/node（前端构建）.

set -euo pipefail

REPO_ROOT="${PANJI_REPO_ROOT:-/root/web_dev}"
LIVE_ROOT="${PANJI_LIVE_ROOT:-/opt/panji-live}"
ENV_FILE="${PANJI_ENV_FILE:-/etc/market-dev/market.env}"
STATE_FILE="${PANJI_STATE_FILE:-/etc/market-dev/.panji-deploy-state}"
LOCK_FILE="${PANJI_LOCK_FILE:-/var/lock/panji-deploy.lock}"
COMPOSE_CMD="docker compose --env-file ${ENV_FILE} -f docker-compose.prod.yml -f docker-compose.live.yml"
# [P0 2026-07-30] 纯镜像部署：不叠加 live.yml，禁止 Live Mount 覆盖 baked-in 代码
# 满足 AGENTS.md §8 "正式镜像部署，禁止Live Mount/docker cp/临时业务脚本，保证repo=image=runtime SHA"
COMPOSE_CMD_NO_LIVE="docker compose --env-file ${ENV_FILE} -f docker-compose.prod.yml"
# 强制镜像构建：PANJI_FORCE_IMAGE_BUILD=1 时无论变更范围都走 image scope
FORCE_IMAGE_BUILD="${PANJI_FORCE_IMAGE_BUILD:-}"

DRY_RUN=false
TARGET_SHA=""

log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

fail() {
    log "错误: $*" >&2
    exit 1
}

usage() {
    echo "用法: $0 <SHA> [--dry-run]"
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

    # [Phase 5B-2] 确保 state 文件目录存在（state 初始化）
    local state_dir
    state_dir="$(dirname "${STATE_FILE}")"
    if [[ ! -d "${state_dir}" ]]; then
        mkdir -p "${state_dir}" || fail "无法创建 state 目录: ${state_dir}"
        log "已创建 state 目录: ${state_dir}"
    fi

    # 验证 SSH Host 别名/身份（如通过别名调用）
    if [[ -n "${PANJI_SSH_HOST:-}" ]]; then
        local resolved
        resolved="$(ssh -G "${PANJI_SSH_HOST}" 2>/dev/null | awk '/^hostname /{print $2; exit}')"
        [[ "${resolved}" == "43.136.118.82" ]] || fail "SSH Host '${PANJI_SSH_HOST}' 解析为 '${resolved}'，期望 43.136.118.82"
    fi
}

validate_sha() {
    log "验证 SHA: ${TARGET_SHA}"

    cd "${REPO_ROOT}"

    # [Phase 5B-2] 部署前必须 fetch origin main，避免使用 stale refs
    log "拉取 origin/main 最新引用..."
    git fetch origin main --no-tags 2>&1 | sed 's/^/  /' || fail "git fetch origin main 失败"

    # 必须是完整或短 SHA，且能被解析
    if ! git cat-file -e "${TARGET_SHA}^{commit}" 2>/dev/null; then
        fail "SHA 不存在或不是 commit: ${TARGET_SHA}"
    fi

    local full_sha
    full_sha="$(git rev-parse "${TARGET_SHA}^{commit}")"

    # 必须属于 origin/main
    if ! git merge-base --is-ancestor "${full_sha}" origin/main 2>/dev/null; then
        fail "SHA ${full_sha} 不是 origin/main 的祖先，拒绝部署"
    fi

    TARGET_SHA="${full_sha}"
    log "SHA 验证通过: ${TARGET_SHA}"
}

check_working_tree() {
    log "检查工作区状态..."

    cd "${REPO_ROOT}"

    if [[ -n "$(git status --porcelain 2>/dev/null)" ]]; then
        fail "工作区不干净，拒绝自动部署。请手动处理未提交修改后再试。"
    fi

    local current_branch
    current_branch="$(git branch --show-current)"
    if [[ "${current_branch}" != "main" ]]; then
        fail "当前分支是 '${current_branch}'，不是 main，拒绝部署"
    fi
}

load_previous_state() {
    if [[ -f "${STATE_FILE}" ]]; then
        PREVIOUS_SHA="$(cat "${STATE_FILE}" 2>/dev/null || echo "")"
    else
        PREVIOUS_SHA=""
    fi
    log "上一次部署 SHA: ${PREVIOUS_SHA:-无}"
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

classify_changes() {
    log "分类变更范围..."

    cd "${REPO_ROOT}"

    # [P0 2026-07-30] 强制镜像构建：PANJI_FORCE_IMAGE_BUILD=1 跳过分类，直接走 image scope
    if [[ "${FORCE_IMAGE_BUILD}" == "1" || "${FORCE_IMAGE_BUILD}" == "true" ]]; then
        log "PANJI_FORCE_IMAGE_BUILD=1，强制镜像构建（纯镜像部署，禁止 Live Mount）"
        CHANGE_SCOPE="image"
        return
    fi

    if [[ -z "${PREVIOUS_SHA}" ]]; then
        log "无上一次部署记录，按全量 backend+frontend 处理"
        CHANGE_SCOPE="all"
        return
    fi

    local changed_files
    changed_files="$(git diff --name-only "${PREVIOUS_SHA}..${TARGET_SHA}" 2>/dev/null || true)"

    if [[ -z "${changed_files}" ]]; then
        log "两次 SHA 之间无文件变化"
        CHANGE_SCOPE="none"
        return
    fi

    # 判断是否为纯文档/治理/部署脚本变更
    local non_docs_files
    non_docs_files="$(echo "${changed_files}" | grep -vE '^(docs/|rules/|AGENTS\.md|README\.md|CHANGELOG|\.github/workflows/deploy-production\.yml|scripts/deploy/panji-deploy\.sh)' || true)"

    if [[ -z "${non_docs_files}" ]]; then
        log "纯文档/治理/部署脚本变更，跳过应用部署"
        CHANGE_SCOPE="docs"
        return
    fi

    # 判断是否需要重建镜像（依赖/基础镜像/Dockerfile/Compose 核心变化）
    local image_build_files
    image_build_files="$(echo "${changed_files}" | grep -E '^(docker-compose\.prod\.yml|backend/Dockerfile|backend/Dockerfile\.capture|backend/pyproject\.toml|backend/pyproject\.lock|frontend/Dockerfile|frontend/package\.json|frontend/package-lock\.json)' || true)"

    if [[ -n "${image_build_files}" ]]; then
        log "检测到镜像/依赖变化，需要重建镜像: ${image_build_files}"
        CHANGE_SCOPE="image"
        return
    fi

    # 判断是否只有前端变化
    local backend_files frontend_files
    backend_files="$(echo "${changed_files}" | grep -E '^(backend/|scripts/)' | grep -vE '^scripts/deploy/panji-deploy\.sh$' || true)"
    frontend_files="$(echo "${changed_files}" | grep -E '^(frontend/|frontend\.config\.|vite\.config)' || true)"

    if [[ -n "${frontend_files}" && -z "${backend_files}" ]]; then
        CHANGE_SCOPE="frontend"
        log "变更范围: frontend only"
        return
    fi

    # 默认 backend + shared workers（Live Mount）
    CHANGE_SCOPE="backend"
    log "变更范围: backend + shared workers（Live Mount）"
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
    run_cmd git checkout -f "${TARGET_SHA}"
    log "已检出: ${TARGET_SHA}"
}

sync_live_mount() {
    log "同步运行时代码到 ${LIVE_ROOT}..."

    if [[ "${DRY_RUN}" == "true" ]]; then
        log "[dry-run] rsync backend/app, backend/alembic, backend/alembic.ini, frontend/dist 到 ${LIVE_ROOT}"
        return
    fi

    mkdir -p "${LIVE_ROOT}/backend" "${LIVE_ROOT}/frontend"

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

    if [[ -d "${REPO_ROOT}/frontend/dist" ]]; then
        rsync -a --delete \
            --exclude='.gitkeep' \
            "${REPO_ROOT}/frontend/dist/" "${LIVE_ROOT}/frontend/dist/"
        mkdir -p "${LIVE_ROOT}/frontend/dist/static/captures"
    fi

    echo -n "${TARGET_SHA}" > /tmp/panji-runtime-sha-tmp
    rsync -a /tmp/panji-runtime-sha-tmp "${LIVE_ROOT}/RUNTIME_SHA"
    rm -f /tmp/panji-runtime-sha-tmp

    log "同步完成"
}

build_frontend() {
    log "本地构建前端..."
    cd "${REPO_ROOT}/frontend"

    if [[ "${DRY_RUN}" == "true" ]]; then
        log "[dry-run] npm run build（或 vite build）"
        return
    fi

    if [[ -x "./node_modules/.bin/vite" ]]; then
        NODE_OPTIONS=--max-old-space-size=1024 ./node_modules/.bin/vite build
    else
        NODE_OPTIONS=--max-old-space-size=1024 npm run build
    fi
}

compose_config_check() {
    log "校验 Compose 配置..."
    cd "${REPO_ROOT}"
    run_cmd ${COMPOSE_CMD} config --quiet
    log "Compose 配置校验通过"
}

recreate_services() {
    local services=("$@")
    log "重建服务: ${services[*]}"
    cd "${REPO_ROOT}"
    run_cmd ${COMPOSE_CMD} up -d --force-recreate --no-build "${services[@]}"
}

build_images() {
    log "构建镜像（backend/frontend/worker-capture）..."
    cd "${REPO_ROOT}"
    # 注入版本信息
    export GIT_SHA="${TARGET_SHA:0:7}"
    export BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    run_cmd docker compose --env-file "${ENV_FILE}" -f docker-compose.prod.yml build backend frontend worker-capture

    # [P0 2026-07-30] 同步更新 market.env 的 GIT_SHA，避免 docker compose --env-file 覆盖 shell export
    # 导致 up -d 时使用旧镜像 tag（deployment_mode=image 必须保证 image=runtime SHA）
    if [[ "${DRY_RUN}" == "false" ]]; then
        if grep -q "^GIT_SHA=" "${ENV_FILE}"; then
            sed -i "s/^GIT_SHA=.*/GIT_SHA=${TARGET_SHA:0:7}/" "${ENV_FILE}"
            log "已更新 ${ENV_FILE} GIT_SHA=${TARGET_SHA:0:7}"
        else
            echo "GIT_SHA=${TARGET_SHA:0:7}" >> "${ENV_FILE}"
            log "已追加 ${ENV_FILE} GIT_SHA=${TARGET_SHA:0:7}"
        fi
    else
        log "[dry-run] 将更新 ${ENV_FILE} GIT_SHA=${TARGET_SHA:0:7}"
    fi
}

deploy_scope() {
    case "${CHANGE_SCOPE}" in
        none|docs)
            log "无需应用变更，跳过部署"
            ;;
        frontend)
            build_frontend
            sync_live_mount
            compose_config_check
            recreate_services frontend goaccess
            ;;
        backend)
            build_frontend
            sync_live_mount
            compose_config_check
            recreate_services \
                backend \
                worker-bars-scheduler worker-strategy-scheduler worker-calendar \
                worker-monitor worker-strategy-batch worker-outbox worker-delivery \
                worker-after-close worker-watchdog worker-capture
            ;;
        image)
            # [P0 2026-07-30] 纯镜像部署：构建镜像后不 sync_live_mount，
            # 使用 docker-compose.prod.yml 单文件重建，保证 repo=image=runtime SHA
            build_images
            build_frontend
            compose_config_check
            # 镜像重建后全量 up -d（不叠加 live.yml，不覆盖 baked-in 代码）
            run_cmd ${COMPOSE_CMD_NO_LIVE} up -d --force-recreate --remove-orphans \
                backend frontend goaccess \
                worker-bars-scheduler worker-strategy-scheduler worker-calendar \
                worker-monitor worker-strategy-batch worker-outbox worker-delivery \
                worker-after-close worker-watchdog worker-capture
            ;;
        all)
            build_frontend
            sync_live_mount
            compose_config_check
            recreate_services \
                backend frontend goaccess \
                worker-bars-scheduler worker-strategy-scheduler worker-calendar \
                worker-monitor worker-strategy-batch worker-outbox worker-delivery \
                worker-after-close worker-watchdog worker-capture
            ;;
        *)
            fail "未知变更范围: ${CHANGE_SCOPE}"
            ;;
    esac
}

health_check() {
    # [Phase 5B-2] dry-run 模式下只做计划验证，不称健康检查
    if [[ "${DRY_RUN}" == "true" ]]; then
        log "[dry-run] 计划验证: 将检查 port 80, /health, /health/ready, /version runtime_git_sha, 关键容器, Scheduler 单实例"
        return 0
    fi

    log "健康检查..."

    local max_wait=60
    local waited=0

    # 等待 backend /health
    while [[ ${waited} -lt ${max_wait} ]]; do
        if curl -sf http://127.0.0.1:8000/health >/dev/null 2>&1; then
            log "backend /health 通过"
            break
        fi
        log "等待 backend /health... (${waited}/${max_wait})"
        sleep 2
        waited=$((waited + 2))
    done

    if [[ ${waited} -ge ${max_wait} ]]; then
        return 1
    fi

    # /health/ready
    if ! curl -sf http://127.0.0.1:8000/health/ready >/dev/null 2>&1; then
        log "/health/ready 未通过"
        return 1
    fi
    log "/health/ready 通过"

    # /version runtime_git_sha
    # [P0 2026-07-30] 纯镜像部署时 GIT_SHA 只有短 SHA（7 chars），比较前 7 位即可
    # Live Mount 部署时 RUNTIME_SHA 文件含完整 SHA，短 SHA 比较仍然成立
    local runtime_sha
    runtime_sha="$(curl -sf http://127.0.0.1:8000/version 2>/dev/null | python3 -c 'import sys,json; print(json.load(sys.stdin).get("runtime_git_sha","unknown"))' 2>/dev/null || echo "unknown")"
    if [[ "${runtime_sha:0:7}" != "${TARGET_SHA:0:7}" ]]; then
        log "runtime_git_sha 不匹配: 期望 ${TARGET_SHA:0:7}, 实际 ${runtime_sha:0:7}"
        return 1
    fi
    log "runtime_git_sha 验证通过: ${runtime_sha:0:7}"

    # 端口 80
    if ! curl -sf http://127.0.0.1:80 >/dev/null 2>&1; then
        log "端口 80 未返回 2xx"
        return 1
    fi
    log "端口 80 通过"

    # 关键容器检查
    local required=(trading-backend trading-frontend trading-redis trading-postgres)
    for c in "${required[@]}"; do
        if ! docker ps --format '{{.Names}}' | grep -qx "${c}"; then
            log "关键容器未运行: ${c}"
            return 1
        fi
    done
    log "关键容器检查通过"

    # Scheduler 单实例检查（每个 scheduler 类型只应有一个容器在运行）
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

    # GoAccess 非破坏性检查（失败报告具体错误，不让整个部署无限等待）
    check_goaccess_health

    return 0
}

check_goaccess_health() {
    log "GoAccess 健康检查..."

    # 1. trading-goaccess 容器必须 running
    if ! docker ps --format '{{.Names}}' | grep -qx "trading-goaccess"; then
        log "GoAccess 容器 trading-goaccess 未运行（非阻塞，报告后继续）"
        return 0
    fi
    log "trading-goaccess 容器运行中"

    # 2. frontend 容器内 /var/log/nginx/access.log 必须存在
    if ! docker exec trading-frontend test -f /var/log/nginx/access.log 2>/dev/null; then
        log "GoAccess 检查: trading-frontend:/var/log/nginx/access.log 不存在（非阻塞）"
        return 0
    fi
    log "frontend access.log 存在"

    # 3. backend 容器内 /srv/goaccess 目录必须存在（挂载点）
    if ! docker exec trading-backend test -d /srv/goaccess 2>/dev/null; then
        log "GoAccess 检查: trading-backend:/srv/goaccess 目录不存在（非阻塞）"
        return 0
    fi
    log "backend /srv/goaccess 目录存在"

    # 4. report.json 允许首次启动后最多等待 300 秒生成
    local report_max_wait=300
    local report_waited=0
    while [[ ${report_waited} -lt ${report_max_wait} ]]; do
        if docker exec trading-backend test -f /srv/goaccess/report.json 2>/dev/null; then
            log "GoAccess report.json 已生成（等待 ${report_waited}s）"
            return 0
        fi
        sleep 10
        report_waited=$((report_waited + 10))
        if [[ $((report_waited % 60)) -eq 0 ]]; then
            log "等待 GoAccess report.json 生成... (${report_waited}/${report_max_wait})"
        fi
    done

    # report.json 未生成：输出 goaccess 最近日志辅助排查，但不让部署失败
    log "GoAccess report.json 在 ${report_max_wait}s 内未生成（非阻塞，检查 goaccess 日志）"
    docker logs --tail 30 trading-goaccess 2>&1 | sed 's/^/  goaccess: /' || true

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

    # 重新同步旧代码
    sync_live_mount

    # 重新创建应用容器（不回滚数据库）
    cd "${REPO_ROOT}"
    run_cmd ${COMPOSE_CMD} up -d --force-recreate --no-build \
        backend frontend \
        worker-bars-scheduler worker-strategy-scheduler worker-calendar \
        worker-monitor worker-strategy-batch worker-outbox worker-delivery \
        worker-after-close worker-watchdog worker-capture

    log "回滚完成"
}

main() {
    parse_args "$@"

    # 串行锁
    (
        flock -n 200 || fail "另一个部署正在进行中"

        check_prerequisites
        validate_sha
        check_working_tree
        load_previous_state
        classify_changes

        if [[ "${CHANGE_SCOPE}" == "none" || "${CHANGE_SCOPE}" == "docs" ]]; then
            checkout_target
            save_state "${TARGET_SHA}"
            # [Phase 5B-2] 部署后切回 main 分支，避免 detached HEAD
            cd "${REPO_ROOT}"
            git checkout main 2>/dev/null || log "警告: 切回 main 分支失败"
            log "部署完成（无应用变更）"
            exit 0
        fi

        # 部署
        if ! (
            checkout_target
            deploy_scope
            health_check
        ); then
            rollback
            fail "部署失败并已回滚"
        fi

        save_state "${TARGET_SHA}"

        # [Phase 5B-2] 部署后切回 main 分支，避免 detached HEAD
        cd "${REPO_ROOT}"
        run_cmd git checkout main 2>/dev/null || log "警告: 切回 main 分支失败，仓库可能处于 detached HEAD"

        log "部署成功: ${TARGET_SHA}"

    ) 200>"${LOCK_FILE}"
}

main "$@"
