#!/usr/bin/env bash
set -Eeuo pipefail

# Intended as SSH forced command.
# SSH_ORIGINAL_COMMAND must contain only the 40-character SHA.

SHA="${SSH_ORIGINAL_COMMAND:-${1:-}}"

if [[ ! "$SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "invalid target sha" >&2
  exit 2
fi

exec sudo /usr/local/lib/panji-deploy/panji-deploy-dev.sh "$SHA"
