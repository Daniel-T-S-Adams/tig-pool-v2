"""Thread-safe Postgres access for the InnoPool master.

The upstream TIG master ships a single global psycopg2 connection. That is not
safe once uvicorn serves many concurrent /get-batches handlers alongside the
main manager loop, and it serializes the whole process behind whichever query
holds the connection (the huge slave_manager.run() refresh is a common culprit).

This replacement keeps the same get_db_conn()/PostgresDB call surface but uses
a ThreadedConnectionPool so request threads and the main loop do not block each
other on one socket.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor

logger = logging.getLogger(os.path.splitext(os.path.basename(__file__))[0])


class PostgresDB:
    def __init__(self, host: str, port: int, dbname: str, user: str, password: str):
        self.conn_params = {
            "host": host,
            "port": port,
            "dbname": dbname,
            "user": user,
            "password": password,
        }
        # Per-session backstop so one warehouse query cannot pin a pool slot
        # for 14 minutes when the leftover table grows with the fleet.
        options = os.environ.get(
            "POSTGRES_OPTIONS",
            "-c statement_timeout=45000 -c idle_in_transaction_session_timeout=20000",
        )
        if options:
            self.conn_params["options"] = options
        self._pool: Optional[pool.ThreadedConnectionPool] = None
        self._pool_lock = threading.Lock()
        self._minconn = max(1, int(os.environ.get("POSTGRES_POOL_MIN", "4")))
        self._maxconn = max(
            self._minconn,
            int(os.environ.get("POSTGRES_POOL_MAX", "48")),
        )
        # psycopg2's pool raises immediately when exhausted; wait briefly so
        # short get-batches bursts do not 500 the fleet.
        self._pool_wait_sec = max(
            0.0, float(os.environ.get("POSTGRES_POOL_WAIT_SEC", "15"))
        )

    @property
    def closed(self) -> bool:
        return self._pool is None or bool(getattr(self._pool, "closed", False))

    def connect(self) -> None:
        """Create the connection pool if needed."""
        with self._pool_lock:
            if self._pool is not None and not getattr(self._pool, "closed", False):
                return
            self._pool = pool.ThreadedConnectionPool(
                self._minconn,
                self._maxconn,
                **self.conn_params,
            )
            logger.info(
                "Postgres pool ready at %s:%s (min=%s max=%s)",
                self.conn_params["host"],
                self.conn_params["port"],
                self._minconn,
                self._maxconn,
            )

    def disconnect(self) -> None:
        with self._pool_lock:
            if self._pool is not None:
                self._pool.closeall()
                self._pool = None
                logger.info("Disconnected Postgres pool")

    def _checkout(self):
        if self.closed:
            self.connect()
        assert self._pool is not None
        deadline = time.monotonic() + self._pool_wait_sec
        last_err: Optional[BaseException] = None
        while True:
            try:
                return self._pool.getconn()
            except pool.PoolError as exc:
                last_err = exc
                if self._pool_wait_sec <= 0 or time.monotonic() >= deadline:
                    logger.error(
                        "Postgres pool exhausted (max=%s wait=%ss)",
                        self._maxconn,
                        self._pool_wait_sec,
                    )
                    raise
                time.sleep(0.05)
        raise last_err  # pragma: no cover

    def _checkin(self, conn) -> None:
        if self._pool is None or conn is None:
            return
        try:
            # Drop broken connections instead of returning them to the pool.
            if getattr(conn, "closed", 1):
                self._pool.putconn(conn, close=True)
                return
            # SELECTs that never commit leave idle-in-transaction sockets
            # holding row locks. The next get-batches wave then occupies
            # every pool slot waiting on those locks.
            if not getattr(conn, "autocommit", False):
                try:
                    conn.rollback()
                except Exception:
                    pass
            self._pool.putconn(conn)
        except Exception:
            try:
                conn.close()
            except Exception:
                pass

    def execute_many(self, *args, lock_timeout: Optional[str] = None) -> None:
        conn = self._checkout()
        try:
            with conn.cursor() as cur:
                if lock_timeout:
                    cur.execute("SET LOCAL lock_timeout = %s", (lock_timeout,))
                for query in args:
                    cur.execute(*query)
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error("Error executing queries: %s", e)
            raise
        finally:
            self._checkin(conn)

    def execute(self, query: str, params: Optional[tuple] = None) -> None:
        conn = self._checkout()
        try:
            with conn.cursor() as cur:
                cur.execute(query, params)
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error("Error executing query: %s", e)
            raise
        finally:
            self._checkin(conn)

    def fetch_one(self, query: str, params: Optional[tuple] = None) -> Optional[Dict[str, Any]]:
        conn = self._checkout()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(query, params)
                row = cur.fetchone()
            conn.commit()
            return row
        except Exception as e:
            conn.rollback()
            logger.error("Error fetching row: %s", e)
            raise
        finally:
            self._checkin(conn)

    def fetch_all(self, query: str, params: Optional[tuple] = None) -> List[Dict[str, Any]]:
        conn = self._checkout()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(query, params)
                rows = cur.fetchall()
            conn.commit()
            return rows
        except Exception as e:
            conn.rollback()
            logger.error("Error fetching rows: %s", e)
            raise
        finally:
            self._checkin(conn)

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()


db_conn = None
_db_lock = threading.Lock()


def get_db_conn():
    global db_conn
    with _db_lock:
        if db_conn is None or db_conn.closed:
            db_conn = PostgresDB(
                host=os.environ["POSTGRES_HOST"],
                port=5432,
                dbname=os.environ["POSTGRES_DB"],
                user=os.environ["POSTGRES_USER"],
                password=os.environ["POSTGRES_PASSWORD"],
            )
            db_conn.connect()
        return db_conn
