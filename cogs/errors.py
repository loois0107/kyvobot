import discord
from discord import app_commands
from discord.ext import commands

class ErrorHandler(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        bot.tree.on_error = self.on_app_command_error

    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        locale = str(interaction.locale)
        if isinstance(error, app_commands.CommandOnCooldown):
            message = self.bot.locale_manager.get(locale, "errors_cooldown", seconds=f"{error.retry_after:.2f}")
        elif isinstance(error, app_commands.MissingPermissions):
            perms = ", ".join(error.missing_permissions)
            message = self.bot.locale_manager.get(locale, "errors_missing_perms", perms=perms)
        elif isinstance(error, app_commands.BotMissingPermissions):
            perms = ", ".join(error.missing_permissions)
            message = self.bot.locale_manager.get(locale, "errors_bot_missing_perms", perms=perms)
        else:
            print(f"[ERROR LOG] Unhandled exception occurred: {error}")
            message = self.bot.locale_manager.get(locale, "errors_internal")

        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(message, ephemeral=True)
            else:
                await interaction.followup.send(message, ephemeral=True)
        except:
            pass

async def setup(bot):
    await bot.add_cog(ErrorHandler(bot))
