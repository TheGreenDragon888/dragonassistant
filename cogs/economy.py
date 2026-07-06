"""
cogs/economy.py

Handles:
  - Tracking chat activity (every message increments a counter)
  - A background loop that fires every 14.4 minutes (1% of a day) and splits
    0.10 of the server's currency proportionally among everyone who chatted
  - /balance - shows a user's DragonCoin and this server's currency balance
"""
import discord
from discord import app_commands
from discord.ext import commands, tasks

from data.materials import CHAT_WINDOW_SECONDS, CHAT_WINDOW_PAYOUT


class EconomyCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db
        self.payout_loop.start()

    def cog_unload(self):
        # Ensures the loop stops cleanly if this cog is ever reloaded.
        self.payout_loop.cancel()

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Ignore DMs and messages from bots (including this bot) to prevent
        # farming currency with bot-to-bot loops.
        if message.guild is None or message.author.bot:
            return
        await self.db.execute(
            """
            INSERT INTO chat_activity_window (guild_id, user_id, message_count)
            VALUES (?, ?, 1)
            ON CONFLICT (guild_id, user_id)
            DO UPDATE SET message_count = message_count + 1
            """,
            (message.guild.id, message.author.id),
        )

    @tasks.loop(seconds=CHAT_WINDOW_SECONDS)
    async def payout_loop(self):
        """Runs every 864 seconds (14.4 minutes). For each server with chat
        activity in the window, splits CHAT_WINDOW_PAYOUT (0.10) of that
        server's currency proportionally by message count, then clears the
        window so the next period starts fresh."""
        guild_ids = await self.db.fetchall(
            "SELECT DISTINCT guild_id FROM chat_activity_window"
        )
        for row in guild_ids:
            guild_id = row["guild_id"]
            participants = await self.db.fetchall(
                "SELECT user_id, message_count FROM chat_activity_window WHERE guild_id = ?",
                (guild_id,),
            )
            total_messages = sum(p["message_count"] for p in participants)
            if total_messages == 0:
                continue

            for p in participants:
                share = CHAT_WINDOW_PAYOUT * (p["message_count"] / total_messages)
                await self.db.execute(
                    """
                    INSERT INTO server_currency_balances (guild_id, user_id, balance)
                    VALUES (?, ?, ?)
                    ON CONFLICT (guild_id, user_id)
                    DO UPDATE SET balance = balance + excluded.balance
                    """,
                    (guild_id, p["user_id"], share),
                )

            # Reset this guild's window now that payouts are done.
            await self.db.execute(
                "DELETE FROM chat_activity_window WHERE guild_id = ?", (guild_id,)
            )

    @payout_loop.before_loop
    async def before_payout_loop(self):
        # Waits until the bot has fully connected before the first tick,
        # otherwise the loop could fire before the database is ready.
        await self.bot.wait_until_ready()

    async def _ensure_user_row(self, user_id: int):
        await self.db.execute(
            "INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,)
        )

    @app_commands.command(name="balance", description="Check your DragonCoin and this server's currency balance")
    async def balance(self, interaction: discord.Interaction):
        await self._ensure_user_row(interaction.user.id)
        user_row = await self.db.fetchone(
            "SELECT dragoncoin FROM users WHERE user_id = ?", (interaction.user.id,)
        )
        dragoncoin = user_row["dragoncoin"] if user_row else 0.0

        embed = discord.Embed(title=f"{interaction.user.display_name}'s Balance", color=discord.Color.gold())
        embed.add_field(name="DragonCoin", value=f"<:DragonCoin:1523399622008246312> {dragoncoin:.2f}", inline=False)

        if interaction.guild is not None:
            server_cfg = await self.db.fetchone(
                "SELECT currency_name, currency_emoji FROM server_config WHERE guild_id = ?",
                (interaction.guild_id,),
            )
            server_balance_row = await self.db.fetchone(
                "SELECT balance FROM server_currency_balances WHERE guild_id = ? AND user_id = ?",
                (interaction.guild_id, interaction.user.id),
            )
            balance = server_balance_row["balance"] if server_balance_row else 0.0
            if server_cfg and server_cfg["currency_name"]:
                embed.add_field(
                    name=server_cfg["currency_name"],
                    value=f"{server_cfg['currency_emoji']} {balance:.2f}",
                    inline=False,
                )
            else:
                embed.add_field(
                    name="Server Currency",
                    value="Not set up yet - an admin can run `/setup currency`",
                    inline=False,
                )

        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(EconomyCog(bot))
