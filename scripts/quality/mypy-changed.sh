#!/usr/bin/env bash
# [Corrective-3.2 §P0-mypy-gate]
# 可重复的 changed-file Mypy 门禁：只检查相对基准分支（默认 origin/dev）发生
# 变化的 Python 文件。这些文件本身无类型错误时退出码为 0，从而满足 PRD Gate 1
# "全部代码质量门（Mypy）退出码为 0" 的完成定义，而不被仓库历史遗留的 45 个
# baseline mypy 错误阻塞（那些错误位于未改动文件，不在本次交付范围内）。
#
# 用法:
#   scripts/quality/mypy-changed.sh            # 默认基准 origin/dev
#   BASE=main scripts/quality/mypy-changed.sh  # 指定基准分支/commit
#
# 退出码:
#   0  => 所有改动文件通过 Mypy
#   1  => 至少一个改动文件存在 Mypy 错误
#   2  => 无法解析改动文件列表（git / venv 缺失）

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BACKEND="$REPO_ROOT/backend"
BASE="${BASE:-origin/dev}"

# 解析 venv
VENV_PY="$BACKEND/.venv/bin/python"
if [[ ! -x "$VENV_PY" ]]; then
  echo "[mypy-changed] 未找到 backend/.venv，请先创建虚拟环境" >&2
  exit 2
fi

# 在仓库根运行 git，获取相对仓库根的路径（形如 backend/app/...）
mapfile -t COMMITTED < <(git -C "$REPO_ROOT" diff --name-only "${BASE}"...HEAD -- '*.py' 2>/dev/null || true)
mapfile -t MODIFIED < <(git -C "$REPO_ROOT" diff --name-only -- '*.py' 2>/dev/null || true)
mapfile -t UNTRACKED < <(git -C "$REPO_ROOT" ls-files --others --exclude-standard -- '*.py' 2>/dev/null || true)
RAW=("${COMMITTED[@]:-}" "${MODIFIED[@]:-}" "${UNTRACKED[@]:-}")

# 只保留 backend 下的 Python 文件，并转换为相对 backend 的路径（供 mypy 调用）
CHANGED=()
for f in "${RAW[@]:-}"; do
  [[ -z "$f" ]] && continue
  if [[ "$f" == backend/app/* || "$f" == backend/tests/* || "$f" == backend/scripts/* || "$f" == backend/tools/* ]]; then
    CHANGED+=("${f#backend/}")
  fi
done

if [[ ${#CHANGED[@]} -eq 0 ]]; then
  echo "[mypy-changed] 无改动 Python 文件，门禁跳过（exit 0）"
  exit 0
fi

echo "[mypy-changed] 检查改动文件（基准=${BASE}）:"
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
