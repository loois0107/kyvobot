import os
import logging
import aiohttp
import discord
from discord.ext import commands, tasks

logger = logging.getLogger(__name__)

class TopGG(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.token = os.getenv("TOPGG_TOKEN")
        
        if self.token:
            self.update_stats.start()
        else:
            logger.warning("[Top.gg] TOPGG_TOKEN이 .env에 없어 자동 업데이트가 비활성화됩니다.")

    def cog_unload(self):
        if self.token:
            self.update_stats.cancel()

    async def post_guild_count(self):
        """Top.gg API로 현재 서버 수 전송"""
        if not self.token or not self.bot.user:
            return

        url = f"https://top.gg/api/bots/{self.bot.user.id}/stats"
        headers = {
            "Authorization": self.token,
            "Content-Type": "application/json"
        }
        payload = {
            "server_count": len(self.bot.guilds)
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, headers=headers) as resp:
                    if resp.status == 200:
                        logger.info(f"[Top.gg] 서버 수 업데이트 성공: {len(self.bot.guilds)}개")
                    else:
                        res_text = await resp.text()
                        logger.error(f"[Top.gg] 업데이트 실패 (HTTP {resp.status}): {res_text}")
        except Exception as e:
            logger.error(f"[Top.gg] API 요청 중 에러 발생: {e}")

    # 30분마다 자동 실행
    @tasks.loop(minutes=30)
    async def update_stats(self):
        await self.post_guild_count()

    @update_stats.before_loop
    async def before_update_stats(self):
        # 봇 캐시 로딩이 완료될 때까지 대기
        await self.bot.wait_until_ready()

    # 트리거 1: 봇 켜졌을 때
    @commands.Cog.listener()
    async def on_ready(self):
        await self.post_guild_count()

    # 트리거 2: 새 서버에 들어갔을 때
    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        await self.post_guild_count()

    # 트리거 3: 서버에서 튕겨나갔을 때
    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        await self.post_guild_count()

async def setup(bot: commands.Bot):
    await bot.add_cog(TopGG(bot))