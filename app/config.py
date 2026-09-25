"""Settings come from environment variables (for local use: the .env file, mode 600).

Everything is read and validated once, at import. A missing or invalid value stops the
program with a message that names the variable, instead of failing later at random.
"""
import hashlib
import hmac
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .envfile import load_env_file

BASE_DIR = Path(__file__).resolve().parent.parent
MODELS_DIR = BASE_DIR / "models"

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}


class ConfigError(RuntimeError):
    pass


@dataclass
class Settings:
    database_url: str
    secret_key: str
    admin_password_hash: str
    data_dir: Path
    cookie_secure: bool
    event_retention_days: int       # default lifetime of an event's photos and face data
    max_retention_days: int
    result_link_minutes: int        # lifetime of a visitor's result links
    match_threshold: float          # cosine similarity needed to show a photo as a possible match
    strong_match: float             # above this the result is labelled "closer resemblance"
    max_results: int                # cap on possible matches returned by one search
    max_upload_mb: int
    max_selfie_mb: int
    max_event_pixels: int
    max_selfie_pixels: int
    max_files_per_request: int
    search_concurrency: int         # searches computed in parallel
    search_queue: int               # searches allowed to wait; more get "busy, try again"
    searches_per_10_min: int        # per visitor IP and event
    login_failures_per_15_min: int  # per IP; the global limit is 10x this
    worker_threads: int
    min_free_disk_mb: int
    db_pool_max: int
    log_level: str
    public_base_url: str            # address guests use (e.g. the Vercel domain); "" = this server's address
    import_token_sha256: str = ""  # IMPORT_TOKEN_SHA256: turns the photo-file import on (only during a transfer)

    def derived_key(self, purpose: str) -> str:
        """Separate keys for sessions and result links, derived from SECRET_KEY."""
        return hmac.new(self.secret_key.encode(), purpose.encode(), hashlib.sha256).hexdigest()


def _get_int(env, name, default, low, high, problems):
    raw = env.get(name, str(default))
    try:
        value = int(raw.replace("_", ""))
    except ValueError:
        problems.append(f"{name} must be a whole number (got {raw!r})")
        return default
    if not low <= value <= high:
        problems.append(f"{name} must be between {low} and {high} (got {value})")
    return value


def _get_float(env, name, default, low, high, problems):
    raw = env.get(name, str(default))
    try:
        value = float(raw)
    except ValueError:
        problems.append(f"{name} must be a number (got {raw!r})")
        return default
    if not low <= value <= high:
        problems.append(f"{name} must be between {low} and {high} (got {value})")
    return value


def _get_bool(env, name, default, problems):
    raw = env.get(name, "true" if default else "false").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    problems.append(f"{name} must be true or false (got {raw!r})")
    return default


def load_settings(env=None) -> Settings:
    env = os.environ if env is None else env
    problems: list[str] = []

    database_url = env.get("DATABASE_URL", "")
    if not database_url.startswith(("postgresql://", "postgres://")):
        problems.append("DATABASE_URL is missing (expected postgresql://user:password@host:port/database)")
    secret_key = env.get("SECRET_KEY", "")
    if len(secret_key) < 32:
        problems.append("SECRET_KEY is missing or shorter than 32 characters")
    password_hash = env.get("ADMIN_PASSWORD_HASH", "")
    if not password_hash.startswith("scrypt$"):
        problems.append("ADMIN_PASSWORD_HASH is missing - run: .venv/bin/python scripts/set_admin_password.py")

    s = Settings(
        database_url=database_url,
        secret_key=secret_key,
        admin_password_hash=password_hash,
        data_dir=Path(env.get("DATA_DIR", str(BASE_DIR / "data"))).resolve(),
        cookie_secure=_get_bool(env, "COOKIE_SECURE", False, problems),
        event_retention_days=_get_int(env, "EVENT_RETENTION_DAYS", 90, 1, 365, problems),
        max_retention_days=365,
        result_link_minutes=_get_int(env, "RESULT_LINK_MINUTES", 120, 5, 7 * 24 * 60, problems),
        match_threshold=_get_float(env, "MATCH_THRESHOLD", 0.363, 0.2, 0.9, problems),
        strong_match=_get_float(env, "STRONG_MATCH", 0.5, 0.2, 0.99, problems),
        max_results=_get_int(env, "MAX_RESULTS", 60, 1, 1000, problems),
        max_upload_mb=_get_int(env, "MAX_UPLOAD_MB", 30, 1, 200, problems),
        max_selfie_mb=_get_int(env, "MAX_SELFIE_MB", 15, 1, 50, problems),
        max_event_pixels=_get_int(env, "MAX_EVENT_PIXELS", 80_000_000, 1_000_000, 200_000_000, problems),
        max_selfie_pixels=_get_int(env, "MAX_SELFIE_PIXELS", 40_000_000, 1_000_000, 200_000_000, problems),
        max_files_per_request=_get_int(env, "MAX_FILES_PER_REQUEST", 5, 1, 50, problems),
        search_concurrency=_get_int(env, "SEARCH_CONCURRENCY", 2, 1, 32, problems),
        search_queue=_get_int(env, "SEARCH_QUEUE", 64, 0, 1000, problems),
        searches_per_10_min=_get_int(env, "SEARCHES_PER_10_MIN", 30, 1, 100_000, problems),
        login_failures_per_15_min=_get_int(env, "LOGIN_FAILURES_PER_15_MIN", 5, 1, 1000, problems),
        worker_threads=_get_int(env, "WORKER_THREADS", 2, 1, 16, problems),
        min_free_disk_mb=_get_int(env, "MIN_FREE_DISK_MB", 1024, 0, 10_000_000, problems),
        db_pool_max=_get_int(env, "DB_POOL_MAX", 20, 2, 200, problems),
        log_level=env.get("LOG_LEVEL", "INFO").upper(),
        public_base_url=env.get("PUBLIC_BASE_URL", "").strip().rstrip("/"),
        import_token_sha256=env.get("IMPORT_TOKEN_SHA256", "").strip().lower(),
    )
    if s.import_token_sha256 and not re.fullmatch(r"[0-9a-f]{64}", s.import_token_sha256):
        problems.append("IMPORT_TOKEN_SHA256 must be a SHA-256 hex digest (64 characters)")
    if s.public_base_url and not s.public_base_url.startswith(("https://", "http://")):
        problems.append(f"PUBLIC_BASE_URL must start with https:// (got {s.public_base_url!r})")
    if s.strong_match < s.match_threshold:
        problems.append("STRONG_MATCH must not be lower than MATCH_THRESHOLD")
    if problems:
        raise ConfigError("Configuration problems:\n  - " + "\n  - ".join(problems))
    return s


load_env_file()
try:
    settings = load_settings()
except ConfigError as exc:
    raise SystemExit(str(exc)) from None
