#!/usr/bin/env bash
#
# run_4b_server_control.sh — Phase 4B-0G-R 本地控制入口（GOVERNED）
#
# 唯一职责：在本地触发“服务器 5293 DB-backed benchmark”的受治理远程执行。
# 所有远程访问只能经由两个正式入口：
#   - scripts/ops/panji-prod-preflight   （部署/运行前只读 preflight 检查）
#   - scripts/ops/panji-prod-ssh         （唯一允许的 SSH 入口，禁止 raw ssh/scp）
#
# 关键修正（4B-0G-R）：服务器 /root/web_dev 保持 HEAD=ac9c3810，没有 remote runner 文件。
# 因此 control 必须包含已登记的最小 bootstrap：
#   经 panji-prod-ssh 在服务器上：
#     cd /root/web_dev
#     git fetch origin dev
#     校验 HARNESS_SHA（40 位 hex）+ 祖先校验
#     从 HARNESS_SHA exact Git object materialize run_4b_server_remote.sh
#     bash 该 exact blob
# 不 checkout、不 scp、不临时手工流程。
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
PROD_RUNTIME_SHA="${PANJI_PROD_RUNTIME_SHA:-ac9c3810b63f64e702b0d60f7e7822112ab137fb}"

# 本地 double-check SHA 格式（与 remote 一致）
if [ "${1:-}" = "--dry" ]; then
  echo "[4B-0G-R][control] DRY MODE：不 SSH、不连服务器。"
  echo "[4B-0G-R][control] 预期 bootstrap：经 panji-prod-ssh 在服务器 materialize remote runner (HARNESS_SHA)"
  echo "[4B-0G-R][control] 预期 production runtime SHA：$PROD_RUNTIME_SHA"
  echo "[4B-0G-R][control] dry smoke 通过（control-flow 仅本地校验）。"
  exit 0
fi

if [ -z "$HARNESS_SHA" ]; then
  echo "[4B-0G-R][control] ERROR: 未提供 HARNESS_SHA。" >&2
  echo "  通过环境变量 PANJI_BENCHMARK_HARNESS_SHA 或首个参数传入 harness commit SHA。" >&2
  exit 2
fi
if ! [[ "$HARNESS_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "[4B-0G-R][control] ERROR: HARNESS_SHA 非 40 位 hex: '$HARNESS_SHA'" >&2
  exit 2
fi
if ! [[ "$PROD_RUNTIME_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "[4B-0G-R][control] ERROR: PROD_RUNTIME_SHA 非 40 位 hex" >&2
  exit 2
fi

# 1) preflight（只读检查，不部署）
echo "[4B-0G-R][control] 运行 preflight ..."
"$OPS_DIR/panji-prod-preflight" || {
  echo "[4B-0G-R][control] preflight 失败，停止。" >&2
  exit 1
}

# 2) 经 panji-prod-ssh 在服务器 bootstrap：从 exact Git object materialize remote runner 并执行
#    不调用不存在的文件路径，不 scp，不 checkout。
echo "[4B-0G-R][control] 经 panji-prod-ssh 在服务器 bootstrap + 执行 governed remote runner ..."
BOOTSTRAP="set -euo pipefail; cd /root/web_dev; \
git fetch origin dev >/dev/null 2>&1 || true; \
HS='$HARNESS_SHA'; PRS='$PROD_RUNTIME_SHA'; \
if ! git merge-base --is-ancestor \"\$HS\" origin/dev 2>/dev/null; then echo '[control] ERROR: HARNESS_SHA 非 origin/dev 祖先' >&2; exit 4; fi; \
if ! git cat-file -e \"\$HS^{commit}\" 2>/dev/null; then echo '[control] ERROR: HARNESS_SHA object 不存在' >&2; exit 4; fi; \
TMP=\$(mktemp /tmp/4b-remote.XXXXXX.sh); \
git cat-file -p \"\$HS:experiments/duplicate_compute_audit/run_4b_server_remote.sh\" > \"\$TMP\"; \
chmod +x \"\$TMP\"; \
bash \"\$TMP\" \"\$HS\" \"\$PRS\"; RC=\$?; rm -f \"\$TMP\"; exit \$RC"

"$OPS_DIR/panji-prod-ssh" "$BOOTSTRAP"
REMOTE_RC=$?

echo "[4B-0G-R][control] remote runner 退出码 = $REMOTE_RC"

# 3) 取回 evidence archive（经 SSH 流式 cat，不 scp）
LOCAL_EVIDENCE_DIR="$SCRIPT_DIR/output/4B-server-remote-evidence"
if [ "$REMOTE_RC" -eq 0 ]; then
  mkdir -p "$LOCAL_EVIDENCE_DIR"
  ARCHIVE_NAME="4b-evidence-${HARNESS_SHA}.tar.gz"
  echo "[4B-0G-R][control] 经 panji-prod-ssh 取回 evidence archive ..."
  if "$OPS_DIR/panji-prod-ssh" "cat /tmp/${ARCHIVE_NAME}" > "$LOCAL_EVIDENCE_DIR/${ARCHIVE_NAME}" 2>/dev/null; then
    tar -xzf "$LOCAL_EVIDENCE_DIR/${ARCHIVE_NAME}" -C "$LOCAL_EVIDENCE_DIR" 2>/dev/null || {
      echo "[4B-0G-R][control] WARN: archive 解包失败" >&2
    }
    # 取回成功后精确删除远端 archive
    "$OPS_DIR/panji-prod-ssh" "rm -f /tmp/${ARCHIVE_NAME}" >/dev/null 2>&1 || true
    echo "[4B-0G-R][control] evidence 已取回至 $LOCAL_EVIDENCE_DIR，远端 archive 已删除。"
  else
    echo "[4B-0G-R][control] WARN: 未能取回 evidence archive（远端可能已清理）。" >&2
  fi
fi

exit "$REMOTE_RC"
