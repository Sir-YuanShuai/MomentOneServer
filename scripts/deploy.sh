#!/usr/bin/env bash
# Manual deployment helper.
#
# The CI workflow (.github/workflows/ci.yml) deploys automatically when code
# is merged into main. Use this script only for emergency manual deploys or
# when you need to redeploy the current image without a new push.
#
# Usage:
#   ./scripts/deploy.sh
#
# Required environment variables (or ~/.moment-one-deploy.env):
#   SERVER_HOST         # server IP or domain
#   SERVER_USER         # SSH user
#   SERVER_PORT         # SSH port (defaults to 22)
#   SERVER_DEPLOY_PATH  # absolute path on server, e.g. /opt/moment-one
#
# SSH auth uses your local SSH agent / ~/.ssh config; no key file needed here.

set -euo pipefail

ENV_FILE="${HOME}/.moment-one-deploy.env"
if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
fi

: "${SERVER_HOST:?Set SERVER_HOST (or put it in ${ENV_FILE})}"
: "${SERVER_USER:?Set SERVER_USER (or put it in ${ENV_FILE})}"
: "${SERVER_DEPLOY_PATH:?Set SERVER_DEPLOY_PATH (or put it in ${ENV_FILE})}"
SERVER_PORT="${SERVER_PORT:-22}"

echo "==> Deploying to ${SERVER_USER}@${SERVER_HOST}:${SERVER_PORT} (${SERVER_DEPLOY_PATH})"

ssh -p "${SERVER_PORT}" "${SERVER_USER}@${SERVER_HOST}" "
  set -euo pipefail
  cd '${SERVER_DEPLOY_PATH}'
  echo '==> Pulling latest image'
  for attempt in 1 2 3 4 5; do
    if docker compose -f compose.prod.yml pull api; then
      break
    fi
    if [ \$attempt -eq 5 ]; then
      echo '==> Image pull failed after 5 attempts'
      exit 1
    fi
    echo '==> Image pull failed; retrying in 10s'
    sleep 10
  done
  echo '==> Applying database migrations'
  docker compose -f compose.prod.yml run --rm api alembic upgrade head
  echo '==> Restarting api container'
  docker compose -f compose.prod.yml up -d api
  echo '==> Health check'
  for attempt in \$(seq 1 30); do
    if docker compose -f compose.prod.yml exec -T api python -c \"import json, urllib.request; data = json.load(urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)); assert data.get('status') == 'ok', data\"; then
      echo '==> Deployment healthy'
      exit 0
    fi
    sleep 1
  done
  echo '==> Health check failed, recent logs:'
  docker compose -f compose.prod.yml logs --tail=100 api
  exit 1
"

echo "==> Done"
