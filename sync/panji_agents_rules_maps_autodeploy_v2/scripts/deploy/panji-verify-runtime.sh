#!/usr/bin/env bash
set -Eeuo pipefail

EXPECTED="${1:-}"
[[ "$EXPECTED" =~ ^[0-9a-f]{40}$ ]] || exit 2

VERSION_URL="${PANJI_VERSION_URL:-http://127.0.0.1:8000/version}"
HEALTH_URL="${PANJI_HEALTH_URL:-http://127.0.0.1:8000/api/v1/health}"
FRONTEND_URL="${PANJI_FRONTEND_URL:-http://127.0.0.1/}"

VERSION="$(curl -fsS "$VERSION_URL")"
echo "$VERSION"
grep -q "$EXPECTED" <<< "$VERSION"

curl -fsS "$HEALTH_URL" >/dev/null

curl -fsS "$FRONTEND_URL" > /tmp/panji-frontend.html
! grep -qi "welcome to nginx" /tmp/panji-frontend.html

echo "[PASS] runtime verified: $EXPECTED"
