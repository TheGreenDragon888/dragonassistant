"""
cogs/setup.py

Implements the /setup command group, restricted to members with "Manage
Server" permission, matching the design doc:
  /setup mine                          - designate this channel as a dig site
  /setup currency <name> <emoji>       - configure this server's currency
  /setup fee <furnace|factory> <amt>   - set infrastructure usage fee

A "cog" is discord.py's term for a self-contained module of commands/events
that gets loaded into the bot at startup (see bot.py's load_extension calls).
"""
import discord
from discord import app_commands
from discord.ext import commands


class SetupCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db  # the shared Database instance, attached in bot.py

    # A "group" bundles related slash commands under one parent, so users
    # see them in Discord as /setup mine, /setup currency, /setup fee.
    setup_group = app_commands.Group(
        name="setup", description="Server configuration (requires Manage Server permission)"
    )

    async def _ensure_server_row(self, guild_id: int):
        """Makes sure a server_config row exists before we try to UPDATE it."""
        await self.db.execute(
            "INSERT OR IGNORE INTO server_config (guild_id) VALUES (?)",
            (guild_id,),
        )

    @setup_group.command(name="mine", description="Designate this channel as a mining dig site")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def setup_mine(self, interaction: discord.Interaction):
        await self._ensure_server_row(interaction.guild_id)
        await self.db.execute(
            "UPDATE server_config SET mine_channel_id = ? WHERE guild_id = ?",
            (interaction.channel_id, interaction.guild_id),
        )
        await interaction.response.send_message(
            f"✅ This channel is now the designated dig site for **{interaction.guild.name}**.",
            ephemeral=True,
        )

    @setup_group.command(name="currency", description="Set this server's custom currency name and emoji")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(name="Currency name, e.g. 'Gold'", emoji="Emoji to represent the currency, e.g. 🪙")
    async def setup_currency(self, interaction: discord.Interaction, name: str, emoji: str):
        await self._ensure_server_row(interaction.guild_id)
        await self.db.execute(
            "UPDATE server_config SET currency_name = ?, currency_emoji = ? WHERE guild_id = ?",
            (name, emoji, interaction.guild_id),
        )
        await interaction.response.send_message(
            f"✅ This server's currency is now **{name}** {emoji}.",
            ephemeral=True,
        )

    @setup_group.command(name="fee", description="Set a fee (in server currency) to use the furnace or factory")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.describe(infrastructure="Which infrastructure to set a fee for", amount="Fee per item produced")
    @app_commands.choices(infrastructure=[
        app_commands.Choice(name="furnace", value="furnace"),
        app_commands.Choice(name="factory", value="factory"),
    ])
    async def setup_fee(self, interaction: discord.Interaction, infrastructure: app_commands.Choice[str], amount: float):
        if amount < 0:
            await interaction.response.send_message("Fee can't be negative.", ephemeral=True)
            return
        await self._ensure_server_row(interaction.guild_id)
        column = "furnace_fee" if infrastructure.value == "furnace" else "factory_fee"
        await self.db.execute(
            f"UPDATE server_config SET {column} = ? WHERE guild_id = ?",
            (amount, interaction.guild_id),
        )
        await interaction.response.send_message(
            f"✅ {infrastructure.value.title()} fee set to **{amount}** per item.",
            ephemeral=True,
        )

    @setup_mine.error
    @setup_currency.error
    @setup_fee.error
    async def setup_error_handler(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        # Fires when a non-admin tries to run a /setup command.
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "You need the **Manage Server** permission to do that.", ephemeral=True
            )
        else:
            raise error


async def setup(bot: commands.Bot):
    """The special function discord.py looks for when loading this file as
    an extension (see bot.py: await bot.load_extension('cogs.setup')).
    Note: bot.add_cog() automatically registers any app_commands.Group
    class attributes on the cog - no need to call bot.tree.add_command()
    separately (doing so causes a "CommandAlreadyRegistered" error)."""
    await bot.add_cog(SetupCog(bot))
