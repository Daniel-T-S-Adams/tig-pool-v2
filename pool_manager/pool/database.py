import logging
import os
import threading
import time
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
_pool_max = max(_pool_min, int(os.environ.get("MANAGER_POSTGRES_POOL_MAX", "16")))
_pool_wait_sec = max(0.0, float(os.environ.get("MANAGER_POSTGRES_POOL_WAIT_SEC", "8")))
_pool: pool.ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()


class SingleFlightCache:
    """One in-flight builder at a time; reuse the last good result.

    Dashboard polls and the autopilot loop must not each open a warehouse
    query. Overlapping /admin/ops/metrics calls were pinning the manager pool.

    After the first success, an expired TTL returns the last snapshot
    immediately and refreshes in the background so Cloudflare/nginx do not
    wait on a 30s rebuild and return an HTML error page.
    """

    def __init__(self, ttl_s: float):
        self.ttl_s = max(0.0, float(ttl_s))
        self._lock = threading.Lock()
        self._value = None
        self._ts = 0.0

    def get(self, builder, force: bool = False, placeholder=None):
        now = time.time()
        if not force and self._value is not None and now - self._ts < self.ttl_s:
            return self._value
        if not force and self._value is not None:
            self._schedule_refresh(builder)
            return self._value
        if placeholder is not None and not force:
            self._schedule_refresh(builder)
            return self._value if self._value is not None else placeholder
        return self._build_locked(builder)

    def _schedule_refresh(self, builder) -> None:
        if not self._lock.acquire(blocking=False):
            return

        def _run():
            try:
                value = builder()
                self._value = value
                self._ts = time.time()
            except Exception:
                logger.warning("background cache refresh failed", exc_info=True)
            finally:
                self._lock.release()

        threading.Thread(target=_run, name="cache-refresh", daemon=True).start()

    def _build_locked(self, builder):
        with self._lock:
            now = time.time()
            if self._value is not None and now - self._ts < self.ttl_s:
                return self._value
            try:
                value = builder()
                self._value = value
                self._ts = time.time()
                return value
            except Exception:
                if self._value is not None:
                    logger.warning("returning stale cache after build failure")
                    return self._value
                raise


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


def _checkout():
    current = _get_pool()
    deadline = time.monotonic() + _pool_wait_sec
    last_err: BaseException | None = None
    while True:
        try:
            return current.getconn()
        except pool.PoolError as exc:
            last_err = exc
            if _pool_wait_sec <= 0 or time.monotonic() >= deadline:
                logger.error(
                    "Postgres pool exhausted (max=%s wait=%ss)",
                    _pool_max,
                    _pool_wait_sec,
                )
                raise
            time.sleep(0.05)
    raise last_err  # pragma: no cover


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
    conn = _checkout()
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


def table_exists(table: str) -> bool:
    row = fetch_one(
        """
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = %s
        """,
        (table,),
    )
    return row is not None


def has_columns(table: str, *columns: str) -> bool:
    if not columns:
        return True
    rows = fetch_all(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = %s
          AND column_name = ANY(%s)
        """,
        (table, list(columns)),
    )
    found = {str(row["column_name"]) for row in rows}
    return all(name in found for name in columns)


def has_index(index_name: str) -> bool:
    row = fetch_one(
        """
        SELECT 1
        FROM pg_class
        WHERE relname = %s AND relkind = 'i'
        """,
        (index_name,),
    )
    return row is not None


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
