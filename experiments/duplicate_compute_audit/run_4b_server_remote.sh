#!/usr/bin/env bash
#
# run_4b_server_remote.sh — Phase 4B-0G-R 服务器执行入口（GOVERNED, one-shot）
#
# 由 run_4b_server_control.sh 经 scripts/ops/panji-prod-ssh 唯一拉起。
# 本脚本在服务器上执行，但严格受限：
#
#   ✅ 允许：
#     - 从 HARNESS_SHA exact Git object materialize 本脚本自身与 harness（不 scp）
#     - git fetch origin dev（仅更新 refs/objects，不 checkout/reset/pull）
#     - 校验四套运行身份一致（server repo HEAD / live RUNTIME_SHA / runtime_git_sha / prod）
#     - 用正式组合 Compose（market.env + prod + live）基于 worker-after-close service
#       创建一次性容器，benchmark Python 作为 MAIN PROCESS
#     - 继承正式 service env（DATABASE_URL/REDIS_URL/JWT_SECRET 由 Compose 注入）
#     - read-only heavy-task preflight（production ORM，不写）
#     - 监控真实业务 progress（output/4B-server-db/progress.jsonl）+ docker stats 采样
#     - benchmark 容器退出码即结论；capture 退出码
#     - 打包脱敏 evidence archive（不删），由 control 经 SSH 取回后精确删除远端 archive
#     - 精确清理临时 container / workspace
#
#   ❌ 禁止：
#     - scp / rsync / docker cp 注入源码
#     - 临时 PYTHONPATH 指向另一 SHA 的 app
#     - stdin 注入 Python
#     - checkout/reset/pull 修改 /root/web_dev working tree
#     - 修改 /opt/panji-live live mount
#     - 新建测试数据库
#     - 写 bz_stock（harness 内 read-only guard 兜底）
#     - 启动 scheduler / publish
#     - sleep infinity + docker exec -d（benchmark 必须是一性容器的 MAIN PROCESS）
#     - 把 env dump 到 shell 变量/log（secrets 只由 Compose 注入容器）
#
# 用法（由 control 脚本调用，勿手动拼）：
#   bash run_4b_server_remote.sh <HARNESS_SHA> <PROD_RUNTIME_SHA>
#
set -euo pipefail

HARNESS_SHA="${1:?HARNESS_SHA required}"
PROD_RUNTIME_SHA="${2:?PROD_RUNTIME_SHA required}"

# ---- 3. SHA input fail-closed（本地 + 此处双重校验） ----
if ! [[ "$HARNESS_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "[4B-0G-R][remote] ERROR: HARNESS_SHA 非 40 位 hex: '$HARNESS_SHA'" >&2
  exit 2
fi
if ! [[ "$PROD_RUNTIME_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "[4B-0G-R][remote] ERROR: PROD_RUNTIME_SHA 非 40 位 hex: '$PROD_RUNTIME_SHA'" >&2
  exit 2
fi

SERVER_REPO="/root/web_dev"
LIVE_DIR="/opt/panji-live"
RUNTIME_SHA_FILE="$LIVE_DIR/RUNTIME_SHA"
APP_DIR="$LIVE_DIR/backend/app"
MARKET_ENV="/etc/market-dev/market.env"
COMPOSE_PROD="$SERVER_REPO/docker-compose.prod.yml"
COMPOSE_LIVE="$SERVER_REPO/docker-compose.live.yml"

BENCHMARK_WORKSPACE="/tmp/4b-benchmark-$(date +%Y%m%d-%H%M%S)-$HARNESS_SHA"
CONTAINER_NAME="panji-benchmark-4b-$(date +%s)"
HARNESS_REL_PATH="experiments/duplicate_compute_audit/run_4b_server_benchmark.py"
REMOTE_REL_PATH="experiments/duplicate_compute_audit/run_4b_server_remote.sh"

# evidence archive：benchmark 结束后保留，待 control 取回；不在 cleanup 删除
EVIDENCE_ARCHIVE="/tmp/4b-evidence-${HARNESS_SHA}.tar.gz"

cleanup() {
  echo "[4B-0G-R][remote] cleanup: 移除临时容器 $CONTAINER_NAME ..."
  docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
  echo "[4B-0G-R][remote] cleanup: 移除临时 workspace $BENCHMARK_WORKSPACE ..."
  rm -rf "$BENCHMARK_WORKSPACE" >/dev/null 2>&1 || true
  # 注意：EVIDENCE_ARCHIVE 由 control 取回后删除，此处不删。
}
trap cleanup EXIT

echo "============================================================"
echo "[4B-0G-R][remote] Governed Server Benchmark Runner (runtime-closed)"
echo "  HARNESS_SHA        = $HARNESS_SHA"
echo "  PROD_RUNTIME_SHA   = $PROD_RUNTIME_SHA"
echo "============================================================"

# ---- a. 服务器 repo / runtime SHA gates（四身份 hard gate，不允许 empty PASS） ----
echo "[4B-0G-R][remote] a. 校验四套运行身份 ..."

cd "$SERVER_REPO"
SERVER_REPO_HEAD="$(git rev-parse HEAD)"
SERVER_REPO_CLEAN="$(git status --porcelain | wc -l)"
if [ "$SERVER_REPO_CLEAN" -ne 0 ]; then
  echo "[4B-0G-R][remote] ERROR: /root/web_dev working tree 不干净（$SERVER_REPO_CLEAN 项），停止。" >&2
  exit 3
fi

LIVE_RUNTIME_SHA="$(cat "$RUNTIME_SHA_FILE" 2>/dev/null || true)"
if [ -z "$LIVE_RUNTIME_SHA" ]; then
  echo "[4B-0G-R][remote] ERROR: 无法读取 $RUNTIME_SHA_FILE" >&2
  exit 3
fi

# runtime_git_sha 必须成功取得（仓库实际 backend 暴露 8000，非 8080）
RUNTIME_GIT_SHA="$(curl -fsS http://localhost:8000/v1/version 2>/dev/null \
  | python3 -c 'import sys,json;print(json.load(sys.stdin).get("runtime_git_sha",""))' 2>/dev/null || true)"
if [ -z "$RUNTIME_GIT_SHA" ]; then
  echo "[4B-0G-R][remote] ERROR: 无法取得 runtime_git_sha（/v1/version 不可达或字段缺失），停止。" >&2
  exit 3
fi

echo "  server_repo_head    = $SERVER_REPO_HEAD"
echo "  live_runtime_sha    = $LIVE_RUNTIME_SHA"
echo "  runtime_git_sha     = $RUNTIME_GIT_SHA"
echo "  prod_runtime_sha    = $PROD_RUNTIME_SHA"

if [ "$SERVER_REPO_HEAD" != "$PROD_RUNTIME_SHA" ]; then
  echo "[4B-0G-R][remote] ERROR: server_repo_head != prod_runtime_sha" >&2
  exit 3
fi
if [ "$LIVE_RUNTIME_SHA" != "$PROD_RUNTIME_SHA" ]; then
  echo "[4B-0G-R][remote] ERROR: live_runtime_sha != prod_runtime_sha" >&2
  exit 3
fi
if [ "$RUNTIME_GIT_SHA" != "$PROD_RUNTIME_SHA" ]; then
  echo "[4B-0G-R][remote] ERROR: runtime_git_sha != prod_runtime_sha" >&2
  exit 3
fi

# ---- b. git fetch origin dev（只更新 refs/objects，不 checkout） ----
echo "[4B-0G-R][remote] b. git fetch origin dev（不修改 working tree） ..."
git fetch origin dev >/dev/null 2>&1 || {
  echo "[4B-0G-R][remote] WARN: git fetch 失败，尝试用本地已有 object" >&2
}

# ---- c. 验证 HARNESS_SHA 为 origin/dev 祖先 + object 存在 ----
echo "[4B-0G-R][remote] c. 验证 HARNESS_SHA 为 origin/dev 祖先且 object 存在 ..."
if ! git merge-base --is-ancestor "$HARNESS_SHA" "origin/dev" 2>/dev/null; then
  echo "[4B-0G-R][remote] ERROR: HARNESS_SHA 不是 origin/dev 祖先，来源不可审计。" >&2
  exit 4
fi
if ! git cat-file -e "$HARNESS_SHA^{commit}" 2>/dev/null; then
  echo "[4B-0G-R][remote] ERROR: HARNESS_SHA object 在服务器 repo 不存在（fetch 失败？）。" >&2
  exit 4
fi

# ---- d. 从 exact Git object materialize harness + 本 remote runner（不 scp） ----
echo "[4B-0G-R][remote] d. 从 exact Git object materialize harness + remote runner ..."
mkdir -p "$BENCHMARK_WORKSPACE"
HARNESS_BLOB="$BENCHMARK_WORKSPACE/run_4b_server_benchmark.py"
REMOTE_BLOB="$BENCHMARK_WORKSPACE/run_4b_server_remote.sh"
git cat-file -p "$HARNESS_SHA:$HARNESS_REL_PATH" > "$HARNESS_BLOB"
git cat-file -p "$HARNESS_SHA:$REMOTE_REL_PATH" > "$REMOTE_BLOB"
HARNESS_FILE_SHA="$(sha256sum "$HARNESS_BLOB" | awk '{print $1}')"
echo "  materialized harness file sha256 = $HARNESS_FILE_SHA"

EXPECTED_HARNESS_FILE_SHA="${PANJI_EXPECTED_HARNESS_FILE_SHA:-}"
if [ -n "$EXPECTED_HARNESS_FILE_SHA" ] && [ "$EXPECTED_HARNESS_FILE_SHA" != "$HARNESS_FILE_SHA" ]; then
  echo "[4B-0G-R][remote] ERROR: materialized harness file hash 不匹配。" >&2
  exit 5
fi

# ---- f/g. read-only heavy-task preflight（production ORM，不写） ----
# 用同类短命容器执行 harness --heavy-check（与 benchmark 同源 production app）。
# --no-deps：禁止 benchmark 启停 production dependencies（PostgreSQL/Redis）。
# 路径统一：/root/web_dev→/repo:ro，PANJI_REPO_ROOT=/repo（与正式 run 一致）。
echo "[4B-0G-R][remote] f/g. read-only heavy-task preflight（after_close_orchestrator 活跃任务）..."
HEAVY_CHECK_RC=0
docker compose --env-file "$MARKET_ENV" -f "$COMPOSE_PROD" -f "$COMPOSE_LIVE" run --rm --no-deps \
  --name "${CONTAINER_NAME}-preflight" \
  -v "$SERVER_REPO:/repo:ro" \
  -v "$APP_DIR:/app/app:ro" \
  -v "$HARNESS_BLOB:/app/benchmark/run_4b_server_benchmark.py:ro" \
  -e PANJI_REPO_ROOT=/repo \
  -e PANJI_BACKEND_ROOT=/app \
  worker-after-close \
  python -m benchmark.run_4b_server_benchmark --heavy-check \
  > "$BENCHMARK_WORKSPACE/heavy_check.json" 2>&1 || HEAVY_CHECK_RC=$?

if [ "$HEAVY_CHECK_RC" -ne 0 ]; then
  echo "[4B-0G-R][remote] ERROR: heavy-task preflight 判定 BLOCKED（存在活跃 after_close 任务）。" >&2
  cat "$BENCHMARK_WORKSPACE/heavy_check.json" >&2 || true
  exit 11
fi
echo "  heavy-task preflight: clear（无活跃 after_close 重型任务）"

# ---- h/i. 创建一次性 worker-after-close 同类容器，benchmark 作为 MAIN PROCESS ----
# 以 --detach 启动（benchmark 仍是该容器 MAIN PROCESS），runner 并行监控真实进度，
# 最后 docker wait 取退出码；禁止 sleep infinity + docker exec -d。
# --no-deps：禁止 benchmark 启停 production dependencies。
echo "[4B-0G-R][remote] h/i. 创建一次性 benchmark 容器（benchmark = MAIN PROCESS, detach）..."

mkdir -p "$BENCHMARK_WORKSPACE/output"

docker compose --env-file "$MARKET_ENV" -f "$COMPOSE_PROD" -f "$COMPOSE_LIVE" run -d --no-deps \
  --name "$CONTAINER_NAME" \
  -v "$SERVER_REPO:/repo:ro" \
  -v "$APP_DIR:/app/app:ro" \
  -v "$HARNESS_BLOB:/app/benchmark/run_4b_server_benchmark.py:ro" \
  -v "$BENCHMARK_WORKSPACE/output:/app/benchmark/output" \
  -e PANJI_REPO_ROOT=/repo \
  -e PANJI_BACKEND_ROOT=/app \
  -e PANJI_BENCHMARK_HARNESS_SHA="$HARNESS_SHA" \
  -e PANJI_SERVER_REPO_HEAD="$SERVER_REPO_HEAD" \
  -e PANJI_LIVE_RUNTIME_SHA="$LIVE_RUNTIME_SHA" \
  -e PANJI_RUNTIME_GIT_SHA="$RUNTIME_GIT_SHA" \
  -e PANJI_4B_OUTPUT_DIR=/app/benchmark/output/4B-server-db \
  --entrypoint python \
  worker-after-close \
  -m benchmark.run_4b_server_benchmark

# ---- i2. 资源契约证据：比较 one-shot 与常驻 worker-after-close 的 resource envelope ----
# 仅记录 Image / Memory / NanoCpus / PidsLimit / Network，mismatch → STOP（不入 compute）。
echo "[4B-0G-R][remote] i2. 资源契约核验（one-shot vs worker-after-close）..."
RUNTIME_CONTRACT="$BENCHMARK_WORKSPACE/output/4B-server-db/runtime_contract.json"
docker inspect trading-worker-after-close "$CONTAINER_NAME" >/dev/null 2>&1 || {
  echo "[4B-0G-R][remote] ERROR: 无法 inspect trading-worker-after-close 或 benchmark 容器" >&2
  exit 12
}
python3 - "$CONTAINER_NAME" "$RUNTIME_CONTRACT" <<'PY'
import sys, json, subprocess
cn, out = sys.argv[1], sys.argv[2]
def inspect(name):
    raw = subprocess.check_output(["docker","inspect",name], text=True)
    return json.loads(raw)[0]
base = inspect("trading-worker-after-close")
one  = inspect(cn)
def hostcfg(c): return c.get("HostConfig", {})
base_h, one_h = hostcfg(base), hostcfg(one)
base_net = list((base.get("NetworkSettings",{}) or {}).get("Networks",{}) or {})
one_net  = list((one.get("NetworkSettings",{}) or {}).get("Networks",{}) or {})
contract = {
    "baseline_container": "trading-worker-after-close",
    "benchmark_container": cn,
    "image_match": base["Image"] == one["Image"],
    "memory_match": base_h.get("Memory") == one_h.get("Memory"),
    "nanocpus_match": base_h.get("NanoCpus") == one_h.get("NanoCpus"),
    "pidslimit_match": base_h.get("PidsLimit") == one_h.get("PidsLimit"),
    "network_compatible": (set(base_net) & set(one_net)) != set() or base_net == one_net,
    "baseline": {"image": base["Image"], "memory": base_h.get("Memory"),
                 "nanocpus": base_h.get("NanoCpus"), "pidslimit": base_h.get("PidsLimit"),
                 "networks": base_net},
    "benchmark": {"image": one["Image"], "memory": one_h.get("Memory"),
                  "nanocpus": one_h.get("NanoCpus"), "pidslimit": one_h.get("PidsLimit"),
                  "networks": one_net},
    "note": "benchmark one-shot 应继承 worker-after-close 同类 resource envelope；"
            "mismatch 表示 cgroup 不对齐，CPU/RSS baseline 不可信。",
}
ok = (contract["image_match"] and contract["memory_match"] and contract["nanocpus_match"]
      and contract["pidslimit_match"] and contract["network_compatible"])
contract["envelope_aligned"] = bool(ok)
with open(out, "w", encoding="utf-8") as f:
    json.dump(contract, f, ensure_ascii=False, indent=2)
if not ok:
    print("[4B-0G-R][remote] ERROR: 资源契约不对齐，STOP before full compute", file=sys.stderr)
    sys.exit(13)
print("[4B-0G-R][remote] 资源契约对齐: image/memory/nanocpus/pidslimit/network 均匹配")
PY
CONTRACT_RC=$?
if [ "$CONTRACT_RC" -ne 0 ]; then
  exit "$CONTRACT_RC"
fi

# ---- j. 监控真实业务 progress + docker stats（并行，非 generic timeout） ----
PROGRESS_FILE="$BENCHMARK_WORKSPACE/output/4B-server-db/progress.jsonl"
RESOURCE_SAMPLES="$BENCHMARK_WORKSPACE/output/4B-server-db/resource_samples.jsonl"
LAST_PROCESSED=0
LAST_TS="$(date +%s)"
STALL_SEC=0
MAX_STALL_SEC="${PANJI_MAX_STALL_SEC:-1800}"   # 默认 30 分钟无真实进展判 stall

echo "[4B-0G-R][remote] j. 监控真实业务进度 + 资源采样 ..."
while docker ps --format '{{.Names}}' | grep -qx "$CONTAINER_NAME"; do
  sleep 30
  # 真实业务 progress
  if [ -f "$PROGRESS_FILE" ]; then
    CUR_PROCESSED="$(tail -n1 "$PROGRESS_FILE" 2>/dev/null | python3 -c 'import sys,json;print(json.load(sys.stdin).get("processed",0))' 2>/dev/null || echo 0)"
    CUR_TS="$(date +%s)"
    if [ "$CUR_PROCESSED" -gt "$LAST_PROCESSED" ]; then
      STALL_SEC=0
      LAST_PROCESSED="$CUR_PROCESSED"
      LAST_TS="$CUR_TS"
      echo "[4B-0G-R][remote] progress: $CUR_PROCESSED / 5293  ($(date))"
    else
      STALL_SEC=$(( CUR_TS - LAST_TS ))
      if [ "$STALL_SEC" -ge "$MAX_STALL_SEC" ]; then
        echo "[4B-0G-R][remote] ERROR: 超过 ${MAX_STALL_SEC}s 无真实业务进展，判定 stall。" >&2
        docker logs --tail 200 "$CONTAINER_NAME" >&2 || true
        docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
        exit 6
      fi
    fi
  fi
  # 资源采样写入 resource_samples.jsonl（不阻断）
  docker stats --no-stream --format \
    "{\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"cpu\":\"{{.CPUPerc}}\",\"mem\":\"{{.MemUsage}}\"}" \
    "$CONTAINER_NAME" 2>/dev/null >> "$RESOURCE_SAMPLES" || true
done

# benchmark 容器已退出 → docker wait 取精确退出码
BENCHMARK_RC="$(docker wait "$CONTAINER_NAME" 2>/dev/null || echo 1)"
echo "[4B-0G-R][remote] benchmark 容器退出码 = $BENCHMARK_RC"

if [ -f "$PROGRESS_FILE" ]; then
  LAST_LINE="$(tail -n1 "$PROGRESS_FILE" 2>/dev/null || true)"
  echo "[4B-0G-R][remote] final progress: $LAST_LINE"
fi

# ---- k. 打包脱敏 evidence archive（保留，待 control 取回） ----
echo "[4B-0G-R][remote] k. 打包 evidence archive ..."
EVIDENCE_DIR="$BENCHMARK_WORKSPACE/output/4B-server-db"
if [ -d "$EVIDENCE_DIR" ]; then
  tar -czf "$EVIDENCE_ARCHIVE" -C "$EVIDENCE_DIR" . \
    --exclude='*.py' --exclude='__pycache__' 2>/dev/null || {
      echo "[4B-0G-R][remote] WARN: evidence 打包失败" >&2
    }
  echo "  archive: $EVIDENCE_ARCHIVE"
  ls -la "$EVIDENCE_DIR" 2>/dev/null || true
fi

# ---- l. 退出码即结论 ----
if [ "$BENCHMARK_RC" -ne 0 ]; then
  echo "[4B-0G-R][remote] FAIL: benchmark 非零退出（$BENCHMARK_RC）。" >&2
  exit "$BENCHMARK_RC"
fi

echo "[4B-0G-R][remote] DONE. evidence archive 保留于 $EVIDENCE_ARCHIVE（由 control 取回后删除）。"
echo "EVIDENCE_ARCHIVE_PATH=$EVIDENCE_ARCHIVE"
exit 0
