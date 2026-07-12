import discord
from discord import app_commands
from discord.ext import commands

class CustomCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="cc_add", description="Create or update a server-specific custom command response.")
    @app_commands.checks.has_permissions(administrator=True)
    async def cc_add(self, interaction: discord.Interaction, name: str, response: str):
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id)
        cmd_name = name.strip().lower()

        if cmd_name.startswith("/"):
            msg = self.bot.locale_manager.get(str(interaction.locale), "cc_err_prefix")
            await interaction.followup.send(msg, ephemeral=True)
            return

        settings = await self.bot.get_guild_settings(guild_id)
        custom_commands = settings.get("custom_commands", {})
        custom_commands[cmd_name] = response

        try:
            await self.bot.bulk_update_guild_settings(guild_id, {"custom_commands": custom_commands})
            msg = self.bot.locale_manager.get(str(interaction.locale), "cc_success_add", name=cmd_name)
            await interaction.followup.send(msg, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ `{str(e)}`", ephemeral=True)

    @app_commands.command(name="cc_delete", description="Permanently delete a custom command from the server configuration.")
    @app_commands.checks.has_permissions(administrator=True)
    async def cc_delete(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id)
        cmd_name = name.strip().lower()

        settings = await self.bot.get_guild_settings(guild_id)
        custom_commands = settings.get("custom_commands", {})

        if cmd_name not in custom_commands:
            msg = self.bot.locale_manager.get(str(interaction.locale), "cc_err_not_found")
            await interaction.followup.send(msg, ephemeral=True)
            return

        custom_commands.pop(cmd_name)

        try:
            await self.bot.bulk_update_guild_settings(guild_id, {"custom_commands": custom_commands})
            msg = self.bot.locale_manager.get(str(interaction.locale), "cc_success_delete", name=cmd_name)
            await interaction.followup.send(msg, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ `{str(e)}`", ephemeral=True)

    @app_commands.command(name="cc_list", description="Display all active custom commands within this server node.")
    async def cc_list(self, interaction: discord.Interaction):
        await interaction.response.defer()
        guild_id = str(interaction.guild_id)

        settings = await self.bot.get_guild_settings(guild_id)
        custom_commands = settings.get("custom_commands", {})

        title = self.bot.locale_manager.get(str(interaction.locale), "cc_list_title")
        embed = discord.Embed(title=title, color=discord.Color.blue())

        if not custom_commands:
            empty_msg = self.bot.locale_manager.get(str(interaction.locale), "cc_list_empty")
            embed.description = empty_msg
            await interaction.followup.send(embed=embed)
            return

        for cmd, resp in custom_commands.items():
            display_resp = resp if len(resp) <= 100 else resp[:97] + "..."
            embed.add_field(name=f"/{cmd}", value=f"┕ `{display_resp}`", inline=False)

        await interaction.followup.send(embed=embed)

async def setup(bot):
    await bot.add_cog(CustomCommands(bot))
