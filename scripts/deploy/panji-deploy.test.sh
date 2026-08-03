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
echo "== 1/8 文件存在性与语法 =="
for f in "${SERVER_SCRIPT}" "${LOCAL_ENTRY}"; do
    if [[ -f "${f}" ]]; then ok "存在: ${f#"${REPO_ROOT}/"}"; else bad "缺失: ${f#"${REPO_ROOT}/"}"; fi
    if bash -n "${f}" 2>/dev/null; then ok "语法正确: ${f#"${REPO_ROOT}/"}"; else bad "语法错误: ${f#"${REPO_ROOT}/"}"; fi
done

# ---------------------------------------------------------------------------
echo "== 2/8 已废止执行入口不得恢复 =="
assert_file_absent "scripts/ops/panji-deploy-remote.sh 已删除" "${REPO_ROOT}/scripts/ops/panji-deploy-remote.sh"
assert_file_absent "scripts/deploy_live_runtime.sh 已删除"     "${REPO_ROOT}/scripts/deploy_live_runtime.sh"
assert_file_absent "scripts/sync_live_runtime.sh 已删除"       "${REPO_ROOT}/scripts/sync_live_runtime.sh"

# ---------------------------------------------------------------------------
echo "== 3/8 唯一运行模式为 Live Mount =="
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
echo "== 4/8 dev 是唯一部署来源 =="
assert_code_contains "服务端 fetch origin dev" 'git fetch origin dev' "${SERVER_SCRIPT}"
assert_code_contains "服务端校验 origin/dev 祖先" 'merge-base --is-ancestor .* origin/dev' "${SERVER_SCRIPT}"
assert_code_absent   "服务端不引用 origin/main"  'origin/main|origin main' "${SERVER_SCRIPT}"
assert_code_absent   "服务端不 checkout main"    'git checkout .*\bmain\b' "${SERVER_SCRIPT}"
assert_code_contains "本地入口校验 origin/dev 祖先" 'merge-base --is-ancestor .* origin/dev' "${LOCAL_ENTRY}"

# ---------------------------------------------------------------------------
echo "== 5/8 执行方式与变更判定 =="
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
echo "== 6/8 有状态服务保护与完整 SHA 判据 =="
assert_code_absent "up -d 不含 postgres/redis/umami" \
    'up -d.*(postgres|redis|umami)' "${SERVER_SCRIPT}"
assert_code_absent "不得 down -v" 'down -v' "${SERVER_SCRIPT}"
assert_code_contains "核验完整 runtime_git_sha" 'runtime_git_sha' "${SERVER_SCRIPT}"
assert_code_contains "核验 deployment_mode=live" 'deployment_mode.*live|"live"' "${SERVER_SCRIPT}"
assert_code_contains "核验容器 Mounts 含 LIVE_ROOT" 'docker inspect .*Mounts' "${SERVER_SCRIPT}"
# 成功判据必须比较完整 SHA，禁止短 SHA 截断比较
assert_code_absent "不以短 SHA 作为成功判据" 'PUBLIC_SHA.*:0:7|EXPECTED_SHORT' "${LOCAL_ENTRY}"

# ---------------------------------------------------------------------------
echo "== 7/8 首次 Live Mount 自举与状态机 =="

# 场景 1：本地入口必须让服务器先自举到目标 SHA，再执行目标工作树的脚本
assert_code_contains "本地入口远端 checkout --detach 目标 SHA" \
    'checkout -f --detach' "${LOCAL_ENTRY}"
# 场景 2：自举失败/ dry-run 必须经 trap 恢复原始 REF（分支名或 detached SHA）
assert_code_contains "本地入口 trap 恢复原始 REF" \
    'trap restore_head EXIT' "${LOCAL_ENTRY}"
assert_code_contains "本地入口记录自举前完整 SHA" \
    'ORIGINAL_SHA=' "${LOCAL_ENTRY}"
assert_code_contains "本地入口记录自举前 REF" \
    'ORIGINAL_REF=' "${LOCAL_ENTRY}"
assert_code_contains "本地入口经环境变量传递自举前 SHA" \
    'PANJI_BOOTSTRAP_PREVIOUS_SHA=' "${LOCAL_ENTRY}"
assert_code_absent "本地入口不把脚本写入 /tmp" \
    '/tmp/panji-deploy' "${LOCAL_ENTRY}"
# 场景 3：正式部署成功后服务器保持在目标 SHA
assert_code_contains "部署成功后不恢复 REF" \
    'RESTORE_HEAD=false' "${LOCAL_ENTRY}"
# 场景 4：服务器端存在首次 Live Mount 检测
assert_code_contains "存在 detect_first_live_deploy 函数" \
    'detect_first_live_deploy\(\)' "${SERVER_SCRIPT}"
assert_code_contains "首次检测基于 docker inspect Mounts" \
    'docker inspect .*trading-(backend|frontend)|for container in trading-backend trading-frontend' "${SERVER_SCRIPT}"
# 场景 5：FIRST_LIVE_DEPLOY 只提升同步范围，不得设置 migration_changed
assert_code_contains "存在 apply_first_live_deploy_override 函数" \
    'apply_first_live_deploy_override\(\)' "${SERVER_SCRIPT}"
assert_code_absent "首次 Live Mount 不强制 migration" \
    'FIRST_LIVE_DEPLOY.*==.*true.*\n?.*MIGRATION_CHANGED=true' "${SERVER_SCRIPT}"
# 场景 6：上一真实运行 SHA 解析（P0 修复）——禁止用 checkout 后的 repo HEAD 当上一 SHA
assert_code_contains "存在 resolve_previous_runtime_sha 函数" \
    'resolve_previous_runtime_sha()' "${SERVER_SCRIPT}"
assert_code_absent "禁止把 repo HEAD 当作上一 SHA 来源" \
    'PREVIOUS_SHA_SOURCE="repo_head"' "${SERVER_SCRIPT}"
assert_code_contains "已 Live Mount 路径含 RUNTIME_SHA 文件来源" \
    'LIVE_ROOT\}/RUNTIME_SHA' "${SERVER_SCRIPT}"
assert_code_contains "未知基线仍保留" 'unknown_baseline' "${SERVER_SCRIPT}"
assert_code_contains "首次 Live Mount 优先读运行版本" 'running_version' "${SERVER_SCRIPT}"
assert_code_contains "接受外层自举前 SHA 作为 fallback" \
    'PANJI_BOOTSTRAP_PREVIOUS_SHA' "${SERVER_SCRIPT}"
assert_code_contains "无法确认运行 SHA 时停止部署" \
    'previous_runtime_sha_unknown' "${SERVER_SCRIPT}"
# 顺序约束：resolve_previous_runtime_sha 在 classify_changes 之前（基于行号比较）
_resolve_line="$(code_of "${SERVER_SCRIPT}" \
    | awk '/^main\(\)/,/^}/' \
    | grep -n 'resolve_previous_runtime_sha' | head -1 | cut -d: -f1)"
_classify_line="$(code_of "${SERVER_SCRIPT}" \
    | awk '/^main\(\)/,/^}/' \
    | grep -n 'classify_changes' | head -1 | cut -d: -f1)"
if [[ -n "${_resolve_line}" && -n "${_classify_line}" && "${_resolve_line}" -lt "${_classify_line}" ]]; then
    ok "main 顺序: resolve_previous_runtime_sha 在 classify_changes 之前"
else
    bad "main 顺序: resolve_previous_runtime_sha 应在 classify_changes 之前"
fi
# 场景 7：migration 状态机与失败路径
assert_code_contains "存在 MIGRATION_ATTEMPTED 状态" 'MIGRATION_ATTEMPTED' "${SERVER_SCRIPT}"
assert_code_contains "存在 MIGRATION_SUCCEEDED 状态" 'MIGRATION_SUCCEEDED' "${SERVER_SCRIPT}"
assert_code_contains "存在 SERVICES_RESTARTED 状态" 'SERVICES_RESTARTED' "${SERVER_SCRIPT}"
assert_code_contains "存在 FAILURE_STAGE 状态" 'FAILURE_STAGE' "${SERVER_SCRIPT}"
assert_code_contains "存在 handle_migration_failure 函数" \
    'handle_migration_failure\(\)' "${SERVER_SCRIPT}"
assert_code_contains "migration 失败输出 migration_failed_requires_inspection" \
    'migration_failed_requires_inspection' "${SERVER_SCRIPT}"
# 场景 8：migration 失败路径不得执行容器重建
if code_of "${SERVER_SCRIPT}" \
    | awk '/^handle_migration_failure\(\)/,/^}/' \
    | grep -qE 'up -d|force-recreate'; then
    bad "migration 失败路径不执行容器重建"
else
    ok "migration 失败路径不执行容器重建"
fi

# ---------------------------------------------------------------------------
echo "== 8/8 镜像 tag 组、RUNTIME_SHA inode 与清理边界 =="

# 场景 9：environment_changed 时按同一 GIT_SHA tag 组整体构建三个镜像
assert_code_contains "存在 ENV_IMAGE_TAG_GROUP 三镜像组" \
    'ENV_IMAGE_TAG_GROUP=\(backend frontend worker-capture\)' "${SERVER_SCRIPT}"
assert_code_contains "构建使用完整 tag 组" \
    'build "\$\{ENV_IMAGE_TAG_GROUP\[@\]\}"' "${SERVER_SCRIPT}"
# 场景 10：普通代码变化零构建
assert_code_contains "无环境变化跳过构建" \
    'if ! environment_changed; then' "${SERVER_SCRIPT}"
# 场景 11：构建后仍以 prod+live 叠加启动（不存在镜像模式恢复）
assert_code_absent "不存在镜像模式状态机" \
    'IMAGE_MODE|restore_image_mode|DEPLOY_MODE=' "${SERVER_SCRIPT}"
# 场景 12：RUNTIME_SHA 必须原地写入，禁止 rename/rsync 覆盖
if code_of "${SERVER_SCRIPT}" \
    | awk '/^write_runtime_sha\(\)/,/^}/' \
    | grep -qE 'rsync .*RUNTIME_SHA|mv .*RUNTIME_SHA'; then
    bad "RUNTIME_SHA 不通过 rename/rsync 覆盖（保持 inode）"
else
    ok "RUNTIME_SHA 不通过 rename/rsync 覆盖（保持 inode）"
fi
assert_code_contains "RUNTIME_SHA 校验 inode 未变" \
    'inode_before|inode_after' "${SERVER_SCRIPT}"
assert_code_contains "RUNTIME_SHA 写后回读校验" \
    'RUNTIME_SHA 回读校验失败' "${SERVER_SCRIPT}"
# 场景 13：全量 Python 服务 Mount 核验
assert_code_contains "核验全部 Python 服务 Mount" \
    'for svc in "\$\{PYTHON_SERVICES\[@\]\}"' "${SERVER_SCRIPT}"
assert_code_contains "无条件核验 trading-frontend Mount" \
    'docker inspect trading-frontend' "${SERVER_SCRIPT}"
# 场景 14：清理边界——未构建镜像时不做任何清理，且永不使用 -a / volume prune
assert_code_contains "未构建镜像时跳过清理" \
    'IMAGES_BUILT.*!=.*"true"' "${SERVER_SCRIPT}"
assert_code_absent "禁止 docker image prune -a" 'image prune -a' "${SERVER_SCRIPT}"
assert_code_absent "禁止 docker system prune" 'system prune' "${SERVER_SCRIPT}"
assert_code_absent "禁止 docker volume prune" 'volume prune' "${SERVER_SCRIPT}"
assert_code_absent "禁止删除 node:20-alpine" 'rmi .*node:20-alpine|node:20-alpine' "${SERVER_SCRIPT}"
assert_code_absent "禁止清理无关容器" 'container prune' "${SERVER_SCRIPT}"

echo "----------------------------------------"
echo "部署脚本结构契约测试：${PASS} 通过 / ${FAIL} 失败"
[[ "${FAIL}" -eq 0 ]] && exit 0 || exit 1
