#!/usr/bin/env bash
# panji-verify-deploy.sh — 在 panji-prod 的 /root/web_dev_verify 内部署 V2.1 验证栈（DS-111）。
#
# [Corrective Pass 2 P0-8] 这是**远程实现脚本**，由本地控制脚本 scripts/ops/panji-verify-deploy
# 通过 panji-prod-ssh 调用。本脚本内部**不得再调用 panji-prod-ssh**（已在远程，避免自环）。
#
# [Corrective Pass 3] 修正 CP2 的以下缺陷：
#   P0-A  compose 无 build: 段，`up --build` 无效 → 改用 Live Mount（只读挂载已 checkout 的代码
#         + RUNTIME_SHA），复用既有正式镜像作为依赖底座（CHANGE-20260724-004 既有约定）。
#   P0-B  探针路径错误：不存在 /v1/version.runtime_git_sha 这个端点。真实合同是
#         `GET /v1/version` 返回 JSON，字段 runtime_git_sha（backend/app/api/health.py:109）。
#   P0-C  DB 强校验缺失：CP2 只做 `grep bz_stock_verify` 包含判断 → 现在解析 DATABASE_URL 的
#         database 段做**全等**比较，并在应用连上后执行 SELECT current_database() 二次确认。
#   P0-D  SHA 强校验缺失：CP2 未校验 /root/web_dev_verify 的 HEAD → 现在要求
#         `git rev-parse HEAD` 与目标 SHA 前缀一致，且工作区 clean。
#   P0-E  无启动等待：CP2 起栈后立即探针，必然抖动 → 现在带超时轮询等待。
#   P0-F  网络名硬编码 market-dev-default → 现在实测 trading-postgres 的真实网络名后注入。
#
# 用法：scripts/deploy/panji-verify-deploy.sh <SHA> [env_file]
#   env_file 默认 market.verify.env（位于 /root/web_dev_verify 根）
#
# 失败保留策略（DS-112）：
#   探针/校验失败时**不自动 down**，保留容器与日志供诊断；脚本打印诊断命令并非 0 退出。
#   仅"起栈前"的静态校验失败才不会产生任何容器。
#
# 退出码：0=部署成功；非0=拒绝/失败（见各分支）

set -euo pipefail

if [ "$#" -lt 1 ]; then
  echo "usage: $0 <SHA> [env_file]" >&2
  exit 2
fi

SHA="$1"
ENV_FILE="${2:-market.verify.env}"
DB_NAME="bz_stock_verify_${SHA}"
PROJECT="panji-verify"
COMPOSE_FILE="docker-compose.verify.yml"
VERIFY_CODE_DIR="/root/web_dev_verify"
# [CHANGE-20260806-CP4A-Amendment] 运行时代码 SHA 放在 repo 外的专用目录（避免写进 repo 破坏
# “工作区 clean”强校验）。默认 /root/.panji-verify/runtime，可用 VERIFY_RUNTIME_DIR 覆盖。
VERIFY_RUNTIME_DIR="${VERIFY_RUNTIME_DIR:-/root/.panji-verify/runtime}"
PG_CONTAINER="${PG_CONTAINER:-trading-postgres}"
# [CHANGE-20260806-CP4A-Amendment] PG 用户可配置，默认与 create_verify_database.sh 一致（bz）
VERIFY_PG_USER="${VERIFY_PG_USER:-bz}"
READY_TIMEOUT="${READY_TIMEOUT:-180}"

log() { echo "panji-verify-deploy: $*"; }
die() { echo "panji-verify-deploy: $*" >&2; exit "${2:-1}"; }

# ---------------------------------------------------------------- 0. SHA 形态
if ! echo "$DB_NAME" | grep -Eq '^bz_stock_verify_[0-9a-f]{7,40}$'; then
  die "非法 SHA '$SHA'（DB 名必须匹配 bz_stock_verify_<7..40 hex>）" 3
fi

# ------------------------------------------------- 1. 必须在验证代码目录内执行
if [ "$(pwd -P)" != "$VERIFY_CODE_DIR" ]; then
  die "必须在 $VERIFY_CODE_DIR 内执行（当前 $(pwd -P)）" 8
fi
if [ ! -f "$COMPOSE_FILE" ] || [ ! -d "backend/alembic" ]; then
  die "缺少 $COMPOSE_FILE 或 backend/alembic，目录不是有效的验证代码目录" 8
fi

# ------------------------------------------------- 2. P0-D 代码 SHA 强校验
HEAD_SHA="$(git rev-parse HEAD)"
if [ "${HEAD_SHA:0:${#SHA}}" != "$SHA" ]; then
  die "代码目录 HEAD=$HEAD_SHA 与目标 SHA=$SHA 不一致，拒绝部署" 4
fi
if [ -n "$(git status --porcelain)" ]; then
  die "代码目录存在未提交改动（工作区不 clean），拒绝部署" 4
fi
log "代码 SHA 校验通过 HEAD=$HEAD_SHA"

# [CHANGE-20260806-CP4A-Amendment] RUNTIME_SHA 由本脚本按 HEAD 生成，写入 **repo 外** 的
# VERIFY_RUNTIME_DIR（Live Mount 依赖它决定 /v1/version.runtime_git_sha）。不再写进 repo 目录，
# 避免与上方“工作区 clean”强校验自相矛盾（写进 repo 会使下次部署检测到未提交改动）。
mkdir -p "$VERIFY_RUNTIME_DIR"
printf '%s' "$SHA" > "$VERIFY_RUNTIME_DIR/RUNTIME_SHA"

# ------------------------------------------------- 3. env 文件与 DB 强校验
[ -f "$ENV_FILE" ] || die "环境文件 $ENV_FILE 不存在" 5

# shellcheck disable=SC2016
ENV_DB_URL="$(grep -E '^[[:space:]]*DATABASE_URL=' "$ENV_FILE" | tail -n1 | cut -d= -f2- | tr -d '"'"'"' ')"
[ -n "$ENV_DB_URL" ] || die "环境文件缺少 DATABASE_URL" 6

# P0-C：解析 URL 的 database 段做全等比较（不是 grep 包含）
ENV_DB_NAME="$(python3 - "$ENV_DB_URL" <<'PY'
import sys
from urllib.parse import urlsplit
print(urlsplit(sys.argv[1]).path.lstrip("/").split("?")[0])
PY
)"
if [ "$ENV_DB_NAME" != "$DB_NAME" ]; then
  die "DATABASE_URL 的数据库名='$ENV_DB_NAME'，期望全等 '$DB_NAME'，拒绝部署" 6
fi
log "DATABASE_URL 数据库名全等校验通过 db=$ENV_DB_NAME"

grep -qE '^[[:space:]]*APP_ENV=verification[[:space:]]*$' "$ENV_FILE" \
  || die "环境文件 APP_ENV 非 verification，拒绝" 7

# ------------------------------------------------- 4. 验证库必须已存在
# [CHANGE-20260806-CP4A-Amendment] 用 VERIFY_PG_USER（默认 bz），不再硬编码 -U postgres
# [CHANGE-20260806-005 Phase 7] 连接 pg_database 必须显式 -d postgres：否则 psql 默认连到与
#   用户名同名的库（如 bz），该库不存在时 SELECT 1 FROM pg_database 报错，误判"验证库不存在"。
docker exec -i "$PG_CONTAINER" psql -U "$VERIFY_PG_USER" -d postgres -tAc \
  "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" 2>/dev/null | grep -q '^1$' \
  || die "验证库 $DB_NAME 不存在（应先由 create_verify_database.sh 创建）" 6

# ------------------------------------------------- 5. P0-F 实测 PG 网络名
PG_NETWORK="$(docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{"\n"}}{{end}}' \
  "$PG_CONTAINER" 2>/dev/null | head -n1)"
[ -n "$PG_NETWORK" ] || die "无法探测 $PG_CONTAINER 所在 Docker 网络" 9
docker network inspect "$PG_NETWORK" >/dev/null 2>&1 \
  || die "探测到的网络 '$PG_NETWORK' 不存在" 9
log "PostgreSQL 网络实测=$PG_NETWORK"

# ------------------------------------------------- 6. 镜像底座必须已存在
VERIFY_BACKEND_IMAGE="${VERIFY_BACKEND_IMAGE:-$(docker inspect -f '{{.Config.Image}}' market-dev-backend 2>/dev/null || echo "")}"
VERIFY_FRONTEND_IMAGE="${VERIFY_FRONTEND_IMAGE:-$(docker inspect -f '{{.Config.Image}}' market-dev-frontend 2>/dev/null || echo "")}"
[ -n "$VERIFY_BACKEND_IMAGE" ] || die "未找到 backend 镜像底座（设置 VERIFY_BACKEND_IMAGE 或确保 market-dev-backend 容器存在）" 9
[ -n "$VERIFY_FRONTEND_IMAGE" ] || die "未找到 frontend 镜像底座（设置 VERIFY_FRONTEND_IMAGE 或确保 market-dev-frontend 容器存在）" 9
docker image inspect "$VERIFY_BACKEND_IMAGE" >/dev/null 2>&1 || die "backend 镜像 $VERIFY_BACKEND_IMAGE 不存在" 9
docker image inspect "$VERIFY_FRONTEND_IMAGE" >/dev/null 2>&1 || die "frontend 镜像 $VERIFY_FRONTEND_IMAGE 不存在" 9
log "镜像底座 backend=$VERIFY_BACKEND_IMAGE frontend=$VERIFY_FRONTEND_IMAGE"

# ------------------------------------------------- 7. 前端产物必须已构建
[ -f "$VERIFY_CODE_DIR/frontend/dist/index.html" ] \
  || die "缺少 frontend/dist/index.html（请先在 $VERIFY_CODE_DIR/frontend 执行 npm ci && npm run build）" 9

export VERIFY_CODE_DIR VERIFY_RUNTIME_DIR VERIFY_PG_NETWORK="$PG_NETWORK" VERIFY_BACKEND_IMAGE VERIFY_FRONTEND_IMAGE

COMPOSE=(docker compose -p "$PROJECT" --env-file "$ENV_FILE" -f "$COMPOSE_FILE")

# ------------------------------------------------- 8. 静态 compose 校验
"${COMPOSE[@]}" config >/dev/null || die "compose 配置静态验证失败" 9

# ------------------------------------------------- 9. 起栈（Live Mount，无 --build）
"${COMPOSE[@]}" up -d

diagnose() {
  echo "--- 诊断（容器已保留，未自动 down）---" >&2
  echo "  ${COMPOSE[*]} ps" >&2
  echo "  ${COMPOSE[*]} logs --tail=200 verify-backend" >&2
}

# ------------------------------------------------- 10. P0-E 启动等待轮询
# [CHANGE-20260806-CP4A-Amendment] 探针用 Python 标准库 urllib，不假定 backend 镜像含 curl。
# _probe <path>：在 verify-backend 内用 python urllib GET http://127.0.0.1:8000<path>，
# 200 且返回空正文（health）或正文非空（version）视为通过。
_probe() {
  local path="$1"
  "${COMPOSE[@]}" exec -T verify-backend python3 -c "import sys,urllib.request;urllib.request.urlopen('http://127.0.0.1:8000${path}',timeout=5);sys.exit(0)" >/dev/null 2>&1
}
log "等待 verify-backend 就绪（超时 ${READY_TIMEOUT}s）..."
deadline=$(( $(date +%s) + READY_TIMEOUT ))
ready=0
while [ "$(date +%s)" -lt "$deadline" ]; do
  if _probe "/v1/health"; then
    ready=1
    break
  fi
  sleep 3
done
if [ "$ready" -ne 1 ]; then
  diagnose
  die "verify-backend 在 ${READY_TIMEOUT}s 内未通过 /v1/health" 11
fi
log "/v1/health 通过"

# ------------------------------------------------- 11. P0-B 运行时 SHA 校验
VERSION_JSON="$("${COMPOSE[@]}" exec -T verify-backend \
  python3 -c 'import sys,urllib.request;print(urllib.request.urlopen("http://127.0.0.1:8000/v1/version",timeout=10).read().decode())' 2>/dev/null || echo "")"
[ -n "$VERSION_JSON" ] || { diagnose; die "GET /v1/version 无响应" 10; }

RUNTIME_SHA="$(printf '%s' "$VERSION_JSON" | python3 -c \
  'import sys,json;print(json.load(sys.stdin).get("runtime_git_sha",""))' 2>/dev/null || echo "")"
DEPLOY_MODE="$(printf '%s' "$VERSION_JSON" | python3 -c \
  'import sys,json;print(json.load(sys.stdin).get("deployment_mode",""))' 2>/dev/null || echo "")"
log "目标SHA=$SHA 运行时SHA=$RUNTIME_SHA deployment_mode=$DEPLOY_MODE"

if [ "$RUNTIME_SHA" != "$SHA" ]; then
  diagnose
  die "运行时 SHA 不匹配（期望 $SHA，实际 '$RUNTIME_SHA'）" 10
fi
if [ "$DEPLOY_MODE" != "live" ]; then
  diagnose
  die "deployment_mode='$DEPLOY_MODE'，期望 live（RUNTIME_SHA 未被挂载）" 10
fi

# ------------------------------------------------- 12. P0-C 应用侧当前库二次确认
CURRENT_DB="$("${COMPOSE[@]}" exec -T verify-backend python3 - <<'PY' 2>/dev/null || echo ""
import asyncio, os
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

async def main() -> None:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.connect() as conn:
        row = (await conn.execute(text("SELECT current_database()"))).scalar_one()
        print(row)
    await engine.dispose()

asyncio.run(main())
PY
)"
CURRENT_DB="$(printf '%s' "$CURRENT_DB" | tr -d '[:space:]')"
if [ "$CURRENT_DB" != "$DB_NAME" ]; then
  diagnose
  die "应用侧 current_database()='$CURRENT_DB'，期望 '$DB_NAME'，立即停止（DS-110 fail-closed）" 6
fi
log "current_database() 二次确认通过 db=$CURRENT_DB"

# ------------------------------------------------- 13. ready 探针（Python stdlib，不依赖 curl）
if ! _probe "/v1/health/ready"; then
  diagnose
  die "/v1/health/ready 失败" 12
fi
log "/v1/health/ready 通过"

log "部署完成 (DS-111). 访问经 SSH Tunnel: ssh -N -L 8080:127.0.0.1:8080 panji-prod"
