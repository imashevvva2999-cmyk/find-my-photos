#!/bin/bash
# This project's own PostgreSQL server: data in data/postgres, port 5439, reachable only from this Mac.
# (Port 5433 is already used by something else on this Mac.) It uses the PostgreSQL programs inside
# Postgres.app but NOT Postgres.app's own server or databases, so other projects are not affected.
#
#   ./scripts/postgres.sh setup    create (first time) or repair: server, database, login, .env entry
#   ./scripts/postgres.sh start | stop | status
#
# Secrets: the app's login is DATABASE_URL in .env. The database superuser password is kept in
# data/.pg-superuser (mode 600) and is never loaded by the web app.
set -euo pipefail
umask 077   # every file this script creates (secrets, .env) is private from the first byte
cd "$(dirname "$0")/.."

PG_BIN="${PG_BIN:-/Applications/Postgres.app/Contents/Versions/17/bin}"
PGDATA="data/postgres"
PORT="${PG_PORT:-5439}"
LOG="data/postgres.log"
SUPER_FILE="data/.pg-superuser"
PY=.venv/bin/python

if [ ! -x "$PG_BIN/pg_ctl" ]; then
  echo "PostgreSQL programs not found in $PG_BIN."
  echo "Install Postgres.app from https://postgresapp.com (or set PG_BIN to your PostgreSQL bin folder)."
  exit 1
fi

running() { "$PG_BIN/pg_ctl" -D "$PGDATA" status >/dev/null 2>&1; }
start() { running || "$PG_BIN/pg_ctl" -D "$PGDATA" -l "$LOG" -w start >/dev/null; echo "PostgreSQL running on 127.0.0.1:$PORT"; }
new_password() { $PY -c "import secrets; print(secrets.token_urlsafe(24))"; }
set_env() { $PY -c "import sys; from app.envfile import update_env_file; update_env_file({sys.argv[1]: (sys.argv[2] or None)})" "$1" "$2"; }
env_has() { [ -f .env ] && grep -q "^$1=" .env; }
superuser_sql() {  # runs SQL from stdin as the superuser; extra args are psql options
  PGPASSWORD="$(cat "$SUPER_FILE")" "$PG_BIN/psql" -h 127.0.0.1 -p "$PORT" -U postgres -d postgres -q -v ON_ERROR_STOP=1 "$@"
}

case "${1:-}" in
  setup)
    mkdir -p data
    touch .env && chmod 600 .env
    # Older setups kept the superuser password in .env: move it out.
    if env_has PG_SUPERUSER_PASSWORD; then
      [ -f "$SUPER_FILE" ] || { grep '^PG_SUPERUSER_PASSWORD=' .env | head -1 | cut -d= -f2- > "$SUPER_FILE"; chmod 600 "$SUPER_FILE"; }
      set_env PG_SUPERUSER_PASSWORD ""
    fi

    if [ ! -d "$PGDATA" ]; then
      new_password > "$SUPER_FILE" && chmod 600 "$SUPER_FILE"
      "$PG_BIN/initdb" -D "$PGDATA" -U postgres --pwfile="$SUPER_FILE" --auth=scram-sha-256 --encoding=UTF8 --locale=C >/dev/null
      cat >> "$PGDATA/postgresql.conf" <<EOF

# --- Find My Photos settings
listen_addresses = '127.0.0.1'   # only this Mac can connect
port = $PORT
unix_socket_directories = ''     # TCP only
max_connections = 100
EOF
      start
      APP_PW=$(new_password)
      superuser_sql -v app_pw="$APP_PW" <<'SQL'
CREATE ROLE findmyphotos LOGIN PASSWORD :'app_pw';
CREATE DATABASE findmyphotos OWNER findmyphotos;
REVOKE ALL ON DATABASE findmyphotos FROM PUBLIC;
SQL
      set_env DATABASE_URL "postgresql://findmyphotos:$APP_PW@127.0.0.1:$PORT/findmyphotos"
      echo "PostgreSQL set up. The app login is saved in .env"
    elif ! env_has DATABASE_URL; then
      # The server exists but .env lost its entry: give the app login a new password.
      start
      [ -f "$SUPER_FILE" ] || { echo "Cannot repair: $SUPER_FILE is missing."; exit 1; }
      APP_PW=$(new_password)
      # Also covers a first setup that stopped half-way (login or database never created).
      superuser_sql -v app_pw="$APP_PW" <<'SQL'
SELECT format('CREATE ROLE findmyphotos LOGIN PASSWORD %L', :'app_pw')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'findmyphotos')\gexec
ALTER ROLE findmyphotos PASSWORD :'app_pw';
SELECT 'CREATE DATABASE findmyphotos OWNER findmyphotos'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'findmyphotos')\gexec
REVOKE ALL ON DATABASE findmyphotos FROM PUBLIC;
SQL
      set_env DATABASE_URL "postgresql://findmyphotos:$APP_PW@127.0.0.1:$PORT/findmyphotos"
      echo "DATABASE_URL restored in .env"
    fi
    ;;
  start) start ;;
  stop) if running; then "$PG_BIN/pg_ctl" -D "$PGDATA" -m fast -w stop >/dev/null; echo "PostgreSQL stopped"; else echo "PostgreSQL was not running"; fi ;;
  status) if running; then echo "PostgreSQL running on 127.0.0.1:$PORT"; else echo "PostgreSQL not running"; fi ;;
  *) echo "Usage: $0 setup|start|stop|status"; exit 1 ;;
esac
