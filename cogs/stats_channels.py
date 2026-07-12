import discord
from discord import app_commands
from discord.ext import commands, tasks

class StatsChannels(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.update_all_stats.start()

    def cog_unload(self):
        self.update_all_stats.cancel()

    # 로그 출력 함수 추가
    def get_msg(self, locale, key, **kwargs):
        msg = self.bot.locale_manager.get(locale, key, **kwargs)
        print(f"[DEBUG] Locale: {locale} | Key: {key} | Result: {msg}")
        return msg

    async def update_guild_stats(self, guild: discord.Guild, settings: dict):
        locale = str(guild.preferred_locale)[:2]
        config = settings.get("stats_channels", {})
        if not config or not config.get("enabled", False): return

        total_count = guild.member_count
        bot_count = sum(1 for m in guild.members if m.bot)
        user_count = total_count - bot_count

        channel_mappings = [
            ("total_id", "stats_total", total_count),
            ("users_id", "stats_users", user_count),
            ("bots_id", "stats_bots", bot_count)
        ]

        for key, locale_key, count in channel_mappings:
            ch_id = config.get(key)
            if ch_id:
                channel = guild.get_channel(int(ch_id))
                if channel:
                    new_name = self.get_msg(locale, locale_key, count=f"{count:,}")
                    if channel.name != new_name:
                        try: await channel.edit(name=new_name)
                        except: pass

    @tasks.loop(minutes=10.0)
    async def update_all_stats(self):
        if not self.bot.is_ready(): return
        for guild in self.bot.guilds:
            settings = await self.bot.get_guild_settings(str(guild.id))
            if settings: await self.update_guild_stats(guild, settings)

    @app_commands.command(name="stats_setup", description="Deploy stats infrastructure.")
    @app_commands.checks.has_permissions(administrator=True)
    async def stats_setup(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        locale = str(interaction.locale)[:2]

        try:
            category_name = self.get_msg(locale, "stats_category_name")
            category = await guild.create_category(name=category_name, position=0)
            
            # (나머지 생성 로직 생략, 동일하게 유지됨)
            name_total = self.get_msg(locale, "stats_total", count=str(guild.member_count))
            name_users = self.get_msg(locale, "stats_users", count=str(guild.member_count - sum(1 for m in guild.members if m.bot)))
            name_bots = self.get_msg(locale, "stats_bots", count=str(sum(1 for m in guild.members if m.bot)))
            
            overwrites = {guild.default_role: discord.PermissionOverwrite(connect=False)}
            ch_total = await guild.create_voice_channel(name=name_total, category=category, overwrites=overwrites)
            ch_users = await guild.create_voice_channel(name=name_users, category=category, overwrites=overwrites)
            ch_bots = await guild.create_voice_channel(name=name_bots, category=category, overwrites=overwrites)

            payload = {"stats_channels": {"enabled": True, "total_id": str(ch_total.id), "users_id": str(ch_users.id), "bots_id": str(ch_bots.id)}}
            await self.bot.bulk_update_guild_settings(str(guild.id), payload)
            
            await interaction.followup.send(self.get_msg(locale, "stats_enabled_msg"), ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ `{str(e)}`", ephemeral=True)

    @app_commands.command(name="stats_disable", description="Dismantle stats.")
    @app_commands.checks.has_permissions(administrator=True)
    async def stats_disable(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        locale = str(interaction.locale)[:2]
        settings = await self.bot.get_guild_settings(str(interaction.guild.id))
        config = settings.get("stats_channels", {})
        
        # ... 채널 삭제 로직 ...
        for key in ["total_id", "users_id", "bots_id"]:
            ch_id = config.get(key)
            if ch_id:
                ch = interaction.guild.get_channel(int(ch_id))
                if ch: await ch.delete()
        
        await self.bot.bulk_update_guild_settings(str(interaction.guild.id), {"stats_channels": {"enabled": False}})
        await interaction.followup.send(self.get_msg(locale, "stats_disabled_msg"), ephemeral=True)

async def setup(bot):
    await bot.add_cog(StatsChannels(bot))
