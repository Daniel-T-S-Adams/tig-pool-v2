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
        self._pool: Optional[pool.ThreadedConnectionPool] = None
        self._pool_lock = threading.Lock()
        self._minconn = max(1, int(os.environ.get("POSTGRES_POOL_MIN", "4")))
        self._maxconn = max(
            self._minconn,
            int(os.environ.get("POSTGRES_POOL_MAX", "32")),
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
        return self._pool.getconn()

    def _checkin(self, conn) -> None:
        if self._pool is None or conn is None:
            return
        try:
            self._pool.putconn(conn)
        except Exception:
            try:
                conn.close()
            except Exception:
                pass

    def execute_many(self, *args) -> None:
        conn = self._checkout()
        try:
            with conn.cursor() as cur:
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
                return cur.fetchone()
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
                return cur.fetchall()
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
