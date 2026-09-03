#!/usr/bin/env bash
# frontend_package_classifier.test.sh — frontend/package.json 语义分类契约测试
#
# 绑定来源（DEPLOY-INFRA FIX）：
#   - 修复 classify_changes 对 frontend/package.json 任意 diff 误置 FRONTEND_ENVIRONMENT_CHANGED。
#   - 仅 environment-relevant 字段（dependencies / devDependencies / optionalDependencies /
#     peerDependencies / engines / packageManager）变化才触发前端镜像重建；
#     scripts/version/name 等 non-env 变化不触发。
#
# 覆盖用例（用户授权清单 CASE 1-7）+ helper 单元级字段判定 + 回归防护。
# 零网络、零 node_modules、零 docker：git 由 PATH stub 驱动（支持 diff --name-only 与
# show <sha>:frontend/package.json）。
#
# 用法: bash scripts/deploy/frontend_package_classifier.test.sh
# 退出码：0 = 全部通过；1 = 任一失败。

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
SERVER_SCRIPT="${REPO_ROOT}/scripts/deploy/panji-deploy.sh"

PASS=0
FAIL=0
ok()   { echo "  [PASS] $1"; PASS=$((PASS + 1)); }
bad()  { echo "  [FAIL] $1" >&2; FAIL=$((FAIL + 1)); }

# ---------------------------------------------------------------------------
# 固定 SHA（仅用于驱动 git stub 的 show 分支；必须 export 给 git stub 子进程）
MOCK_PREV_SHA="abc1234abc1234abc1234abc1234abc1234abc1"
MOCK_TARG_SHA="def5678def5678def5678def5678def5678def5"
export MOCK_PREV_SHA MOCK_TARG_SHA

# ---------------------------------------------------------------------------
# package.json fixtures（valid JSON，单行）
PKG_BASE='{"name":"trading-frontend","version":"1.0.0","private":true,"scripts":{"build":"vite build","test:contract":"echo base"}}'
PKG_SCRIPTS_ONLY='{"name":"trading-frontend","version":"1.0.0","private":true,"scripts":{"build":"vite build","test:contract":"echo base && echo review"}}'
PKG_DEPS='{"name":"trading-frontend","version":"1.0.0","private":true,"scripts":{"build":"vite build"},"dependencies":{"react":"^18.0.0"}}'
PKG_DEVDEPS='{"name":"trading-frontend","version":"1.0.0","private":true,"scripts":{"build":"vite build"},"devDependencies":{"vitest":"^1.0.0"}}'
PKG_INVALID='not valid json {{{'

# ---------------------------------------------------------------------------
# git stub：仅响应 `git diff --name-only` 与 `git show <sha>:frontend/package.json`
_GIT_STUB_DIR="$(mktemp -d -t panji-classifier-git-stub.XXXXXX)"
cat > "${_GIT_STUB_DIR}/git" <<'GITEOF'
#!/usr/bin/env bash
if [[ "$1 $2" == "diff --name-only" ]]; then
    printf '%s\n' "${MOCK_DIFF_FILES:-}"
    exit 0
fi
if [[ "$1" == "show" ]]; then
    ref="${2%%:*}"
    path="${2#*:}"
    if [[ "${path}" == "frontend/package.json" ]]; then
        if [[ "${ref}" == "${MOCK_PREV_SHA}" ]]; then
            printf '%s' "${MOCK_PKG_PREV:-}"
        elif [[ "${ref}" == "${MOCK_TARG_SHA}" ]]; then
            printf '%s' "${MOCK_PKG_TARG:-}"
        else
            printf '{}'
        fi
        exit 0
    fi
    exit 0
fi
exit 0
GITEOF
chmod +x "${_GIT_STUB_DIR}/git"
PATH="${_GIT_STUB_DIR}:${PATH}"

# ---------------------------------------------------------------------------
# source 部署脚本定义（main 之前的全部内容），复用现有 fixture 抽取方式
_DEPLOY_FIXTURE="$(mktemp -t panji-classifier-fixture.XXXXXX.sh)"
awk '/^main\(\) \{/{exit} {print}' "${SERVER_SCRIPT}" > "${_DEPLOY_FIXTURE}"

export PANJI_REPO_ROOT="${REPO_ROOT}"
set +e
# shellcheck disable=SC1090
source "${_DEPLOY_FIXTURE}"
set -e

# ---------------------------------------------------------------------------
# 重置并调用 classify_changes
# $1 = MOCK_DIFF_FILES  $2 = MOCK_PKG_PREV  $3 = MOCK_PKG_TARG
reset_flags() {
    export MOCK_DIFF_FILES="$1"
    export MOCK_PKG_PREV="${2:-}"
    export MOCK_PKG_TARG="${3:-}"
    PREVIOUS_SHA="${MOCK_PREV_SHA}"
    TARGET_SHA="${MOCK_TARG_SHA}"
    PREVIOUS_SHA_SOURCE="git_diff"
    BACKEND_RUNTIME_CHANGED=false
    FRONTEND_RUNTIME_CHANGED=false
    MIGRATION_CHANGED=false
    BACKEND_ENVIRONMENT_CHANGED=false
    FRONTEND_ENVIRONMENT_CHANGED=false
    CAPTURE_ENVIRONMENT_CHANGED=false
    COMPOSE_RUNTIME_CHANGED=false
    BACKEND_LIVE_REFRESH_SERVICES=()
    classify_changes
}

# 直接调用 helper（单元级字段判定）
# $1 = MOCK_PKG_PREV  $2 = MOCK_PKG_TARG
cls_direct() {
    export MOCK_PKG_PREV="$1"
    export MOCK_PKG_TARG="$2"
    PREVIOUS_SHA="${MOCK_PREV_SHA}"
    TARGET_SHA="${MOCK_TARG_SHA}"
    if frontend_package_environment_changed "${PREVIOUS_SHA}" "${TARGET_SHA}"; then
        echo "CHANGED"
    else
        echo "NOTCHANGED"
    fi
}

echo "=== frontend package.json 语义分类契约测试 ==="

# ---------------------------------------------------------------------------
echo "== 0. 结构与回归防护 =="
if grep -q 'frontend_package_environment_changed()' "${SERVER_SCRIPT}"; then
    ok "存在 frontend_package_environment_changed 函数"
else
    bad "缺少 frontend_package_environment_changed 函数"
fi
if grep -Fq 'tsconfig|package\.json' "${SERVER_SCRIPT}"; then
    ok "FRONTEND_RUNTIME grep 已包含 package.json（package.json 任意变化 → runtime changed）"
else
    bad "FRONTEND_RUNTIME grep 未包含 package.json"
fi
# 回归防护：ENV grep 不得再直接匹配 package.json（旧假阳性）
if grep -Fq 'frontend/(Dockerfile|package.json|package-lock' "${SERVER_SCRIPT}"; then
    bad "回归防护失败：ENV grep 仍存在 package.json 直接匹配（旧假阳性）"
else
    ok "回归防护：ENV grep 已移除 package.json 直接匹配"
fi

# ---------------------------------------------------------------------------
echo "== 1. helper 单元级字段判定 =="
r="$(cls_direct "${PKG_BASE}" "${PKG_SCRIPTS_ONLY}")"
if [[ "${r}" == "NOTCHANGED" ]]; then ok "helper: 仅 scripts.test:contract 变化 → NOTCHANGED"; else bad "helper: 仅 scripts 变化应 NOTCHANGED（实际 ${r}）"; fi
r="$(cls_direct "${PKG_BASE}" "${PKG_DEPS}")"
if [[ "${r}" == "CHANGED" ]]; then ok "helper: dependencies 变化 → CHANGED"; else bad "helper: dependencies 变化应 CHANGED（实际 ${r}）"; fi
r="$(cls_direct "${PKG_BASE}" "${PKG_DEVDEPS}")"
if [[ "${r}" == "CHANGED" ]]; then ok "helper: devDependencies 变化 → CHANGED"; else bad "helper: devDependencies 变化应 CHANGED（实际 ${r}）"; fi
r="$(cls_direct "${PKG_DEPS}" "${PKG_DEPS}")"
if [[ "${r}" == "NOTCHANGED" ]]; then ok "helper: 相同 env 字段 → NOTCHANGED"; else bad "helper: 相同 env 字段应 NOTCHANGED（实际 ${r}）"; fi
r="$(cls_direct "${PKG_DEPS}" "${PKG_INVALID}")"
if [[ "${r}" == "CHANGED" ]]; then ok "helper: target package.json 不可解析 → fail closed CHANGED"; else bad "helper: 不可解析应 fail closed CHANGED（实际 ${r}）"; fi
r="$(cls_direct "${PKG_INVALID}" "${PKG_BASE}")"
if [[ "${r}" == "CHANGED" ]]; then ok "helper: prev package.json 不可解析 → fail closed CHANGED"; else bad "helper: prev 不可解析应 fail closed CHANGED（实际 ${r}）"; fi

# ---------------------------------------------------------------------------
echo "== 2-7. classify_changes 集成（CASE 1-7） =="

# CASE 1：仅 scripts.test:contract 改变
reset_flags $'frontend/package.json' "${PKG_BASE}" "${PKG_SCRIPTS_ONLY}"
if [[ "${FRONTEND_RUNTIME_CHANGED}" == "true" && "${FRONTEND_ENVIRONMENT_CHANGED}" == "false" && "$(environment_changed && echo t)" != "t" ]]; then
    ok "CASE1 仅 scripts.test:contract 改变 → runtime=true env=false 不触发镜像构建"
else
    bad "CASE1 期望 runtime=true env=false；实际 runtime=${FRONTEND_RUNTIME_CHANGED} env=${FRONTEND_ENVIRONMENT_CHANGED}"
fi

# CASE 2：dependencies 改变
reset_flags $'frontend/package.json' "${PKG_BASE}" "${PKG_DEPS}"
if [[ "${FRONTEND_ENVIRONMENT_CHANGED}" == "true" ]]; then ok "CASE2 dependencies 改变 → env=true"; else bad "CASE2 dependencies 应 env=true"; fi

# CASE 3：devDependencies 改变
reset_flags $'frontend/package.json' "${PKG_BASE}" "${PKG_DEVDEPS}"
if [[ "${FRONTEND_ENVIRONMENT_CHANGED}" == "true" ]]; then ok "CASE3 devDependencies 改变 → env=true"; else bad "CASE3 devDependencies 应 env=true"; fi

# CASE 4：package-lock.json 改变（保守视为 env）
reset_flags $'frontend/package-lock.json' "${PKG_BASE}" "${PKG_BASE}"
if [[ "${FRONTEND_ENVIRONMENT_CHANGED}" == "true" ]]; then ok "CASE4 package-lock.json 改变 → env=true（保守）"; else bad "CASE4 package-lock.json 应 env=true"; fi

# CASE 5：Dockerfile 改变
reset_flags $'frontend/Dockerfile' "${PKG_BASE}" "${PKG_BASE}"
if [[ "${FRONTEND_ENVIRONMENT_CHANGED}" == "true" ]]; then ok "CASE5 frontend Dockerfile 改变 → env=true"; else bad "CASE5 Dockerfile 应 env=true"; fi

# CASE 6：package.json 无法解析 → fail closed env=true
reset_flags $'frontend/package.json' "${PKG_BASE}" "${PKG_INVALID}"
if [[ "${FRONTEND_ENVIRONMENT_CHANGED}" == "true" ]]; then ok "CASE6 package.json 不可解析 → fail closed env=true"; else bad "CASE6 不可解析应 fail closed env=true"; fi

# CASE 7：backend source + frontend source + scripts-only package.json（模拟 0dc6→e74）
reset_flags $'backend/app/api/market.py\nfrontend/src/App.tsx\nfrontend/package.json' "${PKG_BASE}" "${PKG_SCRIPTS_ONLY}"
if [[ "${BACKEND_RUNTIME_CHANGED}" == "true" \
    && "${FRONTEND_RUNTIME_CHANGED}" == "true" \
    && "${FRONTEND_ENVIRONMENT_CHANGED}" == "false" \
    && "${BACKEND_ENVIRONMENT_CHANGED}" == "false" \
    && "${CAPTURE_ENVIRONMENT_CHANGED}" == "false" \
    && "$(environment_changed && echo t)" != "t" ]]; then
    ok "CASE7 backend+frontend+scripts-only：backend runtime + frontend dist refresh，ZERO 环境镜像构建"
else
    bad "CASE7 期望 backend_rt=true frontend_rt=true env=false backend_env=false capture_env=false；实际 backend_rt=${BACKEND_RUNTIME_CHANGED} frontend_rt=${FRONTEND_RUNTIME_CHANGED} frontend_env=${FRONTEND_ENVIRONMENT_CHANGED} backend_env=${BACKEND_ENVIRONMENT_CHANGED} capture_env=${CAPTURE_ENVIRONMENT_CHANGED}"
fi
# CASE 7 补充：backend 为 API-only → 仅 Live Refresh backend（不改变 PROD 容器镜像）
if [[ "${BACKEND_LIVE_REFRESH_SERVICES[*]}" == "backend" ]]; then
    ok "CASE7 backend API-only → source-only Live Refresh backend（不重建 backend 镜像）"
else
    bad "CASE7 backend API-only 应只 Live Refresh backend（实际: ${BACKEND_LIVE_REFRESH_SERVICES[*]}）"
fi

# ---------------------------------------------------------------------------
rm -f "${_DEPLOY_FIXTURE}"
rm -rf "${_GIT_STUB_DIR}"

echo "----------------------------------------"
echo "frontend package.json 语义分类契约：${PASS} 通过 / ${FAIL} 失败"
[[ "${FAIL}" -eq 0 ]] && exit 0 || exit 1
