#!/usr/bin/env bash
# panji-strategy-history-publish.test.sh — 历史只读资产发布 Runner 安全测试
#
# 绑定来源（PANJI-STRATEGY-ASSET-PUBLISH-001）：
#   - 本地唯一入口：scripts/ops/panji-strategy-history-publish
#   - 服务器端唯一实现：scripts/deploy/panji-strategy-history-publish.sh
#
# 测试范围：
#   1）静态契约：文件存在 / 语法 / 禁用的临时传输通道 / LEAF 与文件清单一致；
#   2）真实本地执行：以临时 git 仓库 + 临时资产根运行服务器端实现，覆盖
#      正常发布、重复发布幂等、冲突拒绝、额外文件、软链接、SHA/大小不符、
#      审核状态与计数不符、索引额外键（路径穿越）、dry-run 零 mutation、
#      部分失败暂存清理、非 40 位 SHA、工作树 SHA 不一致。
#
# 说明：不使用生产主机、不连接数据库、不调用 SSH。
#
# 用法: bash scripts/deploy/panji-strategy-history-publish.test.sh
# 退出码：0 = 全部通过；1 = 任一失败。

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

SERVER_SCRIPT="${REPO_ROOT}/scripts/deploy/panji-strategy-history-publish.sh"
LOCAL_ENTRY="${REPO_ROOT}/scripts/ops/panji-strategy-history-publish"
LEAF="$(sed -n 's/^ALLOWED_LEAF="\([^"]*\)"$/\1/p' "${SERVER_SCRIPT}" | head -1)"
DATA_FILES=("candidate_snapshot.json" "candidate_shard_0000.json" "evaluation_snapshot.json")

PASS=0
FAIL=0
RC=0
OUT=""

ok()  { echo "  [PASS] $1"; PASS=$((PASS + 1)); }
bad() { echo "  [FAIL] $1" >&2; FAIL=$((FAIL + 1)); }

assert_eq() { # label expected actual
    if [[ "$2" == "$3" ]]; then ok "$1"; else bad "$1 (expected=$2 actual=$3)"; fi
}
assert_ok() { if [[ "$2" -eq 0 ]]; then ok "$1"; else bad "$1 (rc=$2): $(echo "$OUT" | tail -3)"; fi; }
assert_fail() { # label rc expected_code
    if [[ "$2" -ne 0 ]]; then ok "$1 (rc=$2)"; else bad "$1：应当失败但成功"; return; fi
    if echo "${OUT}" | grep -q "$3"; then ok "$1 错误码 $3"; else bad "$1 缺少错误码 $3：$(echo "$OUT" | tail -3)"; fi
}

sha_of() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'; else shasum -a 256 "$1" | awk '{print $1}'; fi
}

TMP_ROOT="$(mktemp -d)"
trap 'chmod -R u+w "${TMP_ROOT}" 2>/dev/null || true; rm -rf "${TMP_ROOT}"' EXIT

# 构造一个临时 git 仓库，内容为仓库中已登记的资产副本
make_repo() {
    local d="$1"
    mkdir -p "${d}/deploy/assets/strategy-history/${LEAF}"
    cp "${REPO_ROOT}/deploy/assets/strategy-history/${LEAF}"/*.json \
       "${d}/deploy/assets/strategy-history/${LEAF}/"
    ( cd "${d}" \
      && git init -q \
      && git add -A \
      && git -c user.email=panji-test@example.invalid -c user.name=panji-test \
             commit -q -m "readonly history asset" ) >/dev/null 2>&1
    ( cd "${d}" && git rev-parse HEAD )
}

run_server() { # $1=repo $2=asset_root $3..=args
    local repo="$1" root="$2"; shift 2
    RC=0
    OUT="$(PANJI_REPO_ROOT="${repo}" PANJI_STRATEGY_DATA_ROOT="${root}" \
        bash "${SERVER_SCRIPT}" "$@" 2>&1)" || RC=$?
}

echo "=== 历史只读资产发布 Runner 测试 ==="

# ---------------------------------------------------------------------------
echo "== 1/4 静态契约 =="
for f in "${SERVER_SCRIPT}" "${LOCAL_ENTRY}"; do
    if [[ -f "${f}" ]]; then ok "存在: ${f#"${REPO_ROOT}/"}"; else bad "缺失: ${f#"${REPO_ROOT}/"}"; fi
    if bash -n "${f}" 2>/dev/null; then ok "语法正确: ${f#"${REPO_ROOT}/"}"; else bad "语法错误: ${f#"${REPO_ROOT}/"}"; fi
done

code_of() { grep -v '^[[:space:]]*#' "$1"; }
if code_of "${LOCAL_ENTRY}" | grep -qE 'scp[^_a-z]|docker cp|docker exec .*sh -c'; then
    bad "本地入口不得使用 scp / docker cp / 远端临时 shell"
else
    ok "本地入口未使用 scp / docker cp / 远端临时 shell"
fi
if code_of "${LOCAL_ENTRY}" | grep -q 'panji-prod-ssh'; then ok "本地入口使用唯一 SSH 入口 panji-prod-ssh"; else bad "本地入口未绑定 panji-prod-ssh"; fi
if code_of "${SERVER_SCRIPT}" | grep -qE 'active_release\.json'; then ok "服务器端显式校验不创建 active_release.json"; else bad "服务器端缺少 active_release.json 防护"; fi
if code_of "${SERVER_SCRIPT}" | grep -qE 'ALLOWED_LEAF="'; then ok "服务器端硬编码 LEAF 白名单（非通用上传工具）"; else bad "服务器端缺少 LEAF 白名单"; fi
if code_of "${SERVER_SCRIPT}" | grep -qE 'rm -rf -- "\$\{(ASSET_ROOT|TARGET_DIR)'; then bad "服务器端存在对资产根的 rm -rf"; else ok "服务器端不 rm -rf 既有资产根/目标目录"; fi

# 本地入口与服务器端的文件清单必须一致
ENTRY_FILES="$(code_of "${LOCAL_ENTRY}" | sed -n 's/^ASSET_FILES=(\(.*\))$/\1/p' | head -1)"
MISSING_IN_ENTRY=""
for name in "${DATA_FILES[@]}"; do
    echo "${ENTRY_FILES}" | grep -q "${name}" || MISSING_IN_ENTRY="${MISSING_IN_ENTRY} ${name}"
done
if [[ -z "${MISSING_IN_ENTRY}" ]]; then ok "本地入口资产清单与服务器端 DATA_FILES 一致"; else bad "本地入口缺少:${MISSING_IN_ENTRY}"; fi
if [[ -f "${REPO_ROOT}/deploy/assets/strategy-history/${LEAF}/asset_index.json" ]]; then
    ok "仓库已登记资产索引: deploy/assets/strategy-history/${LEAF}/asset_index.json"
else
    bad "仓库缺少资产索引"
fi

# ---------------------------------------------------------------------------
echo "== 2/4 正常发布 + 幂等 =="
CASE1="${TMP_ROOT}/case1"
REPO1="${CASE1}/repo"; ROOT1="${CASE1}/asset"
mkdir -p "${CASE1}"
SHA1="$(make_repo "${REPO1}")"
run_server "${REPO1}" "${ROOT1}" "${SHA1}"
assert_ok "首次发布成功" "${RC}"
TARGET1="${ROOT1}/release/${LEAF}"
for name in "${DATA_FILES[@]}"; do
    if [[ -f "${TARGET1}/${name}" ]]; then ok "落盘 ${name}"; else bad "未落盘 ${name}"; fi
done
IDX_SHA="$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['sha256']['release/${LEAF}/candidate_snapshot.json'])" \
    "${REPO_ROOT}/deploy/assets/strategy-history/${LEAF}/asset_index.json")"
assert_eq "落盘 candidate_snapshot.json SHA 与索引一致" "${IDX_SHA}" "$(sha_of "${TARGET1}/candidate_snapshot.json")"
if [[ ! -e "${ROOT1}/active_release.json" ]]; then ok "未创建 active_release.json"; else bad "创建了 active_release.json"; fi
if [[ ! -d "${ROOT1}/.staging" ]] || [[ -z "$(ls -A "${ROOT1}/.staging" 2>/dev/null)" ]]; then
    ok "暂存目录无残留"
else
    bad "暂存目录残留: $(ls -A "${ROOT1}/.staging")"
fi

run_server "${REPO1}" "${ROOT1}" "${SHA1}"
assert_ok "重复发布幂等成功" "${RC}"
if echo "${OUT}" | grep -q "幂等成功"; then ok "重复发布输出幂等标记"; else bad "重复发布缺少幂等标记"; fi
assert_eq "幂等后 SHA 不变" "${IDX_SHA}" "$(sha_of "${TARGET1}/candidate_snapshot.json")"

# ---------------------------------------------------------------------------
echo "== 3/4 拒绝路径（fail-closed）=="

# dry-run 零 mutation（目标不存在）
CASE2="${TMP_ROOT}/case2"; mkdir -p "${CASE2}"
REPO2="${CASE2}/repo"; ROOT2="${CASE2}/asset"
SHA2="$(make_repo "${REPO2}")"
run_server "${REPO2}" "${ROOT2}" "${SHA2}" --dry-run
assert_ok "dry-run 成功" "${RC}"
if echo "${OUT}" | grep -q '"status": "PLAN_ONLY"'; then ok "dry-run 输出 PLAN_ONLY"; else bad "dry-run 未输出 PLAN_ONLY"; fi
if [[ -e "${ROOT2}" ]]; then bad "dry-run 产生了 mutation（创建了资产根）"; else ok "dry-run 零 mutation"; fi

# 冲突拒绝 + 部分失败暂存清理：篡改已发布资产后再次发布
chmod u+w "${TARGET1}/candidate_snapshot.json"
printf '%s' '{"tampered":true}' > "${TARGET1}/candidate_snapshot.json"
chmod 0444 "${TARGET1}/candidate_snapshot.json"
run_server "${REPO1}" "${ROOT1}" "${SHA1}"
assert_fail "已存在资产内容不一致 → 拒绝" "${RC}" "ASSET_CONFLICT_REFUSE_OVERWRITE"
assert_eq "冲突后既有资产未被覆盖" '{"tampered":true}' "$(cat "${TARGET1}/candidate_snapshot.json")"
if [[ ! -d "${ROOT1}/.staging" ]] || [[ -z "$(ls -A "${ROOT1}/.staging" 2>/dev/null)" ]]; then
    ok "部分失败后暂存已清理"
else
    bad "部分失败后暂存残留: $(ls -A "${ROOT1}/.staging")"
fi

# dry-run 遇到冲突也必须失败
run_server "${REPO1}" "${ROOT1}" "${SHA1}" --dry-run
assert_fail "dry-run 遇冲突 fail-closed" "${RC}" "ASSET_CONFLICT_REFUSE_OVERWRITE"

# 额外文件
CASE3="${TMP_ROOT}/case3"; mkdir -p "${CASE3}"
REPO3="${CASE3}/repo"; ROOT3="${CASE3}/asset"
SHA3="$(make_repo "${REPO3}")"
printf '{"extra":1}\n' > "${REPO3}/deploy/assets/strategy-history/${LEAF}/extra.json"
run_server "${REPO3}" "${ROOT3}" "${SHA3}"
assert_fail "源目录额外文件 → 拒绝" "${RC}" "EXTRA_ASSET_FILE"
if [[ -e "${ROOT3}" ]]; then bad "额外文件场景产生了资产写入"; else ok "额外文件场景零写入"; fi

# 软链接
CASE4="${TMP_ROOT}/case4"; mkdir -p "${CASE4}"
REPO4="${CASE4}/repo"; ROOT4="${CASE4}/asset"
SHA4="$(make_repo "${REPO4}")"
SRC4="${REPO4}/deploy/assets/strategy-history/${LEAF}"
OUTSIDE="${CASE4}/outside.json"; printf '{"outside":1}\n' > "${OUTSIDE}"
rm -f "${SRC4}/candidate_snapshot.json"
ln -s "${OUTSIDE}" "${SRC4}/candidate_snapshot.json"
run_server "${REPO4}" "${ROOT4}" "${SHA4}"
assert_fail "源资产为软链接 → 拒绝" "${RC}" "NON_REGULAR_ASSET_FILE"

# SHA 不符（同长度篡改）
CASE5="${TMP_ROOT}/case5"; mkdir -p "${CASE5}"
REPO5="${CASE5}/repo"; ROOT5="${CASE5}/asset"
SHA5="$(make_repo "${REPO5}")"
python3 - "${REPO5}/deploy/assets/strategy-history/${LEAF}/candidate_shard_0000.json" <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1])
b = p.read_bytes()
p.write_bytes(b[:100] + b'X' + b[101:])
PY
run_server "${REPO5}" "${ROOT5}" "${SHA5}"
assert_fail "SHA 与索引不符 → 拒绝" "${RC}" "SHA_MISMATCH"

# 大小不符
CASE6="${TMP_ROOT}/case6"; mkdir -p "${CASE6}"
REPO6="${CASE6}/repo"; ROOT6="${CASE6}/asset"
SHA6="$(make_repo "${REPO6}")"
printf 'x' >> "${REPO6}/deploy/assets/strategy-history/${LEAF}/evaluation_snapshot.json"
run_server "${REPO6}" "${ROOT6}" "${SHA6}"
assert_fail "文件大小与索引登记不符 → 拒绝" "${RC}" "SIZE_MISMATCH"

# 审核状态
CASE7="${TMP_ROOT}/case7"; mkdir -p "${CASE7}"
REPO7="${CASE7}/repo"; ROOT7="${CASE7}/asset"
SHA7="$(make_repo "${REPO7}")"
python3 - "${REPO7}/deploy/assets/strategy-history/${LEAF}/asset_index.json" <<'PY'
import json, sys, pathlib
p = pathlib.Path(sys.argv[1]); d = json.loads(p.read_text())
d['source_status'] = 'STAGING'
p.write_text(json.dumps(d, indent=2, ensure_ascii=False) + '\n')
PY
run_server "${REPO7}" "${ROOT7}" "${SHA7}"
assert_fail "source_status 非 AUDITED → 拒绝" "${RC}" "SOURCE_STATUS_INVALID"

# 计数不符
CASE8="${TMP_ROOT}/case8"; mkdir -p "${CASE8}"
REPO8="${CASE8}/repo"; ROOT8="${CASE8}/asset"
SHA8="$(make_repo "${REPO8}")"
python3 - "${REPO8}/deploy/assets/strategy-history/${LEAF}/asset_index.json" <<'PY'
import json, sys, pathlib
p = pathlib.Path(sys.argv[1]); d = json.loads(p.read_text())
d['stock_count'] = 13
p.write_text(json.dumps(d, indent=2, ensure_ascii=False) + '\n')
PY
run_server "${REPO8}" "${ROOT8}" "${SHA8}"
assert_fail "stock_count 与 14 不符 → 拒绝" "${RC}" "STOCK_COUNT_MISMATCH"

# 索引额外键（路径穿越注入）
CASE9="${TMP_ROOT}/case9"; mkdir -p "${CASE9}"
REPO9="${CASE9}/repo"; ROOT9="${CASE9}/asset"
SHA9="$(make_repo "${REPO9}")"
python3 - "${REPO9}/deploy/assets/strategy-history/${LEAF}/asset_index.json" <<'PY'
import json, sys, pathlib
p = pathlib.Path(sys.argv[1]); d = json.loads(p.read_text())
d['sha256']['../../evil'] = '0' * 64
d['size_bytes']['../../evil'] = 1
p.write_text(json.dumps(d, indent=2, ensure_ascii=False) + '\n')
PY
run_server "${REPO9}" "${ROOT9}" "${SHA9}"
assert_fail "索引含穿越键 → 拒绝" "${RC}" "INDEX_EXTRA_KEYS"
if [[ -e "${CASE9}/evil" ]]; then bad "产生了路径穿越写入"; else ok "无路径穿越写入"; fi

# LEAF 白名单：目录改名后不得发布
CASE10="${TMP_ROOT}/case10"; mkdir -p "${CASE10}"
REPO10="${CASE10}/repo"; ROOT10="${CASE10}/asset"
SHA10="$(make_repo "${REPO10}")"
mv "${REPO10}/deploy/assets/strategy-history/${LEAF}" "${REPO10}/deploy/assets/strategy-history/2099-01-01_deadbeefdeadbeef"
run_server "${REPO10}" "${ROOT10}" "${SHA10}"
assert_fail "资产目录不在 LEAF 白名单 → 拒绝" "${RC}" "ASSET_SRC_MISSING"

# ---------------------------------------------------------------------------
echo "== 4/4 参数与工作树校验 =="
run_server "${REPO1}" "${ROOT1}" "abc123"
assert_fail "非 40 位 SHA → 拒绝" "${RC}" "BAD_SHA"

CASE11="${TMP_ROOT}/case11"; mkdir -p "${CASE11}"
REPO11="${CASE11}/repo"; ROOT11="${CASE11}/asset"
SHA11="$(make_repo "${REPO11}")"
printf 'later\n' > "${REPO11}/later.txt"
( cd "${REPO11}" && git add -A && git -c user.email=t@e.invalid -c user.name=t commit -q -m later ) >/dev/null 2>&1
run_server "${REPO11}" "${ROOT11}" "${SHA11}"
assert_fail "工作树 HEAD 与目标 SHA 不一致 → 拒绝" "${RC}" "WORKTREE_SHA_MISMATCH"

# 本地入口参数校验（不触碰 SSH）
set +e
"${LOCAL_ENTRY}" >/dev/null 2>&1; LOCAL_RC=$?
"${LOCAL_ENTRY}" abc123 >/dev/null 2>&1; LOCAL_RC2=$?
"${LOCAL_ENTRY}" "${SHA1}" --bogus >/dev/null 2>&1; LOCAL_RC3=$?
set -e
if [[ "${LOCAL_RC}" -ne 0 ]]; then ok "本地入口缺少参数 → 拒绝"; else bad "本地入口缺少参数未拒绝"; fi
if [[ "${LOCAL_RC2}" -ne 0 ]]; then ok "本地入口非 40 位 SHA → 拒绝"; else bad "本地入口非 40 位 SHA 未拒绝"; fi
if [[ "${LOCAL_RC3}" -ne 0 ]]; then ok "本地入口未知选项 → 拒绝"; else bad "本地入口未知选项未拒绝"; fi

echo "=== 结果: PASS=${PASS} FAIL=${FAIL} ==="
if [[ "${FAIL}" -eq 0 ]]; then echo "全部通过"; exit 0; fi
echo "存在失败项" >&2
exit 1
