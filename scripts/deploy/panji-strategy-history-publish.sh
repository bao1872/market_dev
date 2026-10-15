#!/usr/bin/env bash
# panji-strategy-history-publish.sh — 盘迹历史只读策略资产发布（服务器端唯一实现）
#
# 权威来源：
#   - docs/runbooks/strategy-history-asset-publish.md（操作步骤与验收）
#   - docker-compose.prod.yml / docker-compose.live.yml（只读挂载 /opt/panji-strategy-data:ro）
#   - rules/80-deployment-migration.md（远程唯一入口、禁止 scp/docker cp、最小写入范围）
#
# 职责（服务器端唯一资产发布实现）：
#   读取**目标 SHA 工作树中已登记**的只读历史 JSON 资产 → 校验资产身份 / 审核状态 /
#   文件大小 / 逐文件 SHA → 写入独立暂存目录 → 全部通过后以 rename 原子切换为固定批次
#   目录 → 目标已存在且内容完全一致则幂等成功；存在任何差异立即拒绝覆盖。
#
# 明确不做（超出范围即失败）：
#   - 不写数据库、不创建 active_release.json、不激活模型、不改 AUDITED/PUBLISHED 状态；
#   - 不传输 pickle / 数据库文件 / 密钥 / 任何其他未登记文件；
#   - 不删除既有生产资产，失败只清理本次 Runner 创建的暂存目录；
#   - 不重启容器、不触发部署（部署由 scripts/ops/panji-test-deploy 独立执行）。
#
# 用法:
#   scripts/deploy/panji-strategy-history-publish.sh <FULL_SHA> [--dry-run]
#
# 调用方：scripts/ops/panji-strategy-history-publish（本地唯一用户入口，经 SSH 调用本脚本）
#
# 约束:
# - 必须在 panji-prod 上运行；REPO_ROOT 默认 /root/web_dev；ASSET_ROOT 默认 /opt/panji-strategy-data；
# - 资产只来自 REPO_ROOT/deploy/assets/strategy-history/<LEAF>，LEAF 白名单硬编码，
#   本 Runner 不是通用文件上传工具；
# - 工作树 HEAD 必须等于目标 SHA，防止读到另一版本的资产；
# - --dry-run 为零 mutation：只校验与输出计划，不创建 / 修改任何目录或文件。
#
# 退出码：0 成功；1 校验失败 / 冲突拒绝 / 发布失败；2 参数或环境错误。

set -euo pipefail

REPO_ROOT="${PANJI_REPO_ROOT:-/root/web_dev}"
ASSET_ROOT="${PANJI_STRATEGY_DATA_ROOT:-/opt/panji-strategy-data}"

# ===== 固定批次白名单（硬编码，禁止扩展为通用通道）=====
ALLOWED_LEAF="2026-08-17_89a5be0b28d1dac9"
ASSET_SUBDIR="deploy/assets/strategy-history/${ALLOWED_LEAF}"
INDEX_NAME="asset_index.json"
DATA_FILES=("candidate_snapshot.json" "candidate_shard_0000.json" "evaluation_snapshot.json")

# ===== C 阶段已校验资产身份的期望值（与 asset_index.json 必须一致）=====
EXPECT_STOCK_COUNT=14
EXPECT_CANDIDATE_COUNT=260
EXPECT_BATCH_DAYS=20
EXPECT_SOURCE_STATUS="AUDITED"
EXPECT_EVALUATION_STATUS="AUDITED"

# ===== 大小上限（超出立即停止，防止误传大体量文件）=====
MAX_FILE_BYTES=$((32 * 1024 * 1024))
MAX_TOTAL_BYTES=$((64 * 1024 * 1024))

DRY_RUN=false
TARGET_SHA=""

log() { printf '[history-publish] %s\n' "$*"; }
die() { printf '[history-publish] ERROR %s: %s\n' "$1" "$2" >&2; exit 1; }
usage() {
    echo "用法: $0 <FULL_SHA> [--dry-run]"
    exit 2
}

# 跨平台基础工具：SHA256 与文件大小（生产主机为 GNU coreutils；本地测试可能不是）
sha_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    else
        shasum -a 256 "$1" | awk '{print $1}'
    fi
}
size_of() { wc -c < "$1" | tr -d '[:space:]'; }
# 列出目录下条目相对路径（不使用 GNU find -printf，保证 BSD find 同样可用）
list_entries() { (cd "$1" && find . -mindepth 1 | sed 's|^\./||' | sort); }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=true; shift ;;
        -h|--help) usage ;;
        -*) die BAD_OPTION "未知选项: $1" ;;
        *)
            [[ -z "${TARGET_SHA}" ]] || die BAD_ARGS "只能指定一个 SHA"
            TARGET_SHA="$1"
            shift ;;
    esac
done
[[ -n "${TARGET_SHA}" ]] || usage

[[ "${TARGET_SHA}" =~ ^[0-9a-f]{40}$ ]] || die BAD_SHA "必须提供 40 位完整 SHA: ${TARGET_SHA}"

for cmd in git python3 install find sed awk; do
    command -v "${cmd}" >/dev/null 2>&1 || die MISSING_DEPENDENCY "缺少命令: ${cmd}"
done
if ! command -v sha256sum >/dev/null 2>&1 && ! command -v shasum >/dev/null 2>&1; then
    die MISSING_DEPENDENCY "缺少 sha256sum 或 shasum"
fi

# ===== 1. 工作树必须等于目标 SHA =====
[[ -d "${REPO_ROOT}" ]] || die REPO_ROOT_MISSING "${REPO_ROOT}"
git -C "${REPO_ROOT}" rev-parse --git-dir >/dev/null 2>&1 || die NOT_GIT_WORKTREE "${REPO_ROOT}"
WORKTREE_SHA="$(git -C "${REPO_ROOT}" rev-parse HEAD)"
[[ "${WORKTREE_SHA}" == "${TARGET_SHA}" ]] \
    || die WORKTREE_SHA_MISMATCH "工作树 HEAD=${WORKTREE_SHA} 与目标 ${TARGET_SHA} 不一致"

# ===== 2. 源资产目录必须存在且来自 Git 工作树 =====
SRC="${REPO_ROOT}/${ASSET_SUBDIR}"
[[ -d "${SRC}" && ! -L "${SRC}" ]] || die ASSET_SRC_MISSING "${ASSET_SUBDIR}"
[[ -f "${SRC}/${INDEX_NAME}" && ! -L "${SRC}/${INDEX_NAME}" ]] || die ASSET_INDEX_MISSING "${INDEX_NAME}"

# ===== 3. 解析并校验资产索引（唯一校验真源）=====
INDEX_DUMP="$(python3 - "${SRC}/${INDEX_NAME}" <<'PY'
import json, sys
d = json.load(open(sys.argv[1], encoding='utf-8'))
print('release_id\t%s' % d.get('release_id', ''))
print('as_of\t%s' % d.get('as_of', ''))
print('stock_count\t%s' % d.get('stock_count', ''))
print('candidate_count\t%s' % d.get('candidate_count', ''))
print('batch_days_count\t%s' % len(d.get('batch_days') or []))
print('source_status\t%s' % d.get('source_status', ''))
print('evaluation_status\t%s' % d.get('evaluation_status', ''))
print('bundle_sha256\t%s' % d.get('bundle_sha256', ''))
for k, v in (d.get('sha256') or {}).items():
    print('sha\t%s\t%s' % (k, v))
for k, v in (d.get('size_bytes') or {}).items():
    print('size\t%s\t%s' % (k, v))
PY
)" || die INDEX_UNREADABLE "asset_index.json 解析失败"

IDX_RELEASE_ID=""; IDX_AS_OF=""; IDX_STOCK_COUNT=""; IDX_CANDIDATE_COUNT=""
IDX_BATCH_DAYS=""; IDX_SOURCE_STATUS=""; IDX_EVALUATION_STATUS=""; IDX_BUNDLE_SHA=""
declare -A IDX_SHA=()
declare -A IDX_SIZE=()
declare -a IDX_SHA_KEYS=()
declare -a IDX_SIZE_KEYS=()
while IFS=$'\t' read -r f1 f2 f3; do
    case "${f1}" in
        release_id) IDX_RELEASE_ID="${f2}" ;;
        as_of) IDX_AS_OF="${f2}" ;;
        stock_count) IDX_STOCK_COUNT="${f2}" ;;
        candidate_count) IDX_CANDIDATE_COUNT="${f2}" ;;
        batch_days_count) IDX_BATCH_DAYS="${f2}" ;;
        source_status) IDX_SOURCE_STATUS="${f2}" ;;
        evaluation_status) IDX_EVALUATION_STATUS="${f2}" ;;
        bundle_sha256) IDX_BUNDLE_SHA="${f2}" ;;
        sha) IDX_SHA["${f2}"]="${f3}"; IDX_SHA_KEYS+=("${f2}") ;;
        size) IDX_SIZE["${f2}"]="${f3}"; IDX_SIZE_KEYS+=("${f2}") ;;
    esac
done <<< "${INDEX_DUMP}"

# 索引键集合白名单：拒绝任何额外 / 穿越 / 非登记键（fail-closed）
EXPECT_SHA_KEYS=()
EXPECT_SIZE_KEYS=("${INDEX_NAME}")
for name in "${DATA_FILES[@]}"; do
    EXPECT_SHA_KEYS+=("release/${ALLOWED_LEAF}/${name}")
    EXPECT_SIZE_KEYS+=("release/${ALLOWED_LEAF}/${name}")
done
key_set() { local IFS=$'\n'; printf '%s\n' "$@" | sort | tr '\n' '|'; }
if [[ ${#IDX_SHA_KEYS[@]} -ne ${#EXPECT_SHA_KEYS[@]} \
   || "$(key_set "${IDX_SHA_KEYS[@]}")" != "$(key_set "${EXPECT_SHA_KEYS[@]}")" ]]; then
    die INDEX_EXTRA_KEYS "索引 sha256 键集合与登记清单不一致"
fi
if [[ ${#IDX_SIZE_KEYS[@]} -ne ${#EXPECT_SIZE_KEYS[@]} \
   || "$(key_set "${IDX_SIZE_KEYS[@]}")" != "$(key_set "${EXPECT_SIZE_KEYS[@]}")" ]]; then
    die INDEX_EXTRA_KEYS "索引 size_bytes 键集合与登记清单不一致"
fi

[[ "${IDX_RELEASE_ID}" =~ ^[0-9a-f]{64}$ ]] || die BAD_RELEASE_ID "${IDX_RELEASE_ID}"
[[ "${IDX_AS_OF}" =~ ^20[0-9]{2}-[0-9]{2}-[0-9]{2}$ ]] || die BAD_AS_OF "${IDX_AS_OF}"
COMPUTED_LEAF="${IDX_AS_OF}_${IDX_RELEASE_ID:0:16}"
[[ "${COMPUTED_LEAF}" == "${ALLOWED_LEAF}" ]] \
    || die LEAF_MISMATCH "索引推导 ${COMPUTED_LEAF} != 白名单 ${ALLOWED_LEAF}"
[[ "${IDX_BUNDLE_SHA}" =~ ^[0-9a-f]{64}$ ]] || die BAD_BUNDLE_SHA "${IDX_BUNDLE_SHA}"
[[ "${IDX_STOCK_COUNT}" == "${EXPECT_STOCK_COUNT}" ]] \
    || die STOCK_COUNT_MISMATCH "实际 ${IDX_STOCK_COUNT} != 期望 ${EXPECT_STOCK_COUNT}"
[[ "${IDX_CANDIDATE_COUNT}" == "${EXPECT_CANDIDATE_COUNT}" ]] \
    || die CANDIDATE_COUNT_MISMATCH "实际 ${IDX_CANDIDATE_COUNT} != 期望 ${EXPECT_CANDIDATE_COUNT}"
[[ "${IDX_BATCH_DAYS}" == "${EXPECT_BATCH_DAYS}" ]] \
    || die BATCH_DAYS_MISMATCH "实际 ${IDX_BATCH_DAYS} != 期望 ${EXPECT_BATCH_DAYS}"
[[ "${IDX_SOURCE_STATUS}" == "${EXPECT_SOURCE_STATUS}" ]] \
    || die SOURCE_STATUS_INVALID "实际 ${IDX_SOURCE_STATUS} != ${EXPECT_SOURCE_STATUS}"
[[ "${IDX_EVALUATION_STATUS}" == "${EXPECT_EVALUATION_STATUS}" ]] \
    || die EVALUATION_STATUS_INVALID "实际 ${IDX_EVALUATION_STATUS} != ${EXPECT_EVALUATION_STATUS}"

log "资产索引校验通过: leaf=${ALLOWED_LEAF} release_id=${IDX_RELEASE_ID:0:16}… "\
"stocks=${IDX_STOCK_COUNT} candidates=${IDX_CANDIDATE_COUNT} batch_days=${IDX_BATCH_DAYS} "\
"status=${IDX_SOURCE_STATUS}/${IDX_EVALUATION_STATUS} bundle=${IDX_BUNDLE_SHA:0:16}…"

# ===== 4. 源目录文件集合：只允许登记的 4 个文件，禁止软链 / 子目录 / 额外文件 =====
declare -A SEEN=()
while IFS= read -r rel; do
    [[ -n "${rel}" ]] || continue
    case "${rel}" in
        "${INDEX_NAME}") ;;
        candidate_snapshot.json|candidate_shard_0000.json|evaluation_snapshot.json) ;;
        *) die EXTRA_ASSET_FILE "源资产目录出现未登记条目: ${rel}" ;;
    esac
    p="${SRC}/${rel}"
    [[ -f "${p}" && ! -L "${p}" ]] || die NON_REGULAR_ASSET_FILE "非普通文件或为软链接: ${rel}"
    SEEN["${rel}"]=1
done < <(list_entries "${SRC}")
for required in "${INDEX_NAME}" "${DATA_FILES[@]}"; do
    [[ -n "${SEEN[${required}]:-}" ]] || die MISSING_ASSET_FILE "缺少登记文件: ${required}"
done

# ===== 5. 逐文件：大小（含索引登记值比对与上限）与 SHA256 =====
TOTAL_BYTES=0
for name in "${DATA_FILES[@]}"; do
    p="${SRC}/${name}"
    key="release/${ALLOWED_LEAF}/${name}"
    exp_sha="${IDX_SHA[${key}]:-}"
    exp_size="${IDX_SIZE[${key}]:-}"
    [[ -n "${exp_sha}" ]] || die INDEX_MISSING_SHA "${key}"
    [[ -n "${exp_size}" ]] || die INDEX_MISSING_SIZE "${key}"
    act_size="$(size_of "${p}")"
    [[ "${act_size}" == "${exp_size}" ]] \
        || die SIZE_MISMATCH "${name}: 实际 ${act_size} != 索引登记 ${exp_size}"
    if (( act_size > MAX_FILE_BYTES )); then
        die FILE_TOO_LARGE "${name}: ${act_size} > ${MAX_FILE_BYTES}"
    fi
    TOTAL_BYTES=$((TOTAL_BYTES + act_size))
    act_sha="$(sha_of "${p}")"
    [[ "${act_sha}" == "${exp_sha}" ]] \
        || die SHA_MISMATCH "${name}: 实际 ${act_sha} != 索引 ${exp_sha}"
    log "  校验通过 ${name} bytes=${act_size} sha256=${act_sha:0:16}…"
done
if (( TOTAL_BYTES > MAX_TOTAL_BYTES )); then
    die TOTAL_TOO_LARGE "资产总大小 ${TOTAL_BYTES} > ${MAX_TOTAL_BYTES}"
fi

# ===== 6. 目标目录状态：absent / identical / conflict（只读判定）=====
TARGET_DIR="${ASSET_ROOT}/release/${ALLOWED_LEAF}"
target_state() {
    if [[ ! -e "${TARGET_DIR}" ]]; then
        printf 'absent\n'; return 0
    fi
    if [[ ! -d "${TARGET_DIR}" || -L "${TARGET_DIR}" ]]; then
        printf 'conflict\n'; return 0
    fi
    local rel
    while IFS= read -r rel; do
        [[ -n "${rel}" ]] || continue
        case "${rel}" in
            candidate_snapshot.json|candidate_shard_0000.json|evaluation_snapshot.json) ;;
            *) printf 'conflict\n'; return 0 ;;
        esac
    done < <(list_entries "${TARGET_DIR}")
    local name a
    for name in "${DATA_FILES[@]}"; do
        [[ -f "${TARGET_DIR}/${name}" && ! -L "${TARGET_DIR}/${name}" ]] || { printf 'conflict\n'; return 0; }
        a="$(sha_of "${TARGET_DIR}/${name}")"
        [[ "${a}" == "${IDX_SHA[release/${ALLOWED_LEAF}/${name}]}" ]] || { printf 'conflict\n'; return 0; }
    done
    printf 'identical\n'
}

STATE="$(target_state)"
log "目标目录状态: ${STATE} (${TARGET_DIR})"

emit_summary() {
    local status="$1"
    cat <<EOF
{
  "status": "${status}",
  "dry_run": ${DRY_RUN},
  "target_sha": "${TARGET_SHA}",
  "asset_root": "${ASSET_ROOT}",
  "release_leaf": "${ALLOWED_LEAF}",
  "target_dir": "${TARGET_DIR}",
  "release_id": "${IDX_RELEASE_ID}",
  "as_of": "${IDX_AS_OF}",
  "stock_count": ${IDX_STOCK_COUNT},
  "candidate_count": ${IDX_CANDIDATE_COUNT},
  "batch_days": ${IDX_BATCH_DAYS},
  "source_status": "${IDX_SOURCE_STATUS}",
  "evaluation_status": "${IDX_EVALUATION_STATUS}",
  "bundle_sha256": "${IDX_BUNDLE_SHA}",
  "total_bytes": ${TOTAL_BYTES},
  "files": [$(printf '"%s",' "${DATA_FILES[@]}" | sed 's/,$//')],
  "model_activated": false,
  "active_release_pointer_written": false
}
EOF
}

# ===== 7. dry-run：零 mutation，冲突即失败（fail-closed）=====
if [[ "${DRY_RUN}" == "true" ]]; then
    case "${STATE}" in
        absent) log "[dry-run] 计划：新建 ${TARGET_DIR}（暂存 → 原子 rename），零 mutation" ;;
        identical) log "[dry-run] 计划：目标已存在且内容一致，幂等成功，零 mutation" ;;
        conflict) die ASSET_CONFLICT_REFUSE_OVERWRITE "dry-run 发现目标已存在且内容存在差异" ;;
    esac
    emit_summary "PLAN_ONLY"
    exit 0
fi

# ===== 8. 真实发布：独立暂存目录 → 校验 → 原子切换 =====
STAGING_PARENT="${ASSET_ROOT}/.staging"
STAGING="${STAGING_PARENT}/panji-history-publish-$$-$(date +%Y%m%d%H%M%S)"
cleanup_staging() {
    local rc=$?
    if [[ -n "${STAGING:-}" \
       && "${STAGING}" == "${STAGING_PARENT}/panji-history-publish-"* \
       && -d "${STAGING}" ]]; then
        chmod -R u+w "${STAGING}" 2>/dev/null || true
        rm -rf -- "${STAGING}"
        log "已清理本次暂存目录: ${STAGING}"
    fi
    exit "${rc}"
}
trap cleanup_staging EXIT

mkdir -p "${STAGING}/release/${ALLOWED_LEAF}"
for name in "${DATA_FILES[@]}"; do
    key="release/${ALLOWED_LEAF}/${name}"
    install -m 0444 "${SRC}/${name}" "${STAGING}/release/${ALLOWED_LEAF}/${name}"
    staged_sha="$(sha_of "${STAGING}/release/${ALLOWED_LEAF}/${name}")"
    [[ "${staged_sha}" == "${IDX_SHA[${key}]}" ]] \
        || die STAGING_SHA_MISMATCH "${name}: 暂存副本 ${staged_sha} != ${IDX_SHA[${key}]}"
    log "  暂存副本校验通过 ${name}"
done
# 注意：暂存目录在此保持可写。POSIX rename 移动目录时需要对源目录本身有写权限
# （更新 .. 项），提前 chmod 0555 会让原子切换失败。只读属性在切换成功后施加。

case "${STATE}" in
    absent)
        mkdir -p "${ASSET_ROOT}/release"
        mv -- "${STAGING}/release/${ALLOWED_LEAF}" "${TARGET_DIR}"
        chmod 0555 "${TARGET_DIR}"
        log "原子切换完成并置只读: ${TARGET_DIR}"
        ;;
    identical)
        log "目标已存在且内容完全一致 → 幂等成功，不覆盖"
        ;;
    conflict)
        die ASSET_CONFLICT_REFUSE_OVERWRITE "目标已存在且内容存在差异，拒绝覆盖现有生产资产"
        ;;
esac

# ===== 9. 发布后复核：目标文件 SHA 与索引一致，且未创建现役指针 =====
for name in "${DATA_FILES[@]}"; do
    key="release/${ALLOWED_LEAF}/${name}"
    final_sha="$(sha_of "${TARGET_DIR}/${name}")"
    [[ "${final_sha}" == "${IDX_SHA[${key}]}" ]] \
        || die POST_PUBLISH_SHA_MISMATCH "${name}: ${final_sha}"
done
[[ ! -e "${ASSET_ROOT}/active_release.json" ]] \
    || die ACTIVE_RELEASE_POINTER_DETECTED "检测到 active_release.json，属于越权产物"
[[ ! -e "${TARGET_DIR}/active_release.json" ]] \
    || die ACTIVE_RELEASE_POINTER_DETECTED "批次目录内检测到 active_release.json"

log "发布后复核通过：3 个文件 SHA 与索引一致，未创建现役模型指针"
emit_summary "PUBLISHED_OR_IDEMPOTENT"
exit 0
