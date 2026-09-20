"""Explicit, isolated PostgreSQL connections and checksummed migrations.

Importing this module starts nothing and never uses legacy database settings.
Callers provide a v2 DSN; production must use a separate database and role.
"""

from contextlib import contextmanager
import hashlib
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor, register_uuid

register_uuid()


class Database:
    def __init__(self, dsn):
        if not dsn:
            raise ValueError("an explicit v2 database DSN is required")
        self.dsn = dsn

    @contextmanager
    def transaction(self):
        connection = psycopg2.connect(self.dsn, application_name="innopool-v2", connect_timeout=5)
        try:
            with connection:
                with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                    cursor.execute("SET LOCAL search_path TO pool_v2, pg_catalog")
                    cursor.execute("SET LOCAL lock_timeout TO '15s'")
                    cursor.execute("SET LOCAL statement_timeout TO '60s'")
                    yield cursor
        finally:
            connection.close()

    def migrate(self):
        directory = Path(__file__).with_name("migrations")
        with self.transaction() as cursor:
            lock(cursor, "schema-migration")
            cursor.execute("CREATE SCHEMA IF NOT EXISTS pool_v2")
            cursor.execute("""CREATE TABLE IF NOT EXISTS pool_v2.schema_migrations (
                name text PRIMARY KEY, sha256 text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT clock_timestamp())""")
            cursor.execute("SELECT name, sha256 FROM schema_migrations ORDER BY name")
            installed = {row["name"]: row["sha256"] for row in cursor.fetchall()}
            paths = sorted(directory.glob("[0-9]*.sql"))
            if set(installed) - {path.name for path in paths}:
                raise RuntimeError("database has migrations unknown to this release")
            for path in paths:
                source = path.read_bytes()
                digest = hashlib.sha256(source).hexdigest()
                if path.name in installed:
                    if installed[path.name] != digest:
                        raise RuntimeError(f"applied migration changed: {path.name}")
                    continue
                if installed and path.name < max(installed):
                    raise RuntimeError("migrations must be applied in order")
                cursor.execute(source.decode("utf-8"))
                cursor.execute("INSERT INTO schema_migrations(name, sha256) VALUES (%s,%s)",
                               (path.name, digest))


def lock(cursor, key):
    """Transaction-scoped deterministic advisory lock, independent of processes."""
    digest = hashlib.sha256(("innopool-v2:" + key).encode()).digest()
    cursor.execute("SELECT pg_advisory_xact_lock(%s)",
                   (int.from_bytes(digest[:8], "big", signed=True),))
