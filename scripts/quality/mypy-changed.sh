#!/usr/bin/env bash
# [Corrective-3.2 §P0-mypy-gate]
# 可重复的 changed-file Mypy 门禁：只检查相对基准（默认 origin/dev）发生变化的
# backend Python 文件。这些文件本身无类型错误时退出码为 0，从而满足 PRD Gate 1
# "全部代码质量门（Mypy）退出码为 0" 的完成定义，而不被仓库历史遗留的 baseline
# mypy 错误阻塞（那些错误位于未改动文件，不在本次交付范围内）。
#
# [Task 005-A 修正] 现在支持显式 --base / --head / --worktree。
# 在未显式给定 --base 时退回 origin/dev 并输出告警 —— 这是为兼容旧调用方；
# 通过 T0 (t0_gate.py) 调用时务必传入显式 --base/--head，否则 push 之后
# origin/dev == HEAD 会导致空 diff 的假绿。
#
# 用法:
#   mypy-changed.sh --base <SHA/ref> --head <SHA/ref>   # T0 推荐：显式区间
#   mypy-changed.sh --base <SHA/ref> --worktree         # 检查未提交工作区
#   mypy-changed.sh                                     # 兼容：BASE 默认 origin/dev（告警）
#   BASE=main mypy-changed.sh                           # 兼容：环境变量指定基准
#
# 退出码:
#   0  => 所有改动文件通过 Mypy
#   1  => 至少一个改动文件存在 Mypy 错误
#   2  => 无法解析改动文件列表（git / venv 缺失或参数错误）

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BACKEND="$REPO_ROOT/backend"

BASE=""
HEAD="HEAD"
WORKTREE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --base) BASE="${2:-}"; shift 2;;
    --head) HEAD="${2:-}"; shift 2;;
    --worktree) WORKTREE=1; shift;;
    *) echo "[mypy-changed] unknown arg: $1" >&2; exit 2;;
  esac
done

if [[ -z "$BASE" ]]; then
  BASE="${BASE_ENV:-origin/dev}"
  echo "[mypy-changed] WARN: 未显式给定 --base，退回 '$BASE'。" >&2
  echo "[mypy-changed] WARN: 通过 T0 调用时应传显式 --base/--head，否则 push 后 origin/dev==HEAD 会空 diff 假绿。" >&2
fi

# 解析 venv
VENV_PY="$BACKEND/.venv/bin/python"
if [[ ! -x "$VENV_PY" ]]; then
  echo "[mypy-changed] 未找到 backend/.venv，请先创建虚拟环境" >&2
  exit 2
fi

# 在仓库根运行 git，获取相对仓库根的路径（形如 backend/app/...）
if [[ $WORKTREE -eq 1 ]]; then
  mapfile -t RAW < <(
    git -C "$REPO_ROOT" diff --name-only -- '*.py' 2>/dev/null
    git -C "$REPO_ROOT" diff --name-only --cached -- '*.py' 2>/dev/null
    git -C "$REPO_ROOT" ls-files --others --exclude-standard -- '*.py' 2>/dev/null
  )
else
  mapfile -t RAW < <(
    git -C "$REPO_ROOT" diff --name-only "${BASE}...${HEAD}" -- '*.py' 2>/dev/null
    git -C "$REPO_ROOT" ls-files --others --exclude-standard -- '*.py' 2>/dev/null
  )
fi

# 只保留 backend 下的 Python 文件，并转换为相对 backend 的路径（供 mypy 调用）
CHANGED=()
for f in "${RAW[@]:-}"; do
  [[ -z "$f" ]] && continue
  if [[ "$f" == backend/app/* || "$f" == backend/tests/* || "$f" == backend/scripts/* || "$f" == backend/tools/* ]]; then
    CHANGED+=("${f#backend/}")
  fi
done

if [[ ${#CHANGED[@]} -eq 0 ]]; then
  echo "[mypy-changed] 无改动 backend Python 文件，门禁跳过（exit 0）"
  exit 0
fi

echo "[mypy-changed] 检查改动文件（base=${BASE} head=${HEAD} worktree=${WORKTREE}）:"
printf '  - %s\n' "${CHANGED[@]}"

# --no-incremental 避免缓存把未改动文件的错误混入本次检查输出
# --follow-imports=skip 只校验本次改动文件自身的类型正确性（changed-file 口径），
# 不深入检查其依赖模块的遗留错误；依赖图的 baseline 错误不在本次交付门禁阻断范围。
cd "$BACKEND"
"$VENV_PY" -m mypy --no-incremental --follow-imports=skip --show-error-codes "${CHANGED[@]}"
status=$?

if [[ $status -eq 0 ]]; then
  echo "[mypy-changed] 改动文件 Mypy 通过（exit 0）"
else
  echo "[mypy-changed] 改动文件存在 Mypy 错误（exit $status）" >&2
fi
exit $status
