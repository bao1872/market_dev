#!/usr/bin/env bash
# panji-deploy.test.sh — 部署两脚本结构的静态契约测试
#
# 绑定来源（CHANGE-20260802-003）：
#   - 本地唯一入口：scripts/ops/panji-test-deploy
#   - 服务器端唯一实现：scripts/deploy/panji-deploy.sh
#
# 断言目标：防止回潮到已废止的双模式 / 镜像模式 / stdin 临时脚本 / main 分支来源。
#
# 用法: bash scripts/deploy/panji-deploy.test.sh
# 退出码：0 = 全部契约通过；1 = 任一契约失败。

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

SERVER_SCRIPT="${REPO_ROOT}/scripts/deploy/panji-deploy.sh"
LOCAL_ENTRY="${REPO_ROOT}/scripts/ops/panji-test-deploy"

PASS=0
FAIL=0

ok()   { echo "  [PASS] $1"; PASS=$((PASS + 1)); }
bad()  { echo "  [FAIL] $1" >&2; FAIL=$((FAIL + 1)); }

# 去掉注释行后再匹配，避免注释中的"禁止 X"被误判为存在 X。
# 注意：不使用 `code_of | grep -q` 管道——grep -q 提前关闭管道会触发 SIGPIPE，
# 在 `set -o pipefail` 下导致整条管道返回非零，产生假失败。
code_of() { grep -v '^[[:space:]]*#' "$1"; }

code_matches() {
    # $1=file  $2=ERE pattern；匹配返回 0
    local matched
    matched="$(code_of "$1" | grep -cE "$2" || true)"
    [[ "${matched}" -gt 0 ]]
}

assert_code_contains() {
    local label="$1" pattern="$2" file="$3"
    if code_matches "${file}" "${pattern}"; then ok "${label}"; else bad "${label}"; fi
}

assert_code_absent() {
    local label="$1" pattern="$2" file="$3"
    if code_matches "${file}" "${pattern}"; then bad "${label}"; else ok "${label}"; fi
}

assert_file_absent() {
    local label="$1" path="$2"
    if [[ -e "${path}" ]]; then bad "${label}"; else ok "${label}"; fi
}

echo "=== 部署脚本结构契约测试 ==="

# ---------------------------------------------------------------------------
echo "== 1/6 文件存在性与语法 =="
for f in "${SERVER_SCRIPT}" "${LOCAL_ENTRY}"; do
    if [[ -f "${f}" ]]; then ok "存在: ${f#"${REPO_ROOT}/"}"; else bad "缺失: ${f#"${REPO_ROOT}/"}"; fi
    if bash -n "${f}" 2>/dev/null; then ok "语法正确: ${f#"${REPO_ROOT}/"}"; else bad "语法错误: ${f#"${REPO_ROOT}/"}"; fi
done

# ---------------------------------------------------------------------------
echo "== 2/6 已废止执行入口不得恢复 =="
assert_file_absent "scripts/ops/panji-deploy-remote.sh 已删除" "${REPO_ROOT}/scripts/ops/panji-deploy-remote.sh"
assert_file_absent "scripts/deploy_live_runtime.sh 已删除"     "${REPO_ROOT}/scripts/deploy_live_runtime.sh"
assert_file_absent "scripts/sync_live_runtime.sh 已删除"       "${REPO_ROOT}/scripts/sync_live_runtime.sh"

# ---------------------------------------------------------------------------
echo "== 3/6 唯一运行模式为 Live Mount =="
assert_code_contains "服务端始终叠加 docker-compose.live.yml" \
    'docker-compose\.prod\.yml -f docker-compose\.live\.yml' "${SERVER_SCRIPT}"
assert_code_absent "无 COMPOSE_CMD_NO_LIVE 变体" \
    'COMPOSE_CMD_NO_LIVE' "${SERVER_SCRIPT}"
assert_code_absent "无 DEPLOYMENT_MODE=image" \
    'DEPLOYMENT_MODE=image' "${SERVER_SCRIPT}"
assert_code_absent "无 PANJI_FORCE_IMAGE_BUILD 开关" \
    'PANJI_FORCE_IMAGE_BUILD|FORCE_IMAGE_BUILD' "${SERVER_SCRIPT}"
assert_code_absent "本地入口无 --allow-local-build 开关" \
    'allow-local-build' "${LOCAL_ENTRY}"
assert_code_contains "服务端强制 DEPLOYMENT_MODE=live" \
    'DEPLOYMENT_MODE=live' "${SERVER_SCRIPT}"

# ---------------------------------------------------------------------------
echo "== 4/6 dev 是唯一部署来源 =="
assert_code_contains "服务端 fetch origin dev" 'git fetch origin dev' "${SERVER_SCRIPT}"
assert_code_contains "服务端校验 origin/dev 祖先" 'merge-base --is-ancestor .* origin/dev' "${SERVER_SCRIPT}"
assert_code_absent   "服务端不引用 origin/main"  'origin/main|origin main' "${SERVER_SCRIPT}"
assert_code_absent   "服务端不 checkout main"    'git checkout .*\bmain\b' "${SERVER_SCRIPT}"
assert_code_contains "本地入口校验 origin/dev 祖先" 'merge-base --is-ancestor .* origin/dev' "${LOCAL_ENTRY}"

# ---------------------------------------------------------------------------
echo "== 5/6 执行方式与变更判定 =="
assert_code_absent "本地入口不把脚本拷到 /tmp 执行" '/tmp/panji|bash -s' "${LOCAL_ENTRY}"
assert_code_contains "本地入口执行服务器仓库内脚本" \
    'scripts/deploy/panji-deploy\.sh' "${LOCAL_ENTRY}"
assert_code_contains "本地入口只经 panji-prod-ssh 访问生产" \
    'panji-prod-ssh' "${LOCAL_ENTRY}"
assert_code_absent "本地入口不直接执行 ssh" \
    '^[[:space:]]*ssh[[:space:]]' "${LOCAL_ENTRY}"
assert_code_contains "本地入口部署前 bash -n 预检" 'bash -n' "${LOCAL_ENTRY}"
assert_code_contains "变更判定基于上一部署 SHA" \
    'git diff --name-only "\$\{PREVIOUS_SHA\}" "\$\{TARGET_SHA\}"' "${SERVER_SCRIPT}"
assert_code_absent "不使用 HEAD~1 判定变更" 'HEAD~1' "${SERVER_SCRIPT}"
assert_code_contains "存在 classify_changes 函数" 'classify_changes\(\)' "${SERVER_SCRIPT}"
assert_code_contains "存在 rollback 函数" 'rollback\(\)' "${SERVER_SCRIPT}"
assert_code_contains "存在 flock 并发锁" 'flock -n' "${SERVER_SCRIPT}"

# ---------------------------------------------------------------------------
echo "== 6/6 有状态服务保护与完整 SHA 判据 =="
assert_code_absent "up -d 不含 postgres/redis/umami" \
    'up -d.*(postgres|redis|umami)' "${SERVER_SCRIPT}"
assert_code_absent "不得 down -v" 'down -v' "${SERVER_SCRIPT}"
assert_code_contains "核验完整 runtime_git_sha" 'runtime_git_sha' "${SERVER_SCRIPT}"
assert_code_contains "核验 deployment_mode=live" 'deployment_mode.*live|"live"' "${SERVER_SCRIPT}"
assert_code_contains "核验容器 Mounts 含 LIVE_ROOT" 'docker inspect .*Mounts' "${SERVER_SCRIPT}"
# 成功判据必须比较完整 SHA，禁止短 SHA 截断比较
assert_code_absent "不以短 SHA 作为成功判据" 'PUBLIC_SHA.*:0:7|EXPECTED_SHORT' "${LOCAL_ENTRY}"

echo "----------------------------------------"
echo "部署脚本结构契约测试：${PASS} 通过 / ${FAIL} 失败"
[[ "${FAIL}" -eq 0 ]] && exit 0 || exit 1
