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

from data.materials import COMPONENT_MATERIALS, DRILLS, FACTORY_RATES, FURNACE_FACTORY_UPGRADE_THRESHOLDS

PROCESS_TICK_MINUTES = 5

# Factory can craft either a component material or a full drill - merge both
# tables so app_commands.choices has one flat list to offer.
CRAFTABLE = {**COMPONENT_MATERIALS, **DRILLS}


class FactoryCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db
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

        for input_id, per_unit in recipe["inputs"].items():
            await self._adjust_quantity(interaction.user.id, input_id, -per_unit * quantity)

        await self.db.execute(
            "INSERT INTO production_jobs (guild_id, user_id, job_type, target_id, quantity) VALUES (?, ?, 'factory', ?, ?)",
            (interaction.guild_id, interaction.user.id, item.value, quantity),
        )
        await interaction.response.send_message(
            f"🏭 Queued {quantity}x **{recipe['name']}** for crafting."
        )

    @tasks.loop(minutes=PROCESS_TICK_MINUTES)
    async def process_loop(self):
        ticks_per_hour = 60 / PROCESS_TICK_MINUTES
        configs = await self.db.fetchall(
            "SELECT guild_id, factory_level, factory_fee, factory_fees_collected FROM server_config"
        )
        for cfg in configs:
            rate = FACTORY_RATES[cfg["factory_level"]]
            capacity_this_tick = max(1, round(rate / ticks_per_hour))

            remaining_capacity = capacity_this_tick
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
