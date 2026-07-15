"""
cogs/economy.py

Implements:
  - /balance                              - your balance of this server's currency
  - /inventory                            - your inventory alongside your balance
  - /market sell <material> <quantity>    - sell to the server (the currency faucet)
  - /market buy <material> <quantity>     - buy from the server's stock (a currency sink)
  - /market status                        - show the server's current stock and prices

Per docs/market.md, the server itself is an economic actor with its own
material storage (server_material_storage table). It buys raw/smelted
materials from users at a price that scales down as its stock approaches a
per-server "target stock" (member_count * MARKET_TARGET_STOCK_PER_MEMBER),
and always sells back to users at MARKET_SELL_MARKUP times a material's
market_ceiling_price - constrained by what it actually has in stock, since
the server can't sell what it never acquired.

DragonCoin (users.dragoncoin) is intentionally NOT surfaced here or anywhere
else - per docs/market.md section 2, it exists solely as a future conceptual
unit for cross-server exchange rates and isn't spendable, earnable, or shown
in any menu.

The passive per-message chat payout that used to live in this file has been
removed entirely (docs/market.md section 1: faucets should be tied to real
economic activity, not presence) - the market below is now this server's
only currency faucet, with furnace/factory fees and market buybacks as sinks.
"""
import discord
from discord import app_commands
from discord.ext import commands

from utils.responses import respond
from utils.embeds import add_multi_field
from utils.formatting import format_currency, format_market_currency, format_compact_number, DEFAULT_CURRENCY_EMOJI

from data.materials import (
    RAW_MATERIALS,
    SMELTED_MATERIALS,
    MARKET_SELL_MARKUP,
    get_material_info,
    target_stock,
)

# Only raw and smelted materials are tradeable through the market - component
# materials and drills are excluded (docs/market.md section 3).
TRADEABLE_MATERIALS = {**RAW_MATERIALS, **SMELTED_MATERIALS}


class EconomyCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db

    async def _ensure_user_row(self, user_id: int):
        await self.db.execute(
            "INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,)
        )

    async def _ensure_server_row(self, guild_id: int):
        await self.db.execute(
            "INSERT OR IGNORE INTO server_config (guild_id) VALUES (?)", (guild_id,)
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

    async def _get_server_stock(self, guild_id: int, material_id: str) -> int:
        row = await self.db.fetchone(
            "SELECT quantity FROM server_material_storage WHERE guild_id = ? AND material_id = ?",
            (guild_id, material_id),
        )
        return row["quantity"] if row else 0

    async def _adjust_server_stock(self, guild_id: int, material_id: str, delta: int):
        await self.db.execute(
            """
            INSERT INTO server_material_storage (guild_id, material_id, quantity) VALUES (?, ?, ?)
            ON CONFLICT (guild_id, material_id) DO UPDATE SET quantity = quantity + excluded.quantity
            """,
            (guild_id, material_id, delta),
        )

    async def _get_currency_emoji(self, guild_id: int) -> str | None:
        row = await self.db.fetchone(
            "SELECT currency_emoji FROM server_config WHERE guild_id = ?", (guild_id,)
        )
        return row["currency_emoji"] if row else None

    async def _get_currency_balance(self, guild_id: int, user_id: int) -> float:
        row = await self.db.fetchone(
            "SELECT balance FROM server_currency_balances WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        return row["balance"] if row else 0.0

    async def _adjust_currency_balance(self, guild_id: int, user_id: int, delta: float):
        await self.db.execute(
            """
            INSERT INTO server_currency_balances (guild_id, user_id, balance) VALUES (?, ?, ?)
            ON CONFLICT (guild_id, user_id) DO UPDATE SET balance = balance + excluded.balance
            """,
            (guild_id, user_id, delta),
        )

    async def _record_minted(self, guild_id: int, amount: float):
        await self.db.execute(
            "UPDATE server_config SET currency_minted_total = currency_minted_total + ? WHERE guild_id = ?",
            (amount, guild_id),
        )

    async def _record_burned(self, guild_id: int, amount: float):
        await self.db.execute(
            "UPDATE server_config SET currency_burned_total = currency_burned_total + ? WHERE guild_id = ?",
            (amount, guild_id),
        )

    def _buy_price(self, ceiling_price: float, current_stock: int, quantity: int, target_stock: int) -> float:
        """Price the server pays to acquire `quantity` units it doesn't yet
        have, given it currently holds `current_stock` out of `target_stock`.
        Per-unit price decreases linearly from ceiling_price (at 0 stock) to 0
        (at target_stock and beyond) - priced using the average over however
        much of this sale actually falls below target_stock. Any portion of
        the sale that would push stock past target_stock is still accepted
        into server storage, just isn't paid for."""
        if target_stock <= 0 or current_stock >= target_stock:
            return 0.0
        priced_units = min(quantity, target_stock - current_stock)
        end_stock = current_stock + priced_units
        midpoint_stock = (current_stock + end_stock) / 2
        unit_price = ceiling_price * (1 - midpoint_stock / target_stock)
        return unit_price * priced_units

    def _sell_price(self, ceiling_price: float, quantity: int) -> float:
        """Flat price the server charges to sell `quantity` units back to a
        user - always MARKET_SELL_MARKUP times the material's ceiling price
        per unit, regardless of current stock."""
        return ceiling_price * MARKET_SELL_MARKUP * quantity

    async def _currency_lines(self, interaction: discord.Interaction) -> list[str]:
        """Every server currency balance this user holds, formatted for
        display. The current server's currency always comes first (even if
        the balance is 0), followed by every other server's currency ordered
        highest balance to lowest."""
        rows = await self.db.fetchall(
            """
            SELECT scb.guild_id, scb.balance, sc.currency_name, sc.currency_emoji
            FROM server_currency_balances scb
            JOIN server_config sc ON sc.guild_id = scb.guild_id
            WHERE scb.user_id = ?
            """,
            (interaction.user.id,),
        )
        by_guild = {row["guild_id"]: row for row in rows}

        ordered_rows = []
        if interaction.guild_id is not None:
            current = by_guild.pop(interaction.guild_id, None)
            if current is None:
                server_cfg = await self.db.fetchone(
                    "SELECT currency_name, currency_emoji FROM server_config WHERE guild_id = ?",
                    (interaction.guild_id,),
                )
                current = {
                    "guild_id": interaction.guild_id,
                    "balance": 0.0,
                    "currency_name": server_cfg["currency_name"] if server_cfg else None,
                    "currency_emoji": server_cfg["currency_emoji"] if server_cfg else None,
                }
            ordered_rows.append(current)

        ordered_rows.extend(sorted(by_guild.values(), key=lambda r: r["balance"], reverse=True))

        lines = []
        for row in ordered_rows:
            emoji = row["currency_emoji"] if row["currency_name"] else None
            guild = self.bot.get_guild(row["guild_id"])
            guild_name = guild.name if guild else f"Server {row['guild_id']}"
            suffix = " (this server)" if row["guild_id"] == interaction.guild_id else ""
            lines.append(f"{format_currency(row['balance'], emoji)} - {guild_name}{suffix}")
        return lines

    @app_commands.command(name="balance", description="Check your currency balances across every server")
    async def balance(self, interaction: discord.Interaction):
        embed = discord.Embed(title=f"{interaction.user.display_name}'s Balance", color=discord.Color.gold())
        lines = await self._currency_lines(interaction)
        add_multi_field(embed, "Currencies", lines)
        await respond(interaction, self.db, embed=embed)

    @app_commands.command(name="inventory", description="Show your inventory alongside your balance")
    async def inventory(self, interaction: discord.Interaction):
        embed = discord.Embed(title=f"{interaction.user.display_name}'s Inventory", color=discord.Color.gold())

        currency_lines = await self._currency_lines(interaction)
        if currency_lines:
            embed.description = currency_lines[0]

        inventory_rows = await self.db.fetchall(
            "SELECT material_id, quantity FROM user_materials WHERE user_id = ? AND quantity > 0 ORDER BY material_id",
            (interaction.user.id,),
        )

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

        await respond(interaction, self.db, embed=embed)

    market_group = app_commands.Group(name="market", description="Trade raw and smelted materials with the server")

    @market_group.command(name="sell", description="Sell materials from your inventory to the server")
    @app_commands.describe(material="What to sell", quantity="How many to sell")
    @app_commands.choices(material=[
        app_commands.Choice(name=info["name"], value=key) for key, info in TRADEABLE_MATERIALS.items()
    ])
    async def market_sell(self, interaction: discord.Interaction, material: app_commands.Choice[str], quantity: app_commands.Range[int, 1, 1000]):
        await self._ensure_user_row(interaction.user.id)
        await self._ensure_server_row(interaction.guild_id)

        have = await self._get_quantity(interaction.user.id, material.value)
        if have < quantity:
            await interaction.response.send_message(f"You only have {have} of that item.", ephemeral=True)
            return

        info = TRADEABLE_MATERIALS[material.value]
        ceiling_price = info["market_ceiling_price"]
        current_stock = await self._get_server_stock(interaction.guild_id, material.value)
        target = target_stock(interaction.guild.member_count)
        total_value = self._buy_price(ceiling_price, current_stock, quantity, target)

        if total_value <= 0:
            await interaction.response.send_message(
                f"The server is already fully stocked on **{info['name']}** and won't pay anything more for it right now.",
                ephemeral=True,
            )
            return

        await self._adjust_quantity(interaction.user.id, material.value, -quantity)
        await self._adjust_server_stock(interaction.guild_id, material.value, quantity)
        await self._adjust_currency_balance(interaction.guild_id, interaction.user.id, total_value)
        await self._record_minted(interaction.guild_id, total_value)

        currency_emoji = await self._get_currency_emoji(interaction.guild_id)
        await respond(
            interaction, self.db,
            content=f"Sold {quantity}x **{info['name']}** to the server for {format_market_currency(total_value, currency_emoji)}.",
        )

    @market_group.command(name="buy", description="Buy materials from the server's stock")
    @app_commands.describe(material="What to buy", quantity="How many to buy")
    @app_commands.choices(material=[
        app_commands.Choice(name=info["name"], value=key) for key, info in TRADEABLE_MATERIALS.items()
    ])
    async def market_buy(self, interaction: discord.Interaction, material: app_commands.Choice[str], quantity: app_commands.Range[int, 1, 1000]):
        await self._ensure_user_row(interaction.user.id)
        await self._ensure_server_row(interaction.guild_id)

        current_stock = await self._get_server_stock(interaction.guild_id, material.value)
        if current_stock < quantity:
            await interaction.response.send_message(
                f"The server only has {current_stock} of that in stock.", ephemeral=True
            )
            return

        info = TRADEABLE_MATERIALS[material.value]
        ceiling_price = info["market_ceiling_price"]
        total_cost = self._sell_price(ceiling_price, quantity)
        currency_emoji = await self._get_currency_emoji(interaction.guild_id)

        balance = await self._get_currency_balance(interaction.guild_id, interaction.user.id)
        if balance < total_cost:
            await interaction.response.send_message(
                f"This costs {format_market_currency(total_cost, currency_emoji)}, but you only have {format_market_currency(balance, currency_emoji)}.",
                ephemeral=True,
            )
            return

        await self._adjust_currency_balance(interaction.guild_id, interaction.user.id, -total_cost)
        await self._adjust_server_stock(interaction.guild_id, material.value, -quantity)
        await self._adjust_quantity(interaction.user.id, material.value, quantity)
        await self._record_burned(interaction.guild_id, total_cost)

        await respond(
            interaction, self.db,
            content=f"Bought {quantity}x **{info['name']}** from the server for {format_market_currency(total_cost, currency_emoji)}.",
        )

    @market_group.command(name="status", description="Show the server's current market prices")
    async def market_status(self, interaction: discord.Interaction):
        await self._ensure_server_row(interaction.guild_id)
        target = target_stock(interaction.guild.member_count)
        currency_emoji = await self._get_currency_emoji(interaction.guild_id) or DEFAULT_CURRENCY_EMOJI

        # Three parallel columns (Material / Sell / Buy) rather than one long
        # line per material - custom material emoji only render outside a
        # code block, and bare numbers only align into columns inside one,
        # so the two live in separate embed fields instead of the same line.
        material_lines = []
        sell_values = []
        buy_values = []

        # Add a spacer at the beginning to account for the monospace vertical offset
        # material_lines.append("‎")  # invisible character

        for material_id, info in TRADEABLE_MATERIALS.items():
            current_stock = await self._get_server_stock(interaction.guild_id, material_id)
            ceiling_price = info["market_ceiling_price"]
            # SELL = what you receive per unit selling to the server (/market sell).
            # BUY = what you pay per unit buying from the server (/market buy).
            sell_price_each = self._buy_price(ceiling_price, current_stock, 1, target)
            buy_price_each = ceiling_price * MARKET_SELL_MARKUP
            material_lines.append(f"{info['emoji']} {info['name']}")
            sell_values.append(format_compact_number(sell_price_each))
            buy_values.append(format_compact_number(buy_price_each))

        # width = max(len(v) for v in sell_values + buy_values)
        # sell_block = "```\n" + "\n".join(v.rjust(width) for v in sell_values) + "\n```"
        # buy_block = "```\n" + "\n".join(v.rjust(width) for v in buy_values) + "\n```"

        embed = discord.Embed(title="Server Market", color=discord.Color.gold())
        embed.add_field(name="Material", value="\n".join(material_lines), inline=True)
        embed.add_field(name=f"{currency_emoji} Sell", value="\n".join(sell_values), inline=True)
        embed.add_field(name=f"{currency_emoji} Buy", value="\n".join(buy_values), inline=True)
        await respond(interaction, self.db, embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(EconomyCog(bot))
