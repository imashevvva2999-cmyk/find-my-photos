"""PostgreSQL connection pool. The schema is managed by app/migrate.py, never at startup."""
import logging
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

from .config import settings

log = logging.getLogger("db")
_pool: ConnectionPool | None = None


class DatabaseUnavailable(RuntimeError):
    """The database could not be reached in time. Shown to users as 'try again shortly'."""


def init(max_size: int | None = None) -> None:
    global _pool
    if _pool is not None:
        return
    _pool = ConnectionPool(
        settings.database_url,
        min_size=1,
        max_size=max_size or settings.db_pool_max,
        timeout=10,                                   # wait at most 10 s for a free connection
        max_idle=300,
        check=ConnectionPool.check_connection,        # replaces connections broken by a DB restart
        # prepare_threshold=None: no server-side cached query plans, so a schema change (a new
        # migration) can never leave a running web server or worker failing with
        # "cached plan must not change result type". The queries here are cheap to plan.
        kwargs={"row_factory": dict_row, "connect_timeout": 5, "prepare_threshold": None},
        name="findmyphotos",
        open=True,                                    # starts even if PostgreSQL is briefly down
    )


def close() -> None:
    global _pool
    if _pool is not None:
        _pool.close(timeout=5)
        _pool = None


@contextmanager
def connect():
    """Borrow a connection; commits on success, rolls back on error."""
    if _pool is None:
        raise DatabaseUnavailable("database pool is not initialised")
    try:
        with _pool.connection() as conn:
            yield conn
    except PoolTimeout as exc:
        log.warning("database pool timeout")
        raise DatabaseUnavailable("no database connection available") from exc
    except psycopg.OperationalError as exc:
        if is_retryable(exc):
            raise  # deadlock / serialization conflict: the caller may retry; the database is fine
        log.warning("database operational error: %s", type(exc).__name__)
        raise DatabaseUnavailable("database connection failed") from exc


def is_retryable(exc: BaseException) -> bool:
    """Deadlocks and serialization conflicts (SQLSTATE class 40): retrying usually succeeds."""
    return isinstance(exc, psycopg.Error) and (exc.sqlstate or "").startswith("40")
