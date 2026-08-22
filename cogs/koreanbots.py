import os
import aiohttp
from discord.ext import commands, tasks

KOREANBOTS_STATS_URL = "https://koreanbots.dev/api/v2/bots/{bot_id}/stats"


class KoreanBotsStats(commands.Cog):
    """한디리(Koreanbots)에 서버 수를 주기적으로 보고한다. DB/Redis/i18n이 전혀 필요 없는
    순수 외부 API 핑 작업이라 KyvoBaseCog(모든 코그가 자체 Redis 커넥션을 새로 여는 무거운
    베이스)를 상속하지 않고 commands.Cog를 직접 쓴다."""

    def __init__(self, bot):
        self.bot = bot
        self.update_server_count.start()

    async def cog_unload(self):
        self.update_server_count.cancel()

    @tasks.loop(minutes=30)
    async def update_server_count(self):
        token = os.getenv("KOREANBOTS_TOKEN")
        if not token:
            print("[KOREANBOTS] KOREANBOTS_TOKEN not set, skipping server count update.", flush=True)
            return

        guild_count = len(self.bot.guilds)
        url = KOREANBOTS_STATS_URL.format(bot_id=self.bot.user.id)
        headers = {"Authorization": token, "Content-Type": "application/json"}
        payload = {"servers": guild_count}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=payload) as resp:
                    if resp.status == 200:
                        print(f"[KOREANBOTS] Server count updated successfully: {guild_count} servers.", flush=True)
                    else:
                        body = await resp.text()
                        print(f"[KOREANBOTS][ERROR] Failed to update server count (status={resp.status}): {body}", flush=True)
        except Exception as e:
            print(f"[KOREANBOTS][ERROR] Exception while updating server count: {type(e).__name__}: {e}", flush=True)

    @update_server_count.before_loop
    async def before_update_server_count(self):
        # 🛡️ 봇 게이트웨이 연결이 끝나기 전엔 self.bot.guilds/self.bot.user가 비어있거나 None이라,
        # 첫 틱이 부정확한 서버 수(0 등)를 보고할 수 있다 - wait_until_ready로 첫 틱을 늦춘다.
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(KoreanBotsStats(bot))
    print("[⚡ KOREANBOTS] Cog extension setup complete.", flush=True)
