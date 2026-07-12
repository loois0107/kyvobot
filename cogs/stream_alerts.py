import discord
from discord.ext import commands, tasks
import aiohttp
import asyncio

class StreamAlerts(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.session = None
        self.live_status_cache = {}
        self.stream_check_loop.start()

    def cog_unload(self):
        self.stream_check_loop.cancel()
        if self.session and not self.session.closed:
            asyncio.create_task(self.session.close())

    async def get_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    @tasks.loop(minutes=2.0)
    async def stream_check_loop(self):
        if not self.bot.is_ready():
            return

        session = await self.get_session()
        
        for guild in self.bot.guilds:
            guild_id = str(guild.id)
            locale = str(guild.preferred_locale)
            settings = await self.bot.get_guild_settings(guild_id)
            if not settings:
                continue

            alert_config = settings.get("stream_alerts", {})
            chzzk_id = alert_config.get("chzzk_id")
            channel_id = alert_config.get("alert_channel_id")

            if not chzzk_id or not channel_id:
                continue

            target_channel = guild.get_channel(int(channel_id))
            if not target_channel:
                continue

            try:
                url = f"https://api.chzzk.naver.com/service/v2/channels/{chzzk_id}/live-detail"
                headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
                
                async with session.get(url, headers=headers) as response:
                    if response.status == 200:
                        data = await response.json()
                        content = data.get("content")
                        
                        if content:
                            status = content.get("status")
                            stream_title = content.get("liveTitle", "No Title")
                            stream_category = content.get("liveCategoryValue", "Just Chatting")
                            viewer_count = content.get("concurrentUserCount", 0)
                            channel_name = content.get("channel", {}).get("channelName", "Streamer")
                            channel_image = content.get("channel", {}).get("channelImageUrl", "")
                            live_thumb = content.get("liveImageUrl", "").replace("{type}", "1080")

                            cache_key = f"{guild_id}_{chzzk_id}"
                            was_live = self.live_status_cache.get(cache_key, False)

                            if status == "OPEN" and not was_live:
                                self.live_status_cache[cache_key] = True
                                
                                title_str = self.bot.locale_manager.get(locale, "stream_live_title", name=channel_name)
                                cat_str = self.bot.locale_manager.get(locale, "stream_category")
                                view_str = self.bot.locale_manager.get(locale, "stream_viewers")
                                footer_str = self.bot.locale_manager.get(locale, "stream_footer")
                                content_str = self.bot.locale_manager.get(locale, "stream_alert_content", name=channel_name)
                                log_reason_str = self.bot.locale_manager.get(locale, "stream_log_reason", title=stream_title)

                                embed = discord.Embed(
                                    title=title_str,
                                    description=f"**[{stream_title}](https://chzzk.naver.com/live/{chzzk_id})**",
                                    color=0x00FFBB,
                                    timestamp=discord.utils.utcnow()
                                )
                                embed.add_field(name=cat_str, value=f"`{stream_category}`", inline=True)
                                embed.add_field(name=view_str, value=f"`{viewer_count:,}`", inline=True)
                                if live_thumb:
                                    embed.set_image(url=live_thumb)
                                if channel_image:
                                    embed.set_thumbnail(url=channel_image)
                                embed.set_footer(text=footer_str)
                                
                                await target_channel.send(content=content_str, embed=embed)
                                
                                await self.bot.log_to_supabase(
                                    guild_id=guild_id,
                                    action_type="STREAM_ALERT",
                                    user_name=channel_name,
                                    user_id=chzzk_id,
                                    moderator_name="KYVO SYSTEM",
                                    reason=log_reason_str
                                )

                            elif status != "OPEN":
                                self.live_status_cache[cache_key] = False

            except Exception as e:
                print(f"[Stream Alert Exception] Guild {guild_id}, Streamer {chzzk_id}: {e}")
            
            await asyncio.sleep(0.5)

async def setup(bot):
    await bot.add_cog(StreamAlerts(bot))
