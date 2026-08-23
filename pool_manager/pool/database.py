import logging
import os
import threading
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
from psycopg2 import pool

logger = logging.getLogger(__name__)

_conn_params = {
    "host": os.environ.get("POSTGRES_HOST", "db"),
    "dbname": os.environ.get("POSTGRES_DB", "innopool"),
    "user": os.environ.get("POSTGRES_USER", "postgres"),
    "password": os.environ.get("POSTGRES_PASSWORD", ""),
}

# Distinct from master's POSTGRES_POOL_MAX (48). env_file would otherwise
# make this process try to open the same 48 sockets.
_pool_min = max(1, int(os.environ.get("MANAGER_POSTGRES_POOL_MIN", "2")))
_pool_max = max(_pool_min, int(os.environ.get("MANAGER_POSTGRES_POOL_MAX", "8")))
_pool: pool.ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()


def _get_pool() -> pool.ThreadedConnectionPool:
    global _pool
    if _pool is not None and not getattr(_pool, "closed", False):
        return _pool
    with _pool_lock:
        if _pool is None or getattr(_pool, "closed", False):
            _pool = pool.ThreadedConnectionPool(
                _pool_min,
                _pool_max,
                **_conn_params,
            )
            logger.info(
                "Postgres pool ready at %s (min=%s max=%s)",
                _conn_params.get("host"),
                _pool_min,
                _pool_max,
            )
        return _pool


def _putconn(conn) -> None:
    current = _pool
    if current is None or conn is None:
        return
    try:
        if getattr(conn, "closed", 1):
            current.putconn(conn, close=True)
            return
        if not getattr(conn, "autocommit", False):
            try:
                conn.rollback()
            except Exception:
                pass
        current.putconn(conn)
    except Exception:
        try:
            conn.close()
        except Exception:
            pass


@contextmanager
def get_conn():
    conn = _get_pool().getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        _putconn(conn)


def fetch_one(sql: str, params=None) -> dict | None:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return cur.fetchone()


def fetch_all(sql: str, params=None) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def execute(sql: str, params=None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)


def execute_many(*queries, lock_timeout: str | None = None):
    """Execute multiple (sql, params) tuples atomically."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            if lock_timeout:
                cur.execute(f"SET LOCAL lock_timeout = '{lock_timeout}'")
            for sql, params in queries:
                cur.execute(sql, params)


def get_setting(key: str, default: str = "") -> str:
    row = fetch_one("SELECT value FROM pool_settings WHERE key = %s", (key,))
    return row["value"] if row else default


def set_setting(key: str, value: str):
    execute(
        """
        INSERT INTO pool_settings (key, value, updated_at)
        VALUES (%s, %s, (EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT)
        ON CONFLICT (key) DO UPDATE
            SET value = EXCLUDED.value,
                updated_at = EXCLUDED.updated_at
        """,
        (key, str(value)),
    )
