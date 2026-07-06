"""
database/db.py

A small wrapper around Python's built-in `sqlite3` module.

Why this exists: discord.py runs on asyncio (everything is `async def`), but
Python's standard sqlite3 library is synchronous/blocking. Calling a blocking
function directly inside an async function would freeze the ENTIRE bot
(all servers, all users) while that one query runs. To avoid that, every
query here is pushed to a background thread via `asyncio.to_thread`, so the
bot keeps responding to other events while a query is in flight.

You won't need to touch this file often - cogs import `Database` and call
`fetchone`, `fetchall`, or `execute`.
"""
import asyncio
import sqlite3
from pathlib import Path


class Database:
    def __init__(self, path: str):
        self.path = path
        # Make sure the parent folder (e.g. "data/") exists before sqlite3
        # tries to create the .db file inside it.
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        # row_factory makes query results behave like dicts (row["column"])
        # instead of plain tuples (row[0], row[1]...) - much easier to read.
        conn.row_factory = sqlite3.Row
        # Enforces foreign key constraints (off by default in SQLite).
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_schema_sync(self):
        schema_path = Path(__file__).parent / "schema.sql"
        with self._connect() as conn:
            conn.executescript(schema_path.read_text())

    async def init_schema(self):
        """Creates all tables from schema.sql if they don't already exist.
        Call this once, right after the bot logs in."""
        await asyncio.to_thread(self._init_schema_sync)

    def _execute_sync(self, query: str, params: tuple):
        with self._connect() as conn:
            cur = conn.execute(query, params)
            conn.commit()
            return cur.lastrowid

    async def execute(self, query: str, params: tuple = ()) -> int:
        """Run an INSERT/UPDATE/DELETE. Returns the last inserted row id."""
        return await asyncio.to_thread(self._execute_sync, query, params)

    def _fetchone_sync(self, query: str, params: tuple):
        with self._connect() as conn:
            cur = conn.execute(query, params)
            return cur.fetchone()

    async def fetchone(self, query: str, params: tuple = ()):
        """Run a SELECT and return the first matching row (or None)."""
        return await asyncio.to_thread(self._fetchone_sync, query, params)

    def _fetchall_sync(self, query: str, params: tuple):
        with self._connect() as conn:
            cur = conn.execute(query, params)
            return cur.fetchall()

    async def fetchall(self, query: str, params: tuple = ()):
        """Run a SELECT and return all matching rows as a list."""
        return await asyncio.to_thread(self._fetchall_sync, query, params)
