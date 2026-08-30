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
cat > "${MOCK_BIN}/docker" <<'EOF'
#!/usr/bin/env bash
if [[ "${1:-}" == "inspect" ]]; then
  if [[ "${PANJI_MOCK_NO_LIVE_MOUNT:-0}" == "1" ]]; then
    exit 1
  fi
  printf '%s /var/lib/postgresql \n' "${PANJI_LIVE_ROOT:-/opt/panji-live}"
  exit 0
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
# [E2.1 P1-B] 伪造容器内 psql，用于注入 after-close job 门禁 fixture。
#   PANJI_MOCK_PSQL_COUNTS : 统计查询输出，每行 "status:count"
#   PANJI_MOCK_PSQL_ROWS   : 明细查询输出，每行 "id | job | business_date | status | heartbeat"
#   PANJI_MOCK_PSQL_FAIL=1 : 模拟门禁查询不可用（必须 fail-closed）
if [[ "${1:-}" == "exec" ]]; then
  if [[ "${PANJI_MOCK_PSQL_FAIL:-0}" == "1" ]]; then
    exit 1
  fi
  if printf '%s' "$*" | grep -q 'count('; then
    [[ -n "${PANJI_MOCK_PSQL_COUNTS:-}" ]] && printf '%s\n' "${PANJI_MOCK_PSQL_COUNTS}"
    exit 0
  fi
  [[ -n "${PANJI_MOCK_PSQL_ROWS:-}" ]] && printf '%s\n' "${PANJI_MOCK_PSQL_ROWS}"
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

if PANJI_MIN_MEM_MB=999999 run_deploy "${TARGET_SHA}" --dry-run >/dev/null 2>&1; then
  bad "resource budget failure blocks before deployment"
else
  ok "resource budget failure blocks before deployment"
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

# C. 逐个状态都必须阻止部署（当前无 pause 阶段）
#    用 first-live fixture 强制 backend_runtime_changed=true，使门禁真正执行
CONFLICT_LOG="${TMP_ROOT}/conflict.log"
for st in running queued resume_queued; do
    rc=0
    # 本场景**期望**非零退出（阻塞部署）。`set -e` 下必须用 `|| rc=$?` 捕获，
    # 否则 harness 会在第一条预期失败处直接终止。
    PANJI_MOCK_NO_LIVE_MOUNT=1 \
    PANJI_BOOTSTRAP_PREVIOUS_SHA="${TARGET_SHA}" \
    PANJI_MOCK_PSQL_COUNTS="${st}:1" \
    PANJI_MOCK_PSQL_ROWS="11111111-2222-3333-4444-555555555555 | after_close_orchestrator | 2026-08-28 | ${st} | 2026-08-30 10:00:00" \
        run_deploy "${TARGET_SHA}" --dry-run >"${CONFLICT_LOG}.${st}" 2>&1 || rc=$?
    if [[ "${rc}" -ne 0 ]] \
        && grep -q 'DEPLOYMENT_BLOCKED_PENDING_AFTER_CLOSE=TRUE' "${CONFLICT_LOG}.${st}" \
        && grep -q 'ACTIVE_AFTER_CLOSE_JOB_BLOCKS_DEPLOY' "${CONFLICT_LOG}.${st}"; then
        ok "conflict state ${st} blocks deployment"
    else
        bad "conflict state ${st} blocks deployment"
    fi

    # E. 输出必须包含 count + job id + business_date + status（operator 上下文）
    if grep -q "${st}_COUNT=1" "${CONFLICT_LOG}.${st}" \
        && grep -q '11111111-2222-3333-4444-555555555555' "${CONFLICT_LOG}.${st}" \
        && grep -q '2026-08-28' "${CONFLICT_LOG}.${st}"; then
        ok "conflict state ${st} output carries job id / business_date / status"
    else
        bad "conflict state ${st} output carries job id / business_date / status"
    fi
done

# D. 全部为 0 时放行（沿用 first-live dry-run succeeds 的成功路径 + 显式计数为 0）
PANJI_MOCK_NO_LIVE_MOUNT=1 \
PANJI_BOOTSTRAP_PREVIOUS_SHA="${TARGET_SHA}" \
PANJI_MOCK_PSQL_COUNTS="running:0
queued:0
resume_queued:0" \
    run_deploy "${TARGET_SHA}" --dry-run >"${CONFLICT_LOG}.zero" 2>&1
if [[ $? -eq 0 ]] && grep -q '无强制阻塞盘后长任务，继续部署' "${CONFLICT_LOG}.zero"; then
    ok "all-zero conflict counts allow deployment"
else
    bad "all-zero conflict counts allow deployment"
fi

# C-2. 门禁查询不可用时必须 fail-closed
PANJI_MOCK_NO_LIVE_MOUNT=1 \
PANJI_BOOTSTRAP_PREVIOUS_SHA="${TARGET_SHA}" \
PANJI_MOCK_PSQL_FAIL=1 \
    run_deploy "${TARGET_SHA}" --dry-run >"${CONFLICT_LOG}.unavailable" 2>&1 || unavailable_rc=$?
unavailable_rc="${unavailable_rc:-0}"
if [[ "${unavailable_rc}" -ne 0 ]] && grep -q 'ACTIVE_AFTER_CLOSE_JOB_GATE_UNAVAILABLE' "${CONFLICT_LOG}.unavailable"; then
    ok "unavailable job gate fails closed"
else
    bad "unavailable job gate fails closed"
fi

# F. guard 必须只读：门禁函数体内不得出现任何写操作
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

echo "----------------------------------------"
echo "部署 dry-run 合同测试：${PASS} 通过 / ${FAIL} 失败"
[[ "${FAIL}" -eq 0 ]]
