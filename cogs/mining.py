"""
cogs/mining.py

Implements:
  - /mine place <drill_type> - place a drill in the dig site channel (max 3/user)
  - /mine status              - show active drills + current mining block progress
  - /collect                  - empty a full drill into your inventory
  - A background loop that creates a new "mining block" for every configured
    dig site channel once per day, and another loop that has drills harvest
    from the oldest block periodically.

NOTE: For simplicity, this scaffold's harvest loop runs every 5 minutes and
grants each drill (mines_per_hour / 12) items per tick (since 60/5=12 ticks
per hour). That preserves the correct hourly rate from the design doc.
"""
import random

import discord
from discord import app_commands
from discord.ext import commands, tasks

from data.materials import (
    DRILLS,
    RAW_MATERIALS,
    MAX_DRILLS_PER_USER_PER_CHANNEL,
    MAX_MINING_BLOCKS_PER_CHANNEL,
    ITEMS_PER_MINING_BLOCK_PER_MEMBER,
    get_material_info,
)

HARVEST_TICK_MINUTES = 12


def build_material_breakdown(total_items: int, roll_material=None) -> dict[str, int]:
    if total_items <= 0:
        return {}
    breakdown: dict[str, int] = {}
    for _ in range(total_items):
        material_id = roll_material() if roll_material is not None else next(iter(RAW_MATERIALS))
        breakdown[material_id] = breakdown.get(material_id, 0) + 1
    return breakdown


class MiningCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db
        self.harvest_loop.start()
        self.daily_block_loop.start()

    def cog_unload(self):
        self.harvest_loop.cancel()
        self.daily_block_loop.cancel()

    mine_group = app_commands.Group(name="mine", description="Manage mining drills")

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

    def _roll_raw_material(self) -> str:
        roll = random.random()
        cumulative = 0.0
        for mat_id, info in RAW_MATERIALS.items():
            cumulative += info["drop_chance"]
            if roll <= cumulative:
                return mat_id
        return next(iter(RAW_MATERIALS))

    async def _get_dig_site_channel(self, guild_id: int) -> int | None:
        row = await self.db.fetchone(
            "SELECT mine_channel_id FROM server_config WHERE guild_id = ?", (guild_id,)
        )
        return row["mine_channel_id"] if row else None

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

    @mine_group.command(name="place", description="Place a drill in this dig site channel")
    @app_commands.describe(drill_type="Which drill to place")
    @app_commands.choices(drill_type=[
        app_commands.Choice(name=info["name"], value=key) for key, info in DRILLS.items()
    ])
    async def mine_place(self, interaction: discord.Interaction, drill_type: app_commands.Choice[str]):
        dig_site = await self._get_dig_site_channel(interaction.guild_id)
        if dig_site != interaction.channel_id:
            await interaction.response.send_message(
                "This isn't the designated dig site channel for this server.", ephemeral=True
            )
            return

        existing = await self.db.fetchone(
            "SELECT COUNT(*) AS cnt FROM drills WHERE guild_id = ? AND channel_id = ? AND owner_id = ?",
            (interaction.guild_id, interaction.channel_id, interaction.user.id),
        )
        if existing["cnt"] >= MAX_DRILLS_PER_USER_PER_CHANNEL:
            await interaction.response.send_message(
                f"You already have the max of {MAX_DRILLS_PER_USER_PER_CHANNEL} drills in this channel.",
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
            "INSERT INTO drills (guild_id, channel_id, owner_id, drill_type) VALUES (?, ?, ?, ?)",
            (interaction.guild_id, interaction.channel_id, interaction.user.id, drill_type.value),
        )
        if fallback_granted:
            await interaction.response.send_message(
                f"⛏️ You didn't have any drills, so I gave you an **Iron Drill** and placed it in this dig site."
            )
        else:
            await interaction.response.send_message(
                f"⛏️ Placed a **{drill_type.name}** in this dig site."
            )

    @mine_group.command(name="status", description="Show active drills and mining block progress in this channel")
    async def mine_status(self, interaction: discord.Interaction):
        drills = await self.db.fetchall(
            "SELECT * FROM drills WHERE guild_id = ? AND channel_id = ?",
            (interaction.guild_id, interaction.channel_id),
        )
        blocks = await self.db.fetchall(
            "SELECT * FROM mining_blocks WHERE guild_id = ? AND channel_id = ? ORDER BY created_at ASC",
            (interaction.guild_id, interaction.channel_id),
        )

        embed = discord.Embed(title="Dig Site Status", color=discord.Color.dark_gold())
        if not drills:
            embed.add_field(name="Drills", value="No drills placed here yet.", inline=False)
        else:
            lines = []
            for d in drills:
                info = DRILLS[d["drill_type"]]
                status = "FULL - awaiting /collect" if d["is_full"] else "mining"
                lines.append(f"{info['emoji']} {info['name']} (<@{d['owner_id']}>): {d['stored_amount']}/{info['storage_capacity']} - {status}")
            embed.add_field(name="Drills", value="\n".join(lines), inline=False)

        if not blocks:
            embed.add_field(name="Mining Blocks", value="No mining block yet - one is created daily at midnight.", inline=False)
        else:
            oldest = blocks[0]
            embed.add_field(
                name=f"<:MiningBlock:1523436645729173514> Oldest Mining Block (#{oldest['block_id']})",
                value=f"{oldest['remaining_total']} raw materials remaining ({len(blocks)}/{MAX_MINING_BLOCKS_PER_CHANNEL} blocks queued)",
                inline=False,
            )

        await interaction.response.send_message(embed=embed)

    @mine_group.command(name="remove", description="Remove a drill of a specific type and collect its items")
    @app_commands.describe(drill_type="Which drill type to remove")
    @app_commands.choices(drill_type=[
        app_commands.Choice(name=info["name"], value=key) for key, info in DRILLS.items()
    ])
    async def mine_remove(self, interaction: discord.Interaction, drill_type: app_commands.Choice[str]):
        # Find the drill of this type with the lowest stored_amount
        drill = await self.db.fetchone(
            "SELECT * FROM drills WHERE guild_id = ? AND channel_id = ? AND owner_id = ? AND drill_type = ? ORDER BY stored_amount ASC LIMIT 1",
            (interaction.guild_id, interaction.channel_id, interaction.user.id, drill_type.value),
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
        
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="collect", description="Collect materials from your drill(s) in this channel")
    async def collect(self, interaction: discord.Interaction):
        drills = await self.db.fetchall(
            "SELECT * FROM drills WHERE guild_id = ? AND channel_id = ? AND owner_id = ? AND stored_amount > 0",
            (interaction.guild_id, interaction.channel_id, interaction.user.id),
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
        
        await interaction.response.send_message(embed=embed)

    @tasks.loop(hours=24)
    async def daily_block_loop(self):
        """Creates one new mining block per configured dig site channel,
        sized at (member_count * 200) per the design doc. Runs once per day;
        see before_loop below for how it's aligned to midnight."""
        configs = await self.db.fetchall(
            "SELECT guild_id, mine_channel_id FROM server_config WHERE mine_channel_id IS NOT NULL"
        )
        for cfg in configs:
            guild = self.bot.get_guild(cfg["guild_id"])
            if guild is None:
                continue

            existing_blocks = await self.db.fetchone(
                "SELECT COUNT(*) AS cnt FROM mining_blocks WHERE guild_id = ? AND channel_id = ?",
                (cfg["guild_id"], cfg["mine_channel_id"]),
            )
            if existing_blocks["cnt"] >= MAX_MINING_BLOCKS_PER_CHANNEL:
                continue  # channel already has the max 3 queued blocks

            total_items = guild.member_count * ITEMS_PER_MINING_BLOCK_PER_MEMBER
            block_id = await self.db.execute(
                "INSERT INTO mining_blocks (guild_id, channel_id, remaining_total) VALUES (?, ?, ?)",
                (cfg["guild_id"], cfg["mine_channel_id"], total_items),
            )

            # Roll the composition of the block using each material's drop_chance.
            contents = {}
            for _ in range(total_items):
                roll = random.random()
                cumulative = 0.0
                for mat_id, info in RAW_MATERIALS.items():
                    cumulative += info["drop_chance"]
                    if roll <= cumulative:
                        contents[mat_id] = contents.get(mat_id, 0) + 1
                        break

            for mat_id, qty in contents.items():
                await self.db.execute(
                    "INSERT INTO mining_block_contents (block_id, material_id, remaining) VALUES (?, ?, ?)",
                    (block_id, mat_id, qty),
                )

    @daily_block_loop.before_loop
    async def before_daily_block_loop(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=HARVEST_TICK_MINUTES)
    async def harvest_loop(self):
        """Every tick, each non-full drill pulls (mines_per_hour / (60/tick))
        items from the oldest mining block in its channel, filling up to its
        storage_capacity and then marking itself full."""
        ticks_per_hour = 60 / HARVEST_TICK_MINUTES
        drills = await self.db.fetchall("SELECT * FROM drills WHERE is_full = 0")
        for d in drills:
            info = DRILLS[d["drill_type"]]
            amount_per_tick = max(1, round(info["mines_per_hour"] / ticks_per_hour))

            oldest_block = await self.db.fetchone(
                "SELECT * FROM mining_blocks WHERE guild_id = ? AND channel_id = ? AND remaining_total > 0 ORDER BY created_at ASC LIMIT 1",
                (d["guild_id"], d["channel_id"]),
            )
            if oldest_block is None:
                continue  # nothing left to mine right now

            space_left = info["storage_capacity"] - d["stored_amount"]
            harvested = min(amount_per_tick, space_left, oldest_block["remaining_total"])
            if harvested <= 0:
                continue

            new_stored = d["stored_amount"] + harvested
            is_full = 1 if new_stored >= info["storage_capacity"] else 0
            await self.db.execute(
                "UPDATE drills SET stored_amount = ?, is_full = ? WHERE drill_id = ?",
                (new_stored, is_full, d["drill_id"]),
            )
            await self.db.execute(
                "UPDATE mining_blocks SET remaining_total = remaining_total - ? WHERE block_id = ?",
                (harvested, oldest_block["block_id"]),
            )
            
            # Clean up mining block if it's depleted
            updated_block = await self.db.fetchone(
                "SELECT remaining_total FROM mining_blocks WHERE block_id = ?",
                (oldest_block["block_id"],),
            )
            if updated_block and updated_block["remaining_total"] <= 0:
                await self.db.execute("DELETE FROM mining_block_contents WHERE block_id = ?", (oldest_block["block_id"],))
                await self.db.execute("DELETE FROM mining_blocks WHERE block_id = ?", (oldest_block["block_id"],))

    @harvest_loop.before_loop
    async def before_harvest_loop(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    # bot.add_cog() auto-registers the mine_group app_commands.Group -
    # do not also call bot.tree.add_command() or it'll double-register.
    await bot.add_cog(MiningCog(bot))
