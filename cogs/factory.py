"""
cogs/factory.py

Implements /factory craft <item> <quantity>, covering both component
materials (wiring, drill chassis, drill bits) AND fully assembled drills,
since the design doc has the factory produce both. Structurally identical
to cogs/furnace.py - see that file's comments for the FIFO queue explanation.
"""
import discord
from discord import app_commands
from discord.ext import commands, tasks

from data.materials import (
    COMPONENT_MATERIALS,
    DRILLS,
    FACTORY_MAX_QUEUE_ITEMS,
    FACTORY_RATES,
    FURNACE_FACTORY_UPGRADE_THRESHOLDS,
    get_material_info,
)

PROCESS_TICK_MINUTES = 5

# Factory can craft either a component material or a full drill - merge both
# tables so app_commands.choices has one flat list to offer.
CRAFTABLE = {**COMPONENT_MATERIALS, **DRILLS}


class FactoryCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db
        self._production_progress: dict[int, float] = {}
        self.process_loop.start()

    def cog_unload(self):
        self.process_loop.cancel()

    factory_group = app_commands.Group(name="factory", description="Craft components and drills")

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

    @factory_group.command(name="craft", description="Queue a component or drill to be crafted")
    @app_commands.describe(item="What to craft", quantity="How many to produce")
    @app_commands.choices(item=[
        app_commands.Choice(name=info["name"], value=key) for key, info in CRAFTABLE.items()
    ])
    async def factory_craft(self, interaction: discord.Interaction, item: app_commands.Choice[str], quantity: app_commands.Range[int, 1, 1000]):
        recipe = CRAFTABLE[item.value]

        for input_id, per_unit in recipe["inputs"].items():
            needed = per_unit * quantity
            have = await self._get_quantity(interaction.user.id, input_id)
            if have < needed:
                await interaction.response.send_message(
                    f"You need {needed} of `{input_id}` but only have {have}.", ephemeral=True
                )
                return

        # Check max queue limit based on total queued output units.
        cfg = await self.db.fetchone(
            "SELECT factory_fee, factory_max_queue FROM server_config WHERE guild_id = ?",
            (interaction.guild_id,),
        )
        fee_rate = cfg["factory_fee"] if cfg else 0.0
        max_queue = cfg["factory_max_queue"] if cfg and cfg["factory_max_queue"] is not None else FACTORY_MAX_QUEUE_ITEMS
        user_queue_row = await self.db.fetchone(
            "SELECT COALESCE(SUM(quantity), 0) as queued_items FROM production_jobs WHERE guild_id = ? AND user_id = ? AND job_type = 'factory' AND status != 'complete'",
            (interaction.guild_id, interaction.user.id),
        )
        queued_items = user_queue_row["queued_items"] if user_queue_row else 0
        if queued_items + quantity > max_queue:
            await interaction.response.send_message(
                f"You can only queue up to {max_queue} items worth of factory recipes per user at once. Complete some jobs first.",
                ephemeral=True,
            )
            return

        for input_id, per_unit in recipe["inputs"].items():
            await self._adjust_quantity(interaction.user.id, input_id, -per_unit * quantity)

        await self.db.execute(
            "INSERT INTO production_jobs (guild_id, user_id, job_type, target_id, quantity) VALUES (?, ?, 'factory', ?, ?)",
            (interaction.guild_id, interaction.user.id, item.value, quantity),
        )
        message = f"🏭 Queued {quantity}x **{recipe['name']}** for crafting."
        if fee_rate > 0:
            fee_total = fee_rate * quantity
            message += f"\nThis request will consume **{fee_total:.2f}** from your wallet as items are produced."
        await interaction.response.send_message(message)

    def _build_available_products_field(self, recipes: dict) -> str:
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
        return "\n".join(lines)

    async def _factory_status_impl(self, interaction: discord.Interaction):
        cfg = await self.db.fetchone(
            "SELECT factory_level, factory_fee, factory_fees_collected, factory_max_queue FROM server_config WHERE guild_id = ?",
            (interaction.guild_id,),
        )
        level = cfg["factory_level"] if cfg else 1
        fee_rate = cfg["factory_fee"] if cfg else 0.0
        max_queue = cfg["factory_max_queue"] if cfg and cfg["factory_max_queue"] is not None else FACTORY_MAX_QUEUE_ITEMS
        fees_collected = cfg["factory_fees_collected"] if cfg else 0.0

        rate = FACTORY_RATES.get(level, 15)
        next_level = level + 1
        upgrade_cost = FURNACE_FACTORY_UPGRADE_THRESHOLDS.get(next_level)

        jobs = await self.db.fetchall(
            "SELECT job_id, user_id, target_id, quantity, status FROM production_jobs WHERE guild_id = ? AND job_type = 'factory' AND status != 'complete' ORDER BY queued_at ASC",
            (interaction.guild_id,),
        )
        pending_items = sum(job["quantity"] for job in jobs)

        embed = discord.Embed(title="🏭 Factory Status", color=discord.Color.orange())
        embed.add_field(name="Level", value=f"**{level}**", inline=True)
        embed.add_field(name="Speed", value=f"**{rate}** items/hour", inline=True)
        embed.add_field(name="Queue Limit", value=f"**{max_queue}** items per user", inline=True)
        embed.add_field(name="Fee", value=f"**{fee_rate:.2f}** per item", inline=True)
        embed.add_field(name="Pending", value=f"**{pending_items}** items across **{len(jobs)}** job(s)", inline=True)

        if upgrade_cost is not None:
            progress = min(fees_collected, upgrade_cost)
            embed.add_field(
                name=f"Progress to Level {next_level}",
                value=f"**{progress:.2f}** / **{upgrade_cost:.2f}** currency collected",
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
                lines.append(f"{emoji} {job['quantity']}x {name} - {status_str}")
            if len(jobs) > 10:
                lines.append(f"... and {len(jobs) - 10} more")
            embed.add_field(name="Pending Jobs", value="\n".join(lines), inline=False)

        embed.add_field(name="Recipes", value=self._build_available_products_field(CRAFTABLE), inline=False)

        await interaction.response.send_message(embed=embed)

    @factory_group.command(name="status", description="Show factory level, queue, and upgrade progress")
    async def factory_status(self, interaction: discord.Interaction):
        await self._factory_status_impl(interaction)

    @factory_group.command(name="queue", description="Alias for /factory status")
    async def factory_queue_alias(self, interaction: discord.Interaction):
        await self._factory_status_impl(interaction)

    @tasks.loop(minutes=PROCESS_TICK_MINUTES)
    async def process_loop(self):
        ticks_per_hour = 60 / PROCESS_TICK_MINUTES
        configs = await self.db.fetchall(
            "SELECT guild_id, factory_level, factory_fee, factory_fees_collected FROM server_config"
        )
        for cfg in configs:
            rate = FACTORY_RATES[cfg["factory_level"]]
            progress = self._production_progress.get(cfg["guild_id"], 0.0) + (rate / ticks_per_hour)
            produced_units = int(progress)
            self._production_progress[cfg["guild_id"]] = progress - produced_units

            remaining_capacity = produced_units
            while remaining_capacity > 0:
                job = await self.db.fetchone(
                    """
                    SELECT * FROM production_jobs
                    WHERE guild_id = ? AND job_type = 'factory' AND status != 'complete'
                    ORDER BY queued_at ASC LIMIT 1
                    """,
                    (cfg["guild_id"],),
                )
                if job is None:
                    break

                produced = min(remaining_capacity, job["quantity"])
                new_quantity = job["quantity"] - produced
                remaining_capacity -= produced

                await self._adjust_quantity(job["user_id"], job["target_id"], produced)

                if cfg["factory_fee"] > 0:
                    fee_total = cfg["factory_fee"] * produced
                    await self.db.execute(
                        "UPDATE server_config SET factory_fees_collected = factory_fees_collected + ? WHERE guild_id = ?",
                        (fee_total, cfg["guild_id"]),
                    )
                    await self._charge_user_fee(cfg["guild_id"], job["user_id"], fee_total)
                    await self._maybe_upgrade_factory(cfg["guild_id"])

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

    async def _maybe_upgrade_factory(self, guild_id: int):
        cfg = await self.db.fetchone(
            "SELECT factory_level, factory_fees_collected FROM server_config WHERE guild_id = ?",
            (guild_id,),
        )
        next_level = cfg["factory_level"] + 1
        threshold = FURNACE_FACTORY_UPGRADE_THRESHOLDS.get(next_level)
        if threshold is not None and cfg["factory_fees_collected"] >= threshold:
            await self.db.execute(
                "UPDATE server_config SET factory_level = ? WHERE guild_id = ?",
                (next_level, guild_id),
            )

    @process_loop.before_loop
    async def before_process_loop(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    # bot.add_cog() auto-registers the factory_group app_commands.Group -
    # do not also call bot.tree.add_command() or it'll double-register.
    await bot.add_cog(FactoryCog(bot))
