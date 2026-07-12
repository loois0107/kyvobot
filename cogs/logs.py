import discord
from discord.ext import commands

class ServerLogs(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if message.author.bot or not message.guild: return

        log_channel = discord.utils.get(message.guild.text_channels, name="mod-logs")
        if not log_channel: return

        locale = str(message.guild.preferred_locale)

        title_str = self.bot.locale_manager.get(locale, "logs_delete_title")
        desc_str = self.bot.locale_manager.get(locale, "logs_delete_desc", mention=message.author.mention)
        ch_str = self.bot.locale_manager.get(locale, "logs_channel")
        op_str = self.bot.locale_manager.get(locale, "logs_operator_id")
        content_header = self.bot.locale_manager.get(locale, "logs_delete_content")
        footer_str = self.bot.locale_manager.get(locale, "logs_footer", name=message.author.name)
        fallback_content = self.bot.locale_manager.get(locale, "logs_no_content")

        embed = discord.Embed(
            title=title_str,
            description=desc_str,
            color=discord.Color.from_str("#FF3333"),
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(name=ch_str, value=message.channel.mention, inline=True)
        embed.add_field(name=op_str, value=f"`{message.author.id}`", inline=True)
        
        content = message.content if message.content else fallback_content
        embed.add_field(name=content_header, value=f"```\n{content}\n```", inline=False)
        embed.set_footer(text=footer_str)
        
        try:
            await log_channel.send(embed=embed)
        except Exception as e:
            print(f"[LOG ERROR] Failed to broadcast delete telemetry: {e}")

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.author.bot or before.content == after.content or not before.guild: return

        log_channel = discord.utils.get(before.guild.text_channels, name="mod-logs")
        if not log_channel: return

        locale = str(before.guild.preferred_locale)

        title_str = self.bot.locale_manager.get(locale, "logs_edit_title")
        desc_str = self.bot.locale_manager.get(locale, "logs_edit_desc", mention=before.author.mention)
        ch_str = self.bot.locale_manager.get(locale, "logs_channel")
        op_str = self.bot.locale_manager.get(locale, "logs_operator_id")
        before_header = self.bot.locale_manager.get(locale, "logs_before_data")
        after_header = self.bot.locale_manager.get(locale, "logs_after_data")
        footer_str = self.bot.locale_manager.get(locale, "logs_footer", name=before.author.name)

        embed = discord.Embed(
            title=title_str,
            description=desc_str,
            color=discord.Color.from_str("#FF9900"),
            timestamp=discord.utils.utcnow()
        )
        embed.add_field(name=ch_str, value=before.channel.mention, inline=True)
        embed.add_field(name=op_str, value=f"`{before.author.id}`", inline=True)
        
        embed.add_field(name=before_header, value=f"```\n{before.content}\n```", inline=False)
        embed.add_field(name=after_header, value=f"```\n{after.content}\n```", inline=False)
        embed.set_footer(text=footer_str)

        try:
            await log_channel.send(embed=embed)
        except Exception as e:
            print(f"[LOG ERROR] Failed to broadcast edit telemetry: {e}")

async def setup(bot):
    await bot.add_cog(ServerLogs(bot))
