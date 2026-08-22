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
#     - 校验部署身份一致（server repo HEAD / live RUNTIME_SHA / runtime_git_sha 三者==DEPLOYED）
#     - 从 TARGET_CODE_SHA exact Git object materialize backend/app 到临时 workspace（不 scp）
#     - 从 HARNESS_SHA exact Git object materialize benchmark Compose override（不 scp）
#     - 用正式组合 Compose（market.env + prod + live + benchmark override）基于 worker-after-close
#       service 创建一次性容器，benchmark Python 作为 MAIN PROCESS；override 在最终层显式将
#       /app/app 唯一指向 materialized TARGET 的 exact app（不再依赖 CLI -v 与 service volume 的 precedence）
#     - 继承正式 service env（DATABASE_URL/REDIS_URL/JWT_SECRET 由 Compose 注入）
#     - read-only heavy-task preflight（production ORM，不写）
#     - 监控真实业务 progress（output/4B-server-db/progress.jsonl）+ docker stats 采样
#     - benchmark 容器退出码即结论；capture 退出码
#     - 打包脱敏 evidence archive（不删），由 control 经 SSH 取回后精确删除远端 archive
#     - 精确清理临时 container / workspace / materialized target app
#
#   ❌ 禁止：
#     - scp / rsync / docker cp 注入源码
#     - 临时 PYTHONPATH 指向另一 SHA 的 app
#     - stdin 注入 Python
#     - checkout/reset/pull 修改 /root/web_dev working tree
#     - 修改 /opt/panji-live live mount
#     - 新建测试数据库、执行 migration
#     - 写 bz_stock（harness 内 read-only guard 兜底）
#     - 启动 scheduler / publish
#     - sleep infinity + docker exec -d（benchmark 必须是一性容器的 MAIN PROCESS）
#     - 把 env dump 到 shell 变量/log（secrets 只由 Compose 注入容器）
#
# 用法（由 control 脚本调用，勿手动拼）：
#   bash run_4b_server_remote.sh <HARNESS_SHA> <DEPLOYED_RUNTIME_SHA> <TARGET_CODE_SHA>
#
set -euo pipefail

HARNESS_SHA="${1:?HARNESS_SHA required}"
DEPLOYED_RUNTIME_SHA="${2:?DEPLOYED_RUNTIME_SHA required}"
TARGET_CODE_SHA="${3:?TARGET_CODE_SHA required}"

# ---- 3. SHA input fail-closed（本地 + 此处双重校验） ----
if ! [[ "$HARNESS_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "[4B-0G-R3][remote] ERROR: HARNESS_SHA 非 40 位 hex: '$HARNESS_SHA'" >&2
  exit 2
fi
if ! [[ "$DEPLOYED_RUNTIME_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "[4B-0G-R3][remote] ERROR: DEPLOYED_RUNTIME_SHA 非 40 位 hex: '$DEPLOYED_RUNTIME_SHA'" >&2
  exit 2
fi
if ! [[ "$TARGET_CODE_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "[4B-0G-R3][remote] ERROR: TARGET_CODE_SHA 非 40 位 hex: '$TARGET_CODE_SHA'" >&2
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
TARGET_APP_DIR="/tmp/4b-target-app-$(date +%Y%m%d-%H%M%S)-$TARGET_CODE_SHA"
CONTAINER_NAME="panji-benchmark-4b-$(date +%s)"
HARNESS_REL_PATH="experiments/duplicate_compute_audit/run_4b_server_benchmark.py"
REMOTE_REL_PATH="experiments/duplicate_compute_audit/run_4b_server_remote.sh"

# evidence archive：benchmark 结束后保留，待 control 取回；不在 cleanup 删除
EVIDENCE_ARCHIVE="/tmp/4b-evidence-${HARNESS_SHA}.tar.gz"

cleanup() {
  echo "[4B-0G-R3][remote] cleanup: 移除临时容器 $CONTAINER_NAME ..."
  docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
  echo "[4B-0G-R3][remote] cleanup: 移除临时 workspace $BENCHMARK_WORKSPACE ..."
  rm -rf "$BENCHMARK_WORKSPACE" >/dev/null 2>&1 || true
  echo "[4B-0G-R3][remote] cleanup: 移除 materialized target app $TARGET_APP_DIR ..."
  rm -rf "$TARGET_APP_DIR" >/dev/null 2>&1 || true
  # 注意：EVIDENCE_ARCHIVE 由 control 取回后删除，此处不删。
}
trap cleanup EXIT

echo "============================================================"
echo "[4B-0G-R3][remote] Governed Server Benchmark Runner (deployed/target split)"
echo "  HARNESS_SHA          = $HARNESS_SHA"
echo "  DEPLOYED_RUNTIME_SHA = $DEPLOYED_RUNTIME_SHA"
echo "  TARGET_CODE_SHA      = $TARGET_CODE_SHA"
echo "============================================================"

# ---- a. 部署身份 gates（server repo HEAD / live RUNTIME_SHA / runtime_git_sha 三者==DEPLOYED） ----
echo "[4B-0G-R3][remote] a. 校验部署身份（三者一致 == DEPLOYED_RUNTIME_SHA）..."

cd "$SERVER_REPO"
SERVER_REPO_HEAD="$(git rev-parse HEAD)"
SERVER_REPO_CLEAN="$(git status --porcelain | wc -l)"
if [ "$SERVER_REPO_CLEAN" -ne 0 ]; then
  echo "[4B-0G-R3][remote] ERROR: /root/web_dev working tree 不干净（$SERVER_REPO_CLEAN 项），停止。" >&2
  exit 3
fi

LIVE_RUNTIME_SHA="$(cat "$RUNTIME_SHA_FILE" 2>/dev/null || true)"
if [ -z "$LIVE_RUNTIME_SHA" ]; then
  echo "[4B-0G-R3][remote] ERROR: 无法读取 $RUNTIME_SHA_FILE" >&2
  exit 3
fi

# runtime_git_sha 必须成功取得（仓库实际 backend 暴露 8000，非 8080）
RUNTIME_GIT_SHA="$(curl -fsS http://localhost:8000/v1/version 2>/dev/null \
  | python3 -c 'import sys,json;print(json.load(sys.stdin).get("runtime_git_sha",""))' 2>/dev/null || true)"
if [ -z "$RUNTIME_GIT_SHA" ]; then
  echo "[4B-0G-R3][remote] ERROR: 无法取得 runtime_git_sha（/v1/version 不可达或字段缺失），停止。" >&2
  exit 3
fi

echo "  server_repo_head    = $SERVER_REPO_HEAD"
echo "  live_runtime_sha    = $LIVE_RUNTIME_SHA"
echo "  runtime_git_sha     = $RUNTIME_GIT_SHA"
echo "  deployed_runtime_sha= $DEPLOYED_RUNTIME_SHA"

if [ "$SERVER_REPO_HEAD" != "$DEPLOYED_RUNTIME_SHA" ]; then
  echo "[4B-0G-R3][remote] ERROR: server_repo_head != deployed_runtime_sha" >&2
  exit 3
fi
if [ "$LIVE_RUNTIME_SHA" != "$DEPLOYED_RUNTIME_SHA" ]; then
  echo "[4B-0G-R3][remote] ERROR: live_runtime_sha != deployed_runtime_sha" >&2
  exit 3
fi
if [ "$RUNTIME_GIT_SHA" != "$DEPLOYED_RUNTIME_SHA" ]; then
  echo "[4B-0G-R3][remote] ERROR: runtime_git_sha != deployed_runtime_sha" >&2
  exit 3
fi

# ---- b. git fetch origin dev（只更新 refs/objects，不 checkout） ----
echo "[4B-0G-R3][remote] b. git fetch origin dev（不修改 working tree） ..."
git fetch origin dev >/dev/null 2>&1 || {
  echo "[4B-0G-R3][remote] WARN: git fetch 失败，尝试用本地已有 object" >&2
}

# ---- c. 验证 HARNESS_SHA / TARGET_CODE_SHA 为 origin/dev 祖先 + object 存在 ----
echo "[4B-0G-R3][remote] c. 验证 HARNESS_SHA / TARGET_CODE_SHA 祖先与 object ..."
for SHA in "$HARNESS_SHA" "$TARGET_CODE_SHA"; do
  if ! git merge-base --is-ancestor "$SHA" "origin/dev" 2>/dev/null; then
    echo "[4B-0G-R3][remote] ERROR: $SHA 不是 origin/dev 祖先，来源不可审计。" >&2
    exit 4
  fi
  if ! git cat-file -e "$SHA^{commit}" 2>/dev/null; then
    echo "[4B-0G-R3][remote] ERROR: $SHA object 在服务器 repo 不存在（fetch 失败？）。" >&2
    exit 4
  fi
done

# ---- c2. DB 兼容 gate：TARGET 所需 alembic head <= 服务器 DB 当前 head（绝不 migration） ----
echo "[4B-0G-R3][remote] c2. DB 兼容 gate（target alembic head <= server DB head）..."
TARGET_ALEMBIC_HEAD="$(git ls-tree -r "$TARGET_CODE_SHA" --name-only \
  | grep -E 'backend/alembic/versions/.*\.py$' | sed -E 's#.*/versions/##; s#\.py$##' \
  | sort | tail -1)"
if [ -z "$TARGET_ALEMBIC_HEAD" ]; then
  echo "[4B-0G-R3][remote] ERROR: 无法确定 TARGET_CODE_SHA 的 alembic head" >&2
  exit 4
fi
SERVER_DB_HEAD="$(docker exec trading-backend alembic current 2>/dev/null \
  | grep -oE '[0-9]{3}_[a-z_]+' | tail -1 || true)"
if [ -z "$SERVER_DB_HEAD" ]; then
  echo "[4B-0G-R3][remote] ERROR: 无法取得服务器 DB alembic current（容器/DB 不可达）" >&2
  exit 4
fi
echo "  target_alembic_head = $TARGET_ALEMBIC_HEAD"
echo "  server_db_head      = $SERVER_DB_HEAD"
T_NUM="${TARGET_ALEMBIC_HEAD%%_*}"
S_NUM="${SERVER_DB_HEAD%%_*}"
if [ "${T_NUM:-0}" -gt "${S_NUM:-0}" ]; then
  echo "[4B-0G-R3][remote] ERROR: target 需要 migration ($TARGET_ALEMBIC_HEAD) 但服务器 DB 仅 $SERVER_DB_HEAD；禁止 migration，STOP。" >&2
  exit 7
fi
echo "  DB 兼容: target head <= server DB head，无需 migration。"

# ---- d. 从 exact Git object materialize harness + remote runner（不 scp） ----
echo "[4B-0G-R3][remote] d. 从 exact Git object materialize harness + remote runner ..."
mkdir -p "$BENCHMARK_WORKSPACE"
HARNESS_BLOB="$BENCHMARK_WORKSPACE/run_4b_server_benchmark.py"
REMOTE_BLOB="$BENCHMARK_WORKSPACE/run_4b_server_remote.sh"
COMPOSE_TARGET_BLOB="$BENCHMARK_WORKSPACE/docker-compose.4b-target.yml"
git cat-file -p "$HARNESS_SHA:$HARNESS_REL_PATH" > "$HARNESS_BLOB"
git cat-file -p "$HARNESS_SHA:$REMOTE_REL_PATH" > "$REMOTE_BLOB"
git cat-file -p "$HARNESS_SHA:experiments/duplicate_compute_audit/docker-compose.4b-target.yml" > "$COMPOSE_TARGET_BLOB"
if [ ! -s "$COMPOSE_TARGET_BLOB" ]; then
  echo "[4B-0G-R3F2][remote] ERROR: 无法从 HARNESS_SHA exact Git object materialize benchmark override" >&2
  exit 5
fi
HARNESS_FILE_SHA="$(sha256sum "$HARNESS_BLOB" | awk '{print $1}')"
echo "  materialized harness file sha256 = $HARNESS_FILE_SHA"

EXPECTED_HARNESS_FILE_SHA="${PANJI_EXPECTED_HARNESS_FILE_SHA:-}"
if [ -n "$EXPECTED_HARNESS_FILE_SHA" ] && [ "$EXPECTED_HARNESS_FILE_SHA" != "$HARNESS_FILE_SHA" ]; then
  echo "[4B-0G-R3][remote] ERROR: materialized harness file hash 不匹配。" >&2
  exit 5
fi

# ---- d2. 从 exact Git object materialize TARGET_CODE_SHA:backend/app（隔离 one-shot /app/app） ----
echo "[4B-0G-R3][remote] d2. materialize TARGET_CODE_SHA:backend/app → $TARGET_APP_DIR ..."
mkdir -p "$TARGET_APP_DIR"
if ! git archive "$TARGET_CODE_SHA" backend/app | tar -x -C "$TARGET_APP_DIR"; then
  echo "[4B-0G-R3][remote] ERROR: git archive TARGET_CODE_SHA:backend/app 失败" >&2
  exit 5
fi
TARGET_APP_ACTUAL="$TARGET_APP_DIR/backend/app"
if [ ! -d "$TARGET_APP_ACTUAL" ]; then
  echo "[4B-0G-R3][remote] ERROR: materialized target app 缺失 $TARGET_APP_ACTUAL" >&2
  exit 5
fi
# 交叉验证 exact tree 身份（确保 materialize 的是目标 SHA 的真实 tree）
TARGET_TREE_SHA="$(git rev-parse "$TARGET_CODE_SHA:backend/app" 2>/dev/null || true)"
if [ -z "$TARGET_TREE_SHA" ]; then
  echo "[4B-0G-R3][remote] ERROR: 无法解析 TARGET_CODE_SHA:backend/app tree" >&2
  exit 5
fi
EXPECTED_TARGET_APP_TREE_SHA="8f7ff995d69884e9182c89ab1025103f5a389626"
echo "  materialized app tree sha = $TARGET_TREE_SHA"
echo "  expected   app tree sha   = $EXPECTED_TARGET_APP_TREE_SHA"
# R3F hard gate: materialized tree 必须精确等于 TARGET 的 exact app tree。
# 这是方案 C 的核心事实：实际计算代码 = ac9c。tree 错配即 false-green，立即 STOP。
if [ "$TARGET_TREE_SHA" != "$EXPECTED_TARGET_APP_TREE_SHA" ]; then
  echo "[4B-0G-R3F][remote] ERROR: materialized target app tree ($TARGET_TREE_SHA) " \
       "!= expected ($EXPECTED_TARGET_APP_TREE_SHA)，STOP。" >&2
  exit 5
fi
echo "  target app tree identity: 精确匹配 ac9c:backend/app"

# ---- f/g. read-only heavy-task preflight（production ORM，不写） ----
# 用同类短命容器执行 harness --heavy-check（与 benchmark 同源 production app）。
# --no-deps：禁止 benchmark 启停 production dependencies（PostgreSQL/Redis）。
# 路径统一：/root/web_dev→/repo:ro，PANJI_REPO_ROOT=/repo（与正式 run 一致）。
# /app/app 挂载 TARGET 的 exact app（隔离 one-shot），与正式 benchmark 同源。
echo "[4B-0G-R3F2][remote] f/g. read-only heavy-task preflight（after_close_orchestrator 活跃任务）..."
# R3F2: 用最终 Compose override 显式定义 /app/app 指向 materialized target（不再 CLI -v 冲突）。
export PANJI_4B_TARGET_APP_DIR="$(realpath "$TARGET_APP_ACTUAL")"
HEAVY_CHECK_RC=0
docker compose --env-file "$MARKET_ENV" -f "$COMPOSE_PROD" -f "$COMPOSE_LIVE" -f "$COMPOSE_TARGET_BLOB" run --rm --no-deps \
  --name "${CONTAINER_NAME}-preflight" \
  -v "$SERVER_REPO:/repo:ro" \
  -v "$HARNESS_BLOB:/app/benchmark/run_4b_server_benchmark.py:ro" \
  -e PANJI_REPO_ROOT=/repo \
  -e PANJI_BACKEND_ROOT=/app \
  worker-after-close \
  python -m benchmark.run_4b_server_benchmark --heavy-check \
  > "$BENCHMARK_WORKSPACE/heavy_check.json" 2>&1 || HEAVY_CHECK_RC=$?

if [ "$HEAVY_CHECK_RC" -ne 0 ]; then
  echo "[4B-0G-R3][remote] ERROR: heavy-task preflight 判定 BLOCKED（存在活跃 after_close 任务）。" >&2
  cat "$BENCHMARK_WORKSPACE/heavy_check.json" >&2 || true
  exit 11
fi
echo "  heavy-task preflight: clear（无活跃 after_close 重型任务）"

# ---- h/i. 创建一次性 worker-after-close 同类容器，benchmark 作为 MAIN PROCESS ----
# 以 --detach 启动（benchmark 仍是该容器 MAIN PROCESS），runner 并行监控真实进度，
# 最后 docker wait 取退出码；禁止 sleep infinity + docker exec -d。
# --no-deps：禁止 benchmark 启停 production dependencies。
echo "[4B-0G-R3][remote] h/i. 创建一次性 benchmark 容器（benchmark = MAIN PROCESS, detach）..."
echo "  /app/app 来源 = TARGET_CODE_SHA exact app ($TARGET_APP_ACTUAL)，隔离不部署"

mkdir -p "$BENCHMARK_WORKSPACE/output"

docker compose --env-file "$MARKET_ENV" -f "$COMPOSE_PROD" -f "$COMPOSE_LIVE" -f "$COMPOSE_TARGET_BLOB" run -d --no-deps \
  --name "$CONTAINER_NAME" \
  -v "$SERVER_REPO:/repo:ro" \
  -v "$HARNESS_BLOB:/app/benchmark/run_4b_server_benchmark.py:ro" \
  -v "$BENCHMARK_WORKSPACE/output:/app/benchmark/output" \
  -e PANJI_REPO_ROOT=/repo \
  -e PANJI_BACKEND_ROOT=/app \
  -e PANJI_BENCHMARK_HARNESS_SHA="$HARNESS_SHA" \
  -e PANJI_SERVER_REPO_HEAD="$SERVER_REPO_HEAD" \
  -e PANJI_LIVE_RUNTIME_SHA="$LIVE_RUNTIME_SHA" \
  -e PANJI_RUNTIME_GIT_SHA="$RUNTIME_GIT_SHA" \
  -e PANJI_TARGET_CODE_SHA="$TARGET_CODE_SHA" \
  -e PANJI_TARGET_APP_TREE_SHA="$TARGET_TREE_SHA" \
  -e PANJI_4B_TARGET_APP_DIR="$PANJI_4B_TARGET_APP_DIR" \
  -e PANJI_4B_OUTPUT_DIR=/app/benchmark/output/4B-server-db \
  --entrypoint python \
  worker-after-close \
  -m benchmark.run_4b_server_benchmark

# ---- i2. 资源契约证据：比较 one-shot 与常驻 worker-after-close 的 resource envelope，
#          并核验实际 /app/app mount 来源（R3F：方案 C 核心事实 = 实际计算代码 = ac9c）。
# mismatch → STOP（不入 compute）。
echo "[4B-0G-R3F][remote] i2. 资源契约 + /app/app mount 核验（one-shot vs worker-after-close）..."
RUNTIME_CONTRACT="$BENCHMARK_WORKSPACE/output/4B-server-db/runtime_contract.json"
# R3F1: runner 自己建立 evidence contract 目录，避免依赖后台容器异步创建 4B-server-db/
# 子目录导致的 open() 时序竞态（FileNotFoundError → compute 前崩溃）。
mkdir -p "$(dirname "$RUNTIME_CONTRACT")"
docker inspect trading-worker-after-close "$CONTAINER_NAME" >/dev/null 2>&1 || {
  echo "[4B-0G-R3F][remote] ERROR: 无法 inspect trading-worker-after-close 或 benchmark 容器" >&2
  exit 12
}
# 期望 /app/app 的 effective source = materialized target app 的 realpath。
# 命令行 -v 覆盖 service live mount；此处 fail-closed 实测确认最终结果。
EXPECTED_APP_MOUNT_SOURCE="$(realpath "$TARGET_APP_ACTUAL")"
# R3F2: 用 `|| CONTRACT_RC=$?` 捕获 Python 非零退出，避免 set -e 在赋值前提前触发 EXIT trap，
# 导致 FAIL 时未打印 contract / 未打包 evidence 就 cleanup。
CONTRACT_RC=0
python3 - "$CONTAINER_NAME" "$RUNTIME_CONTRACT" "$EXPECTED_APP_MOUNT_SOURCE" <<'PY' || CONTRACT_RC=$?
import sys, json, subprocess, os
cn, out, expected_src = sys.argv[1], sys.argv[2], sys.argv[3]
def inspect(name):
    raw = subprocess.check_output(["docker","inspect",name], text=True)
    return json.loads(raw)[0]
base = inspect("trading-worker-after-close")
one  = inspect(cn)
def hostcfg(c): return c.get("HostConfig", {})
base_h, one_h = hostcfg(base), hostcfg(one)
base_net = list((base.get("NetworkSettings",{}) or {}).get("Networks",{}) or {})
one_net  = list((one.get("NetworkSettings",{}) or {}).get("Networks",{}) or {})

# ---- R3F: 实际 /app/app mount 核验 ----
# 仅看 Destination == "/app/app" 的 mount（不依赖 Mounts 中的命名类型，直接比字段）。
app_mounts = [
    m for m in (one.get("Mounts") or [])
    if (m.get("Destination") or m.get("Target")) == "/app/app"
]
n_app = len(app_mounts)
if n_app != 1:
    # 多于或少于 1 个 effective /app/app mount 都非法：
    #  - 0 个：/app/app 来自镜像层（= 常驻 live 的 app，即 eff，非 target）→ false-green
    #  - >1 个：mount precedence 不明确，无法保证 source → STOP
    target_mount = {
        "destination": "/app/app",
        "expected_source": expected_src,
        "actual_source": None,
        "readonly": False,
        "exact_match": False,
        "effective_app_mount_count": n_app,
        "error": f"effective /app/app mount 数量={n_app}（必须恰好 1 个）",
    }
    mount_ok = False
else:
    m = app_mounts[0]
    src = m.get("Source") or m.get("Name") or ""
    rw = not bool(m.get("RW", False))
    exact = (os.path.realpath(src) == os.path.realpath(expected_src)) and rw
    target_mount = {
        "destination": "/app/app",
        "expected_source": expected_src,
        "actual_source": src,
        "readonly": rw,
        "exact_match": bool(exact),
        "effective_app_mount_count": 1,
        "error": None if exact else (
            f"source 不匹配：actual={src} expected={expected_src}"
            if not (os.path.realpath(src) == os.path.realpath(expected_src))
            else "mount 非 readonly（RW=true）"
        ),
    }
    mount_ok = bool(exact)

contract = {
    "baseline_container": "trading-worker-after-close",
    "benchmark_container": cn,
    "image_match": base["Image"] == one["Image"],
    "memory_match": base_h.get("Memory") == one_h.get("Memory"),
    "nanocpus_match": base_h.get("NanoCpus") == one_h.get("NanoCpus"),
    "pidslimit_match": base_h.get("PidsLimit") == one_h.get("PidsLimit"),
    "network_compatible": (set(base_net) & set(one_net)) != set() or base_net == one_net,
    "target_app_mount": target_mount,
    "baseline": {"image": base["Image"], "memory": base_h.get("Memory"),
                 "nanocpus": base_h.get("NanoCpus"), "pidslimit": base_h.get("PidsLimit"),
                 "networks": base_net},
    "benchmark": {"image": one["Image"], "memory": one_h.get("Memory"),
                  "nanocpus": one_h.get("NanoCpus"), "pidslimit": one_h.get("PidsLimit"),
                  "networks": one_net},
    "note": "benchmark one-shot 应继承 worker-after-close 同类 resource envelope；"
            "mismatch 表示 cgroup 不对齐，CPU/RSS baseline 不可信。"
            "target_app_mount.exact_match 为方案 C 核心事实：实际 /app/app 必须精确指向 "
            "materialized target app（ac9c），且 readonly；否则性能结果对象错误（false-green）。",
}
envelope_ok = (contract["image_match"] and contract["memory_match"] and contract["nanocpus_match"]
      and contract["pidslimit_match"] and contract["network_compatible"])
contract["envelope_aligned"] = bool(envelope_ok)
# 总 gate：envelope 对齐 AND /app/app mount 精确匹配
contract["accept_run"] = bool(envelope_ok and mount_ok)
with open(out, "w", encoding="utf-8") as f:
    json.dump(contract, f, ensure_ascii=False, indent=2)
if not contract["accept_run"]:
    print("[4B-0G-R3F][remote] ERROR: 契约不对齐或 /app/app mount 非 target，STOP before full compute",
          file=sys.stderr)
    print(f"[4B-0G-R3F][remote]   envelope_aligned={envelope_ok} target_app_mount.exact_match={mount_ok}",
          file=sys.stderr)
    sys.exit(13)
print("[4B-0G-R3F][remote] 资源契约对齐 + /app/app mount 精确指向 target app（readonly）: PASS")
PY
if [ "$CONTRACT_RC" -ne 0 ]; then
  # mount mismatch 属于 false-green 风险：立即 kill 容器，该 run 不作为 benchmark evidence。
  # R3F2: FAIL 时先把完整 contract（含 target_app_mount 的 actual_source/RW/mount_count/error）
  # 打印到 stderr 并打包 evidence archive，避免下次盲诊。
  echo "[4B-0G-R3F][remote] 契约/mount gate 失败，立即 kill benchmark 容器。" >&2
  if [ -f "$RUNTIME_CONTRACT" ]; then
    echo "[4B-0G-R3F][remote] runtime_contract.json:" >&2
    cat "$RUNTIME_CONTRACT" >&2 || true
    tar -czf "$EVIDENCE_ARCHIVE" -C "$BENCHMARK_WORKSPACE" output 2>/dev/null || true
  fi
  docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
  exit "$CONTRACT_RC"
fi

# ---- j. 监控真实业务 progress + docker stats（并行，非 generic timeout） ----
PROGRESS_FILE="$BENCHMARK_WORKSPACE/output/4B-server-db/progress.jsonl"
RESOURCE_SAMPLES="$BENCHMARK_WORKSPACE/output/4B-server-db/resource_samples.jsonl"
LAST_PROCESSED=0
LAST_TS="$(date +%s)"
STALL_SEC=0
MAX_STALL_SEC="${PANJI_MAX_STALL_SEC:-1800}"   # 默认 30 分钟无真实进展判 stall

echo "[4B-0G-R3][remote] j. 监控真实业务进度 + 资源采样 ..."
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
      echo "[4B-0G-R3][remote] progress: $CUR_PROCESSED / 5293  ($(date))"
    else
      STALL_SEC=$(( CUR_TS - LAST_TS ))
      if [ "$STALL_SEC" -ge "$MAX_STALL_SEC" ]; then
        echo "[4B-0G-R3][remote] ERROR: 超过 ${MAX_STALL_SEC}s 无真实业务进展，判定 stall。" >&2
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
echo "[4B-0G-R3][remote] benchmark 容器退出码 = $BENCHMARK_RC"

if [ -f "$PROGRESS_FILE" ]; then
  LAST_LINE="$(tail -n1 "$PROGRESS_FILE" 2>/dev/null || true)"
  echo "[4B-0G-R3][remote] final progress: $LAST_LINE"
fi

# ---- k. 打包脱敏 evidence archive（保留，待 control 取回） ----
echo "[4B-0G-R3][remote] k. 打包 evidence archive ..."
EVIDENCE_DIR="$BENCHMARK_WORKSPACE/output/4B-server-db"
if [ -d "$EVIDENCE_DIR" ]; then
  tar -czf "$EVIDENCE_ARCHIVE" -C "$EVIDENCE_DIR" . \
    --exclude='*.py' --exclude='__pycache__' 2>/dev/null || {
      echo "[4B-0G-R3][remote] WARN: evidence 打包失败" >&2
    }
  echo "  archive: $EVIDENCE_ARCHIVE"
  ls -la "$EVIDENCE_DIR" 2>/dev/null || true
fi

# ---- l. 退出码即结论 ----
if [ "$BENCHMARK_RC" -ne 0 ]; then
  echo "[4B-0G-R3][remote] FAIL: benchmark 非零退出（$BENCHMARK_RC）。" >&2
  exit "$BENCHMARK_RC"
fi

echo "[4B-0G-R3][remote] DONE. evidence archive 保留于 $EVIDENCE_ARCHIVE（由 control 取回后删除）。"
echo "EVIDENCE_ARCHIVE_PATH=$EVIDENCE_ARCHIVE"
exit 0
