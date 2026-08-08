#!/usr/bin/env bash
#
# run_remote_verification.sh — 远程验证生命周期编排（单可复用运行时 + Single-Flight）
#
# 设计（2026-08-06 治理重构，减法版）：
#   - 单可复用验证镜像 panji-verify-runtime:current（Python dependency 合同）
#   - 单一长期容器 panji-verify-python（常驻空闲，禁 Scheduler/Worker/Uvicorn/pytest/seed）
#   - 复用于 trading-postgres（验证库 bz_stock_verify_<SHA>）
#   - 本轮 verification 不连接 Redis（一次性审计结论：full-closure 仅连 PG）
#   - attempt 仅隔离执行状态（SHA/DB/process/env/evidence）
#   - 最外层 single-flight flock 覆盖整段 remote lifecycle
#   - dependency hash 两方比较（expected vs image label），不一致才 build→recreate
#   - 异常恢复：docker restart panji-verify-python（杀容器所有验证进程、不删 infra）
#
# Single-Flight 语义：
#   整个 lifecycle 由最外层 flock 独占。并发第二 attempt 在 acquire 阶段即 exit 75。
#   不再在 VerifyAttempt 内保留第二层锁（single-flight 已保证生命周期独占）。
#
# 安全边界（fail-closed）：
#   - 不连 bz_stock；验证库名严格匹配 bz_stock_verify_<40hex>
#   - 禁止 compose down / 删 Volume / FLUSHALL / 删稳定栈容器
#   - 唯一入口为 scripts/ops/panji-verify
#
set -euo pipefail

# ───────────────────────────── 常量（固定，不随 SHA 变化） ─────────────────────────────
COMPOSE_FILE="docker-compose.verify.yml"
COMPOSE_PROJECT="panji-verify"
VERIFY_IMAGE="panji-verify-runtime:current"
VERIFY_CONTAINER="panji-verify-python"
VERIFY_LOCK="/root/.panji-verify/verify.lock"
RUNTIME_DIR="/root/.panji-verify/runtime"          # 固定 runtime 控制路径（只读 mount 进容器）
ATTEMPT_ENV_FILE="${RUNTIME_DIR}/attempt.env"
RUNTIME_SHA_FILE="${RUNTIME_DIR}/RUNTIME_SHA"
EVIDENCE_ROOT="/root/.panji-verify/evidence"

# Compose 必填变量（docker-compose.verify.yml 以 :? 声明，必须由本入口 export）
export VERIFY_CODE_DIR="/root/web_dev_verify"      # Live Mount 源（固定路径）
export VERIFY_RUNTIME_DIR="${RUNTIME_DIR}"          # /run/panji-verify/:ro 宿主机侧
# 复用 trading-postgres 所在网络（prepare_verify_environment.py 也据此写入 attempt.env）
export VERIFY_PG_NETWORK="$(docker inspect -f '{{ range $k,$v := .NetworkSettings.Networks }}{{$k}}{{end}}' trading-postgres 2>/dev/null | awk '{print $1}')"
if [[ -z "${VERIFY_PG_NETWORK}" ]]; then
  echo "error: 无法获取 trading-postgres 网络，中止" >&2
  exit 3
fi

# ───────────────────────────── 参数 ─────────────────────────────
# 外部 CLI 合同：第二个参数为 plan name（仅接受三个注册 plan，由 scripts/ops/panji-verify 传入）。
# 本入口内部把它映射为磁盘上的 plan 文件路径，不引入 plan registry / 动态扫描。
SHA="${1:-}"
PLAN_NAME="${2:-}"
if [[ -z "${SHA}" ]]; then
  echo "usage: run_remote_verification.sh <40hex-sha> [plan-name]" >&2
  exit 2
fi
if [[ ! "${SHA}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "error: SHA 必须为 40 位 hex（received: ${SHA}）" >&2
  exit 2
fi

case "${PLAN_NAME}" in
  targeted-pg)
    PLAN_PATH="scripts/verify/plans/targeted-pg.json"
    ;;
  migration-roundtrip)
    PLAN_PATH="scripts/verify/plans/migration-roundtrip.json"
    ;;
  full-closure)
    PLAN_PATH="scripts/verify/plans/full-closure.json"
    ;;
  "")
    echo "error: plan name 不能为空（传入 targeted-pg | migration-roundtrip | full-closure）" >&2
    exit 80
    ;;
  *)
    echo "error: unregistered plan '${PLAN_NAME}'" >&2
    exit 80
    ;;
esac

mkdir -p "${RUNTIME_DIR}" "${EVIDENCE_ROOT}"

# ───────────────────────────── 依赖 hash 两方比较 ─────────────────────────────
# expected_dependency_hash = SHA256(backend/Dockerfile + backend/pyproject.toml + lockfile)
# 与 panji-verify-python 当前 image 的 Docker label panji.verify.dependency-hash 比较
compute_expected_dep_hash() {
  local dockerfile="backend/Dockerfile"
  local pyproject="backend/pyproject.toml"
  local parts=""
  if [[ -f "${dockerfile}" ]]; then parts+="$(cat "${dockerfile}")"; fi
  if [[ -f "${pyproject}" ]]; then parts+="$(cat "${pyproject}")"; fi
  # 若存在 lockfile 一并纳入（poetry.lock / uv.lock / requirements.lock）
  for lf in backend/poetry.lock backend/uv.lock backend/requirements.lock; do
    if [[ -f "${lf}" ]]; then parts+="$(cat "${lf}")"; break; fi
  done
  printf '%s' "${parts}" | sha256sum | awk '{print $1}'
}

get_image_dep_hash() {
  # 返回 panji-verify-python 当前 image 的 label；容器/镜像不存在则返回空
  docker inspect -f '{{ index .Config.Labels "panji.verify.dependency-hash" }}' "${VERIFY_CONTAINER}" 2>/dev/null || true
}

ensure_verify_runtime() {
  local expected_hash
  expected_hash="$(compute_expected_dep_hash)"
  echo "expected_dependency_hash=${expected_hash}"

  local image_hash
  image_hash="$(get_image_dep_hash || true)"
  echo "image_dependency_hash=${image_hash:-<none>}"

  local need_build=0
  if [[ -z "${image_hash}" ]]; then
    echo "verify runtime 不存在，需要 build"
    need_build=1
  elif [[ "${image_hash}" != "${expected_hash}" ]]; then
    echo "dependency hash 不一致，需要 rebuild"
    need_build=1
  fi

  if [[ "${need_build}" -eq 1 ]]; then
    echo "building ${VERIFY_IMAGE} ..."
    # 注入 DEP_HASH 作为 build-arg，Dockerfile 写进 label
    DOCKER_BUILDKIT=1 docker build \
      --target verification \
      --build-arg "DEP_HASH=${expected_hash}" \
      -t "${VERIFY_IMAGE}" \
      -f backend/Dockerfile backend \
      || { echo "error: verification_environment_broken (build failed)"; return 1; }

    # 若容器已存在但 image 变化，recreate
    if docker ps -a --format '{{.Names}}' | grep -q "^${VERIFY_CONTAINER}$"; then
      echo "recreating ${VERIFY_CONTAINER} ..."
      docker rm -f "${VERIFY_CONTAINER}" >/dev/null 2>&1 || true
    fi
    docker compose -f "${COMPOSE_FILE}" -p "${COMPOSE_PROJECT}" up -d verify-python \
      || { echo "error: verification_environment_broken (up failed)"; return 1; }

    # basic health check：容器存活
    local healthy=0
    for _ in $(seq 1 10); do
      if docker ps --format '{{.Names}}' | grep -q "^${VERIFY_CONTAINER}$"; then
        healthy=1
        break
      fi
      sleep 2
    done
    if [[ "${healthy}" -ne 1 ]]; then
      echo "error: verification_environment_broken (container not running)"; return 1
    fi
    echo "verify runtime ready (rebuilt)"
  else
    # 容器存在且 hash 一致：确保容器在运行（若被停则起）
    if ! docker ps --format '{{.Names}}' | grep -q "^${VERIFY_CONTAINER}$"; then
      echo "container 未运行，重新 up ..."
      docker compose -f "${COMPOSE_FILE}" -p "${COMPOSE_PROJECT}" up -d verify-python \
        || { echo "error: verification_environment_broken (up failed)"; return 1; }
    fi
    echo "verify runtime ready (reused)"
  fi
}

# ───────────────────────────── Single-Flight 最外层 ─────────────────────────────
exec 9>"${VERIFY_LOCK}"
if ! flock -n 9; then
  echo "error: verification_busy (另一验证 attempt 持有 ${VERIFY_LOCK})" >&2
  exit 75
fi

cleanup_on_exit() {
  # 第一行保存 trap 触发点的退出码（主流程退出码），避免被后续命令重置
  local rc=$?
  # 释放锁（保留容器/镜像/网络/PG/Redis，仅清 attempt 临时状态）
  rm -f "${ATTEMPT_ENV_FILE}" "${RUNTIME_SHA_FILE}" || true
  # [CHANGE-20260806-012] 仅在异常/失败/中断时恢复常驻容器干净环境：
  # 杀容器内所有验证进程、不删 container/image/network/PG/Redis/稳定栈、保留 bind mount。
  # 成功闭环不 restart（避免与正常路径重复；verify_attempt 已自行清理临时状态）。
  if [[ "${rc}" -ne 0 ]]; then
    docker restart "${VERIFY_CONTAINER}" >/dev/null 2>&1 || true
  fi
  echo "single-flight lock released"
}
trap cleanup_on_exit EXIT

echo "=== single-flight acquired: ${SHA} ==="

# ───────────────────────────── fetch + checkout exact SHA ─────────────────────────────
git fetch origin dev >/dev/null 2>&1 || true
git checkout --detach "${SHA}"

HEAD_SHA="$(git rev-parse HEAD)"
if [[ "${HEAD_SHA}" != "${SHA}" ]]; then
  echo "error: checkout 后 HEAD(${HEAD_SHA}) != target(${SHA})"; exit 1
fi
if [[ -n "$(git status --porcelain)" ]]; then
  echo "error: checkout 后工作区不干净，中止以防污染验证"; exit 1
fi
echo "HEAD == target && clean ✓"

# 写入固定 runtime path（lock 内原地覆写）
printf '%s' "${SHA}" > "${RUNTIME_SHA_FILE}"

# ───────────────────────────── 生成 attempt.env（固定 runtime 路径） ─────────────────────────────
# attempt-specific 变量（DATABASE_URL/MIGRATION_DATABASE_URL/JWT_SECRET/...）落到 0600 的
# ${ATTEMPT_ENV_FILE}，容器以 /run/panji-verify/:ro 只读挂载，由 verify_exec.py 注入每个 fresh process。
# 必须在 ensure_verify_runtime 之前，因为 prepare 也负责解析 trading-postgres 网络写入 attempt.env。
python3 scripts/verify/prepare_verify_environment.py \
  --target-sha "${SHA}" \
  --output "${ATTEMPT_ENV_FILE}" \
  || { echo "error: verification_environment_broken (prepare attempt.env failed)"; exit 3; }

# ───────────────────────────── ensure 单可复用运行时 ─────────────────────────────
# VERIFY_PG_NETWORK 已由本入口 export（prepare 也写入 attempt.env 作为 fresh process 连接依据）
ensure_verify_runtime || { echo "error: verification_environment_broken"; exit 3; }

# ───────────────────────────── delegate 给 VerifyAttempt（Python） ─────────────────────────────
# VerifyAttempt 内不再持有第二层锁；attempt.env 已由 prepare_verify_environment.py 生成到 RUNTIME_DIR
python3 scripts/verify/verify_attempt.py \
  --sha "${SHA}" \
  --plan "${PLAN_PATH}" \
  --runtime-dir "${RUNTIME_DIR}" \
  --evidence-root "${EVIDENCE_ROOT}" \
  --compose-project "${COMPOSE_PROJECT}" \
  --verify-container "${VERIFY_CONTAINER}"

EXIT_CODE=$?
echo "=== verify_attempt exit=${EXIT_CODE} ==="
exit "${EXIT_CODE}"
