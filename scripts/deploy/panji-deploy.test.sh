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
# 镜像 tag SHA 解析：40 位需仓库可解析；7 位需 git 唯一解析为完整 commit
assert_code_contains "存在 _resolve_image_tag_sha 函数" \
    '_resolve_image_tag_sha()' "${SERVER_SCRIPT}"
assert_code_contains "镜像 tag 支持 40 位 SHA 解析" \
    '\[0-9a-fA-F\]\{40\}' "${SERVER_SCRIPT}"
assert_code_contains "镜像 tag 支持 7 位短 SHA 解析" \
    '\[0-9a-fA-F\]\{7\}' "${SERVER_SCRIPT}"
assert_code_contains "7 位短 SHA 经 git 唯一解析为完整 commit" \
    'rev-parse --quiet --verify' "${SERVER_SCRIPT}"
assert_code_contains "镜像 tag 解析来源标记为 running_image_tag" \
    'running_image_tag' "${SERVER_SCRIPT}"
# 7 位短 SHA 分支必须先把 candidate 经 rev-parse 解析为完整 SHA（resolved）再采用，
# 不得直接把 7 位候选值作为 PREVIOUS_SHA。
assert_code_contains "7 位分支采用 git 解析后的完整 SHA" \
    'echo "\${resolved}"' "${SERVER_SCRIPT}"
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
    'for svc in "\$\{ENV_IMAGE_TAG_GROUP\[@\]\}"' "${SERVER_SCRIPT}"
assert_code_contains "构建逐项消费 tag 组" \
    '\$\{COMPOSE_CMD\} build "\$\{svc\}"' "${SERVER_SCRIPT}"
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
assert_code_absent "禁止删除 node:20-alpine" 'rmi .*node:20-alpine' "${SERVER_SCRIPT}"
assert_code_absent "禁止清理无关容器" 'container prune' "${SERVER_SCRIPT}"

echo "----------------------------------------"

# ---------------------------------------------------------------------------
echo "== 9/9 COMPOSE_RUNTIME_CHANGED 部署契约（控制流） =="
# 提取 panji-deploy.sh 中 main() 之前的全部定义作为 fixture（避免执行 main）。
# 通过 PATH 上的 git stub（读取 MOCK_DIFF_FILES 环境变量）驱动 classify_changes 的真实控制流。
_DEPLOY_FIXTURE="$(mktemp -t panji-deploy-fixture.XXXXXX.sh)"
awk '/^main\(\) \{/{exit} {print}' "${SERVER_SCRIPT}" > "${_DEPLOY_FIXTURE}"

# PATH 上的 git stub：仅响应 `git diff --name-only ...`，回显 MOCK_DIFF_FILES 内容。
_GIT_STUB_DIR="$(mktemp -d -t panji-git-stub.XXXXXX)"
cat > "${_GIT_STUB_DIR}/git" <<'GITEOF'
#!/usr/bin/env bash
if [[ "$1 $2" == "diff --name-only" ]]; then
    printf '%s\n' "${MOCK_DIFF_FILES:-}"
fi
GITEOF
chmod +x "${_GIT_STUB_DIR}/git"
PATH="${_GIT_STUB_DIR}:${PATH}"

# 仅 source fixture 一次（fixture 内 set -euo pipefail 会继承到当前 shell，
# 故先 set +e 防止 classify_changes 中任何非零返回提前退出）。
set +e
export PANJI_REPO_ROOT="$(pwd)"
source "${_DEPLOY_FIXTURE}"
set -e

reset_flags() {
    export MOCK_DIFF_FILES="$1"
    PREVIOUS_SHA="abc1234abc1234abc1234abc1234abc1234abc1"
    TARGET_SHA="def5678def5678def5678def5678def5678def5"
    PREVIOUS_SHA_SOURCE="git_diff"
    BACKEND_RUNTIME_CHANGED=false
    FRONTEND_RUNTIME_CHANGED=false
    MIGRATION_CHANGED=false
    BACKEND_ENVIRONMENT_CHANGED=false
    FRONTEND_ENVIRONMENT_CHANGED=false
    CAPTURE_ENVIRONMENT_CHANGED=false
    COMPOSE_RUNTIME_CHANGED=false
    classify_changes
}

# CASE 1：仅 docker-compose.prod.yml 变化 → COMPOSE_RUNTIME_CHANGED=true
reset_flags $'docker-compose.prod.yml'
if [[ "${COMPOSE_RUNTIME_CHANGED}" == "true" ]]; then ok "CASE1 仅 prod compose 变化 → COMPOSE_RUNTIME_CHANGED=true"; else bad "CASE1 仅 prod compose 变化 → COMPOSE_RUNTIME_CHANGED=true"; fi

# CASE 2：仅 docker-compose.live.yml 变化 → COMPOSE_RUNTIME_CHANGED=true
reset_flags $'docker-compose.live.yml'
if [[ "${COMPOSE_RUNTIME_CHANGED}" == "true" ]]; then ok "CASE2 仅 live overlay 变化 → COMPOSE_RUNTIME_CHANGED=true"; else bad "CASE2 仅 live overlay 变化 → COMPOSE_RUNTIME_CHANGED=true"; fi

# CASE 3：COMPOSE_RUNTIME_CHANGED=true → _backend_runtime_will_mutate=true
reset_flags $'docker-compose.prod.yml'
if _backend_runtime_will_mutate; then ok "CASE3 compose 运行配置变化 → _backend_runtime_will_mutate=true"; else bad "CASE3 compose 运行配置变化 → _backend_runtime_will_mutate=true"; fi

# CASE 4：compose 运行配置变化 → 走 Compose 配置对账（reconcile_compose_runtime），
# 命令含 --no-build，且不得含 --force-recreate（P1-B：不过度重建）。
assert_code_contains "CASE4 compose 变化走 reconcile（含 reconcile_compose_runtime 定义）" \
    'reconcile_compose_runtime\(' "${SERVER_SCRIPT}"
assert_code_contains "CASE4 reconcile 命令含 --no-build" \
    '\$\{COMPOSE_CMD\} up -d --no-build' "${SERVER_SCRIPT}"
assert_code_absent "CASE4 reconcile 命令不含 --force-recreate" \
    '\$\{COMPOSE_CMD\} up -d --no-build.*--force-recreate' "${SERVER_SCRIPT}"
# 对账 application scope 仍为 PYTHON_SERVICES + frontend（不含 postgres/redis/umami）
assert_code_contains "CASE4 reconcile 范围 = PYTHON_SERVICES + frontend" \
    'reconcile_compose_runtime "\$\{_recon_filtered\[@\]\}" frontend' "${SERVER_SCRIPT}"
# 纯 Compose 变化分支（COMPOSE_RUNTIME_CHANGED==true 块）不再向 restart_list 追加
# PYTHON_SERVICES/frontend（避免 force-recreate）。注意：实际运行代码变化（need_backend）
# 分支仍合法 append restart_list，故只断言 COMPOSE_RUNTIME_CHANGED 分支内不含 append。
if code_of "${SERVER_SCRIPT}" | awk '
    /if \[\[ "\$\{COMPOSE_RUNTIME_CHANGED\}" == "true" \]\]; then/ {in_block=1; next}
    in_block && /^    fi$/ {in_block=0}
    in_block && /restart_list\+=/ {found=1}
    END{exit found}
'; then
    ok "CASE4 纯 Compose 分支不再 append 到 restart_list（不 force-recreate）"
else
    bad "CASE4 纯 Compose 分支不再 append 到 restart_list（不 force-recreate）"
fi

# CASE 5：compose 运行配置变化 → 不自动触发镜像构建 / migration
reset_flags $'docker-compose.prod.yml'
if [[ "${BACKEND_ENVIRONMENT_CHANGED}" == "false" && "${FRONTEND_ENVIRONMENT_CHANGED}" == "false" && "${CAPTURE_ENVIRONMENT_CHANGED}" == "false" ]]; then
    ok "CASE5 compose 变化不置 *_ENVIRONMENT_CHANGED（不自动构建镜像）"
else
    bad "CASE5 compose 变化不置 *_ENVIRONMENT_CHANGED（不自动构建镜像）"
fi
if [[ "${MIGRATION_CHANGED}" == "false" ]]; then ok "CASE5 compose 变化不置 MIGRATION_CHANGED（不自动 migration）"; else bad "CASE5 compose 变化不置 MIGRATION_CHANGED"; fi

# CASE 6：无关 docs-only 变化 → COMPOSE_RUNTIME_CHANGED=false，无新重启行为
reset_flags $'docs/maps/foo.md
README.md'
if [[ "${COMPOSE_RUNTIME_CHANGED}" == "false" ]]; then ok "CASE6 仅 docs 变化 → COMPOSE_RUNTIME_CHANGED=false"; else bad "CASE6 仅 docs 变化 → COMPOSE_RUNTIME_CHANGED=false"; fi
if [[ "${BACKEND_RUNTIME_CHANGED}" == "false" && "${FRONTEND_RUNTIME_CHANGED}" == "false" && "${MIGRATION_CHANGED}" == "false" ]]; then
    ok "CASE6 仅 docs 变化不产生新重启/迁移行为"
else
    bad "CASE6 仅 docs 变化不产生新重启/迁移行为"
fi

# CASE 6A：API-only backend 只刷新 backend；shared backend 保守覆盖全部 Python 服务。
reset_flags $'backend/app/api/market.py'
if [[ "${BACKEND_LIVE_REFRESH_SERVICES[*]}" == "backend" ]]; then
    ok "CASE6A API-only backend → 只 Live Refresh backend"
else
    bad "CASE6A API-only backend 应只 Live Refresh backend（实际: ${BACKEND_LIVE_REFRESH_SERVICES[*]}）"
fi
if ! _after_close_process_will_refresh; then
    ok "CASE6A API-only backend → 不刷新/fence worker-after-close"
else
    bad "CASE6A API-only backend 不应刷新/fence worker-after-close"
fi
reset_flags $'backend/app/services/market_service.py'
if [[ "${#BACKEND_LIVE_REFRESH_SERVICES[@]}" -eq "${#PYTHON_SERVICES[@]}" ]]; then
    ok "CASE6B shared backend → 保守 Live Refresh 全部 Python 服务"
else
    bad "CASE6B shared backend 应覆盖全部 Python 服务"
fi
if _after_close_process_will_refresh; then
    ok "CASE6B shared backend → 保留 worker-after-close fence"
else
    bad "CASE6B shared backend 必须保留 worker-after-close fence"
fi
assert_code_contains "CASE6C source-only Live Refresh 使用 compose restart" \
    '\$\{COMPOSE_CMD\} restart "\$\{wave_services\[@\]\}"' "${SERVER_SCRIPT}"
if code_of "${SERVER_SCRIPT}" \
    | awk '/^live_refresh_services\(\)/,/^}/{if(/force-recreate|up -d/)found=1} END{exit found}'; then
    ok "CASE6C Live Refresh 不含 up/recreate"
else
    bad "CASE6C Live Refresh 不得含 up/recreate"
fi
assert_code_contains "CASE6D frontend source-only 明确不重启容器" \
    'frontend source-only.*不重启 frontend 容器' "${SERVER_SCRIPT}"
assert_code_contains "CASE6E API-only 不触发 after-close fence" \
    '_after_close_process_will_refresh' "${SERVER_SCRIPT}"

# CASE 7-8：P1-C 失败传播结构性断言（不实际执行部署，仅校验源码是否含传播点）。
# 7：restart_services 调用点必须显式 || return 1
assert_code_contains "CASE7 restart_services 调用显式 || return 1（失败传播到 deploy）" \
    'restart_services "\$\{restart_list\[@\]\}" \|\| return 1' "${SERVER_SCRIPT}"
# 7b：reconcile_compose_runtime 调用点必须显式 || return 1
assert_code_contains "CASE7 reconcile_compose_runtime 调用显式 || return 1（失败传播到 deploy）" \
    'reconcile_compose_runtime "\$\{_recon_filtered\[@\]\}" frontend \|\| return 1' "${SERVER_SCRIPT}"
# 8：_wait_health 与 _check_scheduler_single_instance 在 restart_services 内显式 || return 1
assert_code_contains "CASE8 restart_services 内 _wait_health 显式 || return 1" \
    '_wait_health \|\| return 1' "${SERVER_SCRIPT}"
assert_code_contains "CASE8 restart_services 内 _check_scheduler_single_instance 显式 || return 1" \
    '_check_scheduler_single_instance \|\| return 1' "${SERVER_SCRIPT}"
# 8b：_wave_up 仍保留 || return 1（既有 force-recreate 波次失败传播不变）
assert_code_contains "CASE8 _wave_up 仍保留 || return 1" \
    '_wave_up .* \|\| return 1' "${SERVER_SCRIPT}"
# 9：main 仍以 if ! deploy 捕获部署失败
assert_code_contains "CASE9 main 以 if ! deploy 捕获部署失败" \
    'if ! deploy; then' "${SERVER_SCRIPT}"
# 10：reconcile_compose_runtime 定义内不含 --force-recreate（纯对账语义；
# 全文件仍允许 force-recreate，因为它是 restart_services 既有 force-recreate 波次语义）。
if code_of "${SERVER_SCRIPT}" \
    | awk '/^reconcile_compose_runtime\(\)/,/^}/{if(/force-recreate/)found=1} END{exit !found}'; then
    bad "CASE10 reconcile_compose_runtime 定义内含 --force-recreate"
else
    ok "CASE10 reconcile_compose_runtime 定义不含 --force-recreate"
fi

# ---------------------------------------------------------------------------
echo "== 10/10 COMPOSE 对账缺陷修正（Defect 1 & Defect 2） =="

# DEFECT 1：reconcile_compose_runtime 必须在首次 compose up 之前置 SERVICES_RESTARTED=true，
# 否则部分 Compose 变更后命令失败时会被 main 误分类为「服务未重启」（影响回滚决策）。
# 校验：在 reconcile_compose_runtime 函数体内，SERVICES_RESTARTED=true 的行号 < up -d --no-build 的行号。
if code_of "${SERVER_SCRIPT}" | awk '
/^reconcile_compose_runtime\(\)/{f=1; next}
f && /^}/{exit}
f && /SERVICES_RESTARTED=true/ && !u {sr=NR}
f && /\$\{COMPOSE_CMD\} up -d --no-build/ && !u {u=NR}
END{ if(sr>0 && u>0 && sr<u) exit 0; exit 1 }
'; then
    ok "DEFECT1 reconcile 在首次 compose up 之前置 SERVICES_RESTARTED=true"
else
    bad "DEFECT1 reconcile 必须在首次 compose up 之前置 SERVICES_RESTARTED=true"
fi

# DEFECT 1（CASE D 反向）：SERVICES_RESTARTED=true 不得仅在空参早返回分支被置位。
# 即非空参数路径（实际发起 up 前）必须置位。上面已覆盖「前置于 up」语义。
assert_code_contains "DEFECT1 reconcile 非空路径置位 SERVICES_RESTARTED=true" \
    'SERVICES_RESTARTED=true' "${SERVER_SCRIPT}"

# DEFECT 2：Mixed case 控制流——restart_services（代码/环境/Migration）与
# reconcile_compose_runtime（Compose-only 配置对账）为两个独立、顺序执行的 if 分支，
# 二者不被彼此嵌套。校验：deploy() 内 restart_services 调用行号 < reconcile 调用行号。
_mrestart="$(code_of "${SERVER_SCRIPT}" | awk '/^deploy\(\)/{f=1} f&&/^}/{exit} f&&/restart_services "\$\{restart_list\[@\]\}" \|\| return 1/{print NR; exit}')"
_mrec="$(code_of "${SERVER_SCRIPT}" | awk '/^deploy\(\)/{f=1} f&&/^}/{exit} f&&/reconcile_compose_runtime "\$\{_recon_filtered\[@\]\}" frontend \|\| return 1/{print NR; exit}')"
if [[ -n "${_mrestart}" && -n "${_mrec}" && "${_mrestart}" -lt "${_mrec}" ]]; then
    ok "DEFECT2 deploy 控制流：restart_services 在 reconcile_compose_runtime 之前（顺序执行）"
else
    bad "DEFECT2 deploy 控制流：restart_services 应先于 reconcile_compose_runtime"
fi

# DEFECT 2（CASE B/C 结构）：reconcile 的 if 分支与 restart_list 的 -gt 0 守卫为平级兄弟，
# 不相互嵌套。校验：在 deploy() 内，reconcile 调用行不在 restart_list -gt 0 守卫闭合之内。
assert_code_contains "DEFECT2 restart_list 非零守卫使用 -gt 0" \
    '\$\{#restart_list\[@\]\} -gt 0' "${SERVER_SCRIPT}"
assert_code_contains "DEFECT2 reconcile 仍显式 || return 1（无回归）" \
    'reconcile_compose_runtime "\$\{_recon_filtered\[@\]\}" frontend \|\| return 1' "${SERVER_SCRIPT}"
# 在 deploy() 函数体内校验：reconcile 不被包裹在 restart_list 守卫的 else 中。
if code_of "${SERVER_SCRIPT}" | awk '
/^deploy\(\)/{f=1; next}
f && /^}/{f=0}
f && /if \[\[ \$\{#restart_list\[@\]\} -gt 0 \]\]; then/{in_restart=1; next}
in_restart && /reconcile_compose_runtime/{found=1}
in_restart && /^    fi$/{in_restart=0}
END{exit !found}
'; then
    bad "DEFECT2 reconcile 不得嵌套在 restart_list 守卫内（mixed case 必须独立执行）"
else
    ok "DEFECT2 reconcile 独立于 restart_list 守卫（mixed case 始终执行）"
fi

# DEFECT 2（CASE F 反向/CASE A 反向）：纯 Compose 变化（restart_list 为空）仍执行 reconcile。
# 已通过 CASE4（reconcile 调用存在 + 不含 force-recreate）与上面「reconcile 独立执行」覆盖。
assert_code_contains "DEFECT2 纯 Compose 变化走 reconcile（CASE A）" \
    'if \[\[ "\$\{COMPOSE_RUNTIME_CHANGED\}" == "true" \]\]; then' "${SERVER_SCRIPT}"

# ---------------------------------------------------------------------------
# == P1-A / P1-B 行为测试 ==
#
# 位置约束（不可移到文件末尾）：后续 11/11 小节在直接调用 deploy() 时会命中被测源码的
# fail()（内部 exit 1），整个套件在那里终止。若把本节放在其后，断言将永不执行（假覆盖）。
#
# 隔离约束：本节整体在**子 shell** 中执行——
#   1) docker stub PATH / DRY_RUN / LIVE_ROOT / 函数覆盖不污染后续小节；
#   2) 万一被测源码 fail()，只终止子 shell，不吞掉本节其余断言之外的套件。
# 断言结果以 `RESULT|PASS|label` 行回传，由父 shell 统一用 ok/bad 计数；
# 同时校验断言条数（防子 shell 早退造成静默漏跑）。
# ---------------------------------------------------------------------------
echo "== 11/11 frontend artifact identity (DEPLOY-4, artifact owner) =="
# 纯 shell 契约镜像：manifest 是 frontend 唯一身份来源，且只能由 build 生成。
# deployment SHA ≠ 必然等于 frontend artifact SHA：只有 FRONTEND_CHANGED 时才要求 == TARGET_SHA。
_fe_sha() {
    local f="$1"
    [[ -f "$f" ]] || { echo ""; return; }
    python3 -c 'import sys,json; print(json.load(open(sys.argv[1])).get("git_sha",""))' "$f" 2>/dev/null || echo ""
}
# 镜像 verify_deployment CASE A/B 的 identity 决策（repo/live/container/http 四层）
_fe_verify() {
    # $1=FRONTEND_CHANGED(t/f) $2=TARGET_SHA $3=PREDEPLOY_FRONTEND_SHA $4=repo $5=live $6=container $7=http
    local changed="$1" target="$2" predeploy="$3" repo="$4" live="$5" cont="$6" http="$7" expect
    if [[ "$changed" == "t" ]]; then
        expect="$target"
        [[ "$(_fe_sha "$repo")" == "$expect" ]] || return 1
    else
        if [[ -z "$predeploy" ]]; then return 1; fi   # legacy bootstrap → fail-closed
        expect="$predeploy"
    fi
    [[ "$(_fe_sha "$live")" == "$expect" ]] || return 1
    [[ "$(_fe_sha "$cont")" == "$expect" ]] || return 1
    [[ "$(_fe_sha "$http")" == "$expect" ]] || return 1
    return 0
}
_fe_mk() { printf '{"git_sha":"%s","build_time":"t"}' "$1" > "$2"; }

# ---- 静态契约：源码实现 artifact owner 语义 ----
assert_code_contains "build 物化 manifest git_sha=TARGET_SHA" 'git_sha.*TARGET_SHA' "${SERVER_SCRIPT}"
assert_code_contains "FRONTEND_CHANGED 由 runtime||environment 决定" 'FRONTEND_CHANGED=true' "${SERVER_SCRIPT}"
assert_code_contains "部署前只读捕获 PREDEPLOY_FRONTEND_SHA" 'capture_predeploy_frontend_identity' "${SERVER_SCRIPT}"
assert_code_contains "verify CASE A 用 TARGET_SHA" 'fe_expect="\$\{TARGET_SHA\}"' "${SERVER_SCRIPT}"
assert_code_contains "verify CASE B 用 PREDEPLOY_FRONTEND_SHA" 'fe_expect="\$\{PREDEPLOY_FRONTEND_SHA\}"' "${SERVER_SCRIPT}"
assert_code_contains "legacy bootstrap fail-closed" 'FRONTEND_ARTIFACT_IDENTITY_MISSING' "${SERVER_SCRIPT}"

_FE_TMP="$(mktemp -d -t panji-fe-owner.XXXXXX)"
_T="7fb5cf1b5abaa5ae8fb12dde0a051f299cdd46e1"
_P="59ad8938bb23588ba049e091a8cec8dee80aa0f8"

# 1. changed frontend: build manifest == TARGET_SHA
_fe_mk "${_T}" "${_FE_TMP}/repo.json"
[[ "$(_fe_sha "${_FE_TMP}/repo.json")" == "${_T}" ]] && ok "1. changed: build manifest == TARGET_SHA" || bad "1. changed: build manifest == TARGET_SHA"

# 2. changed frontend: repo/live/container/http == TARGET_SHA (PASS)
_fe_mk "${_T}" "${_FE_TMP}/live.json"; _fe_mk "${_T}" "${_FE_TMP}/cont.json"; _fe_mk "${_T}" "${_FE_TMP}/http.json"
_fe_verify t "${_T}" "${_P}" "${_FE_TMP}/repo.json" "${_FE_TMP}/live.json" "${_FE_TMP}/cont.json" "${_FE_TMP}/http.json" \
    && ok "2. changed: repo/live/container/http == TARGET_SHA" || bad "2. changed: repo/live/container/http == TARGET_SHA"

# 3. unchanged frontend: TARGET_SHA != PREDEPLOY, verify PASS
_fe_mk "${_P}" "${_FE_TMP}/live2.json"; _fe_mk "${_P}" "${_FE_TMP}/cont2.json"; _fe_mk "${_P}" "${_FE_TMP}/http2.json"
_fe_verify f "${_T}" "${_P}" "" "${_FE_TMP}/live2.json" "${_FE_TMP}/cont2.json" "${_FE_TMP}/http2.json" \
    && ok "3. unchanged: TARGET_SHA!=PREDEPLOY, verify PASS" || bad "3. unchanged: TARGET_SHA!=PREDEPLOY, verify PASS"

# 4. unchanged frontend: live/container/http mismatch → FAIL
_fe_mk "${_P}" "${_FE_TMP}/live3.json"; _fe_mk "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef" "${_FE_TMP}/cont3.json"; _fe_mk "${_P}" "${_FE_TMP}/http3.json"
_fe_verify f "${_T}" "${_P}" "" "${_FE_TMP}/live3.json" "${_FE_TMP}/cont3.json" "${_FE_TMP}/http3.json" \
    && bad "4. unchanged: mismatch 应 FAIL" || ok "4. unchanged: live/container/http mismatch → FAIL"

# 5. unchanged frontend: predeploy manifest missing → FAIL CLOSED
_fe_verify f "${_T}" "" "" "${_FE_TMP}/live.json" "${_FE_TMP}/cont.json" "${_FE_TMP}/http.json" \
    && bad "5. unchanged: predeploy missing 应 FAIL" || ok "5. unchanged: predeploy manifest missing → FAIL CLOSED"

# 6. missing manifest → FAIL
_fe_verify t "${_T}" "${_P}" "${_FE_TMP}/nope.json" "${_FE_TMP}/live.json" "${_FE_TMP}/cont.json" "${_FE_TMP}/http.json" \
    && bad "6. missing manifest 应 FAIL" || ok "6. missing manifest → FAIL"

# 7. malformed → FAIL
printf 'not json {' > "${_FE_TMP}/bad.json"
_fe_verify t "${_T}" "${_P}" "${_FE_TMP}/bad.json" "${_FE_TMP}/live.json" "${_FE_TMP}/cont.json" "${_FE_TMP}/http.json" \
    && bad "7. malformed 应 FAIL" || ok "7. malformed → FAIL"

# 8. changed frontend wrong SHA → FAIL
_fe_mk "1111111111111111111111111111111111111111" "${_FE_TMP}/repo8.json"
_fe_verify t "${_T}" "${_P}" "${_FE_TMP}/repo8.json" "${_FE_TMP}/live.json" "${_FE_TMP}/cont.json" "${_FE_TMP}/http.json" \
    && bad "8. changed wrong SHA 应 FAIL" || ok "8. changed frontend wrong SHA → FAIL"

# 9. restore with frontend changed: rebuild previous artifact, manifest == PREVIOUS_SHA（同源）
assert_code_contains "9. restore changed: 真实 build_frontend_dist" 'FRONTEND_CHANGED}"' "${SERVER_SCRIPT}"
assert_code_contains "9. restore changed: build 用 TARGET_SHA(=PREVIOUS_SHA)" 'git_sha.*TARGET_SHA' "${SERVER_SCRIPT}"

# 10. restore with frontend unchanged: frontend manifest untouched（不调用禁止的改写 helper）
assert_code_absent "10. restore unchanged: 无 rewrite-manifest helper" 'set_frontend_manifest_sha' "${SERVER_SCRIPT}"

# 11. 显式断言：不存在只改写 manifest git_sha 而不 rebuild artifact 的 helper
assert_code_absent "11. 禁止只改写 manifest 而不 build" 'set_frontend_manifest_sha' "${SERVER_SCRIPT}"

# 12. DEPLOY-1 package classifier remains green
assert_code_contains "12. DEPLOY-1 frontend_package_environment_changed 仍生效" 'frontend_package_environment_changed' "${SERVER_SCRIPT}"

rm -rf "${_FE_TMP}"

echo "== P1-A/P1-B rollback owner 真实拓扑 + dry-run 零容器 mutation =="
_P1_RESULTS="$(mktemp -t panji-p1-results.XXXXXX)"
export _P1_RESULTS
(
    set +e
    _emit() { printf 'RESULT|%s|%s\n' "$1" "$2" >>"${_P1_RESULTS}"; }

    # ---- 真实 Compose 拓扑建模 ----
    #   docker inspect <bare_service>                    -> "No such object"（生产事实：容器名为 trading-<svc>）
    #   docker inspect -f ... trading-worker-after-close -> running（状态探针）
    #   docker compose <...> ps -q <svc>                 -> CID_<svc>
    #   docker inspect CID_<svc>                         -> IMAGE_<svc>
    #   docker compose <...> config                      -> 稳定文本（供 digest 对称计算）
    _STUB="$(mktemp -d -t panji-p1-stub.XXXXXX)"
    cat >"${_STUB}/docker" <<'DOCKEREOF'
#!/usr/bin/env bash
LOG="${PANJI_DOCKER_CALL_LOG:-/tmp/panji_docker_call.log}"
case "$1" in
  inspect)
    if [[ "$*" == *"trading-worker-after-close"* && "$*" == *"-f"* ]]; then
      echo "running"; exit 0
    fi
    arg="$2"
    if [[ "${arg}" == CID_* ]]; then
      echo "IMAGE_${arg#CID_}"
    else
      echo "Error: No such object: ${arg}" >&2
      exit 1
    fi
    ;;
  compose)
    if [[ "$*" == *"stop -t -1 worker-after-close"* ]]; then
      echo "stop" >>"${LOG}"; echo "Stopping worker-after-close"; exit 0
    fi
    if [[ "$*" == *"up -d --force-recreate worker-after-close"* ]]; then
      echo "up" >>"${LOG}"; echo "Recreating worker-after-close"; exit 0
    fi
    for a in "$@"; do
      if [[ "${a}" == "config" ]]; then echo "COMPOSE_DEF"; exit 0; fi
    done
    prev=""
    for a in "$@"; do
      if [[ "${prev}" == "-q" ]]; then svc="$a"; fi
      prev="$a"
    done
    if [[ -n "${svc:-}" ]]; then echo "CID_${svc}"; fi
    ;;
esac
DOCKEREOF
    chmod +x "${_STUB}/docker"
    cat >"${_STUB}/git" <<'GITEOF'
#!/usr/bin/env bash
if [[ "$1" == "rev-parse" ]]; then echo "sha_pre"; exit 0; fi
if [[ "$1" == "diff" && "$2" == "--name-only" ]]; then exit 0; fi
exit 0
GITEOF
    chmod +x "${_STUB}/git"
    PATH="${_STUB}:${PATH}"
    PANJI_DOCKER_CALL_LOG="$(mktemp -t panji-p1-calls.XXXXXX.log)"
    export PANJI_DOCKER_CALL_LOG

    # ---- P1-A：capture / verify 必须共用 compose service → container ID resolver ----
    # FIRST_LIVE_DEPLOY=false → per-service image ID 属 mandatory owner；
    # 若 resolver 仍用 bare service name，本节必然 FAIL（真实拓扑反向证明）。
    _LIVE="$(mktemp -d -t panji-p1-live.XXXXXX)"
    printf 'exp_sha\n' >"${_LIVE}/RUNTIME_SHA"
    LIVE_ROOT="${_LIVE}"
    FIRST_LIVE_DEPLOY=false
    PRE_CHECKOUT_REPO_SHA="sha_pre"
    # 与 verify_rollback_owner 内部完全同一条管线计算，保证对称。
    PRE_CHECKOUT_COMPOSE_DIGEST="$(
        cd "${REPO_ROOT}" && ${COMPOSE_CMD} config 2>/dev/null | sha256sum | awk '{print $1}'
    )"
    PRE_DEPLOY_RUNTIME_OWNER_RESOLVED=false
    PRE_DEPLOY_MANIFEST_FILE="$(mktemp -t panji-p1-manifest.XXXXXX)"

    if [[ -n "${PRE_CHECKOUT_COMPOSE_DIGEST}" ]]; then
        _emit PASS "P1-A 前置: compose digest 可对称计算（sha256sum 可用）"
    else
        _emit FAIL "P1-A 前置: compose digest 为空（sha256sum 不可用，无法做对称校验）"
    fi

    resolve_pre_deploy_runtime_owner
    _rc=$?
    _b="$(grep '^PRE_DEPLOY_IMAGE_ID:backend=' "${PRE_DEPLOY_MANIFEST_FILE}" | cut -d= -f2-)"
    _w="$(grep '^PRE_DEPLOY_IMAGE_ID:worker-after-close=' "${PRE_DEPLOY_MANIFEST_FILE}" | cut -d= -f2-)"
    _f="$(grep '^PRE_DEPLOY_IMAGE_ID:frontend=' "${PRE_DEPLOY_MANIFEST_FILE}" | cut -d= -f2-)"
    if [[ "${_rc}" == "0" && "${_b}" == "IMAGE_backend" \
        && "${_w}" == "IMAGE_worker-after-close" && "${_f}" == "IMAGE_frontend" ]]; then
        _emit PASS "P1-A capture: resolver 经 compose ps -q 解析真实容器 ID（backend/worker-after-close/frontend）"
    else
        _emit FAIL "P1-A capture: rc=${_rc} backend=${_b} worker=${_w} frontend=${_f}"
    fi

    verify_rollback_owner
    _vrc=$?
    if [[ "${_vrc}" == "0" ]]; then
        _emit PASS "P1-A verify: rollback owner 对称校验 PASS（capture/verify 共用同一 resolver）"
    else
        _emit FAIL "P1-A verify: rc=${_vrc}（capture 与 verify resolver 不对称）"
    fi

    # 反向证明：bare inspect 在真实拓扑下必须失败，否则上面的 PASS 是假绿。
    _dout="$(docker inspect backend 2>&1)"
    _drc=$?
    if [[ "${_drc}" != "0" && "${_dout}" == *"No such object"* ]]; then
        _emit PASS "P1-A 反向: bare 'docker inspect backend' 在真实拓扑下失败（resolver 未走 bare name）"
    else
        _emit FAIL "P1-A 反向: bare 'docker inspect backend' 竟成功（${_dout}）—— 拓扑建模错误"
    fi

    # ---- P1-B：dry-run 零容器 mutation ----
    _after_close_running_count() { printf '0'; }   # 只读查询隔离
    AFTER_CLOSE_WAS_RUNNING=false
    AFTER_CLOSE_FENCE_OWNED=false
    AFTER_CLOSE_PICKUP_FENCED=false
    AFTER_CLOSE_PICKUP_FENCE_SIMULATED=false
    DRY_RUN=true
    : >"${PANJI_DOCKER_CALL_LOG}"

    # (1) fence：不得 compose stop；置 SIMULATED=true 且不得伪装真实 FENCED。
    _fence_after_close_worker
    _frc=$?
    _stopn="$(grep -c '^stop$' "${PANJI_DOCKER_CALL_LOG}")"
    if [[ "${_frc}" == "0" && "${_stopn}" == "0" \
        && "${AFTER_CLOSE_PICKUP_FENCE_SIMULATED}" == "true" \
        && "${AFTER_CLOSE_FENCE_OWNED}" == "false" \
        && "${AFTER_CLOSE_PICKUP_FENCED}" == "false" ]]; then
        _emit PASS "P1-B dry-run fence: 零 compose stop（count=${_stopn}），SIMULATED=true，未伪装 FENCED"
    else
        _emit FAIL "P1-B dry-run fence: rc=${_frc} stop=${_stopn} SIM=${AFTER_CLOSE_PICKUP_FENCE_SIMULATED} OWNED=${AFTER_CLOSE_FENCE_OWNED} FENCED=${AFTER_CLOSE_PICKUP_FENCED}"
    fi

    # (2) headroom：无 mock seam → deferred（不读取/不断言真实 MemAvailable）。
    PANJI_MOCK_MEM_AVAILABLE_KB=""
    check_deployment_memory_headroom
    _hrc=$?
    if [[ "${_hrc}" == "0" ]]; then
        _emit PASS "P1-B dry-run headroom: 无 seam 时 deferred（rc=0，不读取真实 MemAvailable）"
    else
        _emit FAIL "P1-B dry-run headroom: rc=${_hrc}（应 deferred 返回 0）"
    fi

    # (3) 临界区门禁：dry-run 下由 SIMULATED 满足，与真实 FENCED 严格区分。
    _backend_pickup_boundary_ready
    _brc=$?
    if [[ "${_brc}" == "0" ]]; then
        _emit PASS "P1-B dry-run 边界门禁: SIMULATED=true 满足（不与真实 FENCED 混用）"
    else
        _emit FAIL "P1-B dry-run 边界门禁: rc=${_brc}（SIMULATED 应被接受）"
    fi

    # (4) restore：即便 FENCE_OWNED 被误置 true，dry-run 也不得 compose up。
    AFTER_CLOSE_FENCE_OWNED=true
    _restore_after_close_pickup_if_owned
    _upn="$(grep -c '^up$' "${PANJI_DOCKER_CALL_LOG}")"
    if [[ "${_upn}" == "0" ]]; then
        _emit PASS "P1-B dry-run restore: 零 compose up（count=${_upn}）"
    else
        _emit FAIL "P1-B dry-run restore: 竟调用 compose up（count=${_upn}）"
    fi

    rm -rf "${_STUB}" "${_LIVE}" "${PANJI_DOCKER_CALL_LOG}" "${PRE_DEPLOY_MANIFEST_FILE}"
)

_P1_EXPECTED=8
_P1_ACTUAL="$(grep -c '^RESULT|' "${_P1_RESULTS}" || true)"
while IFS='|' read -r _tag _st _label; do
    [[ "${_tag}" == "RESULT" ]] || continue
    if [[ "${_st}" == "PASS" ]]; then ok "${_label}"; else bad "${_label}"; fi
done <"${_P1_RESULTS}"
if [[ "${_P1_ACTUAL}" == "${_P1_EXPECTED}" ]]; then
    ok "P1-A/P1-B 断言全部执行（${_P1_ACTUAL}/${_P1_EXPECTED}，子 shell 未早退）"
else
    bad "P1-A/P1-B 断言条数异常（${_P1_ACTUAL}/${_P1_EXPECTED}，子 shell 早退或断言漏跑）"
fi
rm -f "${_P1_RESULTS}"

# ---------------------------------------------------------------------------
echo "== 11/11 DEPLOY ACTIVE-JOB GATE — NARROW TOCTOU CLOSURE =="
# 复用现有 owner guard_active_after_close_jobs，在 deploy() 每个实际 runtime action 前分别
# 做 fresh fail-closed 门禁：
#   guard #1：deploy() 开头，任何 live mutation 之前（防已存在 running job 时开始变更）
#   final guard → restart_services 前
#   final guard → reconcile_compose_runtime 前
# 不新建另一套 active-job 查询 owner。

# CASE A：原有行为——第一道 guard 位于 deploy() 开头（任何 live mutation 之前）。
_deploy_func_body="$(code_of "${SERVER_SCRIPT}" | awk '/^deploy\(\)/{f=1; next} f&&/^}/{exit} f')"
_guard1_line="$(echo "${_deploy_func_body}" | grep -n 'guard_active_after_close_jobs' | head -1 | cut -d: -f1)"
if [[ -n "${_guard1_line}" && "${_guard1_line}" -le 10 ]]; then
    ok "CASE A 第一道 guard 位于 deploy() 开头（live mutation 之前）"
else
    bad "CASE A 第一道 guard 应位于 deploy() 开头"
fi

# CASE D：frontend-only（backend runtime 不 mutate）时，guard 自身 early-return 放行，
#   不被 after-close 活跃任务无关阻塞。结构断言：guard_active_after_close_jobs 函数体内，
#   在 `! _backend_runtime_will_mutate` 守卫后存在 return 0 提前放行（backend 不变则跳过门禁）。
if code_of "${SERVER_SCRIPT}" | awk '
/^guard_active_after_close_jobs\(\)/{f=1; next}
f && /^}/{exit}
f && /! _after_close_process_will_refresh/ {seen=1; next}
seen && /return 0/ {found=1}
END{exit !found}
'; then
    ok "CASE D guard 在 backend runtime 不变化时 early-return 放行（frontend-only 不被阻塞）"
else
    bad "CASE D guard 必须在 backend runtime 不变时 early-return 放行"
fi

# ---------------------------------------------------------------------------
# 行为级测试（Behavior B / C / E）：抽取 deploy() 真实 STEP-6 重启/对账控制流为 snippet，
# 用 stub 替换 restart_services / reconcile_compose_runtime / guard_active_after_close_jobs
# 为调用计数器，并可控 guard 的 PASS / BLOCK。验证真实控制流中的 guard 放置与 action 调用计数。
# 不新建重型 framework——复用已 sourced 的 fixture 函数定义 + 测试内 stub 覆盖。

# 抽取 deploy() 中 STEP-6 重启块（从 FAILURE_STAGE="restart" 起到 STEP-6 结束的 `fi` 之前）。
# 使用 awk 按函数体边界精确截取，保证测试的是源码真实控制流，而非副本。
_STEP6_SNIPPET="$(mktemp -t panji-step6.XXXXXX.sh)"
awk '
/^deploy\(\)/{inf=1; next}
inf && /^}/{inf=0; exit}
inf && /FAILURE_STAGE="restart"/{cap=1}
cap{print}
' "${SERVER_SCRIPT}" > "${_STEP6_SNIPPET}"

if [[ ! -s "${_STEP6_SNIPPET}" ]]; then
    bad "Behavior 抽取 STEP-6 控制流 snippet 失败（需源码含 FAILURE_STAGE=\"restart\" 块）"
fi

# 行为测试 runner：传入输入开关，构造 stub 后执行真实 snippet。
# 不使用 || true 吞掉 source 状态；set +e 后显式捕获 STEP-6 的真实 return code（STEP6_RC），
# 与调用计数一并输出，供断言验证 fail-closed（guard BLOCK 必须使 STEP-6 非零返回）。
run_behavior() {
    # $1 = guard_pre_restart 行为: pass|block
    # $2 = guard_pre_reconcile 行为: pass|block
    # $3.. = 输入：NEED_BACKEND NEED_FRONTEND COMPOSE_RUNTIME_CHANGED
    local _gpr="$1" _gpc="$2"
    local _nb="$3" _nf="$4" _crc="$5"
    (
        source "${_DEPLOY_FIXTURE}"
        set +e   # 不吞掉 STEP-6 真实 return code；显式捕获
        RESTART_CALLS=0; RECONCILE_CALLS=0; GUARD_PR_CALLS=0; GUARD_PC_CALLS=0
        _backend_pickup_boundary_ready() {
            local _who="${FAILURE_STAGE:-?}"
            if [[ "${_who}" == "active_job_gate_pre_restart" ]]; then
                GUARD_PR_CALLS=$((GUARD_PR_CALLS+1))
                if [[ "${_gpr}" == "block" ]]; then return 1; fi
                return 0
            elif [[ "${_who}" == "active_job_gate_pre_reconcile" ]]; then
                GUARD_PC_CALLS=$((GUARD_PC_CALLS+1))
                if [[ "${_gpc}" == "block" ]]; then return 1; fi
                return 0
            fi
            return 0
        }
        restart_services()   { RESTART_CALLS=$((RESTART_CALLS+1)); }
        reconcile_compose_runtime() { RECONCILE_CALLS=$((RECONCILE_CALLS+1)); }
        need_backend="${_nb}"; need_frontend="${_nf}"; COMPOSE_RUNTIME_CHANGED="${_crc}"
        RESTARTED_PYTHON=false; RESTARTED_FRONTEND=false
        source "${_STEP6_SNIPPET}"
        _step6_rc=$?
        echo "STEP6_RC=${_step6_rc}"
        echo "RESTART_CALLS=${RESTART_CALLS}"
        echo "RECONCILE_CALLS=${RECONCILE_CALLS}"
        echo "GUARD_PR_CALLS=${GUARD_PR_CALLS}"
        echo "GUARD_PC_CALLS=${GUARD_PC_CALLS}"
    ) || true
}

# Behavior B — 核心：initial/first guard PASS → 准备 → final guard BLOCK
# 模拟：backend runtime 变化（restart 路径）+ compose 变化（reconcile 路径）均存在，
#       但 pre-restart / pre-reconcile final guard 都 BLOCK。
# 断言：STEP6_RC != 0（被阻塞）；restart_services call_count=0；reconcile call_count=0。
_B_OUT="$(run_behavior block block true true true)"
_B_RC="$(echo "${_B_OUT}" | grep -E '^STEP6_RC=' | head -1 | cut -d= -f2)"
_B_R="$(echo "${_B_OUT}" | grep -E '^RESTART_CALLS=' | head -1 | cut -d= -f2)"
_B_P="$(echo "${_B_OUT}" | grep -E '^RECONCILE_CALLS=' | head -1 | cut -d= -f2)"
if [[ "${_B_RC}" != "0" && "${_B_R}" == "0" && "${_B_P}" == "0" ]]; then
    ok "Behavior B final guard BLOCK → STEP6_RC=${_B_RC}（!=0）restart_services=0 且 reconcile=0（部署被 fail-closed 阻止）"
else
    bad "Behavior B 期望 STEP6_RC!=0 且 restart=0 且 reconcile=0；实际: ${_B_OUT}"
fi

# Behavior C — 所有 guard PASS：backend 变化触发 restart_services 恰好执行一次。
_C_OUT="$(run_behavior pass pass true false false)"
_C_RC="$(echo "${_C_OUT}" | grep -E '^STEP6_RC=' | head -1 | cut -d= -f2)"
_C_R="$(echo "${_C_OUT}" | grep -E '^RESTART_CALLS=' | head -1 | cut -d= -f2)"
if [[ "${_C_RC}" == "0" && "${_C_R}" == "1" ]]; then
    ok "Behavior C 所有 guard PASS → STEP6_RC=${_C_RC} restart_services=1"
else
    bad "Behavior C 期望 STEP6_RC=0 且 restart_services=1；实际: ${_C_OUT}"
fi

# Behavior E — 两种 action 同时存在：
#   initial/first guard PASS，pre-restart guard PASS → restart_services 执行；
#   随后模拟 reconcile 前新出现 active job → pre-reconcile guard BLOCK。
# 断言：STEP6_RC != 0（reconcile 被拦截至非零）；restart_services call_count=1；
#        reconcile_compose_runtime call_count=0；GUARD_PR_CALLS=1；GUARD_PC_CALLS=1
#        （证明第二次 fresh guard 确实执行）。此测试抓住 6c5ead29 仅单一 pre-restart guard 的残余窗口。
_E_OUT="$(run_behavior pass block true true true)"
_E_RC="$(echo "${_E_OUT}" | grep -E '^STEP6_RC=' | head -1 | cut -d= -f2)"
_E_R="$(echo "${_E_OUT}" | grep -E '^RESTART_CALLS=' | head -1 | cut -d= -f2)"
_E_P="$(echo "${_E_OUT}" | grep -E '^RECONCILE_CALLS=' | head -1 | cut -d= -f2)"
_E_GPR="$(echo "${_E_OUT}" | grep -E '^GUARD_PR_CALLS=' | head -1 | cut -d= -f2)"
_E_GPC="$(echo "${_E_OUT}" | grep -E '^GUARD_PC_CALLS=' | head -1 | cut -d= -f2)"
if [[ "${_E_RC}" != "0" && "${_E_R}" == "1" && "${_E_P}" == "0" && "${_E_GPR}" == "1" && "${_E_GPC}" == "1" ]]; then
    ok "Behavior E 两种 action 并存：STEP6_RC=${_E_RC}（!=0）restart=1 reconcile=0 pre-restart_guard=${_E_GPR} pre-reconcile_guard=${_E_GPC}（第二次 fresh guard 已执行并拦截）"
else
    bad "Behavior E 期望 STEP6_RC!=0 且 restart=1 且 reconcile=0 且 GUARD_PR=1 且 GUARD_PC=1；实际: ${_E_OUT}"
fi

rm -f "${_DEPLOY_FIXTURE}"
rm -rf "${_GIT_STUB_DIR}"

echo "----------------------------------------"
echo "部署脚本结构契约测试：${PASS} 通过 / ${FAIL} 失败"
[[ "${FAIL}" -eq 0 ]] && exit 0 || exit 1
