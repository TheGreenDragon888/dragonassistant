-- schema.sql
-- Executed once at bot startup (see database/db.py). SQLite creates the file
-- and these tables if they don't already exist. Re-running this on an
-- existing database is safe because of "IF NOT EXISTS".

-- One row per Discord user, tracked globally (not per-server), matching the
-- design doc's rule that DragonCoin and raw materials are stored per-user,
-- not per-server.
CREATE TABLE IF NOT EXISTS users (
    user_id         INTEGER PRIMARY KEY,   -- Discord snowflake ID
    dragoncoin      REAL NOT NULL DEFAULT 0.0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- A user's stockpile of a given material (raw, smelted, or component).
-- material_id references a hardcoded key in data/materials.py (e.g. "iron_ore").
CREATE TABLE IF NOT EXISTS user_materials (
    user_id         INTEGER NOT NULL,
    material_id     TEXT NOT NULL,
    quantity        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, material_id),
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);

-- Per-server settings: which channel is the designated mining "dig site",
-- the server's custom currency name/emoji, and furnace/factory levels.
CREATE TABLE IF NOT EXISTS server_config (
    guild_id            INTEGER PRIMARY KEY,
    mine_channel_id     INTEGER,
    currency_name       TEXT,
    currency_emoji      TEXT,
    furnace_level       INTEGER NOT NULL DEFAULT 1,
    factory_level       INTEGER NOT NULL DEFAULT 1,
    furnace_fee         REAL NOT NULL DEFAULT 0.0,
    factory_fee         REAL NOT NULL DEFAULT 0.0,
    furnace_fees_collected REAL NOT NULL DEFAULT 0.0,
    factory_fees_collected REAL NOT NULL DEFAULT 0.0
);

-- A user's balance of ONE specific server's custom currency. Unlike
-- DragonCoin (global), this is scoped per (guild, user).
CREATE TABLE IF NOT EXISTS server_currency_balances (
    guild_id        INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    balance         REAL NOT NULL DEFAULT 0.0,
    PRIMARY KEY (guild_id, user_id)
);

-- Tracks chat activity within the current 14.4-minute (1%-of-a-day) window,
-- so the background task knows who to split currency between when the
-- window closes. Cleared out after each payout.
CREATE TABLE IF NOT EXISTS chat_activity_window (
    guild_id        INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    message_count   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);

-- A drill placed by a user in a specific server's dig site channel.
-- drill_type references data/materials.py (e.g. "iron_drill").
CREATE TABLE IF NOT EXISTS drills (
    drill_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    channel_id      INTEGER NOT NULL,
    owner_id        INTEGER NOT NULL,
    drill_type      TEXT NOT NULL,
    stored_amount   INTEGER NOT NULL DEFAULT 0,   -- raw materials waiting for /collect
    is_full         INTEGER NOT NULL DEFAULT 0,   -- 0/1 boolean: stopped until /collect
    last_harvest_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- A "mining block" created daily at midnight for a channel: a fixed pool of
-- raw materials that drills harvest from, oldest block first, per the design.
CREATE TABLE IF NOT EXISTS mining_blocks (
    block_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    channel_id      INTEGER NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    remaining_total INTEGER NOT NULL  -- sum of all remaining material counts in this block
);

-- The remaining count of each material type within a single mining block.
CREATE TABLE IF NOT EXISTS mining_block_contents (
    block_id        INTEGER NOT NULL,
    material_id     TEXT NOT NULL,
    remaining       INTEGER NOT NULL,
    PRIMARY KEY (block_id, material_id),
    FOREIGN KEY (block_id) REFERENCES mining_blocks(block_id)
);

-- A queued furnace (smelting) or factory (crafting) job for a user in a guild.
-- job_type is either 'furnace' or 'factory'; target_id is the material_id
-- being produced (e.g. "iron", "wiring").
CREATE TABLE IF NOT EXISTS production_jobs (
    job_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    job_type        TEXT NOT NULL CHECK (job_type IN ('furnace', 'factory')),
    target_id       TEXT NOT NULL,
    quantity        INTEGER NOT NULL,
    queued_at       TEXT NOT NULL DEFAULT (datetime('now')),
    status          TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'in_progress', 'complete'))
);
