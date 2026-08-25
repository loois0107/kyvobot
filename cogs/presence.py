import discord
from discord.ext import commands, tasks

MEMBER_COUNT_REFRESH_INTERVAL_MINUTES = 10  # 전체 서버 유저 수 재계산 주기 - 무거운 쪽(길드 순회)
PRESENCE_TOGGLE_INTERVAL_SECONDS = 20  # 화면 표시 전환 주기 - 가벼운 쪽(캐시값 읽기만)

INQUIRY_HINT_TEXT = "/inquiry to contact us | /문의 로 문의하기"


class KyvoPresence(commands.Cog):
    """봇의 활동 상태(Watching ...)를 "총 유저 수"와 "/inquiry 안내" 두 문구로 번갈아 표시한다.
    koreanbots.py와 동일하게 DB/Redis/i18n이 전혀 필요 없는 순수 프레즌스 갱신 작업이라
    KyvoBaseCog를 상속하지 않고 commands.Cog를 직접 쓴다.

    🛡️ [루프를 둘로 분리한 이유] 유저 수 재계산(길드 전체 순회)은 무겁고 자주 할 필요가 없어서
    10분 주기로, 화면에 뭘 보여줄지 전환하는 건 가벼워서 20초 주기로 따로 돈다 - 20초마다
    매번 길드를 다시 순회하면 불필요한 반복 연산이 된다. 20초 루프는 10분 루프가 최근에 계산해
    self.cached_user_count에 저장해둔 값을 읽기만 한다.

    change_presence() 호출 빈도: Discord 공식 Gateway 문서 기준 "연결당 60초에 120개 이벤트"가
    한도이고(모든 게이트웨이 명령이 공유), 20초 주기(분당 3회)는 이 안에서 압도적으로 여유롭다.
    """

    def __init__(self, bot):
        self.bot = bot
        self.cached_user_count = 0
        self._show_inquiry_hint = False  # 다음 20초 틱에 어떤 문구를 보여줄지 - 매 틱마다 뒤집는다
        self.update_member_count.start()
        self.toggle_presence.start()

    async def cog_unload(self):
        self.update_member_count.cancel()
        self.toggle_presence.cancel()

    def _count_members(self) -> int:
        # member_count는 Intents.members가 켜져 있어야 정확/실시간으로 유지된다(main.py에서 이미 활성화됨) -
        # 캐시가 아직 덜 찬 길드가 있을 경우를 대비해 None은 0으로 취급한다.
        return sum(guild.member_count or 0 for guild in self.bot.guilds)

    @tasks.loop(minutes=MEMBER_COUNT_REFRESH_INTERVAL_MINUTES)
    async def update_member_count(self):
        self.cached_user_count = self._count_members()

    @update_member_count.before_loop
    async def before_update_member_count(self):
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=PRESENCE_TOGGLE_INTERVAL_SECONDS)
    async def toggle_presence(self):
        if self._show_inquiry_hint:
            name = INQUIRY_HINT_TEXT
        else:
            name = f"{self.cached_user_count:,} users online | {self.cached_user_count:,}명 접속 중"
        self._show_inquiry_hint = not self._show_inquiry_hint

        activity = discord.Activity(type=discord.ActivityType.watching, name=name)
        try:
            await self.bot.change_presence(activity=activity)
        except Exception as e:
            print(f"[PRESENCE][ERROR] Failed to update presence: {type(e).__name__}: {e}", flush=True)

    @toggle_presence.before_loop
    async def before_toggle_presence(self):
        # 🛡️ 봇 게이트웨이 연결이 끝나기 전엔 self.bot.guilds가 비어있거나 member_count가 아직
        # 채워지지 않아 부정확한 값(0 등)을 표시할 수 있다 - wait_until_ready로 대기한다
        # (koreanbots.py와 동일한 이유). 그 다음, update_member_count의 10분 첫 틱을 기다리지
        # 않고 이 루프가 처음 표시를 내보내기 전에 유저 수를 즉시 한 번 계산해 채워둔다 -
        # update_member_count.before_loop에 맡기면 두 before_loop 실행 순서가 보장되지 않는다.
        await self.bot.wait_until_ready()
        self.cached_user_count = self._count_members()


async def setup(bot):
    await bot.add_cog(KyvoPresence(bot))
    print("[⚡ PRESENCE] Cog extension setup complete.", flush=True)
