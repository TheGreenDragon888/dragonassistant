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

from data.materials import (
    CHAT_WINDOW_SECONDS,
    CHAT_WINDOW_PAYOUT,
    RAW_MATERIALS,
    SMELTED_MATERIALS,
    COMPONENT_MATERIALS,
    get_material_info,
)

SELLABLE_MATERIALS = {**RAW_MATERIALS, **SMELTED_MATERIALS, **COMPONENT_MATERIALS}


class EconomyCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db
        self.payout_loop.start()

    async def _ensure_user_row(self, user_id: int):
        await self.db.execute(
            "INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,)
        )

    async def _get_quantity(self, user_id: int, material_id: str) -> int:
        row = await self.db.fetchone(
            "SELECT quantity FROM user_materials WHERE user_id = ? AND material_id = ?",
            (user_id, material_id),
        )
        return row["quantity"] if row else 0

    async def _adjust_quantity(self, user_id: int, material_id: str, delta: int):
        await self.db.execute(
            """
            INSERT INTO user_materials (user_id, material_id, quantity) VALUES (?, ?, ?)
            ON CONFLICT (user_id, material_id) DO UPDATE SET quantity = quantity + excluded.quantity
            """,
            (user_id, material_id, delta),
        )

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

    @app_commands.command(name="balance", description="Check your DragonCoin and this server's currency balance")
    async def balance(self, interaction: discord.Interaction):
        await self._ensure_user_row(interaction.user.id)
        user_row = await self.db.fetchone(
            "SELECT dragoncoin FROM users WHERE user_id = ?", (interaction.user.id,)
        )
        dragoncoin = user_row["dragoncoin"] if user_row else 0.0

        embed = discord.Embed(title=f"{interaction.user.display_name}'s Balance", color=discord.Color.gold())
        currency_parts = [f"<:DragonCoin:1523399622008246312> {dragoncoin:.2f}"]

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
                currency_parts.append(f"{server_cfg['currency_emoji']} {balance:.2f}")
            else:
                currency_parts.append("💰 0.00")

        embed.add_field(name="Currencies", value=" ".join(currency_parts), inline=False)

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="inventory", description="Show your inventory alongside your balances")
    async def inventory(self, interaction: discord.Interaction):
        await self._ensure_user_row(interaction.user.id)
        user_row = await self.db.fetchone(
            "SELECT dragoncoin FROM users WHERE user_id = ?", (interaction.user.id,)
        )
        dragoncoin = user_row["dragoncoin"] if user_row else 0.0

        currency_rows = await self.db.fetchall(
            """
            SELECT scb.guild_id, scb.balance, sc.currency_emoji
            FROM server_currency_balances scb
            JOIN server_config sc ON sc.guild_id = scb.guild_id
            WHERE scb.user_id = ?
            ORDER BY scb.balance DESC
            LIMIT 3
            """,
            (interaction.user.id,),
        )

        inventory_rows = await self.db.fetchall(
            "SELECT material_id, quantity FROM user_materials WHERE user_id = ? AND quantity > 0 ORDER BY material_id",
            (interaction.user.id,),
        )

        embed = discord.Embed(title=f"{interaction.user.display_name}'s Inventory", color=discord.Color.gold())
        balance_parts = [f"<:DragonCoin:1523399622008246312> {dragoncoin:.2f}"]
        for row in currency_rows:
            balance_parts.append(f"{row['currency_emoji'] or '💰'} {row['balance']:.2f}")
        if not currency_rows:
            balance_parts.append("💰 0.00")
        embed.description = " ".join(balance_parts)

        inventory_lines = []
        for row in inventory_rows:
            info = get_material_info(row["material_id"])
            if info is None:
                continue
            inventory_lines.append(f"{info['emoji']} {row['quantity']}")

        if inventory_lines:
            grid_lines = []
            for i in range(0, len(inventory_lines), 4):
                grid_lines.append(" ".join(inventory_lines[i:i + 4]))
            embed.add_field(name="Items", value="\n".join(grid_lines), inline=False)
        else:
            embed.add_field(name="Items", value="Your inventory is empty.", inline=False)

        await interaction.response.send_message(embed=embed)

    market_group = app_commands.Group(name="market", description="Sell materials for DragonCoin")

    @market_group.command(name="sell", description="Sell materials from your inventory for DragonCoin")
    @app_commands.describe(material="What to sell", quantity="How many to sell")
    @app_commands.choices(material=[
        app_commands.Choice(name=info["name"], value=key) for key, info in SELLABLE_MATERIALS.items()
    ])
    async def market_sell(self, interaction: discord.Interaction, material: app_commands.Choice[str], quantity: app_commands.Range[int, 1, 1000]):
        await self._ensure_user_row(interaction.user.id)
        info = SELLABLE_MATERIALS[material.value]
        dc_value = info.get("dc_value", 0.0)
        if dc_value <= 0:
            await interaction.response.send_message("That item is not currently sellable.", ephemeral=True)
            return

        have = await self._get_quantity(interaction.user.id, material.value)
        if have < quantity:
            await interaction.response.send_message(f"You only have {have} of that item.", ephemeral=True)
            return

        await self._adjust_quantity(interaction.user.id, material.value, -quantity)
        total_value = dc_value * quantity
        await self.db.execute(
            "UPDATE users SET dragoncoin = dragoncoin + ? WHERE user_id = ?",
            (total_value, interaction.user.id),
        )
        await interaction.response.send_message(
            f"💰 Sold {quantity}x **{info['name']}** for **{total_value:.2f}** DragonCoin."
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(EconomyCog(bot))
