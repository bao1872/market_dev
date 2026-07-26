#!/usr/bin/env bash
set -Eeuo pipefail

TARGET_SHA="${1:-}"
DEPLOY_DIR="${PANJI_DEPLOY_DIR:-/opt/panji-deploy}"
LIVE_DIR="${PANJI_LIVE_DIR:-/opt/panji-live}"
LOCK="${PANJI_DEPLOY_LOCK:-/var/lock/panji-deploy.lock}"
REPO_URL="${PANJI_REPO_URL:-git@github.com:bao1872/market_dev.git}"

die() {
  echo "[ERROR] $*" >&2
  exit 1
}

log() {
  printf '[%s] %s\n' "$(date -Is)" "$*"
}

[[ "$TARGET_SHA" =~ ^[0-9a-f]{40}$ ]] || die "invalid SHA"

exec 9>"$LOCK"
flock -n 9 || die "deployment already running"

if [[ ! -d "$DEPLOY_DIR/.git" ]]; then
  git clone "$REPO_URL" "$DEPLOY_DIR"
fi

git -C "$DEPLOY_DIR" status --porcelain | grep -q . \
  && die "$DEPLOY_DIR is dirty"

git -C "$DEPLOY_DIR" fetch origin dev --prune

REMOTE_DEV="$(git -C "$DEPLOY_DIR" rev-parse origin/dev)"
git -C "$DEPLOY_DIR" merge-base --is-ancestor "$TARGET_SHA" "$REMOTE_DEV" \
  || die "target is not contained in origin/dev"

git -C "$DEPLOY_DIR" cat-file -e "${TARGET_SHA}^{commit}" \
  || die "target commit unavailable"

PREVIOUS_SHA=""
if [[ -f "$LIVE_DIR/RUNTIME_SHA" ]]; then
  PREVIOUS_SHA="$(tr -d '[:space:]' < "$LIVE_DIR/RUNTIME_SHA")"
fi

git -C "$DEPLOY_DIR" checkout --detach "$TARGET_SHA"

BASE_SHA="${PREVIOUS_SHA:-$TARGET_SHA}"
python3 "$DEPLOY_DIR/scripts/deploy/classify_deployment.py" \
  --base "$BASE_SHA" \
  --target "$TARGET_SHA" \
  --output /tmp/panji-deploy-classification.json

MODE="$(python3 -c \
  'import json;print(json.load(open("/tmp/panji-deploy-classification.json"))["mode"])')"

if [[ "$MODE" == "blocked" ]]; then
  echo "[BLOCKED] High-risk files require TRAE CN manual handling."
  cat /tmp/panji-deploy-classification.json
  exit 20
fi

if [[ "$MODE" == "none" ]]; then
  log "No runtime files changed."
  exit 0
fi

[[ -x "$DEPLOY_DIR/scripts/deploy_live_runtime.sh" ]] \
  || die "existing deploy_live_runtime.sh not found or not executable"

export COMPOSE_PARALLEL_LIMIT=1
export NODE_OPTIONS="${NODE_OPTIONS:---max-old-space-size=1536}"
export PYTHONDONTWRITEBYTECODE=1

(
  cd "$DEPLOY_DIR"
  scripts/deploy_live_runtime.sh
)

"$DEPLOY_DIR/scripts/deploy/panji-verify-runtime.sh" "$TARGET_SHA"

log "Deployment succeeded: $TARGET_SHA"
