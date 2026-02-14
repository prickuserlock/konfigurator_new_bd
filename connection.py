import os
import re

import psycopg

# Connection string example:
# postgresql://USER:PASSWORD@HOST:5432/DBNAME
DATABASE_URL = os.getenv("DATABASE_URL") or "postgresql://bot:bot@localhost:5432/botdb"

# One global connection (same approach as SQLite version).
# For high load it's better to switch to a connection pool, but this keeps your current code almost unchanged.
conn = psycopg.connect(DATABASE_URL)

_cur = conn.cursor()


class CompatCursor:
    """Compatibility wrapper: allows using SQLite-style '?' placeholders with psycopg (%s).

    Your code can keep cur.execute("... WHERE a=?", (val,)) and it'll work.
    """

    def __init__(self, cur):
        self._cur = cur

    def _rewrite_query(self, query: str) -> str:
        """Make common SQLite queries work on PostgreSQL.

        Currently supported rewrites:
        - INSERT OR IGNORE  -> INSERT ... ON CONFLICT DO NOTHING
        - '?' placeholders  -> '%s' placeholders (handled later)
        """

        if not isinstance(query, str):
            return query

        q_strip = query.lstrip()
        # SQLite: INSERT OR IGNORE ...
        if re.match(r"(?is)^INSERT\s+OR\s+IGNORE\b", q_strip):
            # replace only the first occurrence near the beginning
            query = re.sub(r"(?is)^(\s*)INSERT\s+OR\s+IGNORE\b", r"\1INSERT", query, count=1)
            # Postgres allows ON CONFLICT DO NOTHING without specifying a target
            if not re.search(r"(?is)\bON\s+CONFLICT\b", query):
                query = query.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"

        return query

    def execute(self, query, params=None):
        try:
            if isinstance(query, str):
                query = self._rewrite_query(query)

            if params is None:
                return self._cur.execute(query)

            if isinstance(query, str) and "?" in query:
                query = query.replace("?", "%s")

            return self._cur.execute(query, params)
        except Exception:
            # In PostgreSQL a failed statement aborts the whole transaction.
            # Rollback here to prevent "current transaction is aborted" errors.
            try:
                conn.rollback()
            except Exception:
                pass
            raise

    def executemany(self, query, seq_of_params):
        try:
            if isinstance(query, str):
                query = self._rewrite_query(query)

            if isinstance(query, str) and "?" in query:
                query = query.replace("?", "%s")

            return self._cur.executemany(query, seq_of_params)
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def fetchmany(self, size=None):
        return self._cur.fetchmany(size)

    def __iter__(self):
        return iter(self._cur)

    def __getattr__(self, name):
        return getattr(self._cur, name)


cur = CompatCursor(_cur)
