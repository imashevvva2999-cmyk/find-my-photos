"""Test setup: an isolated PostgreSQL database and a temporary data folder.

The real database, the real data/ folder and the owner's photos are never touched.
The test database 'findmyphotos_test' is created (and dropped first) on the project's
own PostgreSQL server, using the superuser password stored outside .env.
"""
import os
import re
import sys
import tempfile
from pathlib import Path

import psycopg
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.passwords import hash_password  # noqa: E402  (no settings needed)

TEST_DB = "findmyphotos_test"
ADMIN_PASSWORD = "test-admin-password-1234"
NASA = ROOT / "test_data" / "collection_40"
SEARCH = ROOT / "test_data" / "search"
SAMPLES = ROOT / "sample_photos"


def _read_env_file() -> dict[str, str]:
    env = {}
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def _superuser_password() -> str:
    secret_file = ROOT / "data" / ".pg-superuser"
    if secret_file.exists():
        return secret_file.read_text().strip()
    return os.environ.get("PG_SUPERUSER_PASSWORD") or _read_env_file()["PG_SUPERUSER_PASSWORD"]


_real_url = _read_env_file()["DATABASE_URL"]
_user, _app_pw, _host, _port, _ = re.match(r"postgres(?:ql)?://([^:]+):([^@]+)@([^:/]+):(\d+)/(\w+)", _real_url).groups()
SUPER_URL = f"postgresql://postgres:{_superuser_password()}@{_host}:{_port}/postgres"
TEST_URL = f"postgresql://{_user}:{_app_pw}@{_host}:{_port}/{TEST_DB}"

with psycopg.connect(SUPER_URL, autocommit=True) as _c:
    _c.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
    _c.execute(f"CREATE DATABASE {TEST_DB} OWNER {_user}")

DATA_DIR = Path(tempfile.mkdtemp(prefix="fmp-test-data-"))
os.environ.update({
    "DATABASE_URL": TEST_URL,
    "DATA_DIR": str(DATA_DIR),
    "SECRET_KEY": "test-secret-" + "x" * 40,
    "ADMIN_PASSWORD_HASH": hash_password(ADMIN_PASSWORD),
    "COOKIE_SECURE": "false",
    "MIN_FREE_DISK_MB": "0",
    "SEARCHES_PER_10_MIN": "1000",
    "LOG_LEVEL": "DEBUG",
})

from app import migrate  # noqa: E402

migrate.upgrade(TEST_URL)

from fastapi.testclient import TestClient  # noqa: E402

from app import db, main, matching, security, worker  # noqa: E402

ORIGIN = {"Origin": "http://testserver"}


@pytest.fixture(scope="session")
def app():
    with TestClient(main.app):  # runs startup/shutdown (pool, models)
        yield main.app


@pytest.fixture(autouse=True)
def clean_state(app):
    """Every test starts with an empty database, an empty data folder and empty caches."""
    with db.connect() as conn:
        conn.execute("TRUNCATE events, photos, faces, rate_limits, search_log, worker_heartbeats, admin_state, admin_passkeys, passkey_challenges "
                     "RESTART IDENTITY CASCADE")
    security._valid_after["checked"] = 0.0
    for path in sorted(DATA_DIR.rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    matching.clear_cache()
    yield


@pytest.fixture
def client(app):
    return TestClient(app, headers=ORIGIN)


@pytest.fixture
def admin(app):
    c = TestClient(app, headers=ORIGIN)
    r = c.post("/admin/login", data={"password": ADMIN_PASSWORD}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return c


def create_event(admin, name="Test event") -> tuple[int, str]:
    r = admin.post("/admin/events", data={"name": name}, follow_redirects=False)
    event_id = int(r.headers["location"].rsplit("/", 1)[1])
    page = admin.get(f"/admin/events/{event_id}").text
    token = re.search(r'/e/([A-Za-z0-9_-]{20,})"', page).group(1)
    return event_id, token


def upload(admin, event_id, paths_or_parts):
    files = []
    for item in paths_or_parts:
        if isinstance(item, Path):
            files.append(("files", (item.name, item.read_bytes(), "image/jpeg")))
        else:
            files.append(("files", item))
    return admin.post(f"/admin/api/events/{event_id}/photos", files=files)


def process_all():
    """Run the background worker until the queue is empty (synchronously, for tests)."""
    return worker.run_pending()


def files_in_data() -> set[str]:
    return {str(p.relative_to(DATA_DIR)) for p in DATA_DIR.rglob("*") if p.is_file()}
