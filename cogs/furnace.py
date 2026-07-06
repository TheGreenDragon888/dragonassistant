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

from data.materials import SMELTED_MATERIALS, FURNACE_RATES, FURNACE_FACTORY_UPGRADE_THRESHOLDS

PROCESS_TICK_MINUTES = 5


class FurnaceCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db
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

    @furnace_group.command(name="smelt", description="Queue raw materials to be smelted")
    @app_commands.describe(material="What to smelt", quantity="How many to produce")
    @app_commands.choices(material=[
        app_commands.Choice(name=info["name"], value=key) for key, info in SMELTED_MATERIALS.items()
    ])
    async def furnace_smelt(self, interaction: discord.Interaction, material: app_commands.Choice[str], quantity: app_commands.Range[int, 1, 1000]):
        recipe = SMELTED_MATERIALS[material.value]

        # Check the user has enough of every required raw material.
        for input_id, per_unit in recipe["inputs"].items():
            needed = per_unit * quantity
            have = await self._get_quantity(interaction.user.id, input_id)
            if have < needed:
                await interaction.response.send_message(
                    f"You need {needed} of `{input_id}` but only have {have}.", ephemeral=True
                )
                return

        # Deduct inputs up front so they can't be double-spent while queued.
        for input_id, per_unit in recipe["inputs"].items():
            await self._adjust_quantity(interaction.user.id, input_id, -per_unit * quantity)

        await self.db.execute(
            "INSERT INTO production_jobs (guild_id, user_id, job_type, target_id, quantity) VALUES (?, ?, 'furnace', ?, ?)",
            (interaction.guild_id, interaction.user.id, material.value, quantity),
        )
        await interaction.response.send_message(
            f"🔥 Queued {quantity}x **{recipe['name']}** for smelting."
        )

    @tasks.loop(minutes=PROCESS_TICK_MINUTES)
    async def process_loop(self):
        """Each tick, every guild's furnace processes (furnace_rate / ticks_per_hour)
        items from its OLDEST queued job first (FIFO, per the design doc's
        'must wait until those complete' queue rule)."""
        ticks_per_hour = 60 / PROCESS_TICK_MINUTES
        configs = await self.db.fetchall(
            "SELECT guild_id, furnace_level, furnace_fee, furnace_fees_collected FROM server_config"
        )
        for cfg in configs:
            rate = FURNACE_RATES[cfg["furnace_level"]]
            capacity_this_tick = max(1, round(rate / ticks_per_hour))

            remaining_capacity = capacity_this_tick
            while remaining_capacity > 0:
                job = await self.db.fetchone(
                    """
                    SELECT * FROM production_jobs
                    WHERE guild_id = ? AND job_type = 'furnace' AND status != 'complete'
                    ORDER BY queued_at ASC LIMIT 1
                    """,
                    (cfg["guild_id"],),
                )
                if job is None:
                    break  # no jobs waiting for this server

                produced = min(remaining_capacity, job["quantity"])
                new_quantity = job["quantity"] - produced
                remaining_capacity -= produced

                # Credit the produced items to the user.
                await self._adjust_quantity(job["user_id"], job["target_id"], produced)

                # Collect the fee (if any) for the produced items.
                if cfg["furnace_fee"] > 0:
                    fee_total = cfg["furnace_fee"] * produced
                    await self.db.execute(
                        "UPDATE server_config SET furnace_fees_collected = furnace_fees_collected + ? WHERE guild_id = ?",
                        (fee_total, cfg["guild_id"]),
                    )
                    await self._maybe_upgrade_furnace(cfg["guild_id"])

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
