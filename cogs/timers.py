import discord
from discord import app_commands
from discord.ext import commands
import asyncio

class Timers(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.running_timers = {}

    async def timer_task(self, guild_id, channel_id, message, interval_minutes):
        while True:
            await asyncio.get_event_loop().run_in_executor(None, lambda: None)
            await asyncio.sleep(interval_minutes * 60)
            settings = await self.bot.get_guild_settings(str(guild_id))
            if not settings.get("timers_enabled", True):
                continue
            
            channel = self.bot.get_channel(channel_id)
            if channel:
                try:
                    await channel.send(message)
                except Exception:
                    break
            else:
                break

    @app_commands.command(name="set_timer", description="Set a periodic recurring message.")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_timer(self, interaction: discord.Interaction, channel: discord.TextChannel, interval_minutes: int, message: str):
        if interval_minutes <= 0:
            msg = self.bot.locale_manager.get(str(interaction.locale), "timers_err_interval")
            await interaction.response.send_message(msg, ephemeral=True)
            return
        
        timer_key = f"{channel.id}_{message[:20]}"
        if timer_key in self.running_timers:
            self.running_timers[timer_key].cancel()

        task = self.bot.loop.create_task(self.timer_task(interaction.guild.id, channel.id, message, interval_minutes))
        self.running_timers[timer_key] = task
        success = self.bot.locale_manager.get(str(interaction.locale), "timers_success_set", mention=channel.mention, minutes=interval_minutes)
        await interaction.response.send_message(success, ephemeral=True)

    @app_commands.command(name="clear_timers", description="Cancel all active recurring timers.")
    @app_commands.checks.has_permissions(administrator=True)
    async def clear_timers(self, interaction: discord.Interaction):
        for task in self.running_timers.values():
            task.cancel()
        self.running_timers.clear()
        success = self.bot.locale_manager.get(str(interaction.locale), "timers_success_clear")
        await interaction.response.send_message(success, ephemeral=True)

async def setup(bot):
    await bot.add_cog(Timers(bot))
