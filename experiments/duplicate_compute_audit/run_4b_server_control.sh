#!/usr/bin/env bash
#
# run_4b_server_control.sh — Phase 4B-0G 本地控制入口（GOVERNED）
#
# 唯一职责：在本地触发“服务器 5293 DB-backed benchmark”的受治理远程执行。
# 所有远程访问只能经由两个正式入口：
#   - scripts/ops/panji-prod-preflight   （部署/运行前只读 preflight 检查）
#   - scripts/ops/panji-prod-ssh         （唯一允许的 SSH 入口，禁止 raw ssh/scp）
#
# 本脚本本身：
#   - 不连 bz_stock
#   - 不部署
#   - 不修改 /root/web_dev 或 /opt/panji-live
#   - 不创建测试数据库
#   - 不启动 scheduler
#   - 不写任何生产数据
#
# 真正的执行流程全部封装在 run_4b_server_remote.sh（由 panji-prod-ssh 远端拉起）。
#
# 用法：
#   ./run_4b_server_control.sh            # 走 preflight + SSH 执行远端 runner
#   ./run_4b_server_control.sh --dry     # 仅本地 dry smoke（不 SSH、不连服务器）
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
OPS_DIR="$REPO_ROOT/scripts/ops"
REMOTE_SCRIPT="experiments/duplicate_compute_audit/run_4b_server_remote.sh"

# 运行参数（由本控制脚本传远端；绝不在本机拼 docker 命令）
HARNESS_SHA="${PANJI_BENCHMARK_HARNESS_SHA:-${1:-}}"
PROD_RUNTIME_SHA="${PANJI_PROD_RUNTIME_SHA:-ac9c3810b63f64e702b0d60f7e7822112ab137fb}"

if [ "${1:-}" = "--dry" ]; then
  echo "[4B-0G][control] DRY MODE：不 SSH、不连服务器。"
  echo "[4B-0G][control] 预期远端 runner：$REMOTE_SCRIPT"
  echo "[4B-0G][control] 预期 harness SHA 来源：\$PANJI_BENCHMARK_HARNESS_SHA"
  echo "[4B-0G][control] 预期 production runtime SHA：$PROD_RUNTIME_SHA"
  echo "[4B-0G][control] dry smoke 通过（control-flow 仅本地校验）。"
  exit 0
fi

if [ -z "$HARNESS_SHA" ]; then
  echo "[4B-0G][control] ERROR: 未提供 HARNESS_SHA。" >&2
  echo "  通过环境变量 PANJI_BENCHMARK_HARNESS_SHA 或首个参数传入 harness commit SHA。" >&2
  exit 2
fi

# 1) preflight（只读检查，不部署）
echo "[4B-0G][control] 运行 preflight ..."
"$OPS_DIR/panji-prod-preflight" || {
  echo "[4B-0G][control] preflight 失败，停止。" >&2
  exit 1
}

# 2) 仅经 panji-prod-ssh 拉起远端 runner；不在本机构造任何 docker / scp 命令
echo "[4B-0G][control] 经 panji-prod-ssh 启动受治理远端 runner ..."
"$OPS_DIR/panji-prod-ssh" \
  "bash $REMOTE_SCRIPT $HARNESS_SHA $PROD_RUNTIME_SHA"

echo "[4B-0G][control] 远端 runner 已退出；证据由远端写入本轮临时 workspace 并经 SSH 取回摘要。"
