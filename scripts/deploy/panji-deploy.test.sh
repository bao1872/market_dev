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
    'reconcile_compose_runtime "\$\{PYTHON_SERVICES\[@\]\}" frontend' "${SERVER_SCRIPT}"
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

# CASE 7-8：P1-C 失败传播结构性断言（不实际执行部署，仅校验源码是否含传播点）。
# 7：restart_services 调用点必须显式 || return 1
assert_code_contains "CASE7 restart_services 调用显式 || return 1（失败传播到 deploy）" \
    'restart_services "\$\{restart_list\[@\]\}" \|\| return 1' "${SERVER_SCRIPT}"
# 7b：reconcile_compose_runtime 调用点必须显式 || return 1
assert_code_contains "CASE7 reconcile_compose_runtime 调用显式 || return 1（失败传播到 deploy）" \
    'reconcile_compose_runtime "\$\{PYTHON_SERVICES\[@\]\}" frontend \|\| return 1' "${SERVER_SCRIPT}"
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
_mrec="$(code_of "${SERVER_SCRIPT}" | awk '/^deploy\(\)/{f=1} f&&/^}/{exit} f&&/reconcile_compose_runtime "\$\{PYTHON_SERVICES\[@\]\}" frontend \|\| return 1/{print NR; exit}')"
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
    'reconcile_compose_runtime "\$\{PYTHON_SERVICES\[@\]\}" frontend \|\| return 1' "${SERVER_SCRIPT}"
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
echo "== 11/11 DEPLOY ACTIVE-JOB GATE — NARROW TOCTOU CLOSURE =="
# 复用现有 owner guard_active_after_close_jobs，在 deploy() 两处各调用一次：
#   guard #1：deploy() 开头，任何 live mutation 之前（防已存在 running job 时开始变更）
#   guard #2：紧邻 destructive runtime action（restart_services / reconcile_compose_runtime）之前，
#             拦截 gate→restart 窗口内新接纳的活跃 after-close 任务。
# 不新建另一套 active-job 查询 owner。

# CASE A：原有行为——第一道 guard 位于 deploy() 开头（任何 live mutation 之前）。
_deploy_func_body="$(code_of "${SERVER_SCRIPT}" | awk '/^deploy\(\)/{f=1; next} f&&/^}/{exit} f')"
_guard1_line="$(echo "${_deploy_func_body}" | grep -n 'guard_active_after_close_jobs' | head -1 | cut -d: -f1)"
_guard_total="$(echo "${_deploy_func_body}" | grep -c 'guard_active_after_close_jobs')"
if [[ -n "${_guard1_line}" && "${_guard1_line}" -le 10 ]]; then
    ok "CASE A 第一道 guard 位于 deploy() 开头（live mutation 之前）"
else
    bad "CASE A 第一道 guard 应位于 deploy() 开头"
fi

# CASE B：本次新增核心合同——第二道 guard 紧邻 restart_services / reconcile_compose_runtime 之前。
_restart_line="$(echo "${_deploy_func_body}" | grep -n 'restart_services "\${restart_list\[@\]}" || return 1' | head -1 | cut -d: -f1)"
_reconcile_line="$(echo "${_deploy_func_body}" | grep -n 'reconcile_compose_runtime "${PYTHON_SERVICES\[@\]}" frontend || return 1' | head -1 | cut -d: -f1)"
_guard2_line="$(echo "${_deploy_func_body}" | grep -n 'guard_active_after_close_jobs' | tail -1 | cut -d: -f1)"
if [[ "${_guard_total}" -ge 2 ]]; then ok "CASE B 存在第二道 guard（复用同一 owner）"; else bad "CASE B 必须存在第二道 guard"; fi
if [[ -n "${_guard2_line}" && -n "${_restart_line}" && "${_guard2_line}" -lt "${_restart_line}" ]]; then
    ok "CASE B 第二道 guard 位于 restart_services 之前"
else
    bad "CASE B 第二道 guard 必须紧邻 restart_services 之前"
fi
if [[ -n "${_guard2_line}" && -n "${_reconcile_line}" && "${_guard2_line}" -lt "${_reconcile_line}" ]]; then
    ok "CASE B 第二道 guard 位于 reconcile_compose_runtime 之前"
else
    bad "CASE B 第二道 guard 必须位于 reconcile_compose_runtime 之前"
fi
# 两道 guard 之间必须存在多阶段窗口（TOCTOU 窗口客观存在，需第二道兜底）。
if [[ -n "${_guard1_line}" && -n "${_guard2_line}" && "$((_guard2_line - _guard1_line))" -ge 5 ]]; then
    ok "CASE B 两道 guard 之间存在多阶段窗口（TOCTOU 窗口客观存在，需第二道兜底）"
else
    bad "CASE B 两道 guard 之间应存在多阶段窗口"
fi

# CASE C：无 active job 时正常重启路径保留（guard 后仍存在 restart_services 调用）。
if [[ -n "${_restart_line}" ]]; then
    ok "CASE C 第二道 guard 后仍存在 restart_services 调用（无 active job 时正常重启路径保留）"
else
    bad "CASE C 必须保留 restart_services 正常路径"
fi

# CASE D：frontend-only（backend runtime 不 mutate）时，guard 自身 early-return 放行，
#   不被 after-close 活跃任务无关阻塞。结构断言：guard_active_after_close_jobs 函数体内，
#   在 `! _backend_runtime_will_mutate` 守卫后存在 return 0 提前放行（backend 不变则跳过门禁）。
if code_of "${SERVER_SCRIPT}" | awk '
/^guard_active_after_close_jobs\(\)/{f=1; next}
f && /^}/{exit}
f && /! _backend_runtime_will_mutate/ {seen=1; next}
seen && /return 0/ {found=1}
END{exit !found}
'; then
    ok "CASE D guard 在 backend runtime 不变化时 early-return 放行（frontend-only 不被阻塞）"
else
    bad "CASE D guard 必须在 backend runtime 不变时 early-return 放行"
fi

rm -f "${_DEPLOY_FIXTURE}"
rm -rf "${_GIT_STUB_DIR}"

echo "----------------------------------------"
echo "部署脚本结构契约测试：${PASS} 通过 / ${FAIL} 失败"
[[ "${FAIL}" -eq 0 ]] && exit 0 || exit 1
