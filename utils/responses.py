"""
utils/responses.py

Shared helper for sending a command's main (successful) response. Per-server,
bot responses are private (ephemeral) by default so the bot doesn't clutter
channels - a "Manage Server" admin can opt a server into public responses via
/setup messages public, e.g. once they've set up a dedicated bot channel.

This does NOT apply to error/validation messages (missing permissions,
insufficient materials, etc.) - those should keep using
interaction.response.send_message(..., ephemeral=True) directly, since
they're personal to the user who triggered them regardless of the server's
setting.
"""
import discord

from database.db import Database


async def respond(interaction: discord.Interaction, db: Database, **kwargs):
    """Sends the interaction's main response, ephemeral unless this server
    has opted into public bot messages."""
    public = False
    if interaction.guild_id is not None:
        cfg = await db.fetchone(
            "SELECT public_messages FROM server_config WHERE guild_id = ?",
            (interaction.guild_id,),
        )
        public = bool(cfg["public_messages"]) if cfg else False

    await interaction.response.send_message(ephemeral=not public, **kwargs)
