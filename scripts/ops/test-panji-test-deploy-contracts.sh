#!/usr/bin/env bash
# Execute the real server deployment implementation in dry-run mode with mocked boundaries.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
SERVER_SCRIPT="${REPO_ROOT}/scripts/deploy/panji-deploy.sh"
TMP_ROOT="$(mktemp -d)"
trap 'rm -rf "${TMP_ROOT}"' EXIT

PASS=0
FAIL=0
ok() { printf '  PASS: %s\n' "$1"; PASS=$((PASS + 1)); }
bad() { printf '  FAIL: %s\n' "$1" >&2; FAIL=$((FAIL + 1)); }
expect_success() { if "$@"; then ok "$1"; else bad "$1"; fi; }

MOCK_BIN="${TMP_ROOT}/bin"
mkdir -p "${MOCK_BIN}"
# worker-after-close 容器状态文件（supervisor-drain fence 合同用）。
# `docker compose stop -t -1 worker-after-close` 将其置 exited，`up ... worker-after-close`
# 置 running；mock `docker inspect ... State.Status` 读取此文件。
WORKER_STATE_FILE="${TMP_ROOT}/worker_state"
reset_worker_state() { printf '%s' "$1" > "${WORKER_STATE_FILE}"; }
export PANJI_REAL_GIT="$(command -v git)"

cat > "${MOCK_BIN}/git" <<'EOF'
#!/usr/bin/env bash
if [[ "${1:-}" == "fetch" ]]; then
  exit 0
fi
if [[ "${1:-}" == "status" && "${2:-}" == "--porcelain" ]]; then
  [[ "${PANJI_MOCK_DIRTY:-0}" == "1" ]] && printf ' M dirty-file\n'
  exit 0
fi
exec "${PANJI_REAL_GIT}" "$@"
EOF
# docker mock：默认让 inspect 报告 Live Mount 已建立（非首次部署）。
# PANJI_MOCK_NO_LIVE_MOUNT=1 时 inspect 返回空，模拟首次 Live Mount 部署。
# worker-after-close 容器状态经 PANJI_MOCK_WORKER_STATE 状态文件模拟。
cat > "${MOCK_BIN}/docker" <<'EOF'
#!/usr/bin/env bash
WORKER_STATE="${PANJI_MOCK_WORKER_STATE:-/tmp/panji_worker_state}"
if [[ "${1:-}" == "inspect" ]]; then
  # 目标必须按参数扫描解析，不能取 $2：真实调用形式包含 flag 与 format，
  # 例如 `docker inspect -f '{{.HostConfig.Memory}}' trading-backend`（$2 是 -f）。
  # 规则：跳过 inspect 自身、以 - 开头的 flag、以及含 {{ 的 format 串，取最后一个剩余参数。
  target=""
  for _a in "$@"; do
    [[ "${_a}" == "inspect" ]] && continue
    [[ "${_a}" == -* ]] && continue
    [[ "${_a}" == *'{{'* ]] && continue
    target="${_a}"
  done
  if printf '%s' "$*" | grep -q 'State.Status'; then
    # worker-after-close 容器状态探针（supervisor-drain fence 线性化点）
    if [[ -f "${WORKER_STATE}" ]]; then
      cat "${WORKER_STATE}"
    else
      printf 'exited'
    fi
    exit 0
  fi
  if printf '%s' "$*" | grep -q 'Mounts'; then
    # Live Mount 探针
    if [[ "${PANJI_MOCK_NO_LIVE_MOUNT:-0}" == "1" ]]; then
      exit 1
    fi
    printf '%s /var/lib/postgresql \n' "${PANJI_LIVE_ROOT:-/opt/panji-live}"
    exit 0
  fi
  # [P1-A] 拓扑感知的 inspect：
  #   - CID_*           → resolver 已解析出真实容器 ID，返回 image 占位（非空）。
  #   - trading-*       → post-deploy 健康/限制复检按真实容器名 inspect，按字段返回语义值。
  #   - bare 逻辑 service 名（backend / worker-after-close / frontend ...）
  #                     → 真实拓扑中不存在（容器名为 trading-<svc>），
  #                       必须失败以暴露 resolver 误用 bare name（P1-A 反向证明）。
  if [[ "${target}" == CID_* ]]; then
    printf 'sha256:image-%s\n' "${target}"
    exit 0
  fi
  if [[ "${target}" == trading-* ]]; then
    # 按查询字段返回语义值（不再统一返回路径占位——那会让 limits 检查形同虚设）。
    if printf '%s' "$*" | grep -q 'OOMKilled'; then printf 'false\n'; exit 0; fi
    if printf '%s' "$*" | grep -q 'RestartCount'; then printf '0\n'; exit 0; fi
    if printf '%s' "$*" | grep -q 'HostConfig.Memory'; then printf '2147483648\n'; exit 0; fi
    if printf '%s' "$*" | grep -q 'PidsLimit'; then printf '512\n'; exit 0; fi
    if printf '%s' "$*" | grep -q 'NanoCpus'; then printf '1000000000\n'; exit 0; fi
    printf '%s' "${PANJI_LIVE_ROOT:-/opt/panji-live}"
    exit 0
  fi
  echo "Error: No such object: ${target}" >&2
  exit 1
fi
# [E2.1-R] `docker ps --filter "name=<c>" --format '{{.Names}}'` 用于 Scheduler
# 单实例校验（count 必须 == 1）。默认 docker mock 不输出任何容器会让该校验
# 恒为 0 并把 dry-run 判失败。这里按 --filter 回显一行容器名，使 count==1；
# 置 PANJI_MOCK_PS_EMPTY=1 可模拟"容器不存在"。
if [[ "${1:-}" == "ps" ]]; then
  name=""
  args=("$@")
  for ((i = 0; i < ${#args[@]}; i++)); do
    if [[ "${args[$i]}" == "--filter" ]]; then
      name="${args[$((i + 1))]}"
      name="${name#name=}"
    fi
  done
  if [[ -n "${name}" && "${PANJI_MOCK_PS_EMPTY:-0}" != "1" ]]; then
    printf '%s\n' "${name}"
  fi
  exit 0
fi
# [E2.1 P1-B / P1-C] 伪造容器内 psql，用于注入 after-close 任务门禁 fixture。
#   PANJI_MOCK_PSQL_COUNTS : GROUP BY status 统计（"status:count" 每行）
#   PANJI_MOCK_PSQL_RUNNING: 仅 running 的 count(*)（fence 线性化点）
#   PANJI_MOCK_PSQL_ROWS   : 明细查询（"id | job | business_date | status | heartbeat"）
#   PANJI_MOCK_PSQL_FAIL=1 : 模拟门禁查询不可用（必须 fail-closed）
if [[ "${1:-}" == "exec" ]]; then
  if [[ "${PANJI_MOCK_PSQL_FAIL:-0}" == "1" ]]; then
    exit 1
  fi
  if printf '%s' "$*" | grep -q 'status, count'; then
    # GROUP BY status 可见性查询
    if [[ -n "${PANJI_MOCK_PSQL_COUNTS:-}" ]]; then
      printf '%s\n' "${PANJI_MOCK_PSQL_COUNTS}"
    else
      printf '0\n'
    fi
    exit 0
  fi
  if printf '%s' "$*" | grep -q 'count('; then
    # 普通 count(*) → fence running 计数（single number）
    printf '%s\n' "${PANJI_MOCK_PSQL_RUNNING:-0}"
    exit 0
  fi
  if printf '%s' "$*" | grep -q 'id, job_name'; then
    [[ -n "${PANJI_MOCK_PSQL_ROWS:-}" ]] && printf '%s\n' "${PANJI_MOCK_PSQL_ROWS}"
    exit 0
  fi
  # preempt 明细查询（status='running' 的 Chip），无 fixture 时为空
  [[ -n "${PANJI_MOCK_PSQL_ROWS_PREEMPT:-}" ]] && printf '%s\n' "${PANJI_MOCK_PSQL_ROWS_PREEMPT}"
  exit 0
fi
# [E2.1 P1-A] compose config：用于 effective compose runtime definition digest。
#   PANJI_MOCK_COMPOSE_FAIL=1 模拟无法取得 compose owner（必须 fail-closed）。
#   stop -t -1 worker-after-close → 状态文件置 exited；up ... worker-after-close → running。
if [[ "${1:-}" == "compose" ]]; then
  if [[ "${PANJI_MOCK_COMPOSE_FAIL:-0}" == "1" ]]; then
    exit 1
  fi
  # [P1-A] compose ps -q <service> → 真实容器 ID（建模 Compose 拓扑为 SSOT；
  #   resolver 据此再 docker inspect 真实容器 ID，bare service name 不会解析到）。
  if printf '%s' "$*" | grep -q 'ps -q'; then
    svc=""
    prev=""
    for a in "$@"; do
      if [[ "${prev}" == "-q" ]]; then svc="$a"; fi
      prev="$a"
    done
    if [[ -n "${svc}" ]]; then printf 'CID_%s\n' "${svc}"; fi
    exit 0
  fi
  if printf '%s' "$*" | grep -q 'worker-after-close'; then
    if printf '%s' "$*" | grep -q 'stop'; then
      printf 'exited' > "${WORKER_STATE}"
    fi
    if printf '%s' "$*" | grep -q 'up'; then
      printf 'running' > "${WORKER_STATE}"
    fi
  fi
  exit 0
fi
exit 0
EOF
cat > "${MOCK_BIN}/flock" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
cat > "${MOCK_BIN}/sysctl" <<'EOF'
#!/usr/bin/env bash
[[ "${1:-}" == "-n" && "${2:-}" == "hw.memsize" ]] || exit 2
printf '17179869184\n'
EOF
# [E2.1-R] df mock：部署脚本的"根分区可用 >= 20GB / 使用率上限"是**合法生产安全约束**，
# 不得在脚本侧放宽。开发机根分区常常不足 20GB（本机约 16GB），会让几乎所有走
# preflight 的 dry-run 在资源预算门禁处失败，从而掩盖真正要验证的
# runtime SHA / image identity / rollback 合同。因此这里 mock 出一个充足的
# 根分区，使本 harness 只验证合同本身，不验证宿主机磁盘容量。
# 格式对齐 `df -Pk /`（脚本用 awk 'NR==2 {print $4}' 取 Available，$5 取 Capacity）。
cat > "${MOCK_BIN}/df" <<'EOF'
#!/usr/bin/env bash
printf 'Filesystem 1024-blocks      Used Available Capacity Mounted on\n'
printf '/dev/mock   971350180  400000000  571350180      42%% /\n'
EOF
# [E2.1-R] curl mock：部署脚本用 `curl -s -o /dev/null -w '%{http_code}' http://localhost:8000/v1/health`
# 轮询等待 backend 健康（超时 120s）。开发机 8000 端口没有服务，真实 curl 会
# 每次跑满 120s 超时并把 dry-run 判为失败，既掩盖合同验证也让 harness 极慢。
# 这里 mock 出健康响应；可用 PANJI_MOCK_HEALTH_CODE 覆盖以模拟健康检查失败路径。
cat > "${MOCK_BIN}/curl" <<'EOF'
#!/usr/bin/env bash
code="${PANJI_MOCK_HEALTH_CODE:-200}"
printf '%s' "${code}"
exit 0
EOF
chmod +x "${MOCK_BIN}/git" "${MOCK_BIN}/docker" "${MOCK_BIN}/flock" "${MOCK_BIN}/sysctl" "${MOCK_BIN}/df" "${MOCK_BIN}/curl"

TARGET_SHA="$(git -C "${REPO_ROOT}" rev-parse HEAD)"
ENV_FILE="${TMP_ROOT}/market.env"
STATE_FILE="${TMP_ROOT}/deploy-state"
LOCK_FILE="${TMP_ROOT}/deploy.lock"
LIVE_ROOT="${TMP_ROOT}/live"
printf 'DEPLOYMENT_MODE=live\n' > "${ENV_FILE}"
printf '%s' "${TARGET_SHA}" > "${STATE_FILE}"
ENV_BEFORE="$(shasum -a 256 "${ENV_FILE}" | awk '{print $1}')"
STATE_BEFORE="$(shasum -a 256 "${STATE_FILE}" | awk '{print $1}')"

run_deploy() {
  PATH="${MOCK_BIN}:${PATH}" \
  PANJI_REPO_ROOT="${REPO_ROOT}" \
  PANJI_LIVE_ROOT="${LIVE_ROOT}" \
  PANJI_ENV_FILE="${ENV_FILE}" \
  PANJI_STATE_FILE="${STATE_FILE}" \
  PANJI_LOCK_FILE="${LOCK_FILE}" \
  PANJI_MOCK_WORKER_STATE="${WORKER_STATE_FILE}" \
  bash "${SERVER_SCRIPT}" "$@"
}

echo "== actual dry-run contract =="
OUTPUT_FILE="${TMP_ROOT}/dry-run.log"
if run_deploy "${TARGET_SHA}" --dry-run >"${OUTPUT_FILE}" 2>&1; then
  ok "full SHA dry-run succeeds"
else
  bad "full SHA dry-run succeeds"
  sed -n '1,160p' "${OUTPUT_FILE}" >&2
fi

[[ "$(shasum -a 256 "${ENV_FILE}" | awk '{print $1}')" == "${ENV_BEFORE}" ]] \
  && ok "dry-run keeps environment file unchanged" || bad "dry-run keeps environment file unchanged"
[[ "$(shasum -a 256 "${STATE_FILE}" | awk '{print $1}')" == "${STATE_BEFORE}" ]] \
  && ok "dry-run keeps deployment state unchanged" || bad "dry-run keeps deployment state unchanged"
[[ ! -e "${LIVE_ROOT}" ]] \
  && ok "dry-run does not create live runtime" || bad "dry-run does not create live runtime"
grep -q 'migration_changed=false，跳过' "${OUTPUT_FILE}" \
  && ok "unchanged SHA skips migration" || bad "unchanged SHA skips migration"

# 普通 Live Mount 代码部署（本轮未构建镜像）→ 完全不做 builder prune。
grep -q 'docker builder prune' "${OUTPUT_FILE}" \
  && bad "no-build deploy skips builder prune" || ok "no-build deploy skips builder prune"
grep -q '未构建任何镜像（images_built=false），跳过资源清理' "${OUTPUT_FILE}" \
  && ok "cleanup skip is explicitly reported" || bad "cleanup skip is explicitly reported"

if run_deploy "${TARGET_SHA:0:7}" --dry-run >/dev/null 2>&1; then
  bad "short SHA is rejected"
else
  ok "short SHA is rejected"
fi

if PANJI_MOCK_DIRTY=1 run_deploy "${TARGET_SHA}" --dry-run >/dev/null 2>&1; then
  bad "dirty remote worktree is rejected"
else
  ok "dirty remote worktree is rejected"
fi

# [RESOURCE_GATE_ORDER_DEBT + DRY-RUN ZERO-MUTATION]
# 内存 headroom 不再是"任何状态修改之前"的静态门槛，而是 fence 之后、首笔 mutation 之前的门槛；
# 且 dry-run 默认不读取真实 MemAvailable（deferred）。因此本用例必须显式注入 mock seam，
# 才能在干跑中真正驱动该门槛——否则断言会退化为"dry-run 恰好失败"的假绿。
if PANJI_MIN_MEM_MB=999999 PANJI_MOCK_MEM_AVAILABLE_KB=3500000 \
    run_deploy "${TARGET_SHA}" --dry-run >/dev/null 2>&1; then
  bad "memory headroom failure blocks before first mutation (via mock seam)"
else
  ok "memory headroom failure blocks before first mutation (via mock seam)"
fi

# 反向：无 seam 时干跑必须 deferred（不读真实 MemAvailable、不用不可达门槛否决干跑）。
if PANJI_MIN_MEM_MB=999999 run_deploy "${TARGET_SHA}" --dry-run >/dev/null 2>&1; then
  ok "dry-run defers memory headroom when no mock seam (does not read real MemAvailable)"
else
  bad "dry-run defers memory headroom when no mock seam (does not read real MemAvailable)"
fi

# --- 首次 Live Mount 部署行为 ---
echo "== first live mount bootstrap =="
FIRST_LOG="${TMP_ROOT}/first-live.log"
# 首次 Live Mount 必须传入外层自举前 SHA 作为 fallback（模拟 panji-test-deploy）
if PANJI_MOCK_NO_LIVE_MOUNT=1 PANJI_BOOTSTRAP_PREVIOUS_SHA="${TARGET_SHA}" \
    run_deploy "${TARGET_SHA}" --dry-run >"${FIRST_LOG}" 2>&1; then
  ok "first live deploy dry-run succeeds"
else
  bad "first live deploy dry-run succeeds"
  sed -n '1,160p' "${FIRST_LOG}" >&2
fi
grep -q '首次 Live Mount 部署' "${FIRST_LOG}" \
  && ok "first live deploy is detected" || bad "first live deploy is detected"
grep -q 'first_live_deploy=true' "${FIRST_LOG}" \
  && ok "first live deploy sets the flag" || bad "first live deploy sets the flag"
grep -q 'backend_runtime_changed=true' "${FIRST_LOG}" \
  && ok "first live deploy forces backend sync" || bad "first live deploy forces backend sync"
grep -q 'frontend_runtime_changed=true' "${FIRST_LOG}" \
  && ok "first live deploy forces frontend sync" || bad "first live deploy forces frontend sync"
# 关键契约：首次挂载不得因此强制 migration
grep -q 'migration_changed=false' "${FIRST_LOG}" \
  && ok "first live deploy does NOT force migration" || bad "first live deploy does NOT force migration"

# --- 上一 SHA 四级解析 ---
echo "== previous SHA resolution tiers =="
# 一级：状态文件
grep -q '来源: state_file' "${OUTPUT_FILE}" \
  && ok "tier1 resolves from state file" || bad "tier1 resolves from state file"

# 二级：状态文件缺失时回落 RUNTIME_SHA 文件
TIER2_STATE="${TMP_ROOT}/absent-state"
TIER2_LIVE="${TMP_ROOT}/live-tier2"
mkdir -p "${TIER2_LIVE}"
printf '%s' "${TARGET_SHA}" > "${TIER2_LIVE}/RUNTIME_SHA"
TIER2_LOG="${TMP_ROOT}/tier2.log"
PATH="${MOCK_BIN}:${PATH}" PANJI_REPO_ROOT="${REPO_ROOT}" PANJI_LIVE_ROOT="${TIER2_LIVE}" \
  PANJI_ENV_FILE="${ENV_FILE}" PANJI_STATE_FILE="${TIER2_STATE}" PANJI_LOCK_FILE="${LOCK_FILE}" \
  bash "${SERVER_SCRIPT}" "${TARGET_SHA}" --dry-run >"${TIER2_LOG}" 2>&1 || true
grep -q '来源: runtime_sha_file' "${TIER2_LOG}" \
  && ok "tier2 falls back to RUNTIME_SHA file" || bad "tier2 falls back to RUNTIME_SHA file"
# 关键契约：状态文件缺失不得导致强制 migration
grep -q 'migration_changed=false' "${TIER2_LOG}" \
  && ok "missing state file alone does NOT force migration" \
  || bad "missing state file alone does NOT force migration"

# 三级（P0 修复）：首次 Live Mount 且状态文件/RUNTIME_SHA 均缺失时，
# 必须回落到外层自举前传入的完整 SHA（PANJI_BOOTSTRAP_PREVIOUS_SHA），
# 而非把 checkout 后的 repo HEAD 当作上一 SHA。
TIER3_LIVE="${TMP_ROOT}/live-tier3"
TIER3_LOG="${TMP_ROOT}/tier3.log"
PATH="${MOCK_BIN}:${PATH}" PANJI_REPO_ROOT="${REPO_ROOT}" PANJI_LIVE_ROOT="${TIER3_LIVE}" \
  PANJI_ENV_FILE="${ENV_FILE}" PANJI_STATE_FILE="${TMP_ROOT}/none-state" PANJI_LOCK_FILE="${LOCK_FILE}" \
  PANJI_BOOTSTRAP_PREVIOUS_SHA="${TARGET_SHA}" \
  bash "${SERVER_SCRIPT}" "${TARGET_SHA}" --dry-run >"${TIER3_LOG}" 2>&1 || true
grep -q '来源: bootstrap_previous_sha' "${TIER3_LOG}" \
  && ok "tier3 (first-live) falls back to bootstrap previous SHA" \
  || bad "tier3 (first-live) falls back to bootstrap previous SHA"

# 四级（P0 修复）：首次 Live Mount 且当前运行版本与外层 SHA 都无法确认时，
# 必须停止并报告 previous_runtime_sha_unknown，不得把 TARGET_SHA 当作上一 SHA。
TIER4_LIVE="${TMP_ROOT}/live-tier4"
TIER4_LOG="${TMP_ROOT}/tier4.log"
PATH="${MOCK_BIN}:${PATH}" PANJI_MOCK_NO_LIVE_MOUNT=1 PANJI_REPO_ROOT="${REPO_ROOT}" PANJI_LIVE_ROOT="${TIER4_LIVE}" \
  PANJI_ENV_FILE="${ENV_FILE}" PANJI_STATE_FILE="${TMP_ROOT}/none-state" PANJI_LOCK_FILE="${LOCK_FILE}" \
  bash "${SERVER_SCRIPT}" "${TARGET_SHA}" --dry-run >"${TIER4_LOG}" 2>&1 || true
grep -q 'previous_runtime_sha_unknown' "${TIER4_LOG}" \
  && ok "first-live with no resolvable runtime SHA refuses to deploy" \
  || bad "first-live with no resolvable runtime SHA refuses to deploy"

# 五级（P0 修复）：首次 Live Mount 优先读取当前运行 backend 的 /v1/version，
# 且 7 位短 SHA 必须唯一解析为完整 SHA（优先于外层传入的 fallback）。
# 通过 curl mock 让 /v1/version 返回 7 位短 SHA。
CURL_MOCK_BIN="${TMP_ROOT}/curlbin"
mkdir -p "${CURL_MOCK_BIN}"
SHORT_SHA="${TARGET_SHA:0:7}"
cat > "${CURL_MOCK_BIN}/curl" <<EOF
#!/usr/bin/env bash
# 仅响应 /v1/version，返回含 7 位短 SHA 的 version JSON
printf '{"runtime_git_sha":"${SHORT_SHA}","deployment_mode":"live"}'
EOF
chmod +x "${CURL_MOCK_BIN}/curl"
TIER5_LIVE="${TMP_ROOT}/live-tier5"
TIER5_LOG="${TMP_ROOT}/tier5.log"
PATH="${CURL_MOCK_BIN}:${MOCK_BIN}:${PATH}" PANJI_REPO_ROOT="${REPO_ROOT}" PANJI_LIVE_ROOT="${TIER5_LIVE}" \
  PANJI_ENV_FILE="${ENV_FILE}" PANJI_STATE_FILE="${TMP_ROOT}/none-state" PANJI_LOCK_FILE="${LOCK_FILE}" \
  PANJI_BOOTSTRAP_PREVIOUS_SHA="0000000000000000000000000000000000000000" \
  bash "${SERVER_SCRIPT}" "${TARGET_SHA}" --dry-run >"${TIER5_LOG}" 2>&1 || true
grep -q '来源: running_version' "${TIER5_LOG}" \
  && ok "first-live prefers running version over bootstrap fallback" \
  || bad "first-live prefers running version over bootstrap fallback"
grep -q "上一真实运行 SHA: ${TARGET_SHA}" "${TIER5_LOG}" \
  && ok "short SHA from version resolves to unique full SHA" \
  || bad "short SHA from version resolves to unique full SHA"

# --- 镜像 tag SHA 解析（支持 40 位与 7 位短 SHA）---
echo "== runtime image tag SHA resolution =="
# 自定义 docker mock：首次路径 Mounts 探测 exit 1（判定首次 Live Mount），
# 但 Config.Image 探测返回含 SHA 的镜像 tag。
IMG_MOCK_BIN="${TMP_ROOT}/imgbin"
mkdir -p "${IMG_MOCK_BIN}"
cat > "${IMG_MOCK_BIN}/docker" <<'EOF'
#!/usr/bin/env bash
if [[ "${1:-}" == "inspect" ]]; then
  # Mounts 探测：首次 Live Mount 场景返回空（exit 1）
  if [[ "${3:-}" == *"Mounts"* ]]; then
    exit 1
  fi
  # Config.Image 探测：返回含 SHA 的镜像 tag
  printf '%s' "${PANJI_MOCK_IMAGE_TAG:-market-dev-backend:unknown}"
  exit 0
fi
exit 0
EOF
chmod +x "${IMG_MOCK_BIN}/docker"

# 场景 A：40 位完整 SHA 镜像 tag 解析成功
IMG_A_LIVE="${TMP_ROOT}/live-imga"
IMG_A_LOG="${TMP_ROOT}/img-a.log"
PATH="${IMG_MOCK_BIN}:${MOCK_BIN}:${PATH}" PANJI_MOCK_NO_LIVE_MOUNT=1 \
  PANJI_MOCK_IMAGE_TAG="market-dev-backend:${TARGET_SHA}" \
  PANJI_REPO_ROOT="${REPO_ROOT}" PANJI_LIVE_ROOT="${IMG_A_LIVE}" \
  PANJI_ENV_FILE="${ENV_FILE}" PANJI_STATE_FILE="${TMP_ROOT}/none-state" PANJI_LOCK_FILE="${LOCK_FILE}" \
  bash "${SERVER_SCRIPT}" "${TARGET_SHA}" --dry-run >"${IMG_A_LOG}" 2>&1 || true
grep -q '来源: running_image_tag' "${IMG_A_LOG}" \
  && ok "40-bit image tag resolves to previous SHA" \
  || bad "40-bit image tag resolves to previous SHA"
grep -q "上一真实运行 SHA: ${TARGET_SHA}" "${IMG_A_LOG}" \
  && ok "40-bit image tag yields full SHA" \
  || bad "40-bit image tag yields full SHA"

# 场景 B：7 位短 SHA 镜像 tag 唯一解析为完整 SHA
IMG_B_LIVE="${TMP_ROOT}/live-imgb"
IMG_B_LOG="${TMP_ROOT}/img-b.log"
PATH="${IMG_MOCK_BIN}:${MOCK_BIN}:${PATH}" PANJI_MOCK_NO_LIVE_MOUNT=1 \
  PANJI_MOCK_IMAGE_TAG="market-dev-backend:${SHORT_SHA}" \
  PANJI_REPO_ROOT="${REPO_ROOT}" PANJI_LIVE_ROOT="${IMG_B_LIVE}" \
  PANJI_ENV_FILE="${ENV_FILE}" PANJI_STATE_FILE="${TMP_ROOT}/none-state" PANJI_LOCK_FILE="${LOCK_FILE}" \
  bash "${SERVER_SCRIPT}" "${TARGET_SHA}" --dry-run >"${IMG_B_LOG}" 2>&1 || true
grep -q '来源: running_image_tag' "${IMG_B_LOG}" \
  && ok "7-bit short image tag resolves to previous SHA" \
  || bad "7-bit short image tag resolves to previous SHA"
grep -q "上一真实运行 SHA: ${TARGET_SHA}" "${IMG_B_LOG}" \
  && ok "7-bit short image tag unique-resolves to full SHA" \
  || bad "7-bit short image tag unique-resolves to full SHA"

# 场景 C：version 不可用但 7 位镜像 tag 可用时，优先于 bootstrap fallback
IMG_C_LIVE="${TMP_ROOT}/live-imgc"
IMG_C_LOG="${TMP_ROOT}/img-c.log"
PATH="${IMG_MOCK_BIN}:${MOCK_BIN}:${PATH}" PANJI_MOCK_NO_LIVE_MOUNT=1 \
  PANJI_MOCK_IMAGE_TAG="market-dev-backend:${SHORT_SHA}" \
  PANJI_REPO_ROOT="${REPO_ROOT}" PANJI_LIVE_ROOT="${IMG_C_LIVE}" \
  PANJI_ENV_FILE="${ENV_FILE}" PANJI_STATE_FILE="${TMP_ROOT}/none-state" PANJI_LOCK_FILE="${LOCK_FILE}" \
  PANJI_BOOTSTRAP_PREVIOUS_SHA="0000000000000000000000000000000000000000" \
  bash "${SERVER_SCRIPT}" "${TARGET_SHA}" --dry-run >"${IMG_C_LOG}" 2>&1 || true
grep -q '来源: running_image_tag' "${IMG_C_LOG}" \
  && ok "image tag preferred over bootstrap fallback when version unavailable" \
  || bad "image tag preferred over bootstrap fallback when version unavailable"

# 场景 D：7 位镜像 tag 无法唯一解析时不采用（回落 bootstrap fallback）
IMG_D_LIVE="${TMP_ROOT}/live-imgd"
IMG_D_LOG="${TMP_ROOT}/img-d.log"
# 用合法 hex 7 位串但仓库中不可解析作为 tag，且传入合法 bootstrap fallback
PATH="${IMG_MOCK_BIN}:${MOCK_BIN}:${PATH}" PANJI_MOCK_NO_LIVE_MOUNT=1 \
  PANJI_MOCK_IMAGE_TAG="market-dev-backend:deadbee" \
  PANJI_REPO_ROOT="${REPO_ROOT}" PANJI_LIVE_ROOT="${IMG_D_LIVE}" \
  PANJI_ENV_FILE="${ENV_FILE}" PANJI_STATE_FILE="${TMP_ROOT}/none-state" PANJI_LOCK_FILE="${LOCK_FILE}" \
  PANJI_BOOTSTRAP_PREVIOUS_SHA="${TARGET_SHA}" \
  bash "${SERVER_SCRIPT}" "${TARGET_SHA}" --dry-run >"${IMG_D_LOG}" 2>&1 || true
grep -q '来源: bootstrap_previous_sha' "${IMG_D_LOG}" \
  && ok "unresolvable 7-bit image tag falls back to bootstrap SHA" \
  || bad "unresolvable 7-bit image tag falls back to bootstrap SHA"

# --- RUNTIME_SHA / Mount / 镜像构建计划 ---
echo "== runtime sha and mount plan =="
grep -q '原地写入' "${OUTPUT_FILE}" \
  && ok "RUNTIME_SHA plan states in-place write" || bad "RUNTIME_SHA plan states in-place write"
grep -q "全部 11 个 Python 服务 Mounts" "${OUTPUT_FILE}" \
  && ok "verification plan covers all 11 python services" \
  || bad "verification plan covers all 11 python services"
grep -q '无运行环境变化，跳过镜像构建' "${OUTPUT_FILE}" \
  && ok "no environment change means zero build" || bad "no environment change means zero build"

# --- E2.1 P1-B：部署冲突任务门禁（pending visibility + fail-closed）---
echo "== E2.1 P1-B deployment conflict job gate =="

WORKER_SRC="${REPO_ROOT}/backend/app/worker.py"

# A. worker 正式 claim owner 必须声明 queued / resume_queued
CLAIM_OWNER_LINE="$(grep -n 'SchedulerJobRun.status.in_(' "${WORKER_SRC}" 2>/dev/null | head -1 || true)"
# 注意：源码使用双引号字符串，这里按标识符匹配，不绑定引号风格
if [[ -n "${CLAIM_OWNER_LINE}" ]] \
    && grep -q '"queued"' <<<"${CLAIM_OWNER_LINE}" \
    && grep -q '"resume_queued"' <<<"${CLAIM_OWNER_LINE}"; then
    ok "worker claim owner declares queued and resume_queued"
else
    bad "worker claim owner declares queued and resume_queued"
fi

# B. 部署脚本两套状态集合必须与 owner 一致（结构化读取数组定义，非随机 grep）
DEPLOY_CLAIM_LINE="$(grep -E '^WORKER_CLAIMABLE_STATES=' "${SERVER_SCRIPT}" | head -1 || true)"
DEPLOY_CONFLICT_LINE="$(grep -E '^DEPLOYMENT_CONFLICT_STATES=' "${SERVER_SCRIPT}" | head -1 || true)"
if [[ -n "${DEPLOY_CLAIM_LINE}" ]] \
    && grep -q 'queued' <<<"${DEPLOY_CLAIM_LINE}" \
    && grep -q 'resume_queued' <<<"${DEPLOY_CLAIM_LINE}"; then
    ok "deploy WORKER_CLAIMABLE_STATES matches worker owner"
else
    bad "deploy WORKER_CLAIMABLE_STATES matches worker owner"
fi
# conflict 必须是 claimable 的超集并额外包含 running
if [[ -n "${DEPLOY_CONFLICT_LINE}" ]] \
    && grep -q 'running' <<<"${DEPLOY_CONFLICT_LINE}" \
    && grep -q 'queued' <<<"${DEPLOY_CONFLICT_LINE}" \
    && grep -q 'resume_queued' <<<"${DEPLOY_CONFLICT_LINE}"; then
    ok "deploy DEPLOYMENT_CONFLICT_STATES supersedes claimable + running"
else
    bad "deploy DEPLOYMENT_CONFLICT_STATES supersedes claimable + running"
fi

# C. 状态语义（P1-B 可见性而非阻塞）：
#    queued / resume_queued 仅可见、不阻塞部署（由 supervisor-drain fence 容忍留队）；
#    running 由 supervisor-drain fence 在 backend runtime 变更时拦截（running 必须自然 drain）。
#    用 first-live fixture（PANJI_MOCK_BACKEND_RUNTIME_CHANGED=1）强制门禁/fence 真正执行。
CONFLICT_LOG="${TMP_ROOT}/conflict.log"

# C1. queued 存在但无 running → 部署放行（可见性日志，不 fail-closed）
reset_worker_state exited
Q_LOG="${TMP_ROOT}/conflict.queued"
PANJI_MOCK_NO_LIVE_MOUNT=1 \
PANJI_BOOTSTRAP_PREVIOUS_SHA="${TARGET_SHA}" \
PANJI_MOCK_BACKEND_RUNTIME_CHANGED=1 \
PANJI_MOCK_PSQL_RUNNING=0 \
PANJI_MOCK_PSQL_COUNTS="running:0
queued:1
resume_queued:0" \
PANJI_MOCK_PSQL_ROWS="11111111-2222-3333-4444-555555555555 | after_close_orchestrator | 2026-08-28 | queued | 2026-08-30 10:00:00" \
    run_deploy "${TARGET_SHA}" --dry-run >"${Q_LOG}" 2>&1 && rc=0 || rc=$?
if [[ "${rc}" -eq 0 ]] \
    && grep -q 'DEPLOYMENT_PENDING_AFTER_CLOSE_VISIBLE=TRUE' "${Q_LOG}" \
    && grep -q 'queued_COUNT=1' "${Q_LOG}" \
    && grep -q 'AFTER_CLOSE_PICKUP_FENCE_SIMULATED=true' "${Q_LOG}"; then
    ok "queued state is visible but does NOT block deployment (fence proceeds)"
else
    bad "queued state is visible but does NOT block deployment (fence proceeds)"
fi

# C2. resume_queued 同理
reset_worker_state exited
RQ_LOG="${TMP_ROOT}/conflict.resume"
PANJI_MOCK_NO_LIVE_MOUNT=1 \
PANJI_BOOTSTRAP_PREVIOUS_SHA="${TARGET_SHA}" \
PANJI_MOCK_BACKEND_RUNTIME_CHANGED=1 \
PANJI_MOCK_PSQL_RUNNING=0 \
PANJI_MOCK_PSQL_COUNTS="running:0
queued:0
resume_queued:1" \
PANJI_MOCK_PSQL_ROWS="11111111-2222-3333-4444-555555555555 | after_close_orchestrator | 2026-08-28 | resume_queued | 2026-08-30 10:00:00" \
    run_deploy "${TARGET_SHA}" --dry-run >"${RQ_LOG}" 2>&1 && rc=0 || rc=$?
if [[ "${rc}" -eq 0 ]] \
    && grep -q 'DEPLOYMENT_PENDING_AFTER_CLOSE_VISIBLE=TRUE' "${RQ_LOG}" \
    && grep -q 'resume_queued_COUNT=1' "${RQ_LOG}" \
    && grep -q 'AFTER_CLOSE_PICKUP_FENCE_SIMULATED=true' "${RQ_LOG}"; then
    ok "resume_queued state is visible but does NOT block deployment (fence proceeds)"
else
    bad "resume_queued state is visible but does NOT block deployment (fence proceeds)"
fi

# C3. running 存在且 worker running → supervisor-drain fence 拦截（fail-closed，不 SIGKILL）
reset_worker_state running
RUN_LOG="${TMP_ROOT}/conflict.running"
PANJI_MOCK_NO_LIVE_MOUNT=1 \
PANJI_BOOTSTRAP_PREVIOUS_SHA="${TARGET_SHA}" \
PANJI_MOCK_BACKEND_RUNTIME_CHANGED=1 \
PANJI_MOCK_PSQL_RUNNING=1 \
PANJI_MOCK_PSQL_COUNTS="running:1
queued:0
resume_queued:0" \
    run_deploy "${TARGET_SHA}" --dry-run >"${RUN_LOG}" 2>&1 && rc=0 || rc=$?
if [[ "${rc}" -ne 0 ]] \
    && grep -q 'AFTER_CLOSE_FENCE_RUNNING_REMAINS=1' "${RUN_LOG}"; then
    ok "running job blocked by supervisor-drain fence (fail-closed, no SIGKILL)"
else
    bad "running job blocked by supervisor-drain fence (fail-closed, no SIGKILL)"
fi

# --- E2.1 P1-C：supervisor-drain 进程 fence 合同（stop -t -1 / 线性化 / NOT-fenced fail-closed / owned-aware restore）---
echo "== E2.1 P1-C supervisor-drain fence contract =="

# A. worker running → 部署以 stop -t -1 自然 drain（绝不 SIGKILL），线性化点 = EXITED 且 running==0，
#    成功后由本 deploy 自己 owned 恢复（AFTER_CLOSE_PICKUP_RESTORED）。
#    注：AFTER_CLOSE_WAS_RUNNING / AFTER_CLOSE_FENCE_OWNED 仅为内部状态变量（不落日志），
#    以可观测行为佐证：出现 "stop -t -1" + RESTORED 即证明本 deploy 拥有并恢复了该 worker。
reset_worker_state running
P1C_A_LOG="${TMP_ROOT}/p1c-fence-owned.log"
PANJI_MOCK_NO_LIVE_MOUNT=1 \
PANJI_BOOTSTRAP_PREVIOUS_SHA="${TARGET_SHA}" \
PANJI_MOCK_BACKEND_RUNTIME_CHANGED=1 \
PANJI_MOCK_PSQL_RUNNING=0 \
PANJI_MOCK_PSQL_COUNTS="running:0
queued:0
resume_queued:0" \
    run_deploy "${TARGET_SHA}" --dry-run >"${P1C_A_LOG}" 2>&1 && rc=0 || rc=$?
if [[ "${rc}" -eq 0 ]] \
   && grep -q 'AFTER_CLOSE_PICKUP_FENCE_SIMULATED=true' "${P1C_A_LOG}" \
   && ! grep -q 'stop -t -1' "${P1C_A_LOG}" \
   && ! grep -q 'AFTER_CLOSE_PICKUP_RESTORED=true' "${P1C_A_LOG}"; then
  ok "P1-C: dry-run simulates fence (zero container mutation); real fence preserved in source"
else
  bad "P1-C: dry-run simulates fence (zero container mutation); real fence preserved in source"
fi

# B. 进入部署时 worker 已 exited/missing → 本 deploy 不拥有恢复权，成功也不得擅自 up。
#    可观测行为：不得出现 "stop -t -1"（说明未将其视为 running），且 RESTORE_SKIPPED_NOT_OWNED
#    出现、RESTORED 不出现。
reset_worker_state exited
P1C_B_LOG="${TMP_ROOT}/p1c-not-owned.log"
PANJI_MOCK_NO_LIVE_MOUNT=1 \
PANJI_BOOTSTRAP_PREVIOUS_SHA="${TARGET_SHA}" \
PANJI_MOCK_BACKEND_RUNTIME_CHANGED=1 \
PANJI_MOCK_PSQL_RUNNING=0 \
PANJI_MOCK_PSQL_COUNTS="running:0
queued:0
resume_queued:0" \
    run_deploy "${TARGET_SHA}" --dry-run >"${P1C_B_LOG}" 2>&1 && rc=0 || rc=$?
if [[ "${rc}" -eq 0 ]] \
   && ! grep -q 'stop -t -1' "${P1C_B_LOG}" \
   && grep -q 'AFTER_CLOSE_RESTORE_SKIPPED_NOT_OWNED=true' "${P1C_B_LOG}" \
   && ! grep -q 'AFTER_CLOSE_PICKUP_RESTORED=true' "${P1C_B_LOG}"; then
  ok "P1-C: pre-stopped worker is NOT restored by this deploy (owned-aware)"
else
  bad "P1-C: pre-stopped worker is NOT restored by this deploy (owned-aware)"
fi

# C. NOT-fenced 必须 refuse runtime mutation（fail-closed 二次线性化门禁）。
#    通过源码级断言确认：AFTER_CLOSE_PICKUP_NOT_FENCED 门禁存在于首笔 file mutation 之前。
P1C_C_BODY="$(sed -n '/^deploy() {/,/^}/p' "${SERVER_SCRIPT}")"
if printf '%s' "${P1C_C_BODY}" | grep -q 'AFTER_CLOSE_PICKUP_NOT_FENCED' \
   && printf '%s' "${P1C_C_BODY}" | grep -q '拒绝 runtime mutation'; then
  ok "P1-C: secondary linearization gate refuses mutation when not fenced"
else
  bad "P1-C: secondary linearization gate refuses mutation when not fenced"
fi

# D. fence 不得 SIGKILL：脚本代码中不存在 docker kill / SIGKILL 形式的强制终止
#    （先剥离注释，避免注释中的"绝不 SIGKILL"字样造成误判）。
if printf '%s' "$(sed 's/#.*//' "${SERVER_SCRIPT}")" | grep -qE 'docker +kill|SIGKILL'; then
  bad "P1-C: no SIGKILL / docker kill in deploy script"
else
  ok "P1-C: no SIGKILL / docker kill in deploy script"
fi

# P1-C A': 真实（非 dry-run）fence 仍实际 stop + 恢复 worker（源码为 source-of-truth；
#   dry-run 已改为模拟，故真实 mutation 只能从源码结构证明，避免脆弱的真实部署沙箱运行）。
FENCE_SRC="$(sed -n '/^_fence_after_close_worker() {/,/^}/p' "${SERVER_SCRIPT}")"
RESTORE_SRC="$(sed -n '/^_restore_after_close_pickup_if_owned() {/,/^}/p' "${SERVER_SCRIPT}")"
if printf '%s' "${FENCE_SRC}" | grep -q 'stop -t -1 worker-after-close' \
   && printf '%s' "${RESTORE_SRC}" | grep -q 'up -d --force-recreate worker-after-close'; then
  ok "P1-C: real (non-dry-run) fence still stops & restores worker (source-of-truth)"
else
  bad "P1-C: real fence mutation commands missing in source"
fi

# E. 全部为 0 且 worker exited → 正常放行（沿用 first-live dry-run 成功路径 + 显式计数为 0）
reset_worker_state exited
PANJI_MOCK_NO_LIVE_MOUNT=1 \
PANJI_BOOTSTRAP_PREVIOUS_SHA="${TARGET_SHA}" \
PANJI_MOCK_BACKEND_RUNTIME_CHANGED=1 \
PANJI_MOCK_PSQL_RUNNING=0 \
PANJI_MOCK_PSQL_COUNTS="running:0
queued:0
resume_queued:0" \
    run_deploy "${TARGET_SHA}" --dry-run >"${CONFLICT_LOG}.zero" 2>&1 && rc=0 || rc=$?
if [[ "${rc}" -eq 0 ]] && grep -q 'AFTER_CLOSE_PICKUP_FENCE_SIMULATED=true' "${CONFLICT_LOG}.zero"; then
    ok "all-zero counts with exited worker allow deployment (fenced)"
else
    bad "all-zero counts with exited worker allow deployment (fenced)"
fi

# F. 门禁查询不可用时必须 fail-closed（psql 失败 → 拒绝部署）。
reset_worker_state exited
PANJI_MOCK_NO_LIVE_MOUNT=1 \
PANJI_BOOTSTRAP_PREVIOUS_SHA="${TARGET_SHA}" \
PANJI_MOCK_BACKEND_RUNTIME_CHANGED=1 \
PANJI_MOCK_PSQL_FAIL=1 \
    run_deploy "${TARGET_SHA}" --dry-run >"${CONFLICT_LOG}.unavailable" 2>&1 || unavailable_rc=$?
unavailable_rc="${unavailable_rc:-0}"
if [[ "${unavailable_rc}" -ne 0 ]] && grep -q 'ACTIVE_AFTER_CLOSE_JOB_GATE_UNAVAILABLE' "${CONFLICT_LOG}.unavailable"; then
    ok "unavailable job gate fails closed"
else
    bad "unavailable job gate fails closed"
fi

# G. guard 必须只读：门禁函数体内不得出现任何写操作
GUARD_BODY="$(sed -n '/^guard_active_after_close_jobs() {/,/^}/p' "${SERVER_SCRIPT}")"
if printf '%s' "${GUARD_BODY}" | grep -qiE '^\s*(UPDATE|DELETE|INSERT|psql[^\n]*-c\s*"\s*(UPDATE|DELETE|INSERT))'; then
    bad "job gate is read-only (no queue mutation)"
else
    ok "job gate is read-only (no queue mutation)"
fi
# 只允许 SELECT 形态的 psql 调用
if printf '%s' "${GUARD_BODY}" | grep -q 'FROM scheduler_job_runs'; then
    ok "job gate queries scheduler_job_runs read path"
else
    bad "job gate queries scheduler_job_runs read path"
fi

# --- E2.1 P1-A：pre-deploy runtime manifest / rollback owner ---
echo "== E2.1 P1-A pre-deploy rollback owner =="

MANIFEST_LOG="${TMP_ROOT}/manifest.log"

# A. 正常路径必须在任何 mutation 之前解析出完整 manifest
PANJI_MOCK_PSQL_COUNTS="running:0
queued:0
resume_queued:0" \
    run_deploy "${TARGET_SHA}" --dry-run >"${MANIFEST_LOG}" 2>&1
if grep -q 'ROLLBACK_OWNER_RESOLVED_BEFORE_MUTATION=PASS' "${MANIFEST_LOG}"; then
    ok "pre-deploy rollback owner resolved before mutation"
else
    bad "pre-deploy rollback owner resolved before mutation"
fi
# 复合 owner 必须都出现在 manifest 中（Live-Mount 不是"一个 image tag"）
manifest_ok=true
for key in PRE_DEPLOY_REPO_SHA PRE_DEPLOY_RUNTIME_SHA PRE_DEPLOY_COMPOSE_DIGEST; do
    grep -q "^  ${key}=" "${MANIFEST_LOG}" || manifest_ok=false
done
# per-service immutable container runtime identity（不得只记 backend）
service_image_keys="$(grep -cE '^  PRE_DEPLOY_IMAGE_ID:[A-Za-z0-9_-]+=' "${MANIFEST_LOG}")"
if [[ "${manifest_ok}" == "true" ]] && [[ "${service_image_keys}" -ge 2 ]]; then
    ok "manifest records composite runtime owners incl. per-service image ids"
else
    bad "manifest records composite runtime owners incl. per-service image ids (keys=${service_image_keys})"
fi

# B. 缺失 mandatory owner ⇒ STOP BEFORE MUTATION，且 mutation 计数为 0
MISSING_LOG="${TMP_ROOT}/missing-owner.log"
missing_rc=0
PANJI_MOCK_COMPOSE_FAIL=1 \
PANJI_MOCK_PSQL_COUNTS="running:0
queued:0
resume_queued:0" \
    run_deploy "${TARGET_SHA}" --dry-run >"${MISSING_LOG}" 2>&1 || missing_rc=$?
if [[ "${missing_rc}" -ne 0 ]] \
    && grep -q 'ROLLBACK_OWNER_RESOLVED_BEFORE_MUTATION=FAIL' "${MISSING_LOG}" \
    && grep -q 'PRE_DEPLOY_COMPOSE_DIGEST' "${MISSING_LOG}"; then
    ok "missing rollback owner stops deployment"
else
    bad "missing rollback owner stops deployment"
fi
# 真正的负向对照：不得触达任何 **runtime** mutation 步骤。
# 注意：`git checkout -f <sha>` 发生在 deploy() 之前的 validate/classify 阶段，
# 只改动远端仓库源码树（分类变更范围所必需），**不属于** runtime mutation；
# runtime owner 是 env 文件 / live mount rsync / RUNTIME_SHA / 容器重建。
if grep -qE '\[dry-run\] 原地写入|\[dry-run\] 将更新 .*market\.env|\[dry-run\] rsync|\[dry-run\] 将执行: docker compose.{0,80}up' "${MISSING_LOG}"; then
    bad "missing owner performs zero runtime mutation"
    grep -E '\[dry-run\] 原地写入|\[dry-run\] 将更新 .*market\.env|\[dry-run\] rsync|\[dry-run\] 将执行: docker compose.{0,80}up' "${MISSING_LOG}" >&2
else
    ok "missing owner performs zero runtime mutation"
fi

# C. migration 失败专用路径：服务未 mutation 前不得 recreate 容器
MIGRATION_BODY="$(sed -n '/^handle_migration_failure() {/,/^}/p' "${SERVER_SCRIPT}")"
if printf '%s' "${MIGRATION_BODY}" | grep -qE 'compose.{0,40}up|force-recreate'; then
    bad "migration failure path does not recreate containers"
else
    ok "migration failure path does not recreate containers"
fi
if printf '%s' "${MIGRATION_BODY}" | grep -q '服务未重启'; then
    ok "migration failure path states runtime untouched"
else
    bad "migration failure path states runtime untouched"
fi
# 不得 false-claim 数据库已回滚
if printf '%s' "${MIGRATION_BODY}" | grep -q '没有也不会自动回滚数据库'; then
    ok "migration failure does not claim DB rollback"
else
    bad "migration failure does not claim DB rollback"
fi

# D. rollback 完成调用 ≠ success：必须有独立 verify owner
ROLLBACK_BODY="$(sed -n '/^rollback() {/,/^}/p' "${SERVER_SCRIPT}")"
if printf '%s' "${ROLLBACK_BODY}" | grep -q 'verify_rollback_owner'; then
    ok "rollback calls independent verify owner"
else
    bad "rollback calls independent verify owner"
fi
# 验证失败时不得打印"回滚完成"（该语义属于独立 verify owner）
VERIFY_ROLLBACK_BODY="$(sed -n '/^verify_rollback_owner() {/,/^}/p' "${SERVER_SCRIPT}")"
if printf '%s' "${VERIFY_ROLLBACK_BODY}" | grep -q 'MANUAL_INTERVENTION_REQUIRED=TRUE' \
    && printf '%s' "${ROLLBACK_BODY}" | grep -q '保持 fail-closed，不声称回滚完成'; then
    ok "rollback verification failure is fail-closed"
else
    bad "rollback verification failure is fail-closed"
fi


# ============================================================
# E2.1 P1-A 捕获顺序修正（§4 / §5）
#
# 独立审计发现：resolve_pre_deploy_runtime_owner 原先位于 deploy() 内，
# 而 main() 的顺序是 checkout_target → deploy()，因此
#   PRE_DEPLOY_REPO_SHA       = git rev-parse HEAD  → 读到 candidate(B)
#   PRE_DEPLOY_COMPOSE_DIGEST = compose config      → 读到 candidate(B)
# manifest 于是在「回滚依据」的名义下记录了 B。这比没有 manifest 更危险：
# verify_rollback_owner 是对着 manifest 比对的，会**假通过**。
# ============================================================

# §5-a 结构锁：main() 中 pre-checkout 捕获点必须严格早于 checkout_target
CAPTURE_LINE="$(grep -n '^        capture_pre_checkout_repo_owners$' "${SERVER_SCRIPT}" | head -1 | cut -d: -f1)"
CHECKOUT_LINE="$(grep -n '^        checkout_target$' "${SERVER_SCRIPT}" | head -1 | cut -d: -f1)"
if [[ -n "${CAPTURE_LINE}" && -n "${CHECKOUT_LINE}" ]] && [[ "${CAPTURE_LINE}" -lt "${CHECKOUT_LINE}" ]]; then
    ok "PRE_DEPLOY capture point precedes checkout_target (capture@${CAPTURE_LINE} < checkout@${CHECKOUT_LINE})"
else
    bad "PRE_DEPLOY capture point precedes checkout_target (capture=${CAPTURE_LINE} checkout=${CHECKOUT_LINE})"
fi
# resolve 必须优先使用 pre-checkout 捕获值，而不是重新读（已 checkout 的）repo
RESOLVE_BODY="$(sed -n '/^resolve_pre_deploy_runtime_owner() {/,/^}/p' "${SERVER_SCRIPT}")"
if printf '%s' "${RESOLVE_BODY}" | grep -q 'PRE_CHECKOUT_REPO_SHA' \
    && printf '%s' "${RESOLVE_BODY}" | grep -q 'PRE_CHECKOUT_COMPOSE_DIGEST'; then
    ok "resolve uses pre-checkout captured owners instead of re-reading candidate"
else
    bad "resolve uses pre-checkout captured owners instead of re-reading candidate"
fi
# deploy() 内的兜底调用必须带 skip-if-resolved 守卫，否则会把 candidate 重新捕获
if grep -q 'PRE_DEPLOY_RUNTIME_OWNER_RESOLVED}" != "true"' "${SERVER_SCRIPT}"; then
    ok "deploy() fallback cannot re-capture candidate as PRE_DEPLOY owner"
else
    bad "deploy() fallback cannot re-capture candidate as PRE_DEPLOY owner"
fi

# [E2.1 P1-A §2] resolve 内严禁存在 post-checkout fallback：
# 本函数在 checkout_target 之后运行，任何重新推导都会拿到 candidate(B)。
# 必须先剥离注释再判定：源码里的说明文字同样包含这些命令名。
RESOLVE_CODE="$(printf '%s' "${RESOLVE_BODY}" | sed 's/#.*//')"
if printf '%s' "${RESOLVE_CODE}" | grep -q 'git rev-parse HEAD'; then
    bad "resolve has no post-checkout git rev-parse fallback"
else
    ok "resolve has no post-checkout git rev-parse fallback"
fi
if printf '%s' "${RESOLVE_CODE}" | grep -q 'COMPOSE_CMD} config'; then
    bad "resolve has no post-checkout compose config fallback"
else
    ok "resolve has no post-checkout compose config fallback"
fi

# §5-b 真实 A→B 负向对照：A = HEAD，B = HEAD~1（两个真实可区分 SHA）
AB_FIXTURE_RAN=false
ORDER_LOG="${TMP_ROOT}/capture-order.log"
SHA_A="$(git -C "${REPO_ROOT}" rev-parse HEAD)"
SHA_B="$(git -C "${REPO_ROOT}" rev-parse HEAD~1)"
if [[ "${SHA_A}" != "${SHA_B}" ]] && [[ -z "$(git -C "${REPO_ROOT}" status --porcelain --untracked-files=no)" ]]; then
    PANJI_MOCK_PSQL_COUNTS="running:0
queued:0
resume_queued:0" \
        run_deploy "${SHA_B}" --dry-run >"${ORDER_LOG}" 2>&1 || true
    # 仅当 fixture 真正执行过才需要在末尾恢复仓库（恢复本身会 checkout -f，
    # 无条件执行会连带还原仓库里**其它**未提交的 tracked 改动）。
    AB_FIXTURE_RAN=true
    MANIFEST_REPO_SHA="$(grep -E '^  PRE_DEPLOY_REPO_SHA=' "${ORDER_LOG}" | head -1 | sed 's/^  PRE_DEPLOY_REPO_SHA=//')"
    if [[ "${MANIFEST_REPO_SHA}" == "${SHA_A}" ]]; then
        ok "A->B capture records old runtime A as PRE_DEPLOY_REPO_SHA"
    else
        bad "A->B capture records old runtime A as PRE_DEPLOY_REPO_SHA (manifest=${MANIFEST_REPO_SHA} A=${SHA_A})"
    fi
    if [[ "${MANIFEST_REPO_SHA}" != "${SHA_B}" ]]; then
        ok "A->B capture does not record candidate B as PRE_DEPLOY_REPO_SHA"
    else
        bad "A->B capture does not record candidate B as PRE_DEPLOY_REPO_SHA (captured B=${SHA_B})"
    fi

    # ---- §4 compose A→B 行为级证明 ----
    # compose 文件在 HEAD 与 HEAD~1 之间**无差异**，且 docker 是 mock，
    # 因此无法用真实 compose 内容区分 A/B。这里构造 deterministic fixture：
    # mock `docker compose config` 输出当前 `git rev-parse HEAD`，于是
    #   捕获发生在 checkout 前（repo=A）→ digest = sha256(SHA_A)
    #   若在 checkout 后 fallback 重算（repo=B）→ digest = sha256(SHA_B)
    # 两者必然不同，因此可**行为级**判定捕获时机，而不只是 grep 源码。
    COMPOSE_AB_MOCK="${TMP_ROOT}/compose-ab-mock"
    mkdir -p "${COMPOSE_AB_MOCK}"
    cp -a "${MOCK_BIN}/." "${COMPOSE_AB_MOCK}/"
    cp "${MOCK_BIN}/docker" "${COMPOSE_AB_MOCK}/docker-orig"
    cat > "${COMPOSE_AB_MOCK}/docker" <<'MOCKEOF'
#!/usr/bin/env bash
if [[ "${1:-}" == "compose" ]]; then
  git rev-parse HEAD 2>/dev/null || echo "no-head"
  exit 0
fi
exec "$(dirname "$0")/docker-orig" "$@"
MOCKEOF
    chmod +x "${COMPOSE_AB_MOCK}/docker"

    COMPOSE_AB_LOG="${TMP_ROOT}/compose-ab.log"
    PATH="${COMPOSE_AB_MOCK}:${PATH}" \
    PANJI_REPO_ROOT="${REPO_ROOT}" \
    PANJI_LIVE_ROOT="${LIVE_ROOT}" \
    PANJI_ENV_FILE="${ENV_FILE}" \
    PANJI_STATE_FILE="${STATE_FILE}" \
    PANJI_LOCK_FILE="${LOCK_FILE}" \
    PANJI_MOCK_PSQL_COUNTS="running:0
queued:0
resume_queued:0" \
        bash "${SERVER_SCRIPT}" "${SHA_B}" --dry-run >"${COMPOSE_AB_LOG}" 2>&1 || true

    DIGEST_A="$(printf '%s\n' "${SHA_A}" | shasum -a 256 | awk '{print $1}')"
    DIGEST_B="$(printf '%s\n' "${SHA_B}" | shasum -a 256 | awk '{print $1}')"
    MANIFEST_DIGEST="$(grep -E '^  PRE_DEPLOY_COMPOSE_DIGEST=' "${COMPOSE_AB_LOG}" \
        | head -1 | sed 's/^  PRE_DEPLOY_COMPOSE_DIGEST=//')"
    if [[ "${DIGEST_A}" != "${DIGEST_B}" ]]; then
        ok "compose A->B fixture is deterministic (DIGEST_A != DIGEST_B)"
    else
        bad "compose A->B fixture is deterministic (DIGEST_A != DIGEST_B)"
    fi
    if [[ -n "${MANIFEST_DIGEST}" && "${MANIFEST_DIGEST}" == "${DIGEST_A}" ]]; then
        ok "compose capture records old runtime DIGEST_A"
    else
        bad "compose capture records old runtime DIGEST_A (manifest=${MANIFEST_DIGEST} A=${DIGEST_A})"
    fi
    if [[ "${MANIFEST_DIGEST}" != "${DIGEST_B}" ]]; then
        ok "compose capture does not fall back to candidate DIGEST_B"
    else
        bad "compose capture does not fall back to candidate DIGEST_B (captured B=${DIGEST_B})"
    fi
elif [[ -z "$(git -C "${REPO_ROOT}" status --porcelain --untracked-files=no)" ]]; then
    bad "A->B fixture requires A != B"
else
    bad "A->B fixture requires clean tracked worktree (it performs git checkout -f)"
fi
# 恢复仓库到测试前状态（run_deploy 内部会真的 checkout 到 TARGET_SHA）。
# 必须仅在 fixture 真正跑过时才恢复：否则 `checkout -f` 会无条件还原仓库中
# 其它未提交的 tracked 改动，静默销毁正在进行的工作。
if [[ "${AB_FIXTURE_RAN}" == "true" ]]; then
    git -C "${REPO_ROOT}" checkout -f "${SHA_A}" >/dev/null 2>&1 || true
    git -C "${REPO_ROOT}" checkout -f dev >/dev/null 2>&1 || true
    if [[ "$(git -C "${REPO_ROOT}" rev-parse HEAD)" == "${SHA_A}" ]]; then
        ok "A->B fixture restores repo branch/SHA after real checkout"
    else
        bad "A->B fixture restores repo branch/SHA after real checkout"
    fi
fi

# ============================================================
# §7 pre-file failure 负向对照
#
# 与「owner missing → zero mutation」是**不同**的场景：
# 这里 rollback owner 已正确捕获成功，只是在第一笔 live-file mutation
# （update_env_file）之前就失败（活跃任务门禁拦截）。
# 要求：runtime mutation = 0 且 restore_files_to_previous_sha = 0。
# 若 MUTATION_STAGE 被提前标成 files，handler 会误判并主动 restore，
# 凭空制造 env / rsync / RUNTIME_SHA 三处 mutation —— 正是要闭合的缺陷。
# ============================================================
PRE_FILE_LOG="${TMP_ROOT}/pre-file-failure.log"
PRE_FILE_RC=0
# 必须用 SHA_B（HEAD~1）而非 TARGET_SHA：active job gate 仅在 backend runtime
# 会变更时启用；TARGET_SHA==HEAD 无任何变更，门禁根本不会执行，fixture 无效。
PANJI_MOCK_PSQL_COUNTS="running:1
queued:0
resume_queued:0" \
    run_deploy "${SHA_B}" --dry-run >"${PRE_FILE_LOG}" 2>&1 || PRE_FILE_RC=$?
# fixture 说明：dry-run 下 active job gate 是否启用取决于是否检测到 backend 变更，
# 难以稳定构造出「owner 已捕获 + 门禁拦截」的运行场景。因此这里锁定 handler 的
# pre-mutation(none) 分支语义（源码级），并用可稳定复现的 runtime 断言锁住
# 「restore 未被调用」—— 后者正是提前标 files 会违反的性质。
HANDLER_NONE="$(sed -n '/case "${MUTATION_STAGE}" in/,/esac/p' "${SERVER_SCRIPT}")"
if printf '%s' "${HANDLER_NONE}" | grep -q 'none)'; then
    ok "failure handler has explicit pre-mutation (none) branch"
else
    bad "failure handler has explicit pre-mutation (none) branch"
fi
if printf '%s' "${HANDLER_NONE}" | grep -q '不执行任何文件层恢复，也不执行容器级回滚'; then
    ok "pre-mutation branch performs no file restore and no container rollback"
else
    bad "pre-mutation branch performs no file restore and no container rollback"
fi
if grep -qE '恢复代码与运行文件到 previous SHA|文件层已恢复到' "${PRE_FILE_LOG}"; then
    bad "pre-file failure performs zero restore_files_to_previous_sha"
    grep -E '恢复代码与运行文件到 previous SHA|文件层已恢复到' "${PRE_FILE_LOG}" >&2
else
    ok "pre-file failure performs zero restore_files_to_previous_sha"
fi
if printf '%s' "${HANDLER_NONE}" | grep -q 'mutation_stage=none'; then
    ok "pre-file failure reports real mutation stage owner (stage=none)"
else
    bad "pre-file failure reports real mutation stage owner (stage=none)"
fi

# ============================================================
# §6 mutation stage 线性化：不得在 build / 纯检查 / 分类之前提前标 files
# ============================================================
MUT_HELPER="$(sed -n '/^_mark_files_mutated() {/,/^}/p' "${SERVER_SCRIPT}")"
if printf '%s' "${MUT_HELPER}" | grep -q 'MUTATION_STAGE="files"'; then
    ok "file mutation stage is advanced by a dedicated owner"
else
    bad "file mutation stage is advanced by a dedicated owner"
fi
DEPLOY_HEAD="$(sed -n '/^deploy() {/,/^    # 1\. 运行环境镜像/p' "${SERVER_SCRIPT}")"
if printf '%s' "${DEPLOY_HEAD}" | grep -q 'MUTATION_STAGE="files"'; then
    bad "deploy() does not pre-mark stage=files before build"
else
    ok "deploy() does not pre-mark stage=files before build"
fi
mut_all=true
for mut in update_env_file sync_backend_runtime sync_frontend_runtime write_runtime_sha; do
    mut_body="$(sed -n "/^${mut}() {/,/^}/p" "${SERVER_SCRIPT}")"
    printf '%s' "${mut_body}" | grep -q '_mark_files_mutated' || mut_all=false
done
if [[ "${mut_all}" == "true" ]]; then
    ok "all file-layer mutators advance mutation stage themselves"
else
    bad "all file-layer mutators advance mutation stage themselves"
fi

# ============================================================
# §9 shared image_ref 冲突契约：禁止顺序 docker tag 静默覆盖
# ============================================================
PIN_BODY="$(sed -n '/^_pin_predeploy_image_refs() {/,/^}/p' "${SERVER_SCRIPT}")"
if printf '%s' "${PIN_BODY}" | grep -q 'ref_to_id\[' \
    && printf '%s' "${PIN_BODY}" | grep -q 'conflict'; then
    ok "shared image_ref collision is detected before any tag"
else
    bad "shared image_ref collision is detected before any tag"
fi
if printf '%s' "${PIN_BODY}" | grep -q '禁止用一个服务覆盖另一个服务的镜像引用'; then
    ok "shared image_ref collision fails closed without tagging"
else
    bad "shared image_ref collision fails closed without tagging"
fi


# ============================================================
# §5 / §6 / §7 — 行为级：真正**调用** owner 函数，不是 grep 源码
#
# manifest 记录了 4 类 owner；若 verify 只校验其中一部分，rollback 会假通过。
# 这里逐个破坏 mandatory owner，证明校验与 manifest 对称。
# ============================================================
VERIFY_LIB="${TMP_ROOT}/verify-lib.sh"
# [P1-A] verify 已改为经 compose service → container ID → image ID 解析，
# 因此行为级 lib 必须连同两个 resolver helper 一起抽取，否则只是 command-not-found 假红。
sed -n '/^_compose_container_id_for_service() {/,/^}/p' "${SERVER_SCRIPT}" > "${VERIFY_LIB}"
sed -n '/^_container_image_id_for_service() {/,/^}/p' "${SERVER_SCRIPT}" >> "${VERIFY_LIB}"
sed -n '/^verify_rollback_owner() {/,/^}/p' "${SERVER_SCRIPT}" >> "${VERIFY_LIB}"
PIN_LIB="${TMP_ROOT}/pin-lib.sh"
sed -n '/^_pin_predeploy_image_refs() {/,/^}/p' "${SERVER_SCRIPT}" > "${PIN_LIB}"

BEHAV_MOCK="${TMP_ROOT}/behav-mock"
mkdir -p "${BEHAV_MOCK}"
BEHAV_TAG_LOG="${TMP_ROOT}/behav-tags.log"
: > "${BEHAV_TAG_LOG}"
cat > "${BEHAV_MOCK}/docker" <<'MOCKEOF'
#!/usr/bin/env bash
if [[ "${1:-}" == "inspect" ]]; then
  # [P1-A] 目标是 compose ps -q 解析出的容器 ID（CID_<service>），不是 bare service 名。
  target=""
  for a in "$@"; do
    [[ "${a}" == "inspect" ]] && continue
    [[ "${a}" == -* ]] && continue
    [[ "${a}" == *'{{'* ]] && continue
    target="${a}"
  done
  svc="${target#CID_}"
  var="BEHAV_IMG_${svc//-/_}"
  printf '%s\n' "${!var:-}"
  exit 0
fi
if [[ "${1:-}" == "compose" ]]; then
  # `compose ps -q <svc>` → 容器 ID；其余（config 等）→ compose 定义内容。
  prev=""; svc=""
  for a in "$@"; do
    [[ "${prev}" == "-q" ]] && svc="${a}"
    prev="${a}"
  done
  if [[ -n "${svc}" ]]; then printf 'CID_%s\n' "${svc}"; exit 0; fi
  printf '%s\n' "${BEHAV_COMPOSE_OUT:-}"
  exit 0
fi
if [[ "${1:-}" == "tag" ]]; then
  printf '%s -> %s\n' "${2}" "${3}" >> "${BEHAV_TAG_LOG}"
  exit 0
fi
exit 0
MOCKEOF
chmod +x "${BEHAV_MOCK}/docker"

# repo/live identity owner 需要一个真实 git repo
BEHAV_REPO="${TMP_ROOT}/behav-repo"
mkdir -p "${BEHAV_REPO}"
git -C "${BEHAV_REPO}" init -q
git -C "${BEHAV_REPO}" config user.email t@example.com
git -C "${BEHAV_REPO}" config user.name t
echo a > "${BEHAV_REPO}/f"
git -C "${BEHAV_REPO}" add f
git -C "${BEHAV_REPO}" commit -q -m a
BEHAV_REPO_SHA="$(git -C "${BEHAV_REPO}" rev-parse HEAD)"
BEHAV_LIVE="${TMP_ROOT}/behav-live"
mkdir -p "${BEHAV_LIVE}"
BEHAV_MANIFEST="${TMP_ROOT}/behav-manifest"
BEHAV_RT_SHA="aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111"
BEHAV_CONTENT_A="compose-content-A"
BEHAV_DIGEST_A="$(printf '%s\n' "${BEHAV_CONTENT_A}" | shasum -a 256 | awk '{print $1}')"

write_behav_manifest() {
  cat > "${BEHAV_MANIFEST}" <<EOF
PRE_DEPLOY_REPO_SHA=$1
PRE_DEPLOY_RUNTIME_SHA=$2
PRE_DEPLOY_COMPOSE_DIGEST=$3
PRE_DEPLOY_IMAGE_ID:backend=$4
PRE_DEPLOY_IMAGE_ID:worker-after-close=$5
PRE_DEPLOY_IMAGE_ID:frontend=$6
EOF
}

run_behav_verify() {
  PATH="${BEHAV_MOCK}:${PATH}" \
  BEHAV_TAG_LOG="${BEHAV_TAG_LOG}" \
  bash -c "
    log() { printf 'V: %s\n' \"\$1\"; }
    REPO_ROOT='${BEHAV_REPO}'
    LIVE_ROOT='${BEHAV_LIVE}'
    COMPOSE_CMD='docker compose -f docker-compose.prod.yml'
    PRE_DEPLOY_MANIFEST_FILE='${BEHAV_MANIFEST}'
    source '${VERIFY_LIB}'
    verify_rollback_owner
  "
}

# ---- happy path：四个 owner 全部恢复 → 必须 PASS ----
printf '%s\n' "${BEHAV_RT_SHA}" > "${BEHAV_LIVE}/RUNTIME_SHA"
write_behav_manifest "${BEHAV_REPO_SHA}" "${BEHAV_RT_SHA}" "${BEHAV_DIGEST_A}" \
  IMG_BACKEND_A IMG_WORKER_A IMG_FRONTEND_A
if BEHAV_IMG_backend=IMG_BACKEND_A BEHAV_IMG_worker_after_close=IMG_WORKER_A \
   BEHAV_IMG_frontend=IMG_FRONTEND_A BEHAV_COMPOSE_OUT="${BEHAV_CONTENT_A}" \
   run_behav_verify >/dev/null 2>&1; then
  ok "rollback verify PASSES when all four owners restored"
else
  bad "rollback verify PASSES when all four owners restored"
fi

# ---- A. RUNTIME_SHA 错误 → FAIL ----
printf '%s\n' "dead2222dead2222dead2222dead2222dead2222" > "${BEHAV_LIVE}/RUNTIME_SHA"
if BEHAV_IMG_backend=IMG_BACKEND_A BEHAV_IMG_worker_after_close=IMG_WORKER_A \
   BEHAV_IMG_frontend=IMG_FRONTEND_A BEHAV_COMPOSE_OUT="${BEHAV_CONTENT_A}" \
   run_behav_verify >/dev/null 2>&1; then
  bad "rollback verify FAILS on wrong RUNTIME_SHA"
else
  ok "rollback verify FAILS on wrong RUNTIME_SHA"
fi
printf '%s\n' "${BEHAV_RT_SHA}" > "${BEHAV_LIVE}/RUNTIME_SHA"

# ---- B. compose digest 错误 → FAIL ----
if BEHAV_IMG_backend=IMG_BACKEND_A BEHAV_IMG_worker_after_close=IMG_WORKER_A \
   BEHAV_IMG_frontend=IMG_FRONTEND_A BEHAV_COMPOSE_OUT="other-content" \
   run_behav_verify >/dev/null 2>&1; then
  bad "rollback verify FAILS on wrong compose digest"
else
  ok "rollback verify FAILS on wrong compose digest"
fi

# ---- C. 单个 service immutable image 错误 → FAIL ----
if BEHAV_IMG_backend=IMG_BACKEND_A BEHAV_IMG_worker_after_close=WRONG_IMG \
   BEHAV_IMG_frontend=IMG_FRONTEND_A BEHAV_COMPOSE_OUT="${BEHAV_CONTENT_A}" \
   run_behav_verify >/dev/null 2>&1; then
  bad "rollback verify FAILS on wrong per-service image"
else
  ok "rollback verify FAILS on wrong per-service image"
fi

# ---- D. repo/live identity 错误 → FAIL ----
write_behav_manifest "0000000000000000000000000000000000000abc" "${BEHAV_RT_SHA}" \
  "${BEHAV_DIGEST_A}" IMG_BACKEND_A IMG_WORKER_A IMG_FRONTEND_A
if BEHAV_IMG_backend=IMG_BACKEND_A BEHAV_IMG_worker_after_close=IMG_WORKER_A \
   BEHAV_IMG_frontend=IMG_FRONTEND_A BEHAV_COMPOSE_OUT="${BEHAV_CONTENT_A}" \
   run_behav_verify >/dev/null 2>&1; then
  bad "rollback verify FAILS on wrong repo/live identity"
else
  ok "rollback verify FAILS on wrong repo/live identity"
fi

# ============================================================
# §6 per-service exact restore：backend→IMAGE_A, worker→IMAGE_B, frontend→IMAGE_C
#    任一串线（例如 worker 被钉到 IMAGE_A）都必须 FAIL。
# ============================================================
cat > "${BEHAV_MANIFEST}" <<'EOF'
PRE_DEPLOY_IMAGE_ID:backend=IMAGE_A
PRE_DEPLOY_IMAGE_ID:worker-after-close=IMAGE_B
PRE_DEPLOY_IMAGE_ID:frontend=IMAGE_C
PRE_DEPLOY_IMAGE_REF:backend=repo/backend:old
PRE_DEPLOY_IMAGE_REF:worker-after-close=repo/worker:old
PRE_DEPLOY_IMAGE_REF:frontend=repo/frontend:old
EOF
: > "${BEHAV_TAG_LOG}"
PATH="${BEHAV_MOCK}:${PATH}" BEHAV_TAG_LOG="${BEHAV_TAG_LOG}" bash -c "
  log() { printf 'P: %s\n' \"\$1\"; }
  PRE_DEPLOY_MANIFEST_FILE='${BEHAV_MANIFEST}'
  source '${PIN_LIB}'
  _pin_predeploy_image_refs
" >/dev/null 2>&1 || true
if grep -q '^IMAGE_A -> repo/backend:old$' "${BEHAV_TAG_LOG}" \
  && grep -q '^IMAGE_B -> repo/worker:old$' "${BEHAV_TAG_LOG}" \
  && grep -q '^IMAGE_C -> repo/frontend:old$' "${BEHAV_TAG_LOG}"; then
  ok "per-service exact restore maps each service to its own immutable image"
else
  bad "per-service exact restore maps each service to its own immutable image"
  sed 's/^/    /' "${BEHAV_TAG_LOG}" >&2
fi

# ============================================================
# §7 shared image_ref 冲突：同一 REF 对应 IMAGE_A + IMAGE_B
#    必须 non-zero 且 docker tag count = 0（禁止静默覆盖）。
# ============================================================
cat > "${BEHAV_MANIFEST}" <<'EOF'
PRE_DEPLOY_IMAGE_ID:backend=IMAGE_A
PRE_DEPLOY_IMAGE_ID:worker-after-close=IMAGE_B
PRE_DEPLOY_IMAGE_REF:backend=repo/shared:old
PRE_DEPLOY_IMAGE_REF:worker-after-close=repo/shared:old
EOF
: > "${BEHAV_TAG_LOG}"
PIN_COLLISION_RC=0
PATH="${BEHAV_MOCK}:${PATH}" BEHAV_TAG_LOG="${BEHAV_TAG_LOG}" bash -c "
  log() { printf 'P: %s\n' \"\$1\"; }
  PRE_DEPLOY_MANIFEST_FILE='${BEHAV_MANIFEST}'
  source '${PIN_LIB}'
  _pin_predeploy_image_refs
" >/dev/null 2>&1 || PIN_COLLISION_RC=$?
if [[ "${PIN_COLLISION_RC}" -ne 0 ]] && [[ ! -s "${BEHAV_TAG_LOG}" ]]; then
  ok "image_ref collision fails closed with zero docker tag (rc=${PIN_COLLISION_RC})"
else
  bad "image_ref collision fails closed with zero docker tag (rc=${PIN_COLLISION_RC})"
  sed 's/^/    /' "${BEHAV_TAG_LOG}" >&2
fi


# ============================================================
# §8 captured immutable owner survivability
#
# manifest 记住的 image ID 必须在 capture → 可能的 rollback 之间仍然存在。
# 两个删除向量：
#   (1) `docker image prune -f` 删除 dangling image —— 容器重建到新镜像后，
#       旧 owner 可能已无任何 tag 引用；
#   (2) 旧 SHA 镜像组回收 `docker rmi market-dev-*:<sha>`。
# ============================================================
CLEANUP_BODY="$(sed -n '/^cleanup_resources() {/,/^}/p' "${SERVER_SCRIPT}")"

# --- 行为级：protect owner 必须给每个 captured ID 打上稳定保护标签 ---
PROTECT_LIB="${TMP_ROOT}/protect-lib.sh"
sed -n '/^protect_pre_deploy_image_owners() {/,/^}/p' "${SERVER_SCRIPT}" > "${PROTECT_LIB}"
: > "${BEHAV_TAG_LOG}"
cat > "${BEHAV_MANIFEST}" <<'EOF'
PRE_DEPLOY_IMAGE_ID:backend=sha256:AAA
PRE_DEPLOY_IMAGE_ID:worker-after-close=sha256:BBB
EOF
PATH="${BEHAV_MOCK}:${PATH}" BEHAV_TAG_LOG="${BEHAV_TAG_LOG}" bash -c "
  log() { :; }
  run_cmd() { \"\$@\"; }
  PRE_DEPLOY_MANIFEST_FILE='${BEHAV_MANIFEST}'
  source '${PROTECT_LIB}'
  protect_pre_deploy_image_owners
" >/dev/null 2>&1 || true
if grep -q '^sha256:AAA -> panji-rollback-keep:backend-AAA$' "${BEHAV_TAG_LOG}" \
  && grep -q '^sha256:BBB -> panji-rollback-keep:worker-after-close-BBB$' "${BEHAV_TAG_LOG}"; then
  ok "captured immutable owners receive stable rollback protection tags"
else
  bad "captured immutable owners receive stable rollback protection tags"
  sed 's/^/    /' "${BEHAV_TAG_LOG}" >&2
fi

# --- 行为级：保护必须先于 destructive prune（顺序即 invariant） ---
PROTECT_AT="$(printf '%s' "${CLEANUP_BODY}" | grep -n 'protect_pre_deploy_image_owners' | head -1 | cut -d: -f1)"
PRUNE_AT="$(printf '%s' "${CLEANUP_BODY}" | grep -n 'docker image prune' | head -1 | cut -d: -f1)"
if [[ -n "${PROTECT_AT}" && -n "${PRUNE_AT}" && "${PROTECT_AT}" -lt "${PRUNE_AT}" ]]; then
  ok "owner protection runs before destructive image prune (${PROTECT_AT} < ${PRUNE_AT})"
else
  bad "owner protection runs before destructive image prune (protect=${PROTECT_AT} prune=${PRUNE_AT})"
fi

# --- 旧 SHA 组回收必须按 content ID 跳过受保护的 owner ---
# SHA tag 与 image content 并非一一对应（tag 可被重新指向别的 content），
# 因此只有 content ID 才是可靠判据。
if printf '%s' "${CLEANUP_BODY}" | grep -q 'KEEP_IMAGE_IDS' \
  && printf '%s' "${CLEANUP_BODY}" | grep -q '跳过回收'; then
  ok "old image reclamation skips captured rollback owners by content id"
else
  bad "old image reclamation skips captured rollback owners by content id"
fi

# ============================================================
# §10 RESOURCE_GATE_ORDER_DEBT 修复契约（E3 前发现的部署脚本缺陷）
#
# 核心不变量：部署内存 headroom 必须在 supervisor-drain fence 之后、首笔
# runtime mutation 之前检查；不得在 fence 之前用 MemAvailable 门槛阻止部署。
# post-deploy 不得把"worker 被 fence 停掉时的临时状态"当作稳态 host 空闲下限。
# worker-after-close 必须在 save_state / 宣布成功之前恢复运行。
# ============================================================
echo "== RESOURCE_GATE_ORDER_DEBT 修复契约 =="

DEPLOY_BODY_RG="$(sed -n '/^deploy() {/,/^}/p' "${SERVER_SCRIPT}")"
MAIN_BODY_RG="$(sed -n '/^main() {/,/^}/p' "${SERVER_SCRIPT}")"

# CASE C: headroom 检查必须位于 fence 之后（fence 失败 → headroom 永不授权 mutation）
FENCE_CALL_AT="$(printf '%s' "${DEPLOY_BODY_RG}" | grep -n '_fence_after_close_worker' | head -1 | cut -d: -f1)"
HEADROOM_AT="$(printf '%s' "${DEPLOY_BODY_RG}" | grep -n 'check_deployment_memory_headroom' | head -1 | cut -d: -f1)"
if [[ -n "${FENCE_CALL_AT}" && -n "${HEADROOM_AT}" && "${FENCE_CALL_AT}" -lt "${HEADROOM_AT}" ]]; then
    ok "CASE C: headroom check placed AFTER fence (fence fail → headroom never authorizes mutation)"
else
    bad "CASE C: headroom check placed AFTER fence (fence=${FENCE_CALL_AT} headroom=${HEADROOM_AT})"
fi

# CASE F / G / I: restore worker before save_state; final health after restore; save_state last
# 最终稳态健康检查（main 成功路径）出现处的相对行号
FINAL_HEALTH_AT="$(printf '%s' "${MAIN_BODY_RG}" | grep -n 'post_deploy_resource_check' | tail -1 | cut -d: -f1)"
# 取"严格位于最终健康检查之前"的最后一次 restore（即成功路径 restore，而非健康失败分支内嵌 restore）
RESTORE_AT="$(printf '%s' "${MAIN_BODY_RG}" | grep -n '_restore_after_close_pickup_if_owned' | awk -F: -v h="${FINAL_HEALTH_AT}" '$1 < h {last=$1} END{print last}')"
SAVE_AT="$(printf '%s' "${MAIN_BODY_RG}" | grep -n 'save_state "${TARGET_SHA}"' | head -1 | cut -d: -f1)"
if [[ -n "${RESTORE_AT}" && -n "${SAVE_AT}" && "${RESTORE_AT}" -lt "${SAVE_AT}" ]]; then
    ok "CASE F: worker-after-close restored BEFORE save_state (restore failure → no success declared)"
else
    bad "CASE F: worker-after-close restored BEFORE save_state (restore=${RESTORE_AT} save=${SAVE_AT})"
fi
if [[ -n "${RESTORE_AT}" && -n "${FINAL_HEALTH_AT}" && "${RESTORE_AT}" -lt "${FINAL_HEALTH_AT}" ]]; then
    ok "CASE G: final steady-state runtime health check runs AFTER worker restore"
else
    bad "CASE G: final steady-state runtime health check runs AFTER worker restore"
fi
if [[ -n "${RESTORE_AT}" && -n "${FINAL_HEALTH_AT}" && -n "${SAVE_AT}" \
      && "${RESTORE_AT}" -lt "${FINAL_HEALTH_AT}" && "${FINAL_HEALTH_AT}" -lt "${SAVE_AT}" ]]; then
    ok "CASE I: save_state occurs only AFTER worker restore AND final runtime health PASS"
else
    bad "CASE I: save_state occurs only AFTER worker restore AND final runtime health PASS"
fi

# CASE H 已提升为行为级契约（见下方“行为级”段），这里不再用结构化 grep 假证明（false-green）。

# ---- 行为级：真正调用 owner，验证 headroom 在 fence 之后按注入内存判定 ----
# CASE A: backend runtime 部署，worker running，fence 后 MemAvailable=4300 → 允许；首笔 mutation 在 fence 之后
echo "== CASE A: backend-runtime deploy, worker running, post-fence MemAvailable=4300 → allowed, fence before mutation =="
reset_worker_state "running"
CASE_A_LOG="${TMP_ROOT}/case-a.log"
PANJI_MOCK_BACKEND_RUNTIME_CHANGED=1 \
PANJI_MOCK_MEM_AVAILABLE_KB=4403200 \
    run_deploy "${TARGET_SHA}" --dry-run >"${CASE_A_LOG}" 2>&1
CASE_A_RC=$?
if [[ "${CASE_A_RC}" -eq 0 ]] \
   && grep -q 'AFTER_CLOSE_PICKUP_FENCE_SIMULATED=true' "${CASE_A_LOG}" \
   && ! grep -q 'AFTER_CLOSE_PICKUP_FENCED=true' "${CASE_A_LOG}" \
   && grep -q 'deployment headroom: mem_available=4300' "${CASE_A_LOG}" \
   && grep -q '部署成功' "${CASE_A_LOG}"; then
    ok "CASE A: backend deploy proceeds after post-fence headroom PASS (fence before mutation)"
else
    bad "CASE A: backend deploy proceeds after post-fence headroom PASS (rc=${CASE_A_RC})"
    sed 's/^/    /' "${CASE_A_LOG}" >&2
fi
A_FENCE="$(grep -n 'AFTER_CLOSE_PICKUP_FENCE_SIMULATED=true' "${CASE_A_LOG}" | head -1 | cut -d: -f1)"
A_HEAD="$(grep -n 'deployment headroom: mem_available=4300' "${CASE_A_LOG}" | head -1 | cut -d: -f1)"
A_SUCC="$(grep -n '部署成功' "${CASE_A_LOG}" | head -1 | cut -d: -f1)"
if [[ -n "${A_FENCE}" && -n "${A_HEAD}" && -n "${A_SUCC}" \
      && "${A_FENCE}" -lt "${A_HEAD}" && "${A_HEAD}" -lt "${A_SUCC}" ]]; then
    ok "CASE A: fence precedes headroom check precedes success"
else
    bad "CASE A: fence precedes headroom check precedes success (fence=${A_FENCE} head=${A_HEAD} succ=${A_SUCC})"
fi

# CASE B: backend runtime 部署，worker running，fence 后 MemAvailable=3800 → FAIL；零 mutation；worker 被恢复
echo "== CASE B: backend-runtime deploy, worker running, post-fence MemAvailable=3800 → FAIL, zero mutation, worker restored =="
reset_worker_state "running"
CASE_B_LOG="${TMP_ROOT}/case-b.log"
CASE_B_RC=0
PANJI_MOCK_BACKEND_RUNTIME_CHANGED=1 \
PANJI_MOCK_MEM_AVAILABLE_KB=3891200 \
    run_deploy "${TARGET_SHA}" --dry-run >"${CASE_B_LOG}" 2>&1 || CASE_B_RC=$?
if [[ "${CASE_B_RC}" -ne 0 ]] \
   && grep -q 'DEPLOYMENT_MEMORY_HEADROOM_INSUFFICIENT=true' "${CASE_B_LOG}" \
   && ! grep -qE 'rsync|原地写入 RUNTIME_SHA|\[dry-run\] 将更新' "${CASE_B_LOG}" \
   && ! grep -qE 'stop -t -1|up .*force-recreate|AFTER_CLOSE_PICKUP_RESTORED=true' "${CASE_B_LOG}"; then
    ok "CASE B: post-fence insufficient headroom → zero runtime mutation (dry-run restores nothing)"
else
    bad "CASE B: post-fence insufficient headroom → zero runtime mutation + worker restored (rc=${CASE_B_RC})"
    sed 's/^/    /' "${CASE_B_LOG}" >&2
fi

# CASE D: queued/resume_queued 可见，backend 部署，MemAvailable=4300 → 不阻塞（queue 不被 mutate）
echo "== CASE D: queued visible, backend deploy, MemAvailable=4300 → proceeds (queue untouched) =="
reset_worker_state "exited"
CASE_D_LOG="${TMP_ROOT}/case-d.log"
PANJI_MOCK_BACKEND_RUNTIME_CHANGED=1 \
PANJI_MOCK_PSQL_COUNTS="running:0
queued:1
resume_queued:0" \
PANJI_MOCK_MEM_AVAILABLE_KB=4403200 \
    run_deploy "${TARGET_SHA}" --dry-run >"${CASE_D_LOG}" 2>&1
CASE_D_RC=$?
if [[ "${CASE_D_RC}" -eq 0 ]] \
   && grep -q 'AFTER_CLOSE_PICKUP_FENCE_SIMULATED=true' "${CASE_D_LOG}" \
   && ! grep -q 'DEPLOYMENT_MEMORY_HEADROOM_INSUFFICIENT=true' "${CASE_D_LOG}"; then
    ok "CASE D: queued jobs do not block deploy; dry-run fence simulated"
else
    bad "CASE D: queued jobs do not block deploy (rc=${CASE_D_RC})"
    sed 's/^/    /' "${CASE_D_LOG}" >&2
fi

# CASE E: frontend-only，MemAvailable=3800 < 4096 → FAIL；不得停止 worker-after-close
echo "== CASE E: frontend-only, MemAvailable=3800 < 4096 → FAIL before frontend mutation; worker NOT stopped =="
reset_worker_state "running"
CASE_E_LOG="${TMP_ROOT}/case-e.log"
CASE_E_RC=0
PANJI_MOCK_FRONTEND_RUNTIME_CHANGED=1 \
PANJI_MOCK_MEM_AVAILABLE_KB=3891200 \
    run_deploy "${TARGET_SHA}" --dry-run >"${CASE_E_LOG}" 2>&1 || CASE_E_RC=$?
if [[ "${CASE_E_RC}" -ne 0 ]] \
   && ! grep -q 'AFTER_CLOSE_PICKUP_FENCED=true' "${CASE_E_LOG}" \
   && ! grep -q 'stop -t -1' "${CASE_E_LOG}" \
   && grep -q 'DEPLOYMENT_MEMORY_HEADROOM_INSUFFICIENT=true' "${CASE_E_LOG}"; then
    ok "CASE E: frontend-only below headroom FAILs WITHOUT stopping worker-after-close"
else
    bad "CASE E: frontend-only below headroom FAILs WITHOUT stopping worker-after-close (rc=${CASE_E_RC})"
    sed 's/^/    /' "${CASE_E_LOG}" >&2
fi

# ---- CASE H（行为级，替代原 false-green 结构化断言）----
# 最终稳态健康检查失败时，必须在回滚 backend runtime mutation 之前重新建立 after-close fence。
echo "== CASE H: final steady-state health FAIL → re-fence before rollback file mutation (behavioral) =="
reset_worker_state "running"
CASE_H_LOG="${TMP_ROOT}/case-h.log"
CASE_H_RC=0
PATH="${MOCK_BIN}:${PATH}" \
PANJI_REPO_ROOT="${REPO_ROOT}" PANJI_LIVE_ROOT="${LIVE_ROOT}" \
PANJI_ENV_FILE="${ENV_FILE}" PANJI_STATE_FILE="${STATE_FILE}" PANJI_LOCK_FILE="${LOCK_FILE}" \
PANJI_MOCK_WORKER_STATE="${WORKER_STATE_FILE}" \
PANJI_MOCK_BACKEND_RUNTIME_CHANGED=1 \
PANJI_MOCK_POST_DEPLOY_FAIL_FINAL=1 \
PANJI_MOCK_MEM_AVAILABLE_KB=4403200 \
    bash "${SERVER_SCRIPT}" "${TARGET_SHA}" --dry-run >"${CASE_H_LOG}" 2>&1 || CASE_H_RC=$?
if [[ "${CASE_H_RC}" -ne 0 ]] \
   && grep -q 'AFTER_CLOSE_PICKUP_FENCE_SIMULATED=true' "${CASE_H_LOG}" \
   && grep -q '恢复代码与运行文件到 previous SHA' "${CASE_H_LOG}" \
   && grep -q 'AFTER_CLOSE_PICKUP_FENCED=false' "${CASE_H_LOG}"; then
    ok "CASE H: final-health FAIL drives re-fence (simulated) before rollback (rc!=0, rollback ran, state reset)"
else
    bad "CASE H: final-health FAIL drives re-fence (simulated) before rollback (rc=${CASE_H_RC})"
    sed 's/^/    /' "${CASE_H_LOG}" >&2
fi
# 顺序契约：第一次 simulate fence 之后，第二次 simulate fence 必须出现在回滚文件 mutation 之前
H_RESTORE1="$(grep -n 'AFTER_CLOSE_PICKUP_FENCE_SIMULATED=true' "${CASE_H_LOG}" | head -1 | cut -d: -f1)"
H_FENCE2="$(grep -n 'AFTER_CLOSE_PICKUP_FENCE_SIMULATED=true' "${CASE_H_LOG}" | tail -1 | cut -d: -f1)"
H_ROLLBACK="$(grep -n '恢复代码与运行文件到 previous SHA' "${CASE_H_LOG}" | head -1 | cut -d: -f1)"
if [[ -n "${H_RESTORE1}" && -n "${H_FENCE2}" && -n "${H_ROLLBACK}" \
      && "${H_RESTORE1}" -lt "${H_FENCE2}" && "${H_FENCE2}" -lt "${H_ROLLBACK}" ]]; then
    ok "CASE H: second fence (drain) precedes rollback file mutation (no mixed runtime)"
else
    bad "CASE H: second fence (drain) precedes rollback file mutation (restore1=${H_RESTORE1} fence2=${H_FENCE2} rollback=${H_ROLLBACK})"
fi
if [[ "$(cat "${WORKER_STATE_FILE}")" == "running" ]]; then
    ok "CASE H: worker restored to running on OLD runtime after rollback"
else
    bad "CASE H: worker restored to running on OLD runtime after rollback (state=$(cat "${WORKER_STATE_FILE}"))"
fi

# ---- CASE J（行为级）：owned restore 后 PICKUP_FENCED 必须与真实容器状态一致 ----
echo "== CASE J: owned restore resets AFTER_CLOSE_PICKUP_FENCED=false (state machine) =="
if [[ -f "${CASE_A_LOG}" ]] && grep -q 'AFTER_CLOSE_PICKUP_FENCED=false' "${CASE_A_LOG}"; then
    ok "CASE J: owned restore clears AFTER_CLOSE_PICKUP_FENCED (worker running, not fenced)"
else
    bad "CASE J: owned restore clears AFTER_CLOSE_PICKUP_FENCED (worker running, not fenced)"
fi

# ---- FIX F（行为级）：测试 seam 不得在真实部署中绕过真实 MemAvailable ----
echo "== FIX F: mock memory seam rejected in production deploy (fail-closed) =="
HEADROOM_LIB="${TMP_ROOT}/headroom-lib.sh"
sed -n '/^check_deployment_memory_headroom() {/,/^}/p' "${SERVER_SCRIPT}" > "${HEADROOM_LIB}"
if DRY_RUN=false PANJI_MOCK_MEM_AVAILABLE_KB=99999999 bash -c "
  log() { :; }
  MIN_MEM_MB=4096
  source '${HEADROOM_LIB}'
  check_deployment_memory_headroom
" >/dev/null 2>&1; then
  bad "FIX F: production deploy with mock memory seam must NOT pass headroom"
else
  ok "FIX F: production deploy with mock memory seam is rejected fail-closed"
fi
if DRY_RUN=true PANJI_MOCK_MEM_AVAILABLE_KB=4403200 bash -c "
  log() { :; }
  MIN_MEM_MB=4096
  source '${HEADROOM_LIB}'
  check_deployment_memory_headroom
" >/dev/null 2>&1; then
  ok "FIX F: dry-run still honors mock memory seam"
else
  bad "FIX F: dry-run still honors mock memory seam"
fi

echo "----------------------------------------"
echo "部署 dry-run 合同测试：${PASS} 通过 / ${FAIL} 失败"
[[ "${FAIL}" -eq 0 ]]
