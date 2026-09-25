"""Versioned database migrations.

Each file in app/migrations/ (NNN_name.sql) runs once, in order, inside its own
transaction, and is recorded in schema_migrations. A PostgreSQL advisory lock makes it
safe to run from several processes at the same time.

    .venv/bin/python -m app.migrate          apply pending migrations
    .venv/bin/python -m app.migrate --check  exit 1 if migrations are pending
"""
import os
import sys
from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
LOCK_ID = 734_500_001  # arbitrary number identifying "Find My Photos migrations"


def available() -> list[tuple[str, Path]]:
    return sorted((p.stem, p) for p in MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))


def pending(url: str) -> list[str]:
    with psycopg.connect(url, connect_timeout=5) as conn:
        exists = conn.execute("SELECT to_regclass('schema_migrations') IS NOT NULL").fetchone()[0]
        done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")} if exists else set()
    return [version for version, _ in available() if version not in done]


def upgrade(url: str) -> list[str]:
    applied_now = []
    with psycopg.connect(url, connect_timeout=5, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_ID,))
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version     TEXT PRIMARY KEY,
                    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
                )""")
            done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
            for version, path in available():
                if version in done:
                    continue
                with conn.transaction():
                    conn.execute(path.read_text())
                    conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
                applied_now.append(version)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))
    return applied_now


def main() -> None:
    from .envfile import load_env_file  # migrations need only DATABASE_URL, not all settings

    load_env_file()
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL is not set.")
    if "--check" in sys.argv:
        todo = pending(url)
        print("Pending migrations: " + (", ".join(todo) if todo else "none"))
        sys.exit(1 if todo else 0)
    done = upgrade(url)
    print("Applied migrations: " + (", ".join(done) if done else "none (database is up to date)"))


if __name__ == "__main__":
    main()
