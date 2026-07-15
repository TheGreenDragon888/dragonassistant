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

            existing_columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(server_config)").fetchall()
            }
            if "furnace_max_queue" not in existing_columns:
                conn.execute(
                    "ALTER TABLE server_config ADD COLUMN furnace_max_queue INTEGER NOT NULL DEFAULT 25"
                )
            if "factory_max_queue" not in existing_columns:
                conn.execute(
                    "ALTER TABLE server_config ADD COLUMN factory_max_queue INTEGER NOT NULL DEFAULT 5"
                )
            if "public_messages" not in existing_columns:
                conn.execute(
                    "ALTER TABLE server_config ADD COLUMN public_messages INTEGER NOT NULL DEFAULT 0"
                )
            if "mining_pool_remaining" not in existing_columns:
                conn.execute(
                    "ALTER TABLE server_config ADD COLUMN mining_pool_remaining INTEGER NOT NULL DEFAULT 0"
                )
            if "mining_pool_last_topup" not in existing_columns:
                conn.execute(
                    "ALTER TABLE server_config ADD COLUMN mining_pool_last_topup TEXT NOT NULL DEFAULT ''"
                )
            if "currency_minted_total" not in existing_columns:
                conn.execute(
                    "ALTER TABLE server_config ADD COLUMN currency_minted_total REAL NOT NULL DEFAULT 0.0"
                )
            if "currency_burned_total" not in existing_columns:
                conn.execute(
                    "ALTER TABLE server_config ADD COLUMN currency_burned_total REAL NOT NULL DEFAULT 0.0"
                )

            # One-time backfill for the old default (5) - now unconditional so it
            # doesn't clobber a server that intentionally set its queue to 5 via
            # /setup max_queue on a later restart.
            conn.execute(
                "UPDATE server_config SET furnace_max_queue = 25 WHERE furnace_max_queue IS NULL"
            )

            # Mining pool rebuild: fold any unharvested mining_blocks totals into
            # the new per-server pool, then drop the old per-channel block tables.
            table_names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            if "mining_blocks" in table_names:
                leftover_totals = conn.execute(
                    "SELECT guild_id, SUM(remaining_total) AS total FROM mining_blocks GROUP BY guild_id"
                ).fetchall()
                for row in leftover_totals:
                    conn.execute(
                        "INSERT INTO server_config (guild_id, mining_pool_remaining) VALUES (?, ?) "
                        "ON CONFLICT (guild_id) DO UPDATE SET mining_pool_remaining = mining_pool_remaining + excluded.mining_pool_remaining",
                        (row["guild_id"], row["total"]),
                    )
                conn.execute("DROP TABLE IF EXISTS mining_block_contents")
                conn.execute("DROP TABLE IF EXISTS mining_blocks")

            # Market rebuild: the passive chat-activity payout is retired in
            # favor of the market being the sole currency faucet (docs/market.md
            # section 1) - its rolling window table is no longer needed.
            if "chat_activity_window" in table_names:
                conn.execute("DROP TABLE IF EXISTS chat_activity_window")

            if "mine_channel_id" in existing_columns:
                conn.execute("ALTER TABLE server_config DROP COLUMN mine_channel_id")

            drill_columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(drills)").fetchall()
            }
            if "channel_id" in drill_columns:
                conn.execute("ALTER TABLE drills DROP COLUMN channel_id")

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
