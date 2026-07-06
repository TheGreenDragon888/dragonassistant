"""
bot.py

The entry point you actually run: `python bot.py`.

What it does, in order:
  1. Creates a discord.py Bot instance with the permissions ("intents") it needs
  2. Attaches a shared Database object so every cog can query SQLite
  3. Loads each cog (extension) from the cogs/ folder
  4. Syncs slash commands with Discord so they show up in the / menu
  5. Logs in and starts listening for events
"""
import logging

import discord
from discord.ext import commands

import config
from database.db import Database

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("dragonassistant")

# Intents are permission flags telling Discord which events your bot wants
# to receive. message_content is required to read chat text (needed for the
# chat-mining feature); it must ALSO be enabled in the Discord Developer
# Portal under your bot's "Privileged Gateway Intents", or login will fail.
intents = discord.Intents.default()
intents.message_content = True
intents.members = True  # needed for guild.member_count used in mining blocks

bot = commands.Bot(command_prefix="!", intents=intents)

# Every cog accesses this via `bot.db`, so it's created once here and shared,
# rather than each cog opening its own separate connection pool.
bot.db = Database(config.DATABASE_PATH)

# Every file in cogs/ that should be loaded as an extension. Add new cogs
# here as you build more features.
INITIAL_EXTENSIONS = [
    "cogs.setup",
    "cogs.economy",
    "cogs.mining",
    "cogs.furnace",
    "cogs.factory",
]


@bot.event
async def setup_hook():
    """discord.py calls this automatically once, before the bot logs in.
    This is the correct place to load extensions and sync the command tree -
    doing it here (rather than in on_ready) guarantees it only runs once,
    even if the bot's connection drops and reconnects later."""
    await bot.db.init_schema()
    log.info("Database schema ready.")

    for ext in INITIAL_EXTENSIONS:
        await bot.load_extension(ext)
        log.info(f"Loaded extension: {ext}")

    if config.DEV_GUILD_ID:
        # Fast path for development: syncing to one specific guild applies
        # instantly, whereas a global sync can take up to an hour to show
        # up in every server's slash command list.
        guild = discord.Object(id=config.DEV_GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        log.info(f"Synced {len(synced)} commands to dev guild {config.DEV_GUILD_ID}.")
    else:
        synced = await bot.tree.sync()
        log.info(f"Synced {len(synced)} commands globally.")


@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")


if __name__ == "__main__":
    bot.run(config.DISCORD_BOT_TOKEN, log_handler=None)
