#!/bin/bash
# Render start command: the background worker and the web server in one service, so they share
# the photo disk. If either stops, the script exits and Render restarts the whole service.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p "${DATA_DIR:-/var/data}"
python -m app.worker &
# --proxy-headers: rate limits see the visitor's address forwarded by Render/Vercel, not the proxy's.
uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-10000}" --no-access-log \
  --proxy-headers --forwarded-allow-ips='*' --workers 1 &
wait -n
echo "A process stopped; exiting so Render restarts the service."
exit 1
