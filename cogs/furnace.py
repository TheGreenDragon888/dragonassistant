"""
cogs/furnace.py

Implements /furnace smelt <material> <quantity>, which:
  1. Checks the user has enough raw materials
  2. Deducts the raw materials immediately and queues a production job
  3. A background loop processes queued jobs at the server's furnace_level
     rate (5/10/15 per hour), crediting completed items to the user and
     accumulating fees toward the next furnace level upgrade.
"""
import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils.embeds import add_multi_field
from utils.responses import respond
from utils.formatting import format_currency

from data.materials import (
    SMELTED_MATERIALS,
    FURNACE_MAX_QUEUE_ITEMS,
    FURNACE_RATES,
    FURNACE_FACTORY_UPGRADE_THRESHOLDS,
    get_material_info,
    FURNACE_COAL_COST_PER_UNIT,
    target_stock,
)

PROCESS_TICK_MINUTES = 5

# Discord snowflake IDs are always large positive integers, so 0 is safe to
# reserve as a sentinel marking a production_jobs row as owned by the server
# itself (the auto-smelt feature below) rather than a real user.
SERVER_JOB_USER_ID = 0

# Target ratio of iron:steel the server's auto-smelt steers its own stockpile
# towards when both recipes draw from the same iron_ore supply.
SERVER_IRON_TO_STEEL_RATIO = 4


class FurnaceCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db
        self._production_progress: dict[int, float] = {}
        self.process_loop.start()

    def cog_unload(self):
        self.process_loop.cancel()

    furnace_group = app_commands.Group(name="furnace", description="Smelt raw materials")

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

    async def _get_server_balance(self, guild_id: int, user_id: int) -> float:
        row = await self.db.fetchone(
            "SELECT balance FROM server_currency_balances WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        return row["balance"] if row else 0.0

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


    @furnace_group.command(name="smelt", description="Queue raw materials to be smelted")
    @app_commands.describe(material="What to smelt", quantity="How many to produce")
    @app_commands.choices(material=[
        app_commands.Choice(name=info["name"], value=key) for key, info in SMELTED_MATERIALS.items()
    ])
    async def furnace_smelt(self, interaction: discord.Interaction, material: app_commands.Choice[str], quantity: app_commands.Range[int, 1, 1000]):
        recipe = SMELTED_MATERIALS[material.value]

        # Total material cost = the recipe's own inputs + a flat per-item coal
        # cost to run the furnace at all (combined with any coal the recipe
        # itself already needs, e.g. steel's 4 coal per unit).
        needs: dict[str, int] = {}
        for input_id, per_unit in recipe["inputs"].items():
            needs[input_id] = needs.get(input_id, 0) + per_unit * quantity
        needs["coal"] = needs.get("coal", 0) + FURNACE_COAL_COST_PER_UNIT * quantity

        for input_id, needed in needs.items():
            have = await self._get_quantity(interaction.user.id, input_id)
            if have < needed:
                await interaction.response.send_message(
                    f"You need {needed} of `{input_id}` but only have {have}.", ephemeral=True
                )
                return

        cfg = await self.db.fetchone(
            "SELECT furnace_fee, furnace_max_queue, currency_emoji FROM server_config WHERE guild_id = ?",
            (interaction.guild_id,),
        )
        fee_rate = cfg["furnace_fee"] if cfg else 0.0
        currency_emoji = cfg["currency_emoji"] if cfg else None
        max_queue = cfg["furnace_max_queue"] if cfg and cfg["furnace_max_queue"] is not None else FURNACE_MAX_QUEUE_ITEMS
        user_queue_row = await self.db.fetchone(
            "SELECT COALESCE(SUM(quantity), 0) as queued_items FROM production_jobs WHERE guild_id = ? AND user_id = ? AND job_type = 'furnace' AND status != 'complete'",
            (interaction.guild_id, interaction.user.id),
        )
        queued_items = user_queue_row["queued_items"] if user_queue_row else 0
        if queued_items + quantity > max_queue:
            await interaction.response.send_message(
                f"You can only queue up to {max_queue} items worth of furnace recipes per user at once. Complete some jobs first.",
                ephemeral=True,
            )
            return

        # Fee is charged UP FRONT now (previously it was charged per-item as
        # the job completed) — so check affordability before touching inventory.
        fee_total = fee_rate * quantity
        if fee_total > 0:
            balance = await self._get_server_balance(interaction.guild_id, interaction.user.id)
            if balance < fee_total:
                await interaction.response.send_message(
                    f"This would cost {format_currency(fee_total, currency_emoji)} up front, but you only have {format_currency(balance, currency_emoji)}.",
                    ephemeral=True,
                )
                return

        for input_id, needed in needs.items():
            await self._adjust_quantity(interaction.user.id, input_id, -needed)

        if fee_total > 0:
            await self._charge_user_fee(interaction.guild_id, interaction.user.id, fee_total)
            await self.db.execute(
                "UPDATE server_config SET furnace_fees_collected = furnace_fees_collected + ? WHERE guild_id = ?",
                (fee_total, interaction.guild_id),
            )
            await self._maybe_upgrade_furnace(interaction.guild_id)

        await self.db.execute(
            "INSERT INTO production_jobs (guild_id, user_id, job_type, target_id, quantity) VALUES (?, ?, 'furnace', ?, ?)",
            (interaction.guild_id, interaction.user.id, material.value, quantity),
        )

        message = f"🔥 Queued {quantity}x **{recipe['name']}** for smelting (burning {FURNACE_COAL_COST_PER_UNIT * quantity} extra coal to run the furnace)."
        if fee_total > 0:
            message += f"\n{format_currency(fee_total, currency_emoji)} has been charged up front."
        await respond(interaction, self.db, content=message)

    def _build_available_products_lines(self, recipes: dict) -> list[str]:
        lines = []
        for material_id, recipe in recipes.items():
            info = get_material_info(material_id)
            emoji = info["emoji"] if info else "❓"
            name = info["name"] if info else material_id
            costs = []
            for input_id, qty in recipe.get("inputs", {}).items():
                input_info = get_material_info(input_id)
                input_emoji = input_info["emoji"] if input_info else "❓"
                costs.append(f"{input_emoji} {qty}")
            lines.append(f"{emoji} {name} - {' , '.join(costs)}")
        return lines

    async def _furnace_status_impl(self, interaction: discord.Interaction):
        cfg = await self.db.fetchone(
            "SELECT furnace_level, furnace_fee, furnace_fees_collected, furnace_max_queue, currency_emoji FROM server_config WHERE guild_id = ?",
            (interaction.guild_id,),
        )
        level = cfg["furnace_level"] if cfg else 1
        fee_rate = cfg["furnace_fee"] if cfg else 0.0
        max_queue = cfg["furnace_max_queue"] if cfg and cfg["furnace_max_queue"] is not None else FURNACE_MAX_QUEUE_ITEMS
        fees_collected = cfg["furnace_fees_collected"] if cfg else 0.0
        currency_emoji = cfg["currency_emoji"] if cfg else None

        rate = FURNACE_RATES.get(level, 15)
        next_level = level + 1
        upgrade_cost = FURNACE_FACTORY_UPGRADE_THRESHOLDS.get(next_level)

        jobs = await self.db.fetchall(
            "SELECT job_id, user_id, target_id, quantity, status FROM production_jobs WHERE guild_id = ? AND job_type = 'furnace' AND status != 'complete' ORDER BY queued_at ASC",
            (interaction.guild_id,),
        )
        pending_items = sum(job["quantity"] for job in jobs)

        embed = discord.Embed(title="🔥 Furnace Status", color=discord.Color.red())
        embed.add_field(name="Level", value=f"**{level}**", inline=True)
        embed.add_field(name="Speed", value=f"**{rate}** items/hour", inline=True)
        embed.add_field(name="Queue Limit", value=f"**{max_queue}** items per user", inline=True)
        embed.add_field(name="Fee", value=f"{format_currency(fee_rate, currency_emoji)} per item", inline=True)
        embed.add_field(name="Pending", value=f"**{pending_items}** items across **{len(jobs)}** job(s)", inline=True)

        if upgrade_cost is not None:
            progress = min(fees_collected, upgrade_cost)
            embed.add_field(
                name=f"Progress to Level {next_level}",
                value=f"{format_currency(progress, currency_emoji)} / {format_currency(upgrade_cost, currency_emoji)} collected",
                inline=False,
            )
        else:
            embed.add_field(name="Status", value="Max level reached!", inline=False)

        if jobs:
            lines = []
            for job in jobs[:10]:  # Show first 10
                info = get_material_info(job["target_id"])
                emoji = info["emoji"] if info else "❓"
                name = info["name"] if info else job["target_id"]
                status_str = "In Progress" if job["status"] == "in_progress" else "Queued"
                owner_str = " (🏛️ Server)" if job["user_id"] == SERVER_JOB_USER_ID else ""
                lines.append(f"{emoji} {job['quantity']}x {name} - {status_str}{owner_str}")
            if len(jobs) > 10:
                lines.append(f"... and {len(jobs) - 10} more")
            add_multi_field(embed, "Pending Jobs", lines)

        add_multi_field(embed, "Recipes", self._build_available_products_lines(SMELTED_MATERIALS))

        await respond(interaction, self.db, embed=embed)

    @furnace_group.command(name="status", description="Show furnace level, queue, and upgrade progress")
    async def furnace_status(self, interaction: discord.Interaction):
        await self._furnace_status_impl(interaction)

    @furnace_group.command(name="queue", description="Alias for /furnace status")
    async def furnace_queue_alias(self, interaction: discord.Interaction):
        await self._furnace_status_impl(interaction)

    @tasks.loop(minutes=PROCESS_TICK_MINUTES)
    async def process_loop(self):
        """Each tick, every guild's furnace processes its hourly rate spread over time.
        The loop keeps a fractional accumulator per guild so level 1 can produce 1 item/hour
        without over-producing every 5 minutes."""
        ticks_per_hour = 60 / PROCESS_TICK_MINUTES
        configs = await self.db.fetchall(
            "SELECT guild_id, furnace_level, furnace_fee, furnace_fees_collected FROM server_config"
        )
        for cfg in configs:
            rate = FURNACE_RATES[cfg["furnace_level"]]
            progress = self._production_progress.get(cfg["guild_id"], 0.0) + (rate / ticks_per_hour)
            produced_units = int(progress)
            self._production_progress[cfg["guild_id"]] = progress - produced_units

            remaining_capacity = produced_units
            while remaining_capacity > 0:
                # Real users' jobs always process ahead of the server's own
                # auto-smelt job (see _try_auto_smelt) so the server never
                # hogs the furnace from the people actually playing.
                job = await self.db.fetchone(
                    """
                    SELECT * FROM production_jobs
                    WHERE guild_id = ? AND job_type = 'furnace' AND status != 'complete'
                    ORDER BY (user_id = ?) ASC, queued_at ASC LIMIT 1
                    """,
                    (cfg["guild_id"], SERVER_JOB_USER_ID),
                )
                if job is None:
                    break  # no jobs waiting for this server

                produced = min(remaining_capacity, job["quantity"])
                new_quantity = job["quantity"] - produced
                remaining_capacity -= produced

                # Credit the produced items - to the server's own market
                # storage for its auto-smelt jobs, otherwise to the user.
                if job["user_id"] == SERVER_JOB_USER_ID:
                    await self._adjust_server_stock(job["guild_id"], job["target_id"], produced)
                else:
                    await self._adjust_quantity(job["user_id"], job["target_id"], produced)

                # (fee charging removed here — it now happens in furnace_smelt, up front)

                if new_quantity <= 0:
                    await self.db.execute(
                        "UPDATE production_jobs SET status = 'complete', quantity = 0 WHERE job_id = ?",
                        (job["job_id"],),
                    )
                else:
                    await self.db.execute(
                        "UPDATE production_jobs SET quantity = ?, status = 'in_progress' WHERE job_id = ?",
                        (new_quantity, job["job_id"]),
                    )

            # If nothing at all is queued for this guild's furnace, let the
            # server consider queuing its own auto-smelt job(s).
            pending = await self.db.fetchone(
                "SELECT 1 FROM production_jobs WHERE guild_id = ? AND job_type = 'furnace' AND status != 'complete' LIMIT 1",
                (cfg["guild_id"],),
            )
            if pending is None:
                await self._try_auto_smelt(cfg["guild_id"])

    async def _try_auto_smelt(self, guild_id: int):
        """Queues the server's own furnace job(s) against its own material
        storage - only called when the furnace queue is completely empty.
        Follows the normal recipe cost + coal tax, but skips the furnace fee
        entirely (the server isn't paying itself). Only touches ore that's
        above the market's target stock for that ore, so it never eats into
        the reserve the /market buy price curve depends on."""
        guild = self.bot.get_guild(guild_id)
        if guild is None or not guild.member_count:
            return
        target = target_stock(guild.member_count)

        coal_stock = await self._get_server_stock(guild_id, "coal")
        jobs_to_queue: list[tuple[str, int, dict[str, int]]] = []

        iron_ore_stock = await self._get_server_stock(guild_id, "iron_ore")
        if iron_ore_stock >= target:
            surplus = iron_ore_stock - target
            iron_stock = await self._get_server_stock(guild_id, "iron")
            steel_stock = await self._get_server_stock(guild_id, "steel")
            # Steer the stockpile towards an iron:steel 4:1 ratio - produce
            # whichever one is currently under-represented for that ratio.
            recipe_id = "steel" if steel_stock < iron_stock / SERVER_IRON_TO_STEEL_RATIO else "iron"
            recipe = SMELTED_MATERIALS[recipe_id]
            ore_per_unit = recipe["inputs"]["iron_ore"]
            coal_per_unit = recipe["inputs"].get("coal", 0) + FURNACE_COAL_COST_PER_UNIT
            quantity = min(surplus // ore_per_unit, coal_stock // coal_per_unit)
            if quantity > 0:
                jobs_to_queue.append((recipe_id, quantity, {"iron_ore": ore_per_unit * quantity, "coal": coal_per_unit * quantity}))
                coal_stock -= coal_per_unit * quantity

        copper_ore_stock = await self._get_server_stock(guild_id, "copper_ore")
        if copper_ore_stock >= target:
            surplus = copper_ore_stock - target
            recipe = SMELTED_MATERIALS["copper"]
            ore_per_unit = recipe["inputs"]["copper_ore"]
            coal_per_unit = FURNACE_COAL_COST_PER_UNIT
            quantity = min(surplus // ore_per_unit, coal_stock // coal_per_unit)
            if quantity > 0:
                jobs_to_queue.append(("copper", quantity, {"copper_ore": ore_per_unit * quantity, "coal": coal_per_unit * quantity}))

        for target_id, quantity, needs in jobs_to_queue:
            for material_id, amount in needs.items():
                await self._adjust_server_stock(guild_id, material_id, -amount)
            await self.db.execute(
                "INSERT INTO production_jobs (guild_id, user_id, job_type, target_id, quantity) VALUES (?, ?, 'furnace', ?, ?)",
                (guild_id, SERVER_JOB_USER_ID, target_id, quantity),
            )

    async def _charge_user_fee(self, guild_id: int, user_id: int, amount: float):
        if amount <= 0:
            return
        await self.db.execute(
            "INSERT OR IGNORE INTO server_currency_balances (guild_id, user_id, balance) VALUES (?, ?, 0.0)",
            (guild_id, user_id),
        )
        await self.db.execute(
            "UPDATE server_currency_balances SET balance = CASE WHEN balance >= ? THEN balance - ? ELSE 0.0 END WHERE guild_id = ? AND user_id = ?",
            (amount, amount, guild_id, user_id),
        )
        # Fees are a currency sink (docs/market.md section 1/4) - the amount
        # leaves circulation entirely rather than moving to another balance.
        await self.db.execute(
            "UPDATE server_config SET currency_burned_total = currency_burned_total + ? WHERE guild_id = ?",
            (amount, guild_id),
        )

    async def _maybe_upgrade_furnace(self, guild_id: int):
        cfg = await self.db.fetchone(
            "SELECT furnace_level, furnace_fees_collected FROM server_config WHERE guild_id = ?",
            (guild_id,),
        )
        next_level = cfg["furnace_level"] + 1
        threshold = FURNACE_FACTORY_UPGRADE_THRESHOLDS.get(next_level)
        if threshold is not None and cfg["furnace_fees_collected"] >= threshold:
            await self.db.execute(
                "UPDATE server_config SET furnace_level = ? WHERE guild_id = ?",
                (next_level, guild_id),
            )

    @process_loop.before_loop
    async def before_process_loop(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    # bot.add_cog() auto-registers the furnace_group app_commands.Group -
    # do not also call bot.tree.add_command() or it'll double-register.
    await bot.add_cog(FurnaceCog(bot))
