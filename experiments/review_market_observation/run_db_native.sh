#!/usr/bin/env bash
# Round 1 DB-native pipeline shell wrapper（v2 架构，不生成 parquet / full DataFrame）。
#
# 子命令（按执行顺序）：
#   resource-preflight  §6：只收集内存 + systemd 能力信息 + MEM_AVAIL <3GiB→exit3（可选 --smoke）。
#   db-native-dry-run   §8：round1_db_native.py --dry-run（不连 DB；参数 + query-shape 校验）。
#   db-native-run       §8–§14：真实执行（caller 负责 export DATABASE_URL / DSN_HOST_FROM / DSN_HOST_TO）。
#   all                 =  resource-preflight --smoke + db-native-dry-run（remote true run 不自动执行）。
#
# 环境变量（真实远程调用时提供）：
#   DEV_BASE_SHA            必须 = 6fc7384228b2e51f13d3cf5af2a6b6a26b2837b0（§8 已写死）
#   EXP_SHA                 当前实验 commit（=git rev-parse HEAD；脚本会比较）
#   END_DATE                YYYY-MM-DD（§8 fail-closed：不允许猜测最新交易日）
#   DATABASE_URL            PostgreSQL DSN（可带 sqlalchemy 风格 scheme，内部 normalize）
#   DSN_HOST_FROM / DSN_HOST_TO   如 container hostname 需要换容器 IP
#   REMOTE_REQUIRE_SYSTEMD_RUN=1  远程真实运行时 systemd-run 不可用 → STOP（exit 4）
#
# 本脚本不把默认模式设为 old full-extract；old 路径需要显式 `bash run_round1.sh extract` 才会执行。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKTREE_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
export PYTHONPATH="${WORKTREE_ROOT}:${PYTHONPATH:-}"

DEV_BASE_SHA_DEFAULT="6fc7384228b2e51f13d3cf5af2a6b6a26b2837b0"
DEV_BASE_SHA="${DEV_BASE_SHA:-$DEV_BASE_SHA_DEFAULT}"

# 若 EXP_SHA 未显式提供：使用 git rev-parse HEAD（worktree root）
if [ -z "${EXP_SHA:-}" ]; then
  EXP_SHA="$(git -C "$WORKTREE_ROOT" rev-parse HEAD)"
  echo "[run_db_native] EXP_SHA 未显式提供；取 git HEAD = $EXP_SHA"
fi

OUT_DIR="${OUT_DIR:-$WORKTREE_ROOT/experiments/review_market_observation/out/round1_db_native}"
mkdir -p "$OUT_DIR"

usage() {
  cat >&2 <<EOF
Usage: $0 <subcommand> [options]
Subcommands:
  resource-preflight [--smoke]   §6 resource gate（默认不启动 scope；--smoke 额外 smoke systemd-run）
  db-native-dry-run              §8 dry-run（不连 DB；参数 + query shape 断言）
  db-native-run                  §8–§14 real execution（需 DATABASE_URL / END_DATE）
  all                            §20 default: resource-preflight --smoke + db-native-dry-run
EOF
}

run_resource_preflight() {
  local extra=()
  if [ "${1:-}" = "--smoke" ]; then
    extra=(--smoke-test)
  fi
  # 实际命令留空 -- 等待 true 替换（smoke 模式下 resource_runner 自动替换为 true 内置命令）
  bash "$SCRIPT_DIR/resource_runner.sh" "${extra[@]}" -- true
}

run_db_native_dry_run() {
  python3 -m experiments.review_market_observation.round1.round1_db_native \
    --out-dir "$OUT_DIR/dry_run_$$" \
    --dev-base-sha "$DEV_BASE_SHA" \
    --exp-sha "$EXP_SHA" \
    --dry-run
}

run_db_native_run() {
  if [ -z "${END_DATE:-}" ]; then
    echo "ERROR: END_DATE 环境变量必须显式设置（§8 fail-closed，禁止默认最新）。" >&2
    exit 2
  fi
  if [ -z "${DATABASE_URL:-}" ]; then
    echo "ERROR: DATABASE_URL 环境变量必须显式设置。" >&2
    exit 2
  fi
  # 真实执行：要求 systemd-run 资源围栏（REMOTE_REQUIRE_SYSTEMD_RUN=1）
  echo "[run_db_native] real run：via resource_runner.sh（MemoryHigh=3G MemoryMax=4G Nice=10）"
  REMOTE_REQUIRE_SYSTEMD_RUN=1 \
  bash "$SCRIPT_DIR/resource_runner.sh" -- \
    python3 -m experiments.review_market_observation.round1.round1_db_native \
      --out-dir "$OUT_DIR" \
      --dev-base-sha "$DEV_BASE_SHA" \
      --exp-sha "$EXP_SHA" \
      --end-date "$END_DATE"
}

case "${1:-}" in
  resource-preflight)
    shift
    run_resource_preflight "${1:-}"
    ;;
  db-native-dry-run)
    run_db_native_dry_run
    ;;
  db-native-run)
    run_db_native_run
    ;;
  all)
    echo "==> 1. resource-preflight --smoke"
    run_resource_preflight --smoke
    echo
    echo "==> 2. db-native-dry-run"
    run_db_native_dry_run
    ;;
  ""|-h|--help|help)
    usage
    exit 2
    ;;
  *)
    echo "Unknown subcommand: $1" >&2
    usage
    exit 2
    ;;
esac
