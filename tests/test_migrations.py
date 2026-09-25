"""Migrations: versioned, idempotent, safe to run concurrently, adopt the old MVP schema."""
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from conftest import SUPER_URL, TEST_URL, _user

from app import migrate

ALL = [v for v, _ in migrate.available()]


@pytest.fixture
def scratch_db():
    name = "findmyphotos_migtest"
    with psycopg.connect(SUPER_URL, autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        c.execute(f"CREATE DATABASE {name} OWNER {_user}")
    yield TEST_URL.rsplit("/", 1)[0] + "/" + name
    with psycopg.connect(SUPER_URL, autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def test_test_database_is_fully_migrated_and_upgrade_is_idempotent():
    assert migrate.pending(TEST_URL) == []
    assert migrate.upgrade(TEST_URL) == []


def test_concurrent_upgrades_apply_each_migration_exactly_once(scratch_db):
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: migrate.upgrade(scratch_db), range(4)))
    applied = [v for r in results for v in r]
    assert sorted(applied) == sorted(ALL)          # every migration once, no errors
    assert migrate.pending(scratch_db) == []


def test_existing_mvp_database_is_adopted_and_backfilled(scratch_db):
    old_schema = (migrate.MIGRATIONS_DIR / "001_initial.sql").read_text()
    with psycopg.connect(scratch_db, autocommit=True) as c:
        c.execute(old_schema)  # database created by the MVP, without schema_migrations
        c.execute("INSERT INTO events (id, name, token, created_at) VALUES (1, 'Old', 'tok', '2026-01-01T00:00:00Z')")
        c.execute("INSERT INTO photos (event_id, original_name, file_name, status) VALUES (1, 'a.jpg', 'a.jpg', 'done')")
    assert migrate.upgrade(scratch_db) == ALL
    with psycopg.connect(scratch_db) as c:
        days_left = c.execute("SELECT EXTRACT(DAY FROM expires_at - now())::int FROM events").fetchone()[0]
        status = c.execute("SELECT status FROM photos").fetchone()[0]
    # An event created long ago gets 90 days from the migration, so the first clean-up
    # run does not delete it (it was created on 2026-01-01, more than 90 days ago).
    assert 89 <= days_left <= 90
    assert status == "pending"          # re-queued to create the metadata-free download copy


def test_old_events_survive_the_first_cleanup_after_migration(scratch_db):
    """Regression test for the second review's N1: backfilled expiry must not be in the past."""
    old_schema = (migrate.MIGRATIONS_DIR / "001_initial.sql").read_text()
    with psycopg.connect(scratch_db, autocommit=True) as c:
        c.execute(old_schema)
        c.execute("INSERT INTO events (name, token, created_at) VALUES ('Last year', 'tok2', now() - interval '400 days')")
    migrate.upgrade(scratch_db)
    with psycopg.connect(scratch_db) as c:
        assert c.execute("SELECT COUNT(*) FROM events WHERE expires_at < now()").fetchone()[0] == 0


def test_running_processes_survive_a_schema_change(app):
    """A column added while the site runs (a migration) must not break a pooled connection
    that has already run the same query many times."""
    import psycopg
    from app import db
    from app.config import settings
    query = "SELECT * FROM admin_state WHERE key <> %s"
    with db.connect() as conn:
        for _ in range(10):  # more than psycopg's default threshold for caching a query plan
            conn.execute(query, ("x",)).fetchall()
        conn.commit()
        with psycopg.connect(settings.database_url, autocommit=True) as other:  # "the migration"
            other.execute("ALTER TABLE admin_state ADD COLUMN IF NOT EXISTS extra_test_column INT")
        try:
            conn.execute(query, ("x",)).fetchall()
        finally:
            conn.rollback()
            with psycopg.connect(settings.database_url, autocommit=True) as other:
                other.execute("ALTER TABLE admin_state DROP COLUMN IF EXISTS extra_test_column")
