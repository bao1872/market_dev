#!/usr/bin/env bash
# Independent, non-network, non-Docker unit contract of the *real* deploy functions.
# No source code clone: extract all function definitions before main() from panji-deploy.sh.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SERVER="${ROOT}/scripts/deploy/panji-deploy.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT
awk '/^main\(\) \{/{exit} {print}' "${SERVER}" > "${TMP}/functions.sh"
[[ -s "${TMP}/functions.sh" ]] || { echo 'FAIL cannot extract deploy functions'; exit 1; }

pass=0; fail=0
assert_case() {
  local name="$1" mode="$2" mem_mib="$3" expect_rc="$4" expected_gate="$5"
  local log="${TMP}/${pass}-${fail}.log" rc=0
  PANJI_MOCK_MEM_AVAILABLE_KB="$((mem_mib * 1024))" bash -c '
    source "$1"
    DRY_RUN=true
    FRONTEND_CHANGED=true
    FRONTEND_RUNTIME_CHANGED=true
    FIRST_LIVE_DEPLOY=false
    case "$2" in
      frontend) ;;
      backend) BACKEND_RUNTIME_CHANGED=true ;;
      migration) MIGRATION_CHANGED=true ;;
      compose) COMPOSE_RUNTIME_CHANGED=true ;;
      backend_env) BACKEND_ENVIRONMENT_CHANGED=true ;;
      frontend_env) FRONTEND_ENVIRONMENT_CHANGED=true ;;
      capture_env) CAPTURE_ENVIRONMENT_CHANGED=true ;;
      first_live) FIRST_LIVE_DEPLOY=true ;;
      noop) FRONTEND_CHANGED=false; FRONTEND_RUNTIME_CHANGED=false ;;
      *) exit 66 ;;
    esac
    check_deployment_memory_headroom
  ' bash "${TMP}/functions.sh" "${mode}" >"${log}" 2>&1 || rc=$?
  local found=false
  if grep -q "class=${expected_gate} " "${log}"; then found=true; fi
  if [[ "${rc}" -eq "${expect_rc}" && "${found}" == true ]]; then
    echo "PASS ${name} (rc=${rc}, class=${expected_gate})"
    pass=$((pass + 1))
  else
    echo "FAIL ${name} (rc=${rc}, wanted=${expect_rc}; class=${expected_gate})"
    cat "${log}"
    fail=$((fail + 1))
  fi
}

# The value is not a guess by the test: production uses real /proc/meminfo;
# only dry-run unit contracts may inject the official test seam.
assert_case 'frontend 3813 MiB passes' frontend 3813 0 frontend_source_only
assert_case 'frontend exactly 3584 passes' frontend 3584 0 frontend_source_only
assert_case 'frontend 3583 stops' frontend 3583 1 frontend_source_only
assert_case 'backend 3813 stops' backend 3813 1 standard
assert_case 'backend 4096 passes' backend 4096 0 standard
assert_case 'migration 3813 stops' migration 3813 1 standard
assert_case 'compose 3813 stops' compose 3813 1 standard
assert_case 'backend env 3813 stops' backend_env 3813 1 standard
assert_case 'frontend env 3813 stops' frontend_env 3813 1 standard
assert_case 'capture env 3813 stops' capture_env 3813 1 standard
assert_case 'first live 3813 stops' first_live 3813 1 standard
assert_case 'no change 3813 stops' noop 3813 1 standard

if grep -Fq 'frontend_build_limits=(--memory=2g --memory-swap=2g)' "${SERVER}" \
  && grep -Fq 'docker run --rm "${frontend_build_limits[@]}"' "${SERVER}"; then
  echo 'PASS frontend build uses 2 GiB cgroup cap'; pass=$((pass+1))
else
  echo 'FAIL frontend build cap not found'; fail=$((fail+1))
fi

# dry-run without injected values never touches actual host MemAvailable.
NO_SEAM_LOG="${TMP}/no-seam.log"
bash -c '
    source "$1"
    DRY_RUN=true
    FIRST_LIVE_DEPLOY=false
    FRONTEND_CHANGED=true; FRONTEND_RUNTIME_CHANGED=true
    check_deployment_memory_headroom
' bash "${TMP}/functions.sh" >"${NO_SEAM_LOG}" 2>&1 || { echo 'FAIL no-seam dry-run'; fail=$((fail+1)); }
if grep -q 'deployment memory headroom deferred' "${NO_SEAM_LOG}"; then
  echo 'PASS no-seam dry-run is deferred'; pass=$((pass+1))
else
  echo 'FAIL no-seam dry-run not deferred'; fail=$((fail+1))
fi

echo "frontend memory gate contracts: ${pass} passed; ${fail} failed"
[[ ${fail} -eq 0 ]]
