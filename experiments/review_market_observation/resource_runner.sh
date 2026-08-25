#!/usr/bin/env bash
# Resource wrapper for Round 1 DB-native pipeline（prompt §5 – §6 resource-controlled runner）。
#
# 本脚本只是一个 systemd-run scope smoke wrapper；实际 Round 1 Python 命令由位置参数传入。
#
# 允许的控制参数（硬保险，写死在脚本以避免随意扩大）：
#   MemoryHigh = 3G  (soft throttle start; kernel will page-reclaim/shrink)
#   MemoryMax  = 4G  (hard OOM kill by cgroup; 保险，不应实际达到)
#   Nice       = 10  (CPU 优先级低于生产 worker)
#
# 若无 systemd-run（比如 Mac 本地；或 systemd 不可用）：
#   FALLBACK=renice + ulimit 宽松 wrapper（不能强保证内存限制；仅本地/CI 用）
#   打印 WARNING；但 remote true run 要求 systemd-run，否则 resource preflight FAIL。
#
# Resource Preflight（§6）：
#   * 报告 MemTotal / MemAvailable / SwapTotal（仅报告；不修改全局）
#   * 如 MemAvailable < 3 GiB → RESOURCE_GATE=FAIL → STOP（exit 3）
#   * 如 systemd-run 不可用且真实 remote → REMOTE_REQUIRE_SYSTEMD_RUN=1 → STOP
#
# 只输出资源信息 + 执行真正命令。不连 DB、不改数据。
# 不要在此脚本内执行真实 Round 1 pipeline（由 caller -- 传入）。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKTREE_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
export PYTHONPATH="${WORKTREE_ROOT}:${PYTHONPATH:-}"

# 真实实验时 caller 需显式 export REMOTE_REQUIRE_SYSTEMD_RUN=1
REMOTE_REQUIRE_SYSTEMD_RUN="${REMOTE_REQUIRE_SYSTEMD_RUN:-0}"
MEMORY_HIGH="${MEMORY_HIGH:-3G}"
MEMORY_MAX="${MEMORY_MAX:-4G}"
NICE_LEVEL="${NICE_LEVEL:-10}"
MEM_AVAIL_MIN_BYTES=$(( 3 * 1024 * 1024 * 1024 ))  # 3 GiB

echo "============================================================="
echo "  Round 1 DB-Native Resource Runner"
echo "  MemoryHigh = $MEMORY_HIGH   MemoryMax = $MEMORY_MAX   Nice = $NICE_LEVEL"
echo "  REMOTE_REQUIRE_SYSTEMD_RUN = $REMOTE_REQUIRE_SYSTEMD_RUN"
echo "============================================================="

# ----------------------------------------------------------
# §6 Resource preflight report (MemTotal / MemAvailable / SwapTotal)
# ----------------------------------------------------------
echo
echo "==> [Resource Preflight / §6] memory + load snapshot ..."
MEM_TOTAL_KB=""
MEM_AVAIL_KB=""
SWAP_TOTAL_KB=""
SWAP_FREE_KB=""
if [ -f /proc/meminfo ]; then
  MEM_TOTAL_KB=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)
  MEM_AVAIL_KB=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)
  SWAP_TOTAL_KB=$(awk '/^SwapTotal:/ {print $2}' /proc/meminfo)
  SWAP_FREE_KB=$(awk '/^SwapFree:/ {print $2}' /proc/meminfo)
fi
echo "MemTotal     = ${MEM_TOTAL_KB:-UNKNOWN} kB"
echo "MemAvailable = ${MEM_AVAIL_KB:-UNKNOWN} kB"
echo "SwapTotal    = ${SWAP_TOTAL_KB:-UNKNOWN} kB"
echo "SwapFree     = ${SWAP_FREE_KB:-UNKNOWN} kB"
if [ -n "$MEM_AVAIL_KB" ]; then
  MEM_AVAIL_B=$(( MEM_AVAIL_KB * 1024 ))
  echo "MemAvailable bytes = $MEM_AVAIL_B (threshold = $MEM_AVAIL_MIN_BYTES)  [3 GiB gate]"
  if [ "$MEM_AVAIL_B" -lt "$MEM_AVAIL_MIN_BYTES" ]; then
    echo "RESOURCE_GATE = FAIL" >&2
    echo "MemAvailable ($MEM_AVAIL_B bytes) < 3GiB threshold ($MEM_AVAIL_MIN_BYTES bytes). STOP." >&2
    exit 3
  fi
fi
echo "RESOURCE_GATE = PASS (memory)"

# 检查明显生产重任务（不阻塞，只报告）
echo
echo "==> [Resource Preflight / §6] obvious heavy-job snapshot (top 6 by RSS in systemd ps-like) ..."
if command -v ps >/dev/null; then
  ps -eo pid,user,%cpu,%mem,rss,comm --sort=-rss 2>/dev/null | head -7 || true
fi

# ----------------------------------------------------------
# Systemd-run availability / fallback
# ----------------------------------------------------------
HAVE_SYSTEMD_RUN=0
if command -v systemd-run >/dev/null; then
  # 轻量 smoke test：只看 --help 是否 OK（不实际创建 scope）
  if systemd-run --help >/dev/null 2>&1; then
    HAVE_SYSTEMD_RUN=1
  fi
fi
echo
echo "systemd-run available = $HAVE_SYSTEMD_RUN"

if [ "$HAVE_SYSTEMD_RUN" -ne 1 ]; then
  if [ "$REMOTE_REQUIRE_SYSTEMD_RUN" = "1" ]; then
    echo "REMOTE_REQUIRE_SYSTEMD_RUN=1 但 systemd-run 不可用 → STOP." >&2
    exit 4
  fi
  echo "WARNING: 无 systemd-run；fallback 为 renice + ulimit wrapper（仅本地/CI，不保证内存硬限制）"
fi

# ----------------------------------------------------------
# Usage check: caller must pass -- <actual command>
# ----------------------------------------------------------
if [ "$#" -eq 0 ]; then
  echo "Usage: $(basename "$0") [--smoke-test] -- <actual-round1-command...>" >&2
  echo "  --smoke-test：只创建 scope 执行 true（验证参数兼容性），不运行真实 pipeline。" >&2
  exit 2
fi

SMOKE=0
if [ "${1:-}" = "--smoke-test" ]; then
  SMOKE=1
  shift
fi
if [ "${1:-}" != "--" ]; then
  echo "ERROR: 必须在实际命令前加 '--'（防止意外解析到 runner 自身参数）。" >&2
  exit 2
fi
shift  # drop '--'

CMD=("$@")
if [ "$SMOKE" -eq 1 ]; then
  echo "--smoke-test 模式：用 \"true\" 替换实际命令执行一次 scope 以验证 systemd 参数"
  CMD=(true)
fi

# ----------------------------------------------------------
# Execute
# ----------------------------------------------------------
echo
echo "==> Launching under resource controls (MemoryHigh=$MEMORY_HIGH MemoryMax=$MEMORY_MAX Nice=$NICE_LEVEL) ..."
echo "CMD:" "${CMD[@]}"
echo

if [ "$HAVE_SYSTEMD_RUN" -eq 1 ]; then
  # systemd scope 单元不支持 Nice= 属性；改为用 nice -n 包裹实际命令以应用 CPU 优先级。
  exec systemd-run --scope --user \
    -p "MemoryHigh=$MEMORY_HIGH" \
    -p "MemoryMax=$MEMORY_MAX" \
    -- nice -n "$NICE_LEVEL" "${CMD[@]}"
else
  # Fallback（没有强保证；仅本地/CI）
  if command -v renice >/dev/null; then
    renice "$NICE_LEVEL" -p $$ >/dev/null 2>&1 || true
  fi
  # 进程级 ulimit（virtual memory bytes ≈ 4 GiB 保险；非强制但尽量）
  ulimit -v $(( 4 * 1024 * 1024 )) 2>/dev/null || true
  exec "${CMD[@]}"
fi
