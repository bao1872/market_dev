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
chmod +x "${MOCK_BIN}/git" "${MOCK_BIN}/docker" "${MOCK_BIN}/flock" "${MOCK_BIN}/sysctl"

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
if PANJI_MOCK_NO_LIVE_MOUNT=1 run_deploy "${TARGET_SHA}" --dry-run >"${FIRST_LOG}" 2>&1; then
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

# 三级：状态文件与 RUNTIME_SHA 都不可解析时回落部署前 repo HEAD
TIER3_LIVE="${TMP_ROOT}/live-tier3"
TIER3_LOG="${TMP_ROOT}/tier3.log"
PATH="${MOCK_BIN}:${PATH}" PANJI_REPO_ROOT="${REPO_ROOT}" PANJI_LIVE_ROOT="${TIER3_LIVE}" \
  PANJI_ENV_FILE="${ENV_FILE}" PANJI_STATE_FILE="${TMP_ROOT}/none-state" PANJI_LOCK_FILE="${LOCK_FILE}" \
  bash "${SERVER_SCRIPT}" "${TARGET_SHA}" --dry-run >"${TIER3_LOG}" 2>&1 || true
grep -q '来源: repo_head' "${TIER3_LOG}" \
  && ok "tier3 falls back to pre-deploy repo HEAD" || bad "tier3 falls back to pre-deploy repo HEAD"

# --- RUNTIME_SHA / Mount / 镜像构建计划 ---
echo "== runtime sha and mount plan =="
grep -q '原地写入' "${OUTPUT_FILE}" \
  && ok "RUNTIME_SHA plan states in-place write" || bad "RUNTIME_SHA plan states in-place write"
grep -q "全部 11 个 Python 服务 Mounts" "${OUTPUT_FILE}" \
  && ok "verification plan covers all 11 python services" \
  || bad "verification plan covers all 11 python services"
grep -q '无运行环境变化，跳过镜像构建' "${OUTPUT_FILE}" \
  && ok "no environment change means zero build" || bad "no environment change means zero build"

echo "----------------------------------------"
echo "部署 dry-run 合同测试：${PASS} 通过 / ${FAIL} 失败"
[[ "${FAIL}" -eq 0 ]]
