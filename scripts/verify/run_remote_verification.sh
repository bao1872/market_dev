#!/usr/bin/env bash
# Target-SHA remote runner. Secrets stay in an attempt-scoped 0600 env file.
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "usage: $0 <FULL_SHA> <REGISTERED_PLAN>" >&2
  exit 80
fi
SHA="$1"
PLAN="$2"
echo "$SHA" | grep -Eq '^[0-9a-f]{40}$' || exit 80
[ "$PLAN" = "full-closure" ] || exit 80
[ "$(git rev-parse HEAD)" = "$SHA" ] || exit 20
[ -z "$(git status --porcelain)" ] || exit 20

ATTEMPT_DIR="/root/.panji-verify/${SHA}"
ENV_FILE="${ATTEMPT_DIR}/market.verify.env"
cleanup_sensitive() {
  rm -f "$ENV_FILE" "${ATTEMPT_DIR}/runtime/RUNTIME_SHA"
  rmdir "${ATTEMPT_DIR}/runtime" "$ATTEMPT_DIR" 2>/dev/null || true
  docker image rm "panji-verify-test:${SHA}" >/dev/null 2>&1 || true
}
trap cleanup_sensitive EXIT INT TERM

DOCKER_BUILDKIT=1 docker build --target verification \
  --build-arg "GIT_SHA=${SHA}" \
  -t "panji-verify-test:${SHA}" backend
python scripts/verify/prepare_verify_environment.py \
  --target-sha "$SHA" --output "$ENV_FILE" >/dev/null
python scripts/verify/verify_attempt.py \
  --target-sha "$SHA" \
  --compose-project "panji-verify-${SHA}" \
  --env-file "$ENV_FILE" \
  --compose-file docker-compose.verify.yml \
  --plan "scripts/verify/plans/${PLAN}.json"
