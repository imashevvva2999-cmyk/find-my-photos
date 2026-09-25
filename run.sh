#!/bin/bash
# Starts the site at http://127.0.0.1:8000 — reachable only from this Mac, not the internet.
# Runs two programs: the web server and the background worker that processes photos.
# Stop both with Ctrl+C. The database keeps running; stop it with ./scripts/postgres.sh stop
set -euo pipefail
cd "$(dirname "$0")"
PY=.venv/bin/python

# 1. Python environment; libraries are installed only when requirements.txt changes
if [ ! -d .venv ]; then
  echo "Creating Python environment (first run only)…"
  python3 -m venv .venv
fi
STAMP=.venv/.requirements.sha256
HASH=$(shasum -a 256 requirements.txt | cut -d' ' -f1)
if [ ! -f "$STAMP" ] || [ "$(cat "$STAMP")" != "$HASH" ]; then
  echo "Installing the exact library versions from requirements.txt…"
  .venv/bin/pip install -q -r requirements.txt
  echo "$HASH" > "$STAMP"
fi

# 2. Face models from the OpenCV model zoo, verified by SHA-256
ZOO=https://github.com/opencv/opencv_zoo/raw/main/models
fetch_model() {  # name, url, sha256
  local file="models/$1"
  if [ ! -f "$file" ]; then
    mkdir -p models
    curl -fL -o "$file.part" "$2" && mv "$file.part" "$file"
  fi
  if [ "$(shasum -a 256 "$file" | cut -d' ' -f1)" != "$3" ]; then
    echo "Model $1 does not match its expected checksum; deleting it. Run ./run.sh again to re-download."
    rm -f "$file"
    exit 1
  fi
}
fetch_model face_detection_yunet_2023mar.onnx "$ZOO/face_detection_yunet/face_detection_yunet_2023mar.onnx" \
  8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4
fetch_model face_recognition_sface_2021dec.onnx "$ZOO/face_recognition_sface/face_recognition_sface_2021dec.onnx" \
  0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79

# 3. Secrets in .env (mode 600): session/link key and the admin password hash
touch .env && chmod 600 .env
if ! grep -q '^SECRET_KEY=' .env; then
  $PY -c "import secrets; from app.envfile import update_env_file; update_env_file({'SECRET_KEY': secrets.token_hex(32)})"
fi
$PY scripts/set_admin_password.py --convert           # an old plain-text password becomes a hash
grep -q '^ADMIN_PASSWORD_HASH=' .env || $PY scripts/set_admin_password.py --generate

# 4. Database: this project's own PostgreSQL server, then pending migrations
./scripts/postgres.sh setup
./scripts/postgres.sh start
$PY -m app.migrate

# 5. Background worker + web server
$PY -m app.worker &
WORKER=$!
trap 'kill -TERM $WORKER 2>/dev/null; wait $WORKER 2>/dev/null || true' EXIT INT TERM

echo ""
echo "  Admin area:    http://127.0.0.1:${PORT:-8000}/admin"
echo "  Health check:  http://127.0.0.1:${PORT:-8000}/readyz"
echo "  Stop with Ctrl+C"
echo ""
.venv/bin/uvicorn app.main:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}" --no-access-log --workers "${WEB_WORKERS:-1}"
