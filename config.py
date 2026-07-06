"""
config.py

Loads settings from the .env file (or real environment variables) into one
place. Every other module imports from here instead of calling os.getenv()
directly - that way, if you ever change how config is loaded, you only edit
one file.
"""
import os
from dotenv import load_dotenv

# Reads the ".env" file in the current directory and loads its key=value
# pairs into the process environment (os.environ). If .env doesn't exist,
# this just does nothing and os.getenv() falls back to real env vars,
# which is useful in production (e.g. systemd EnvironmentFile).
load_dotenv()

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DATABASE_PATH = os.getenv("DATABASE_PATH", "data/dragonassistant.db")

# getenv returns a string or None. We convert to int only if present, since
# discord.py's guild sync functions expect an int object ID, not a string.
_dev_guild = os.getenv("DEV_GUILD_ID")
DEV_GUILD_ID = int(_dev_guild) if _dev_guild else None

if not DISCORD_BOT_TOKEN:
    raise RuntimeError(
        "DISCORD_BOT_TOKEN is not set. Copy .env.example to .env and fill it in."
    )
