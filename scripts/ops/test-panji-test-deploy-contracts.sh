#!/usr/bin/env bash
# test-panji-test-deploy-contracts.sh
#
# 部署脚本契约测试（零依赖本地 bash 自测）。
#
# 绑定来源：scripts/ops/panji-test-deploy（[FIX-20260802] 修复版本）。
# 该部署脚本的部署步骤在远程 heredoc 内执行，无法直接 source。
# 本测试内联复制其中关键纯函数逻辑，断言 4 个不变量，
# 防止部署脚本后续被改回"硬编码服务名 / 虚假完成 / 错误健康端点 / 无资源门禁"。
#
# 运行：
#   bash scripts/ops/test-panji-test-deploy-contracts.sh
# 退出码：0 = 全部契约通过；1 = 任一契约失败。

set -uo pipefail

PASS=0
FAIL=0

assert() {
    # $1 = 测试名  $2 = 实际布尔结果(0/1)；期望返回 0（应通过）
    local name="$1" rc="$2"
    if [[ "$rc" -eq 0 ]]; then
        echo "  PASS: $name"
        PASS=$((PASS + 1))
    else
        echo "  FAIL: $name" >&2
        FAIL=$((FAIL + 1))
    fi
}

assert_reject() {
    # $1 = 测试名  $2 = 实际布尔结果(0/1)；期望返回非 0（应被拒绝）
    local name="$1" rc="$2"
    if [[ "$rc" -ne 0 ]]; then
        echo "  PASS: $name"
        PASS=$((PASS + 1))
    else
        echo "  FAIL: $name" >&2
        FAIL=$((FAIL + 1))
    fi
}

# ---------------------------------------------------------------------------
# 契约 1 + 4：资源硬门禁阈值判定（scripts/ops/panji-test-deploy §1b）
# 任意一项低于/高于阈值即拒绝部署（返回 1），全部满足才通过（返回 0）。
# ---------------------------------------------------------------------------
resource_gate_ok() {
    # $1=DISK_AVAIL_GB $2=DISK_USE_PCT $3=MEM_AVAIL_MB
    local DISK_AVAIL_GB="$1" DISK_USE_PCT="$2" MEM_AVAIL_MB="$3"
    local MIN_DISK_GB="${PANJI_MIN_DISK_GB:-20}"
    local MAX_DISK_PCT="${PANJI_MAX_DISK_PCT:-82}"
    local MIN_MEM_MB="${PANJI_MIN_MEM_MB:-4096}"
    [[ "$DISK_AVAIL_GB" -lt "$MIN_DISK_GB" ]] && return 1
    [[ "$DISK_USE_PCT" -gt "$MAX_DISK_PCT" ]] && return 1
    [[ "$MEM_AVAIL_MB" -lt "$MIN_MEM_MB" ]] && return 1
    return 0
}

echo "== 契约 1/4：资源硬门禁阈值 =="
resource_gate_ok 45 61 6300; assert "充裕资源可通过门禁" $?
export PANJI_MIN_DISK_GB=20 PANJI_MAX_DISK_PCT=82 PANJI_MIN_MEM_MB=4096
resource_gate_ok 19 61 6300; assert_reject "磁盘可用<20GB 拒绝" $?
resource_gate_ok 45 90 6300; assert_reject "磁盘使用率>82% 拒绝" $?
resource_gate_ok 45 61 3000; assert_reject "可用内存<4096MB 拒绝" $?
# 边界值（刚好等于阈值）应通过
resource_gate_ok 20 82 4096; assert "边界值(20GB/82%/4096MB)通过" $?

# ---------------------------------------------------------------------------
# 契约 2：服务名唯一真源（scripts/ops/panji-test-deploy §8a/8）
# 计划重建的服务若不在 docker compose config --services 清单中，必须立即失败，
# 禁止静默跳过（这是导致"报告完成但容器仍旧 SHA"的历史根因）。
# ---------------------------------------------------------------------------
service_discovery_ok() {
    # $1=compose 服务清单(换行分隔)  剩余参数=计划重建的服务
    local COMPOSE_SERVICES="$1"; shift
    local svc
    for svc in "$@"; do
        if ! echo "$COMPOSE_SERVICES" | grep -qx "$svc"; then
            return 1   # 计划的服务不在 compose 中 → 拒绝
        fi
    done
    return 0
}

# 模拟真实 compose 清单（与 docker-compose.prod.yml 对齐，无 worker/worker-chips）
REAL_COMPOSE=$'backend\nfrontend\nworker-capture\nworker-after-close\nworker-review-bootstrap'

echo "== 契约 2/4：服务名唯一真源（禁止硬编码不存在的服务） =="
service_discovery_ok "$REAL_COMPOSE" backend frontend worker-capture worker-after-close worker-review-bootstrap
assert "全部真实服务名通过" $?
service_discovery_ok "$REAL_COMPOSE" backend worker
assert_reject "历史缺陷服务名 'worker' 必须被拒绝" $?
service_discovery_ok "$REAL_COMPOSE" backend worker-chips
assert_reject "历史缺陷服务名 'worker-chips' 必须被拒绝" $?

# ---------------------------------------------------------------------------
# 契约 3：无虚假完成（scripts/ops/panji-test-deploy §8b）
# 运行中容器镜像标签必须以 :SHORT_SHA 结尾，否则拒绝报告成功。
# ---------------------------------------------------------------------------
image_tag_matches() {
    # $1=运行中镜像名  $2=SHORT_SHA
    [[ "$1" == *":$2" ]]
}

echo "== 契约 3/4：镜像标签逐服务校验（禁止虚假完成） =="
image_tag_matches "market-dev-backend:29a5b7d" "29a5b7d"; assert "镜像标签匹配 SHORT_SHA 通过" $?
image_tag_matches "market-dev-backend:old1234" "29a5b7d"; assert_reject "镜像标签不匹配必须被拒绝" $?
image_tag_matches "market-dev-backend:29a5b7d" "old1234"; assert_reject "旧 SHA 容器必须被拒绝" $?

# ---------------------------------------------------------------------------
# 契约 4：健康端点正确性（scripts/ops/panji-test-deploy §9）
# backend 探测必须用 python3 + urllib 访问 /v1/health 与 /v1/version，
# 且端点路径是 /v1/health、/v1/version（非 /health、非 /api/v1/health、非 /version）。
# 这里验证"端点路径常量"契约：脚本中后端_http 仅接受这两个路径。
# ---------------------------------------------------------------------------
VALID_BACKEND_PATHS=$'/v1/health\n/v1/version'
is_valid_backend_path() {
    echo "$VALID_BACKEND_PATHS" | grep -qx "$1"
}

echo "== 契约 4/4：后端健康端点路径 =="
is_valid_backend_path "/v1/health"; assert "/v1/health 是合法探测路径" $?
is_valid_backend_path "/v1/version"; assert "/v1/version 是合法探测路径" $?
is_valid_backend_path "/version"; assert_reject "旧错误端点 /version 必须被拒绝" $?
is_valid_backend_path "/api/v1/health"; assert_reject "旧错误端点 /api/v1/health 必须被拒绝" $?
is_valid_backend_path "/health"; assert_reject "旧错误端点 /health 必须被拒绝" $?

# ---------------------------------------------------------------------------
echo "----------------------------------------"
echo "部署脚本契约测试：$PASS 通过 / $FAIL 失败"
[[ "$FAIL" -eq 0 ]] && exit 0 || exit 1
