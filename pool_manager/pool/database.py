import os
import logging
import psycopg2
import psycopg2.extras
from contextlib import contextmanager

logger = logging.getLogger(__name__)

_conn_params = {
    "host": os.environ.get("POSTGRES_HOST", "db"),
    "dbname": os.environ.get("POSTGRES_DB", "innopool"),
    "user": os.environ.get("POSTGRES_USER", "postgres"),
    "password": os.environ.get("POSTGRES_PASSWORD", ""),
}


@contextmanager
def get_conn():
    conn = psycopg2.connect(**_conn_params)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


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


def execute_many(*queries):
    """Execute multiple (sql, params) tuples atomically."""
    with get_conn() as conn:
        with conn.cursor() as cur:
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
