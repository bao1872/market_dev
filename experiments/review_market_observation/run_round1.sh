#!/usr/bin/env bash
# ============================================================================
#  run_round1.sh — Round 1 Frozen Dataset Extraction + Audit + Public Summary
#
#  §2.1 执行入口要求：
#    - 使用 python -m module 方式（不直接 python x/y/z/round1_extract.py）
#    - 显式传入 DEV_BASE_SHA / EXP_SHA / DATABASE_URL
#    - EXP_SHA 在提取器内部再次执行 git rev-parse HEAD 校验；不一致 STOP
#
#  子命令 (positional $1)：
#    diagnose      → --diagnose-recent=30 打印最近 30 交易日完整度诊断
#    extract       → 需 END_DATE=<YYYY-MM-DD> 执行 frozen dataset 提取
#    analyze       → 读 data/ frozen dataset → audit/ JSON + public summary
#    all           → diagnose（可选）+ extract + analyze + write-public
#
#  Safety:
#    - data/ 目录存在 frozen 会 STOP（防止 overwrite）
#    - 所有 DB 事务强制 SET TRANSACTION READ ONLY + SHOW transaction_read_only=on
#    - 不向 /root/web_dev 写任何东西；输出到 $EXP_OUTPUT_ROOT (默认 /root/.panji-experiments/...)
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# --- Required baseline ------------------------------------------------------
DEV_BASE_SHA="${DEV_BASE_SHA:-6fc7384228b2e51f13d3cf5af2a6b6a26b2837b0}"

# EXP_SHA：优先环境变量；否则 git rev-parse（远程 worktree/本地 worktree 均可）
EXP_SHA="${EXP_SHA:-$(git -C "$SCRIPT_DIR" rev-parse HEAD 2>/dev/null || true)}"
if [[ -z "$EXP_SHA" || ${#EXP_SHA} -ne 40 ]]; then
    echo "ERROR: EXP_SHA 无法从环境变量或 git HEAD 解析（需要 40 位）。" >&2
    echo "       export EXP_SHA=<preexec-sha> 再运行。" >&2
    exit 2
fi

# --- Subcommand & positional ------------------------------------------------
SUB_CMD="${1:-all}"
END_DATE="${END_DATE:-}"

# --- DATABASE_URL -----------------------------------------------------------
if [[ -f "$HOME/.panji/experiment.env" ]]; then
    # shellcheck disable=SC1091
    set -a; source "$HOME/.panji/experiment.env"; set +a
fi
if [[ -z "${DATABASE_URL:-}" ]]; then
    echo "ERROR: DATABASE_URL 未设置（env 或 ~/.panji/experiment.env）" >&2
    exit 2
fi

# --- Output roots (§12. §9) -------------------------------------------------
# EXP_OUTPUT_ROOT: 大型 frozen/audit 放独立目录（不 Git）
EXP_OUTPUT_ROOT="${EXP_OUTPUT_ROOT:-${SCRIPT_DIR}/_run}"
RUN_DIR="${EXP_OUTPUT_ROOT}/r1/${EXP_SHA}"
DATA_DIR="${RUN_DIR}/data"
AUDIT_DIR="${RUN_DIR}/audit"
# PUBLIC_DIR: 小型脱敏证据（直接写到 experiment 根，便于 git add）
PUBLIC_DIR="${SCRIPT_DIR}"

mkdir -p "${EXP_OUTPUT_ROOT}" "${RUN_DIR}"

echo "============================================================="
echo "  Round 1 Review-Market-Observation"
echo "  DEV_BASE_SHA  = ${DEV_BASE_SHA}"
echo "  EXP_SHA       = ${EXP_SHA}"
echo "  sub-command   = ${SUB_CMD}"
echo "  RUN_DIR       = ${RUN_DIR}"
echo "  PUBLIC_DIR    = ${PUBLIC_DIR}"
echo "============================================================="

# --- Run from module root ---------------------------------------------------
# Package root = <worktree>/experiments; PYTHONPATH = <worktree>
# 这样 `python -m experiments.review_market_observation.round1.X` 即可解析
WORKTREE_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"  # <worktree> (parent of experiments/)
export PYTHONPATH="${WORKTREE_ROOT}:${PYTHONPATH:-}"

cmd_diagnose() {
    mkdir -p "${RUN_DIR}"
    echo "==> [Round 1 / Step 0-Diagnose] 最近 30 交易日完整度诊断 ..."
    python -m experiments.review_market_observation.round1.round1_extract \
        --data-dir "${RUN_DIR}" \
        --dev-base-sha "${DEV_BASE_SHA}" \
        --exp-sha "${EXP_SHA}" \
        --diagnose-recent 30
    echo "==> Diagnose 完成。选择 complete 的 END_DATE 后再执行："
    echo "    END_DATE=YYYY-MM-DD bash run_round1.sh extract"
}

cmd_extract() {
    if [[ -z "$END_DATE" ]]; then
        echo "ERROR: extract 需要 END_DATE=YYYY-MM-DD（§5 fail-closed，不得默认 max）。" >&2
        exit 2
    fi
    echo "==> [Round 1 / Step 1] Extract frozen dataset (readonly; end=$END_DATE) ..."
    python -m experiments.review_market_observation.round1.round1_extract \
        --data-dir "${DATA_DIR}" \
        --dev-base-sha "${DEV_BASE_SHA}" \
        --exp-sha "${EXP_SHA}" \
        --end-date "${END_DATE}"
}

cmd_analyze() {
    if [[ ! -f "${DATA_DIR}/frozen_dataset.parquet" ]]; then
        echo "ERROR: ${DATA_DIR}/frozen_dataset.parquet 不存在。请先 extract。" >&2
        exit 2
    fi
    echo "==> [Round 1 / Step 2] Integrity + primitive + transition audit ..."
    python -m experiments.review_market_observation.round1.round1_analyze \
        --data-dir "${DATA_DIR}" \
        --audit-dir "${AUDIT_DIR}" \
        --write-public \
        --public-dir "${PUBLIC_DIR}"
}

case "$SUB_CMD" in
    diagnose)
        cmd_diagnose ;;
    extract)
        cmd_extract ;;
    analyze)
        cmd_analyze ;;
    all)
        if [[ -n "$END_DATE" ]]; then
            cmd_extract
            cmd_analyze
        else
            cmd_diagnose
            echo
            echo "[all] END_DATE 未提供：只做了 diagnose（§5 fail-closed）。选择 END_DATE 后再执行 bash run_round1.sh all。"
        fi
        ;;
    *)
        echo "Unknown sub-command: $SUB_CMD. Use {diagnose|extract|analyze|all}." >&2
        exit 2
        ;;
esac

echo
echo "Done. RUN_DIR=${RUN_DIR}"
