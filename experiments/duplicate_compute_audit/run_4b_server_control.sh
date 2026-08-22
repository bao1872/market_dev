#!/usr/bin/env bash
#
# run_4b_server_control.sh — Phase 4B-0G-R 本地控制入口（GOVERNED）
#
# 唯一职责：在本地触发“服务器 5293 DB-backed benchmark”的受治理远程执行。
# 所有远程访问只能经由两个正式入口：
#   - scripts/ops/panji-prod-preflight   （部署/运行前只读 preflight 检查）
#   - scripts/ops/panji-prod-ssh         （唯一允许的 SSH 入口，禁止 raw ssh/scp）
#
# 关键修正（4B-0G-R3 / 方案 C）：服务器 /root/web_dev 保持真实部署 HEAD（ecc2388），
# 没有 remote runner 文件。control 包含已登记的最小 bootstrap：
#   经 panji-prod-ssh 在服务器上：
#     cd /root/web_dev
#     git fetch origin dev
#     校验 HARNESS_SHA / TARGET_CODE_SHA（40 位 hex）+ 祖先校验
#     从 HARNESS_SHA exact Git object materialize run_4b_server_remote.sh
#     bash 该 exact blob，传入 DEPLOYED_RUNTIME_SHA + TARGET_CODE_SHA
# 不 checkout、不 scp、不临时手工流程。
# 身份模型（方案 C）：deployed runtime（ecc2388）与 benchmark target code（ac9c3810）是两个
# 独立概念；benchmark one-shot 用 exact Git object 的 ac9c app 作为 /app/app，隔离不部署。
#
# 本脚本本身：
#   - 不连 bz_stock
#   - 不部署
#   - 不修改 /root/web_dev 或 /opt/panji-live
#   - 不创建测试数据库
#   - 不启动 scheduler
#   - 不写任何生产数据
#   - 不 scp（evidence 经 panji-prod-ssh 流式 cat 取回）
#
# 用法：
#   PANJI_BENCHMARK_HARNESS_SHA=<sha> ./run_4b_server_control.sh
#   ./run_4b_server_control.sh --dry     # 仅本地 dry smoke（不 SSH、不连服务器）
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
OPS_DIR="$REPO_ROOT/scripts/ops"

HARNESS_SHA="${PANJI_BENCHMARK_HARNESS_SHA:-${1:-}}"
# 方案 C 身份拆分：
#   DEPLOYED_RUNTIME_SHA —— 服务器当前真实部署/运行身份（默认 ecc2388）
#   TARGET_CODE_SHA     —— 本轮被测应用代码 SHA（默认 ac9c3810，隔离 one-shot 的 /app/app）
DEPLOYED_RUNTIME_SHA="${PANJI_DEPLOYED_RUNTIME_SHA:-ecc2388ef736a42f89d9d2a4b1b74907cc806253}"
TARGET_CODE_SHA="${PANJI_TARGET_CODE_SHA:-ac9c3810b63f64e702b0d60f7e7822112ab137fb}"

# 本地 double-check SHA 格式（与 remote 一致）
if [ "${1:-}" = "--dry" ]; then
  echo "[4B-0G-R3][control] DRY MODE：不 SSH、不连服务器。"
  echo "[4B-0G-R3][control] 预期 bootstrap：经 panji-prod-ssh 在服务器 materialize remote runner (HARNESS_SHA)"
  echo "[4B-0G-R3][control] 预期 deployed runtime SHA：$DEPLOYED_RUNTIME_SHA"
  echo "[4B-0G-R3][control] 预期 target code SHA   ：$TARGET_CODE_SHA"
  echo "[4B-0G-R3][control] dry smoke 通过（control-flow 仅本地校验）。"
  exit 0
fi

if [ -z "$HARNESS_SHA" ]; then
  echo "[4B-0G-R3][control] ERROR: 未提供 HARNESS_SHA。" >&2
  echo "  通过环境变量 PANJI_BENCHMARK_HARNESS_SHA 或首个参数传入 harness commit SHA。" >&2
  exit 2
fi
if ! [[ "$HARNESS_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "[4B-0G-R3][control] ERROR: HARNESS_SHA 非 40 位 hex: '$HARNESS_SHA'" >&2
  exit 2
fi
if ! [[ "$DEPLOYED_RUNTIME_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "[4B-0G-R3][control] ERROR: DEPLOYED_RUNTIME_SHA 非 40 位 hex" >&2
  exit 2
fi
if ! [[ "$TARGET_CODE_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "[4B-0G-R3][control] ERROR: TARGET_CODE_SHA 非 40 位 hex" >&2
  exit 2
fi

# 1) preflight（只读检查，不部署）
echo "[4B-0G-R3][control] 运行 preflight ..."
"$OPS_DIR/panji-prod-preflight" || {
  echo "[4B-0G-R3][control] preflight 失败，停止。" >&2
  exit 1
}

# 2) 经 panji-prod-ssh 在服务器 bootstrap：从 exact Git object materialize remote runner 并执行
#    不调用不存在的文件路径，不 scp，不 checkout。
echo "[4B-0G-R3][control] 经 panji-prod-ssh 在服务器 bootstrap + 执行 governed remote runner ..."
BOOTSTRAP="set -u; cd /root/web_dev; \
git fetch origin dev >/dev/null 2>&1 || true; \
HS='$HARNESS_SHA'; DRS='$DEPLOYED_RUNTIME_SHA'; TCS='$TARGET_CODE_SHA'; \
if ! git merge-base --is-ancestor \"\$HS\" origin/dev 2>/dev/null; then echo '[control] ERROR: HARNESS_SHA 非 origin/dev 祖先' >&2; exit 4; fi; \
if ! git merge-base --is-ancestor \"\$TCS\" origin/dev 2>/dev/null; then echo '[control] ERROR: TARGET_CODE_SHA 非 origin/dev 祖先' >&2; exit 4; fi; \
if ! git cat-file -e \"\$HS^{commit}\" 2>/dev/null; then echo '[control] ERROR: HARNESS_SHA object 不存在' >&2; exit 4; fi; \
if ! git cat-file -e \"\$TCS^{commit}\" 2>/dev/null; then echo '[control] ERROR: TARGET_CODE_SHA object 不存在' >&2; exit 4; fi; \
TMP=\$(mktemp /tmp/4b-remote.XXXXXX.sh); \
git cat-file -p \"\$HS:experiments/duplicate_compute_audit/run_4b_server_remote.sh\" > \"\$TMP\"; \
chmod +x \"\$TMP\"; \
set +e; \
bash \"\$TMP\" \"\$HS\" \"\$DRS\" \"\$TCS\"; RC=\$?; \
set -e; \
rm -f \"\$TMP\"; \
exit \$RC"

# Blocker 3: 显式捕获 remote RC；set -e 不跳过取回逻辑
set +e
"$OPS_DIR/panji-prod-ssh" "$BOOTSTRAP"
REMOTE_RC=$?
set -e

echo "[4B-0G-R3][control] remote runner 退出码 = $REMOTE_RC"

# 3) 取回 evidence archive（经 SSH 流式 cat，不 scp）
# 无论 REMOTE_RC 成功/失败，只要远端 archive 存在都尝试取回（失败场景更需要 evidence）。
# R3F1: 按 HARNESS_SHA 隔离本地 evidence，避免不同 run 共用同一目录导致历史残留被误读为本轮证据。
LOCAL_EVIDENCE_DIR="$SCRIPT_DIR/output/4B-server-remote-evidence/$HARNESS_SHA"
ARCHIVE_NAME="4b-evidence-${HARNESS_SHA}.tar.gz"
echo "[4B-0G-R3][control] 经 panji-prod-ssh 取回 evidence archive（若存在）..."
if "$OPS_DIR/panji-prod-ssh" "test -f /tmp/${ARCHIVE_NAME}" 2>/dev/null; then
  # 取回前校验：目标目录必须不存在或为空，防止与任何历史 run 的 evidence 混淆。
  if [ -e "$LOCAL_EVIDENCE_DIR" ] && [ -n "$(ls -A "$LOCAL_EVIDENCE_DIR" 2>/dev/null)" ]; then
    echo "[4B-0G-R3][control] ERROR: 本地 evidence 目录 $LOCAL_EVIDENCE_DIR 已存在且非空，" \
         "疑似历史残留，拒绝覆盖（STOP before extract）。" >&2
    exit 1
  fi
  mkdir -p "$LOCAL_EVIDENCE_DIR"
  if "$OPS_DIR/panji-prod-ssh" "cat /tmp/${ARCHIVE_NAME}" > "$LOCAL_EVIDENCE_DIR/${ARCHIVE_NAME}" 2>/dev/null; then
    tar -xzf "$LOCAL_EVIDENCE_DIR/${ARCHIVE_NAME}" -C "$LOCAL_EVIDENCE_DIR" 2>/dev/null || {
      echo "[4B-0G-R3][control] WARN: archive 解包失败" >&2
    }
    # 取回成功后精确删除远端 archive
    "$OPS_DIR/panji-prod-ssh" "rm -f /tmp/${ARCHIVE_NAME}" >/dev/null 2>&1 || true
    echo "[4B-0G-R3][control] evidence 已取回至 $LOCAL_EVIDENCE_DIR，远端 archive 已删除。"
  else
    echo "[4B-0G-R3][control] WARN: 取回 archive 内容失败。" >&2
  fi
else
  echo "[4B-0G-R3][control] WARN: 远端 archive 不存在（remote runner 未生成或已清理）。" >&2
fi

exit "$REMOTE_RC"
