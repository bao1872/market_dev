#!/usr/bin/env bash
# panji-deploy.test.sh — 静态测试 panji-deploy.sh 关键修复点
#
# 验证项：
# 1. bash 语法正确
# 2. 关键函数存在
# 3. calendar 容器名正确（trading-worker-calendar，非 -scheduler）
# 4. dry-run 使用"计划验证"而非"健康检查"
# 5. validate_sha 前有 git fetch origin main
# 6. 部署后有 git checkout main（避免 detached HEAD）
# 7. state 目录初始化
# 8. 不碰 postgres/redis（不在 recreate_services 列表中）
#
# 用法: bash scripts/deploy/panji-deploy.test.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_PATH="${SCRIPT_DIR}/panji-deploy.sh"
PASS=0
FAIL=0

assert_contains() {
    local label="$1"
    local pattern="$2"
    local file="$3"
    if grep -q "${pattern}" "${file}" 2>/dev/null; then
        echo "  [PASS] ${label}"
        PASS=$((PASS + 1))
    else
        echo "  [FAIL] ${label}"
        FAIL=$((FAIL + 1))
    fi
}

assert_not_contains() {
    local label="$1"
    local pattern="$2"
    local file="$3"
    if grep -q "${pattern}" "${file}" 2>/dev/null; then
        echo "  [FAIL] ${label}"
        FAIL=$((FAIL + 1))
    else
        echo "  [PASS] ${label}"
        PASS=$((PASS + 1))
    fi
}

echo "=== panji-deploy.sh 静态测试 ==="

# 1. 语法检查
if bash -n "${SCRIPT_PATH}" 2>/dev/null; then
    echo "  [PASS] bash 语法正确"
    PASS=$((PASS + 1))
else
    echo "  [FAIL] bash 语法错误"
    FAIL=$((FAIL + 1))
fi

# 2. 关键函数存在
assert_contains "validate_sha 函数存在" "validate_sha()" "${SCRIPT_PATH}"
assert_contains "health_check 函数存在" "health_check()" "${SCRIPT_PATH}"
assert_contains "rollback 函数存在" "rollback()" "${SCRIPT_PATH}"
assert_contains "classify_changes 函数存在" "classify_changes()" "${SCRIPT_PATH}"

# 3. calendar 容器名正确
assert_contains "calendar 容器名: trading-worker-calendar" "trading-worker-calendar)" "${SCRIPT_PATH}"
assert_not_contains "不使用 trading-worker-calendar-scheduler" "calendar-scheduler" "${SCRIPT_PATH}"

# 4. dry-run 使用"计划验证"
assert_contains "dry-run 使用计划验证" "计划验证" "${SCRIPT_PATH}"

# 5. validate_sha 前有 git fetch
assert_contains "git fetch origin main 存在" "git fetch origin main" "${SCRIPT_PATH}"

# 6. 部署后有 git checkout main
assert_contains "部署后 checkout main" "git checkout main" "${SCRIPT_PATH}"

# 7. state 目录初始化
assert_contains "state 目录初始化" "state_dir" "${SCRIPT_PATH}"

# 8. 不碰 postgres/redis（不在 up -d / recreate_services 的服务列表中）
#    postgres/redis 仅出现在 health_check 的 required 容器检查中，不应出现在 up -d 命令行
#    过滤掉注释行后再检查
if grep -v '^ *#' "${SCRIPT_PATH}" | grep -E 'up -d.*postgres' 2>/dev/null; then
    echo "  [FAIL] up -d 命令包含 postgres"
    FAIL=$((FAIL + 1))
else
    echo "  [PASS] up -d 命令不含 postgres"
    PASS=$((PASS + 1))
fi
if grep -v '^ *#' "${SCRIPT_PATH}" | grep -E 'up -d.*redis' 2>/dev/null; then
    echo "  [FAIL] up -d 命令包含 redis"
    FAIL=$((FAIL + 1))
else
    echo "  [PASS] up -d 命令不含 redis"
    PASS=$((PASS + 1))
fi

# 9. 锁机制
assert_contains "flock 锁存在" "flock -n" "${SCRIPT_PATH}"

# 10. SHA 精确验证
assert_contains "git cat-file SHA 验证" "git cat-file -e" "${SCRIPT_PATH}"
assert_contains "main 祖先验证" "merge-base --is-ancestor" "${SCRIPT_PATH}"

echo ""
echo "=== 结果: ${PASS} passed, ${FAIL} failed ==="

if [[ ${FAIL} -gt 0 ]]; then
    exit 1
fi
exit 0
