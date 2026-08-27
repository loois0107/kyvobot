import os
import aiohttp
from discord.ext import tasks, commands

class BotsGG(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.token = os.getenv("BOTS_GG_TOKEN")
        self.post_stats.start()

    def cog_unload(self):
        self.post_stats.cancel()

    @tasks.loop(minutes=30)
    async def post_stats(self):
        await self.bot.wait_until_ready()
        
        if not self.token:
            print("[discord.bots.gg] .env에서 BOTS_GG_TOKEN을 찾을 수 없습니다.")
            return

        url = f"https://discord.bots.gg/api/v1/bots/{self.bot.user.id}/stats"
        headers = {
            "Authorization": self.token,
            "Content-Type": "application/json"
        }
        payload = {
            "guildCount": len(self.bot.guilds)
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, headers=headers) as resp:
                    if resp.status == 200:
                        print("[discord.bots.gg] 서버 수 반영 성공!")
                    else:
                        print(f"[discord.bots.gg] 반영 실패 (Status: {resp.status})")
        except Exception as e:
            print(f"[discord.bots.gg] 통신 에러: {e}")

async def setup(bot):
    await bot.add_cog(BotsGG(bot))