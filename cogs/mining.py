"""
cogs/mining.py

Implements:
  - /mine place <drill_type> - place a drill anywhere in the server (max 3/user/server)
  - /mine status              - show your active drills + this server's mining pool
  - /collect                  - empty your full drill(s) into your inventory
  - /mine remove <drill_type> - pull a drill back out early, refunding it + its contents
  - A background loop that tops up each server's shared raw-material pool once
    per day, and another loop that has drills harvest from that pool periodically.

Mining is server-wide, not channel-scoped - there's no more designated "dig
site" channel. Every server has a single raw-material pool that all of that
server's drills draw from, regardless of which channel a command is run in.

HARVEST_TICK_MINUTES is 24 (2.5 ticks/hour) rather than something rounder like
5 or 10, because every drill's mines_per_hour is a multiple of 2.5 - so each
tick harvests a whole number of items per drill with no rounding.
"""
import random
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils.responses import respond

from data.materials import (
    DRILLS,
    RAW_MATERIALS,
    MAX_DRILLS_PER_USER_PER_SERVER,
    MINING_POOL_DAILY_PER_MEMBER,
    MINING_POOL_CAP_MULTIPLIER,
    get_material_info,
)

HARVEST_TICK_MINUTES = 24
POOL_TOPUP_CHECK_MINUTES = 60


def build_material_breakdown(total_items: int, roll_material=None) -> dict[str, int]:
    if total_items <= 0:
        return {}
    breakdown: dict[str, int] = {}
    for _ in range(total_items):
        material_id = roll_material() if roll_material is not None else next(iter(RAW_MATERIALS))
        breakdown[material_id] = breakdown.get(material_id, 0) + 1
    return breakdown


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


class MiningCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db
        self.harvest_loop.start()
        self.pool_topup_loop.start()

    def cog_unload(self):
        self.harvest_loop.cancel()
        self.pool_topup_loop.cancel()

    mine_group = app_commands.Group(name="mine", description="Manage mining drills")

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

    def _roll_raw_material(self) -> str:
        roll = random.random()
        cumulative = 0.0
        for mat_id, info in RAW_MATERIALS.items():
            cumulative += info["drop_chance"]
            if roll <= cumulative:
                return mat_id
        # RAW_MATERIALS' drop_chance values sum to exactly 1.0, so this only
        # triggers on float rounding at the roll==1.0 edge - falls back to the
        # first (most common) material rather than ever returning nothing.
        return next(iter(RAW_MATERIALS))

    async def _ensure_player_has_any_drill(self, user_id: int) -> bool:
        """If a player has no drills in inventory or placed in any server, give them an iron drill."""
        await self._ensure_user_row(user_id)

        for drill_id in DRILLS:
            if await self._get_quantity(user_id, drill_id) > 0:
                return False

        placed_count = await self.db.fetchone(
            "SELECT COUNT(*) AS cnt FROM drills WHERE owner_id = ?",
            (user_id,),
        )
        if placed_count and placed_count["cnt"] > 0:
            return False

        await self._adjust_quantity(user_id, "iron_drill", 1)
        return True

    @mine_group.command(name="place", description="Place a drill somewhere in this server")
    @app_commands.describe(drill_type="Which drill to place")
    @app_commands.choices(drill_type=[
        app_commands.Choice(name=info["name"], value=key) for key, info in DRILLS.items()
    ])
    async def mine_place(self, interaction: discord.Interaction, drill_type: app_commands.Choice[str]):
        await self._ensure_server_row(interaction.guild_id)

        existing = await self.db.fetchone(
            "SELECT COUNT(*) AS cnt FROM drills WHERE guild_id = ? AND owner_id = ?",
            (interaction.guild_id, interaction.user.id),
        )
        if existing["cnt"] >= MAX_DRILLS_PER_USER_PER_SERVER:
            await interaction.response.send_message(
                f"You already have the max of {MAX_DRILLS_PER_USER_PER_SERVER} drills in this server.",
                ephemeral=True,
            )
            return

        fallback_granted = await self._ensure_player_has_any_drill(interaction.user.id)
        have = await self._get_quantity(interaction.user.id, drill_type.value)
        if have < 1:
            if drill_type.value != "iron_drill" or not fallback_granted:
                await interaction.response.send_message(
                    f"You need one **{DRILLS[drill_type.value]['name']}** in your inventory before you can place it.",
                    ephemeral=True,
                )
                return

        await self._adjust_quantity(interaction.user.id, drill_type.value, -1)
        await self.db.execute(
            "INSERT INTO drills (guild_id, owner_id, drill_type) VALUES (?, ?, ?)",
            (interaction.guild_id, interaction.user.id, drill_type.value),
        )
        if fallback_granted:
            await respond(
                interaction, self.db,
                content="⛏️ You didn't have any drills, so I gave you an **Iron Drill** and placed it.",
            )
        else:
            await respond(
                interaction, self.db,
                content=f"⛏️ Placed a **{drill_type.name}** in this server.",
            )

    @mine_group.command(name="status", description="Show your drills and this server's mining pool")
    async def mine_status(self, interaction: discord.Interaction):
        await self._ensure_server_row(interaction.guild_id)

        drills = await self.db.fetchall(
            "SELECT * FROM drills WHERE guild_id = ? AND owner_id = ?",
            (interaction.guild_id, interaction.user.id),
        )
        cfg = await self.db.fetchone(
            "SELECT mining_pool_remaining FROM server_config WHERE guild_id = ?",
            (interaction.guild_id,),
        )
        pool_remaining = cfg["mining_pool_remaining"] if cfg else 0
        member_count = interaction.guild.member_count if interaction.guild else 0
        pool_cap = member_count * MINING_POOL_DAILY_PER_MEMBER * MINING_POOL_CAP_MULTIPLIER

        embed = discord.Embed(title="Mining Status", color=discord.Color.dark_gold())
        if not drills:
            embed.add_field(name="Your Drills", value="No drills placed yet.", inline=False)
        else:
            lines = []
            for d in drills:
                info = DRILLS[d["drill_type"]]
                status = "FULL - awaiting /collect" if d["is_full"] else "mining"
                lines.append(f"{info['emoji']} {info['name']}: {d['stored_amount']}/{info['storage_capacity']} - {status}")
            embed.add_field(name="Your Drills", value="\n".join(lines), inline=False)

        other_drills = await self.db.fetchall(
            "SELECT * FROM drills WHERE guild_id = ? AND owner_id != ? AND is_full = 0",
            (interaction.guild_id, interaction.user.id),
        )
        if other_drills:
            counts: dict[str, int] = {}
            for d in other_drills:
                counts[d["drill_type"]] = counts.get(d["drill_type"], 0) + 1

            lines = [
                f"{DRILLS[drill_type]['emoji']} {DRILLS[drill_type]['name']}{'s' if count != 1 else ''}: {count}"
                for drill_type, count in counts.items()
            ]
            embed.add_field(name="Other Active Drills in Server", value="\n".join(lines), inline=False)

        embed.add_field(
            name="<:MiningBlock:1523436645729173514> Server Mining Pool",
            value=f"{pool_remaining}/{pool_cap} raw materials remaining",
            inline=False,
        )

        await respond(interaction, self.db, embed=embed)

    @mine_group.command(name="remove", description="Remove a drill of a specific type and collect its items")
    @app_commands.describe(drill_type="Which drill type to remove")
    @app_commands.choices(drill_type=[
        app_commands.Choice(name=info["name"], value=key) for key, info in DRILLS.items()
    ])
    async def mine_remove(self, interaction: discord.Interaction, drill_type: app_commands.Choice[str]):
        # Find the drill of this type with the lowest stored_amount
        drill = await self.db.fetchone(
            "SELECT * FROM drills WHERE guild_id = ? AND owner_id = ? AND drill_type = ? ORDER BY stored_amount ASC LIMIT 1",
            (interaction.guild_id, interaction.user.id, drill_type.value),
        )
        if drill is None:
            await interaction.response.send_message(
                f"You don't have any **{drill_type.name}** drills here to remove.", ephemeral=True
            )
            return

        await self._ensure_user_row(interaction.user.id)

        # Collect items from this drill
        collected_breakdown = build_material_breakdown(drill["stored_amount"], self._roll_raw_material)
        for material_id, qty in collected_breakdown.items():
            await self._adjust_quantity(interaction.user.id, material_id, qty)

        # Refund the drill item to inventory
        await self._adjust_quantity(interaction.user.id, drill["drill_type"], 1)

        # Remove the drill
        await self.db.execute("DELETE FROM drills WHERE drill_id = ?", (drill["drill_id"],))

        # Build response embed
        embed = discord.Embed(title=f"Drill Removed", color=discord.Color.blurple())
        embed.add_field(name="Drill", value=f"{DRILLS[drill['drill_type']]['emoji']} Refunded to inventory", inline=False)

        if collected_breakdown:
            lines = []
            for mat_id, qty in sorted(collected_breakdown.items()):
                info = get_material_info(mat_id)
                if info:
                    lines.append(f"{info['emoji']} {qty}x {info['name']}")
            embed.add_field(name="Items Collected", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="Items Collected", value="None", inline=False)

        await respond(interaction, self.db, embed=embed)

    @app_commands.command(name="collect", description="Collect materials from your drill(s) in this server")
    async def collect(self, interaction: discord.Interaction):
        drills = await self.db.fetchall(
            "SELECT * FROM drills WHERE guild_id = ? AND owner_id = ? AND stored_amount > 0",
            (interaction.guild_id, interaction.user.id),
        )
        if not drills:
            await interaction.response.send_message("You have no drills with materials to collect here.", ephemeral=True)
            return

        await self._ensure_user_row(interaction.user.id)

        collected_breakdown = {}
        total_collected = 0
        for d in drills:
            total_collected += d["stored_amount"]
            drill_breakdown = build_material_breakdown(d["stored_amount"], self._roll_raw_material)
            for material_id, qty in drill_breakdown.items():
                collected_breakdown[material_id] = collected_breakdown.get(material_id, 0) + qty
            await self.db.execute(
                "UPDATE drills SET stored_amount = 0, is_full = 0 WHERE drill_id = ?",
                (d["drill_id"],),
            )

        for material_id, qty in collected_breakdown.items():
            await self._adjust_quantity(interaction.user.id, material_id, qty)

        # Build response embed
        embed = discord.Embed(title=f"Collection Complete", color=discord.Color.gold())
        embed.description = f"📦 Collected **{total_collected}** raw materials from **{len(drills)}** drill(s)"

        lines = []
        for mat_id in sorted(collected_breakdown.keys()):
            qty = collected_breakdown[mat_id]
            info = get_material_info(mat_id)
            if info:
                lines.append(f"{info['emoji']} {qty}x {info['name']}")

        if lines:
            embed.add_field(name="Materials", value="\n".join(lines), inline=False)

        await respond(interaction, self.db, embed=embed)

    @tasks.loop(minutes=POOL_TOPUP_CHECK_MINUTES)
    async def pool_topup_loop(self):
        """Checks every server the bot is in once an hour; if that server's UTC
        calendar date has changed since its last top-up, adds
        (member_count * MINING_POOL_DAILY_PER_MEMBER) to its pool, capped at
        MINING_POOL_CAP_MULTIPLIER times that daily amount."""
        today = _today()
        for guild in self.bot.guilds:
            await self._ensure_server_row(guild.id)
            cfg = await self.db.fetchone(
                "SELECT mining_pool_remaining, mining_pool_last_topup FROM server_config WHERE guild_id = ?",
                (guild.id,),
            )
            if cfg["mining_pool_last_topup"] == today:
                continue  # already topped up today

            daily_amount = guild.member_count * MINING_POOL_DAILY_PER_MEMBER
            cap = daily_amount * MINING_POOL_CAP_MULTIPLIER
            new_remaining = min(cfg["mining_pool_remaining"] + daily_amount, cap)

            await self.db.execute(
                "UPDATE server_config SET mining_pool_remaining = ?, mining_pool_last_topup = ? WHERE guild_id = ?",
                (new_remaining, today, guild.id),
            )

    @pool_topup_loop.before_loop
    async def before_pool_topup_loop(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=HARVEST_TICK_MINUTES)
    async def harvest_loop(self):
        """Every tick, each non-full drill pulls (mines_per_hour / ticks_per_hour)
        items from its server's mining pool, filling up to its storage_capacity
        and then marking itself full."""
        ticks_per_hour = 60 / HARVEST_TICK_MINUTES
        drills = await self.db.fetchall("SELECT * FROM drills WHERE is_full = 0")
        for d in drills:
            info = DRILLS[d["drill_type"]]
            amount_per_tick = max(1, round(info["mines_per_hour"] / ticks_per_hour))

            cfg = await self.db.fetchone(
                "SELECT mining_pool_remaining FROM server_config WHERE guild_id = ?",
                (d["guild_id"],),
            )
            pool_remaining = cfg["mining_pool_remaining"] if cfg else 0
            if pool_remaining <= 0:
                continue  # nothing left to mine right now

            space_left = info["storage_capacity"] - d["stored_amount"]
            harvested = min(amount_per_tick, space_left, pool_remaining)
            if harvested <= 0:
                continue

            new_stored = d["stored_amount"] + harvested
            is_full = 1 if new_stored >= info["storage_capacity"] else 0
            await self.db.execute(
                "UPDATE drills SET stored_amount = ?, is_full = ? WHERE drill_id = ?",
                (new_stored, is_full, d["drill_id"]),
            )
            await self.db.execute(
                "UPDATE server_config SET mining_pool_remaining = mining_pool_remaining - ? WHERE guild_id = ?",
                (harvested, d["guild_id"]),
            )

    @harvest_loop.before_loop
    async def before_harvest_loop(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    # bot.add_cog() auto-registers the mine_group app_commands.Group -
    # do not also call bot.tree.add_command() or it'll double-register.
    await bot.add_cog(MiningCog(bot))
