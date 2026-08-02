#!/usr/bin/env bash
# panji-deploy-remote.sh — 在 panji-prod 服务器上执行的部署主体（固定流程）
#
# 设计要点（CHANGE-20260802-002 部署架构重构）：
#   1. 这是一个**真实的可执行脚本文件**，不是嵌在 SSH 命令里的 heredoc。
#      旧实现把 ~389 行逻辑写进未加引号的 heredoc，全部内容先经本地 shell 展开，
#      再由 `bash -s` 从 stdin 执行。后果：
#        - 每个 $ 都要手工转义，漏一个就静默改变语义；
#        - 语法/引用错误只在远端运行时暴露，本地无法 shellcheck、无法测试；
#        - 从 stdin 执行时脚本自身不可被 `bash -n` 预检；
#        - 2026-08-02 部署 73a46ae 时 §8（up -d）整段未执行，
#          日志从 §7 直接跳到结尾，镜像已构建但容器仍跑旧 SHA。
#      改为独立文件后：可 shellcheck、可 bash -n、可单独执行、可加 trap 定位失败行。
#
#   2. **不按变更文件决定重建哪些服务**。旧实现用 git diff 分类推断
#      RESTART_BACKEND / RESTART_FRONTEND / RESTART_CAPTURE，任一判定失误
#      都会让部分服务停留在旧 SHA 且不告警。现改为：一次性 force-recreate
#      **全部无状态服务**，保证全站 SHA 原子一致。
#
#   3. **不在服务器上构建镜像**。镜像由 Release Gate 构建后推送 Registry，
#      本脚本只 pull 指定 tag/digest。若镜像不存在则直接失败，不回退到本地 build。
#
#   4. 任何阶段失败：返回非 0、输出阶段名 / 行号 / 失败命令 / 退出码，
#      不继续后续阶段、不报告完成、保留旧版本。
#
# 用法（由 panji-test-deploy 通过 SSH 调用，也可在服务器上手工执行）：
#   panji-deploy-remote.sh --sha <FULL_SHA> [--dry-run] [--allow-local-build]
#
# 退出码：0 成功；1 部署失败；2 参数/前置错误

set -euo pipefail

# ---------------------------------------------------------------------------
# 失败定位：任何非预期退出都打印阶段 / 行号 / 命令 / 退出码
# ---------------------------------------------------------------------------
CURRENT_STAGE="init"
on_error() {
    local rc=$?
    echo "" >&2
    echo "==================== 部署失败 ====================" >&2
    echo "  阶段     : ${CURRENT_STAGE}" >&2
    echo "  行号     : ${BASH_LINENO[0]:-unknown}" >&2
    echo "  失败命令 : ${BASH_COMMAND}" >&2
    echo "  退出码   : ${rc}" >&2
    echo "  处置     : 未继续后续阶段；服务保持部署前状态；未写入 state。" >&2
    echo "==================================================" >&2
    exit "${rc}"
}
trap on_error ERR

log()   { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
stage() { CURRENT_STAGE="$1"; log "===== $1 ====="; }
fail()  { log "错误: $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------
TARGET_SHA=""
DRY_RUN="false"
ALLOW_LOCAL_BUILD="false"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --sha)               TARGET_SHA="${2:-}"; shift 2 ;;
        --dry-run)           DRY_RUN="true"; shift ;;
        --allow-local-build) ALLOW_LOCAL_BUILD="true"; shift ;;
        *) echo "未知参数: $1" >&2; exit 2 ;;
    esac
done

[[ -n "${TARGET_SHA}" ]] || { echo "缺少 --sha" >&2; exit 2; }
[[ "${TARGET_SHA}" =~ ^[0-9a-f]{40}$ ]] || { echo "--sha 必须是 40 位完整 SHA: ${TARGET_SHA}" >&2; exit 2; }

SHORT_SHA="${TARGET_SHA:0:7}"
# 路径为服务器实测真值（2026-08-02 核实）：
#   market.env 在 /etc/market-dev/ 而非仓库目录内。
REPO_ROOT="/root/web_dev"
ENV_DIR="/etc/market-dev"
ENV_FILE="${ENV_DIR}/market.env"
COMPOSE_FILE="docker-compose.prod.yml"
STATE_FILE="${ENV_DIR}/.panji-test-deploy-state"
MANIFEST_FILE="${ENV_DIR}/.panji-deployed-manifest.json"
LOCK_FILE="/var/lock/panji-test-deploy.lock"

run_cmd() {
    if [[ "${DRY_RUN}" == "true" ]]; then
        log "[dry-run] $*"
    else
        "$@"
    fi
}

log "目标 SHA=${TARGET_SHA} (${SHORT_SHA})  dry_run=${DRY_RUN}  allow_local_build=${ALLOW_LOCAL_BUILD}"

# ---------------------------------------------------------------------------
stage "0. 串行化锁"
# ---------------------------------------------------------------------------
# 防止并发部署互相覆盖（即使人工并发触发也不会重叠）。
mkdir -p "$(dirname "${LOCK_FILE}")" "${ENV_DIR}"
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
    fail "已有部署在运行中（锁 ${LOCK_FILE} 被占用），请等待完成后重试。"
fi
log "已获取部署锁"

# ---------------------------------------------------------------------------
stage "1. 资源门禁"
# ---------------------------------------------------------------------------
MIN_DISK_GB="${PANJI_MIN_DISK_GB:-20}"
MAX_DISK_PCT="${PANJI_MAX_DISK_PCT:-82}"
MIN_MEM_MB="${PANJI_MIN_MEM_MB:-4096}"

DISK_AVAIL_GB="$(df -BG --output=avail / | tail -1 | tr -dc '0-9')"
DISK_PCT="$(df --output=pcent / | tail -1 | tr -dc '0-9')"
MEM_AVAIL_MB="$(free -m | awk '/^Mem:/{print $7}')"

log "可用磁盘=${DISK_AVAIL_GB}GB 使用率=${DISK_PCT}% 可用内存=${MEM_AVAIL_MB}MB"
[[ "${DISK_AVAIL_GB}" -ge "${MIN_DISK_GB}" ]] \
    || fail "可用磁盘 ${DISK_AVAIL_GB}GB < 门禁 ${MIN_DISK_GB}GB"
[[ "${DISK_PCT}" -le "${MAX_DISK_PCT}" ]] \
    || fail "磁盘使用率 ${DISK_PCT}% > 门禁 ${MAX_DISK_PCT}%"
[[ "${MEM_AVAIL_MB}" -ge "${MIN_MEM_MB}" ]] \
    || fail "可用内存 ${MEM_AVAIL_MB}MB < 门禁 ${MIN_MEM_MB}MB"

# ---------------------------------------------------------------------------
stage "2. 校验仓库与 SHA"
# ---------------------------------------------------------------------------
cd "${REPO_ROOT}" || fail "仓库目录不存在: ${REPO_ROOT}"
[[ -f "${ENV_FILE}" ]] || fail "环境文件不存在: ${ENV_FILE}"
[[ -f "${REPO_ROOT}/${COMPOSE_FILE}" ]] || fail "${COMPOSE_FILE} 不存在于 ${REPO_ROOT}"

git fetch origin dev --no-tags 2>&1 | sed 's/^/  /' || fail "git fetch origin dev 失败"
git cat-file -e "${TARGET_SHA}^{commit}" 2>/dev/null \
    || fail "SHA 在服务器仓库中不存在: ${TARGET_SHA}"
git merge-base --is-ancestor "${TARGET_SHA}" origin/dev \
    || fail "SHA ${SHORT_SHA} 不是 origin/dev 的祖先，拒绝部署"
log "SHA 校验通过（origin/dev 祖先）"

# ---------------------------------------------------------------------------
stage "3. checkout 目标 SHA"
# ---------------------------------------------------------------------------
if [[ -n "$(git status --porcelain)" ]]; then
    fail "服务器仓库存在未提交改动，拒绝部署（禁止在服务器改码）。请人工核查后清理。"
fi
run_cmd git checkout -q --detach "${TARGET_SHA}"
if [[ "${DRY_RUN}" != "true" ]]; then
    ACTUAL="$(git rev-parse HEAD)"
    [[ "${ACTUAL}" == "${TARGET_SHA}" ]] || fail "checkout 后 HEAD=${ACTUAL} != 目标 ${TARGET_SHA}"
fi
log "仓库已在目标 SHA"

# ---------------------------------------------------------------------------
stage "4. 原子更新 market.env"
# ---------------------------------------------------------------------------
BUILD_TIME="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
[[ -f "${ENV_FILE}" ]] || fail "缺少 ${ENV_FILE}"

if [[ "${DRY_RUN}" == "true" ]]; then
    log "[dry-run] 将设置 GIT_SHA=${SHORT_SHA} BUILD_TIME=${BUILD_TIME} DEPLOYMENT_MODE=image"
else
    TMP_ENV="$(mktemp "${ENV_FILE}.XXXXXX")"
    grep -vE '^(GIT_SHA|BUILD_TIME|DEPLOYMENT_MODE)=' "${ENV_FILE}" > "${TMP_ENV}"
    {
        echo "GIT_SHA=${SHORT_SHA}"
        echo "BUILD_TIME=${BUILD_TIME}"
        echo "DEPLOYMENT_MODE=image"
    } >> "${TMP_ENV}"
    chmod --reference="${ENV_FILE}" "${TMP_ENV}" 2>/dev/null || true
    mv "${TMP_ENV}" "${ENV_FILE}" || fail "原子替换 ${ENV_FILE} 失败"
    log "market.env 已更新 GIT_SHA=${SHORT_SHA}"
fi

COMPOSE_CMD=(docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}")

# ---------------------------------------------------------------------------
stage "5. 获取镜像"
# ---------------------------------------------------------------------------
# 盘迹共 3 个业务镜像；全部 worker-* 复用 backend 镜像（见 docker-compose.prod.yml）。
REQUIRED_IMAGES=(
    "market-dev-backend:${SHORT_SHA}"
    "market-dev-frontend:${SHORT_SHA}"
    "market-dev-capture:${SHORT_SHA}"
)

REGISTRY_PREFIX="${PANJI_REGISTRY_PREFIX:-}"

missing_images() {
    local miss=()
    for img in "${REQUIRED_IMAGES[@]}"; do
        docker image inspect "${img}" >/dev/null 2>&1 || miss+=("${img}")
    done
    printf '%s\n' "${miss[@]:-}"
}

MISSING="$(missing_images | tr -d '\n' | tr -s ' ')"

if [[ -z "${MISSING}" ]]; then
    log "3 个目标镜像本地均已存在，无需拉取"
elif [[ -n "${REGISTRY_PREFIX}" ]]; then
    log "从 Registry 拉取镜像（prefix=${REGISTRY_PREFIX}）..."
    for img in "${REQUIRED_IMAGES[@]}"; do
        if docker image inspect "${img}" >/dev/null 2>&1; then
            log "  已存在: ${img}"
            continue
        fi
        REMOTE_IMG="${REGISTRY_PREFIX}/${img}"
        log "  pull ${REMOTE_IMG}"
        run_cmd docker pull "${REMOTE_IMG}" \
            || fail "拉取失败: ${REMOTE_IMG}（Registry 凭据或镜像不存在）"
        run_cmd docker tag "${REMOTE_IMG}" "${img}"
    done
elif [[ "${ALLOW_LOCAL_BUILD}" == "true" ]]; then
    # 过渡方案：Registry 尚未可用时，显式授权在服务器构建。
    # 注意这是**显式开关**，而非旧实现那种按变更文件自动决定构建。
    log "警告: 镜像缺失且未配置 Registry，按 --allow-local-build 在服务器构建。"
    log "缺失: ${MISSING}"
    export GIT_SHA="${SHORT_SHA}" BUILD_TIME
    run_cmd "${COMPOSE_CMD[@]}" build backend frontend worker-capture
else
    fail "缺少镜像: ${MISSING}
未配置 PANJI_REGISTRY_PREFIX，且未传 --allow-local-build。
按新部署架构，镜像应由 Release Gate 构建并推送 Registry，服务器只负责 pull。"
fi

# 校验镜像确实就绪（dry-run 下跳过）
if [[ "${DRY_RUN}" != "true" ]]; then
    for img in "${REQUIRED_IMAGES[@]}"; do
        docker image inspect "${img}" >/dev/null 2>&1 || fail "镜像仍不存在: ${img}"
    done
    log "3 个目标镜像全部就绪"
fi

# ---------------------------------------------------------------------------
stage "6. Alembic Migration"
# ---------------------------------------------------------------------------
# 用一次性容器执行，避免旧代码在新 schema 上启动。
# 显式 </dev/null：防止子进程消费父脚本 stdin 导致后续阶段被跳过
# （旧实现从 stdin 执行整个脚本，此处极易吞掉剩余内容）。
run_cmd "${COMPOSE_CMD[@]}" run --rm --no-deps -T backend alembic upgrade head </dev/null \
    || fail "Migration 失败。未重建服务，仍运行旧版本。"
log "Migration 完成"

# ---------------------------------------------------------------------------
stage "7. 重建全部无状态服务"
# ---------------------------------------------------------------------------
# 不依据变更文件判断，一次性重建所有无状态服务，保证全站 SHA 原子一致。
ALL_SERVICES="$("${COMPOSE_CMD[@]}" config --services 2>/dev/null || true)"
[[ -n "${ALL_SERVICES}" ]] || fail "无法读取 compose 服务清单"

# 有状态服务：绝不 recreate（保护数据卷）
STATEFUL_SERVICES="postgres redis"

SERVICES_TO_UP=()
while IFS= read -r svc; do
    [[ -z "${svc}" ]] && continue
    skip="false"
    for st in ${STATEFUL_SERVICES}; do
        [[ "${svc}" == "${st}" ]] && skip="true" && break
    done
    [[ "${skip}" == "true" ]] && continue
    SERVICES_TO_UP+=("${svc}")
done <<< "${ALL_SERVICES}"

[[ "${#SERVICES_TO_UP[@]}" -gt 0 ]] || fail "无状态服务清单为空，异常"

log "compose 全部服务 : $(echo "${ALL_SERVICES}" | tr '\n' ' ')"
log "跳过（有状态）   : ${STATEFUL_SERVICES}"
log "将重建 ${#SERVICES_TO_UP[@]} 个   : ${SERVICES_TO_UP[*]}"

run_cmd "${COMPOSE_CMD[@]}" up -d --force-recreate --no-build "${SERVICES_TO_UP[@]}" \
    || fail "up -d 失败，服务可能处于中间态，请立即人工检查。"
log "全部无状态服务已重建"

# ---------------------------------------------------------------------------
stage "8. 逐服务校验镜像 SHA"
# ---------------------------------------------------------------------------
if [[ "${DRY_RUN}" == "true" ]]; then
    log "[dry-run] 跳过镜像标签校验"
else
    sleep 3
    MISMATCH=""
    for svc in "${SERVICES_TO_UP[@]}"; do
        CID="$("${COMPOSE_CMD[@]}" ps -q "${svc}" 2>/dev/null | head -1)"
        if [[ -z "${CID}" ]]; then
            MISMATCH="${MISMATCH} ${svc}(无运行容器)"
            continue
        fi
        IMG="$(docker inspect -f '{{.Config.Image}}' "${CID}" 2>/dev/null || echo '')"
        # 仅校验业务镜像；第三方镜像（umami 等）不带 SHA tag
        case "${IMG}" in
            market-dev-*)
                [[ "${IMG}" == *":${SHORT_SHA}" ]] || MISMATCH="${MISMATCH} ${svc}(${IMG})"
                ;;
            *)
                log "  ${svc}: 第三方镜像 ${IMG}（不校验 SHA）"
                ;;
        esac
    done
    [[ -z "${MISMATCH}" ]] || fail "以下服务未运行在 ${SHORT_SHA}:${MISMATCH}"
    log "全部业务服务镜像标签匹配 ${SHORT_SHA}"
fi

# ---------------------------------------------------------------------------
stage "9. 健康检查"
# ---------------------------------------------------------------------------
if [[ "${DRY_RUN}" == "true" ]]; then
    log "[dry-run] 跳过健康检查"
else
    sleep 5
    HEALTH_OK="true"

    # backend 镜像内无 curl，只有 python3
    backend_http() {
        docker exec trading-backend python3 -c "
import sys, urllib.request
try:
    r = urllib.request.urlopen('http://127.0.0.1:8000' + sys.argv[1], timeout=10)
    print(r.status); print(r.read().decode('utf-8', 'replace'))
except Exception as exc:
    print('000'); print(str(exc))
" "$1" 2>/dev/null || printf '000\n'
    }

    HEALTH_CODE="$(backend_http /v1/health | head -1)"
    if [[ "${HEALTH_CODE}" == "200" ]]; then
        log "  backend /v1/health 200 OK"
    else
        log "  警告: backend /v1/health = ${HEALTH_CODE}"
        HEALTH_OK="false"
    fi

    VER_OUT="$(backend_http /v1/version)"
    VER_BODY="$(echo "${VER_OUT}" | tail -n +2)"
    RUNTIME_SHA="$(echo "${VER_BODY}" \
        | python3 -c 'import sys,json; print(json.load(sys.stdin).get("git_sha",""))' 2>/dev/null || true)"
    if [[ "${RUNTIME_SHA}" == "${SHORT_SHA}" || "${RUNTIME_SHA}" == "${TARGET_SHA}" ]]; then
        log "  backend runtime SHA 匹配: ${RUNTIME_SHA}"
    else
        log "  警告: runtime SHA=${RUNTIME_SHA:-空} != ${SHORT_SHA}"
        HEALTH_OK="false"
    fi

    TRIPLE="$(echo "${VER_BODY}" | python3 -c '
import sys, json
d = json.load(sys.stdin)
vals = {d.get("git_sha"), d.get("runtime_git_sha"), d.get("image_git_sha")}
vals.discard(None)
print("ok" if len(vals) == 1 else "mismatch:" + ",".join(sorted(map(str, vals))))
' 2>/dev/null || echo parse_error)"
    if [[ "${TRIPLE}" == "ok" ]]; then
        log "  /v1/version 三项 SHA 一致"
    else
        log "  警告: /v1/version SHA 不一致: ${TRIPLE}"
        HEALTH_OK="false"
    fi

    FE_CODE="$(docker exec trading-frontend curl -sS -o /dev/null -w '%{http_code}' \
        http://127.0.0.1:80/ 2>/dev/null || echo 000)"
    case "${FE_CODE}" in
        200|301|302) log "  frontend HTTP ${FE_CODE} OK" ;;
        *) log "  警告: frontend HTTP ${FE_CODE}"; HEALTH_OK="false" ;;
    esac

    [[ "${HEALTH_OK}" == "true" ]] || fail "健康检查未通过。state 未更新。"
fi

# ---------------------------------------------------------------------------
stage "10. 记录部署 manifest 与 state"
# ---------------------------------------------------------------------------
if [[ "${DRY_RUN}" == "true" ]]; then
    log "[dry-run] 将写入 ${STATE_FILE} 与 ${MANIFEST_FILE}"
else
    ALEMBIC_HEAD="$("${COMPOSE_CMD[@]}" run --rm --no-deps -T backend \
        alembic current 2>/dev/null </dev/null | tail -1 || echo unknown)"
    echo "${TARGET_SHA}" > "${STATE_FILE}"
    {
        echo "{"
        echo "  \"git_sha\": \"${TARGET_SHA}\","
        echo "  \"short_sha\": \"${SHORT_SHA}\","
        echo "  \"deployed_at\": \"$(date -u '+%Y-%m-%dT%H:%M:%SZ')\","
        echo "  \"alembic\": \"${ALEMBIC_HEAD}\","
        echo "  \"services\": \"${SERVICES_TO_UP[*]}\""
        echo "}"
    } > "${MANIFEST_FILE}"
    log "已写入 state 与 manifest"
fi

# ---------------------------------------------------------------------------
stage "11. 受控清理"
# ---------------------------------------------------------------------------
# 只清 builder cache / dangling image / stopped container。
# 严禁 system prune -a / image prune -a / volume prune（会删基础镜像与持久卷）。
if [[ "${DRY_RUN}" == "true" ]]; then
    log "[dry-run] 将执行 builder/image/container prune -f"
else
    BEFORE="$(df -BG --output=avail / | tail -1 | tr -dc '0-9')"
    docker builder prune -f   >/dev/null 2>&1 || log "  警告: builder prune 失败（忽略）"
    docker image prune -f     >/dev/null 2>&1 || log "  警告: image prune 失败（忽略）"
    docker container prune -f >/dev/null 2>&1 || log "  警告: container prune 失败（忽略）"
    AFTER="$(df -BG --output=avail / | tail -1 | tr -dc '0-9')"
    log "  可用磁盘 ${BEFORE}GB -> ${AFTER}GB"
    if [[ "${AFTER}" -lt "${MIN_DISK_GB}" ]]; then
        log "  警告: 清理后仍低于 ${MIN_DISK_GB}GB 门禁，下次部署将被拦截。"
    fi
fi

CURRENT_STAGE="done"
log "部署完成: ${SHORT_SHA}（repo == image == runtime，全部无状态服务已原子重建）"
exit 0
