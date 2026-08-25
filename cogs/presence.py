import discord
from discord.ext import commands, tasks

PRESENCE_UPDATE_INTERVAL_MINUTES = 10


class KyvoPresence(commands.Cog):
    """봇의 활동 상태(Watching ...)를 주기적으로 전체 서버 유저 수로 갱신한다. koreanbots.py와
    동일하게 DB/Redis/i18n이 전혀 필요 없는 순수 프레즌스 갱신 작업이라 KyvoBaseCog를 상속하지
    않고 commands.Cog를 직접 쓴다."""

    def __init__(self, bot):
        self.bot = bot
        self.update_presence.start()

    async def cog_unload(self):
        self.update_presence.cancel()

    @tasks.loop(minutes=PRESENCE_UPDATE_INTERVAL_MINUTES)
    async def update_presence(self):
        # member_count는 Intents.members가 켜져 있어야 정확/실시간으로 유지된다(main.py에서 이미 활성화됨) -
        # 캐시가 아직 덜 찬 길드가 있을 경우를 대비해 None은 0으로 취급한다.
        user_count = sum(guild.member_count or 0 for guild in self.bot.guilds)
        activity = discord.Activity(type=discord.ActivityType.watching,
                                     name=f"{user_count:,} users | /inquiry (/문의)")
        try:
            await self.bot.change_presence(activity=activity)
        except Exception as e:
            print(f"[PRESENCE][ERROR] Failed to update presence: {type(e).__name__}: {e}", flush=True)

    @update_presence.before_loop
    async def before_update_presence(self):
        # 🛡️ 봇 게이트웨이 연결이 끝나기 전엔 self.bot.guilds가 비어있거나 member_count가 아직
        # 채워지지 않아, 첫 틱이 부정확한 유저 수(0 등)를 표시할 수 있다 - wait_until_ready로
        # 첫 틱을 늦춘다 (koreanbots.py와 동일한 이유).
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(KyvoPresence(bot))
    print("[⚡ PRESENCE] Cog extension setup complete.", flush=True)
