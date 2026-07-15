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

-- Per-server settings: the server's custom currency name/emoji, furnace/
-- factory levels, and its shared raw-material mining pool.
CREATE TABLE IF NOT EXISTS server_config (
    guild_id            INTEGER PRIMARY KEY,
    currency_name       TEXT,
    currency_emoji      TEXT,
    furnace_level       INTEGER NOT NULL DEFAULT 1,
    factory_level       INTEGER NOT NULL DEFAULT 1,
    furnace_fee         REAL NOT NULL DEFAULT 0.0,
    factory_fee         REAL NOT NULL DEFAULT 0.0,
    furnace_fees_collected REAL NOT NULL DEFAULT 0.0,
    factory_fees_collected REAL NOT NULL DEFAULT 0.0,
    furnace_max_queue   INTEGER NOT NULL DEFAULT 25,
    factory_max_queue   INTEGER NOT NULL DEFAULT 5,
    -- 0/1 boolean: whether bot responses are public in this server instead of
    -- ephemeral (private). Off by default - see utils/responses.py.
    public_messages         INTEGER NOT NULL DEFAULT 0,
    -- The server-wide shared pool of unharvested raw materials that drills draw
    -- from. Topped up once/day by mining_pool_last_topup's date changing.
    mining_pool_remaining    INTEGER NOT NULL DEFAULT 0,
    mining_pool_last_topup   TEXT NOT NULL DEFAULT '',
    -- Lifetime faucet/sink running totals for this server's currency, per
    -- docs/market.md section 4. Minted only by the market buying materials
    -- from users; burned by furnace/factory fees and the market selling
    -- materials back to users.
    currency_minted_total    REAL NOT NULL DEFAULT 0.0,
    currency_burned_total    REAL NOT NULL DEFAULT 0.0
);

-- A user's balance of ONE specific server's custom currency. Unlike
-- DragonCoin (global), this is scoped per (guild, user).
CREATE TABLE IF NOT EXISTS server_currency_balances (
    guild_id        INTEGER NOT NULL,
    user_id         INTEGER NOT NULL,
    balance         REAL NOT NULL DEFAULT 0.0,
    PRIMARY KEY (guild_id, user_id)
);

-- The server's own material storage - the market's inventory, acquired from
-- and sold back to users (docs/market.md section 3). Only raw and smelted
-- materials are ever stored here; components/drills are not tradeable.
CREATE TABLE IF NOT EXISTS server_material_storage (
    guild_id        INTEGER NOT NULL,
    material_id     TEXT NOT NULL,
    quantity        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, material_id)
);

-- A drill placed by a user somewhere in a server (mining is no longer
-- restricted to a designated channel). drill_type references
-- data/materials.py (e.g. "iron_drill").
CREATE TABLE IF NOT EXISTS drills (
    drill_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    owner_id        INTEGER NOT NULL,
    drill_type      TEXT NOT NULL,
    stored_amount   INTEGER NOT NULL DEFAULT 0,   -- raw materials waiting for /collect
    is_full         INTEGER NOT NULL DEFAULT 0,   -- 0/1 boolean: stopped until /collect
    last_harvest_at TEXT NOT NULL DEFAULT (datetime('now'))
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
