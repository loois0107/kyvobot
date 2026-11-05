import discord
from discord import app_commands
from discord.ext import commands

from cogs.base import KyvoBaseCog

# 🛡️ [스파이크 전용 - 임시 검증 코드] brawl-spike Activity를 일반 텍스트 채널의 버튼에서
# discord.py의 InteractionResponse.launch_activity()(interaction response type 12,
# LAUNCH_ACTIVITY, 2.6+에 추가됨)로 실행할 수 있는지만 확인하는 최소 스파이크다. 파티 카드
# UI나 실제 매칭 로직은 전혀 없다 - 검증 끝나면 이 파일은 지우거나 실제 기능으로 바꿔치기할
# 예정이다. 커맨드 name/description은 다른 코그들과 동일하게 KoreanCommandTranslator +
# COMMAND_KO(cogs/i18n_commands.py)로 로케일별 번역된다(유저 개인의 Discord 클라이언트
# 언어 기준). 버튼 클릭 전 /moa 응답의 본문 메시지는 KyvoBaseCog.get_msg()로 서버 설정
# 언어(guild_settings.language) 기준으로 번역한다 - 이 둘은 서로 다른 축이라 유저 개인
# 언어와 서버 설정 언어가 다르면 커맨드 이름/설명과 본문이 각각 다른 언어로 보일 수 있다.
#
# 🛡️ [사전 확인] brawl-spike의 DISCORD_CLIENT_ID(.env)는 kyvobot-dashboard의 AUTH_DISCORD_ID를
# 재사용한 값인데, 이 봇 토큰을 디코딩해서 나온 application_id와 정확히 일치한다 - 즉 이 봇과
# brawl-spike Activity가 "같은 디스코드 애플리케이션"이다. launch_activity()는 파라미터 없이
# "앱에 연결된 액티비티"를 그대로 띄우는 방식이라, 이 전제가 맞아야 스파이크가 의미가 있다.

# 버튼의 custom_id는 재시작 후에도 discord.py가 콜백을 다시 연결할 수 있게 고정 문자열이어야
# 한다(party.py의 PARTY_JOIN_CUSTOM_ID 등과 같은 이유) - 여기서는 추가로, 이 custom_id가
# Activity 쪽(discordSdk.customId)에 실제로 전달되는지도 같이 확인하는 게 스파이크의 핵심이다.
LAUNCH_BUTTON_CUSTOM_ID = "kyvo_brawl_spike_test:launch"


class LaunchBrawlSpikeView(discord.ui.View):
    # 🛡️ [음성 채널 참가 제약] voice_channel_id는 /moa를 실행한 사람이 그 순간 있던 음성
    # 채널을 기억해두는 용도 - 버튼을 누르는 사람이 "같은" 음성 채널에 있는지 비교할 기준이다.
    # 기본값 None은 setup()의 bot.add_view(LaunchBrawlSpikeView())(영구 뷰 재등록용, 특정
    # 메시지에 묶이지 않은 템플릿 인스턴스)가 인자 없이 호출돼도 되게 하기 위함 - 재시작 전에
    # 발급된 실제 메시지들은 메모리상 자기 voice_channel_id를 계속 들고 있으니 영향 없고,
    # 봇이 재시작된 뒤 그 이전 메시지의 버튼을 누르면(discord.py가 이 템플릿 인스턴스로 라우팅)
    # voice_channel_id=None이라 항상 "참가 불가" 쪽으로 떨어진다 - 조용히 아무나 통과시키는
    # 것보다 안전한 방향이라 의도적으로 이렇게 둔다(재시작 케이스는 요청 범위 밖).
    def __init__(self, voice_channel_id: int | None = None):
        super().__init__(timeout=None)
        self.voice_channel_id = voice_channel_id

    @discord.ui.button(
        label="격투 Activity 실행",
        style=discord.ButtonStyle.primary,
        custom_id=LAUNCH_BUTTON_CUSTOM_ID,
    )
    async def launch(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 🛡️ [음성 채널 참가 제약] /moa를 실행한 사람과 "같은" 음성 채널에 있는 사람만 통과시킨다.
        member = interaction.user
        clicker_voice_channel = (
            member.voice.channel if isinstance(member, discord.Member) and member.voice else None
        )
        same_channel = (
            self.voice_channel_id is not None
            and clicker_voice_channel is not None
            and clicker_voice_channel.id == self.voice_channel_id
        )
        if not same_channel:
            party_channel = (
                interaction.guild.get_channel(self.voice_channel_id)
                if interaction.guild and self.voice_channel_id
                else None
            )
            party_channel_name = party_channel.name if party_channel else "해당"
            await interaction.response.send_message(
                f"이 파티는 {party_channel_name} 음성 채널에 있는 사람만 참가할 수 있어요.",
                ephemeral=True,
            )
            return

        # 🛡️ 이 스파이크의 핵심 검증 대상 - LAUNCH_ACTIVITY 콜백이 실제로 동작하는지.
        try:
            await interaction.response.launch_activity()
        except Exception as e:
            print(f"[BRAWL_SPIKE_TEST][ERROR] launch_activity() 실패: {type(e).__name__}: {e}", flush=True)
            import traceback
            traceback.print_exc()
            # 🛡️ launch_activity() 자체가 이 interaction의 "첫 응답"이라, 실패하면 아직 응답
            # 안 된 상태일 수 있다 - 그 경우에만 에러를 채널에 보여주고, 이미 응답된 뒤
            # (InteractionResponded 등) 실패한 거면 콘솔 로그로 충분하니 조용히 넘어간다.
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    f"LAUNCH_ACTIVITY 실패: {type(e).__name__}: {e}", ephemeral=True
                )


class BrawlActivityTestCog(KyvoBaseCog):
    @app_commands.command(
        name="moa",
        description="Start a 4-player free-for-all mini-game with friends.",
    )
    async def brawl_test(self, interaction: discord.Interaction):
        # 🛡️ [음성 채널 참가 제약] 커맨드를 실행한 사람이 음성 채널에 없으면 버튼 자체를
        # 띄우지 않는다 - party.py/scrim.py 등에서 쓰는 "member.voice is None or
        # member.voice.channel is None" 체크와 동일한 패턴.
        member = interaction.user
        voice_channel = (
            member.voice.channel if isinstance(member, discord.Member) and member.voice else None
        )
        if voice_channel is None:
            await interaction.response.send_message(
                "먼저 음성 채널에 들어간 뒤 다시 시도해주세요.",
                ephemeral=True,
            )
            return

        message = await self.get_msg(
            interaction.guild_id,
            "moa_launch_message",
            voice_channel=voice_channel.name,
        )
        await interaction.response.send_message(
            message,
            view=LaunchBrawlSpikeView(voice_channel.id),
        )


async def setup(bot: commands.Bot):
    # 🛡️ 영구 뷰(timeout=None)는 봇이 재시작된 뒤에도 기존에 떠 있던 메시지의 버튼이 계속
    # 눌리도록, 미리 한 번 등록해둬야 한다(custom_id 기준으로 콜백이 다시 연결됨) - party.py 등
    # 기존 코그들과 동일한 패턴.
    bot.add_view(LaunchBrawlSpikeView())
    await bot.add_cog(BrawlActivityTestCog(bot))
