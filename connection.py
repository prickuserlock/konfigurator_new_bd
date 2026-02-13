import os

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

    def execute(self, query, params=None):
        if params is None:
            return self._cur.execute(query)
        if "?" in query:
            query = query.replace("?", "%s")
        return self._cur.execute(query, params)

    def executemany(self, query, seq_of_params):
        if "?" in query:
            query = query.replace("?", "%s")
        return self._cur.executemany(query, seq_of_params)

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
