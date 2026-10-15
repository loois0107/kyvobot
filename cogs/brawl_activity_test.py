import discord
from discord import app_commands
from discord.ext import commands

# 🛡️ [스파이크 전용 - 임시 검증 코드] brawl-spike Activity를 일반 텍스트 채널의 버튼에서
# discord.py의 InteractionResponse.launch_activity()(interaction response type 12,
# LAUNCH_ACTIVITY, 2.6+에 추가됨)로 실행할 수 있는지만 확인하는 최소 스파이크다. 파티 카드
# UI나 실제 매칭 로직은 전혀 없다 - 검증 끝나면 이 파일은 지우거나 실제 기능으로 바꿔치기할
# 예정이라 i18n(KyvoBaseCog.get_msg)도 안 쓰고 메시지를 그냥 하드코딩했다.
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
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="격투 Activity 실행",
        style=discord.ButtonStyle.primary,
        custom_id=LAUNCH_BUTTON_CUSTOM_ID,
    )
    async def launch(self, interaction: discord.Interaction, button: discord.ui.Button):
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


class BrawlActivityTestCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="격투테스트",
        description="[스파이크] 텍스트 채널 버튼으로 brawl-spike Activity 실행을 테스트한다.",
    )
    async def brawl_test(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "아래 버튼을 누르면 격투 Activity(brawl-spike)가 이 텍스트 채널에서 바로 실행됩니다.\n"
            "(LAUNCH_ACTIVITY 스파이크 테스트용 - 실제 파티 매칭 기능은 아직 없음)",
            view=LaunchBrawlSpikeView(),
        )


async def setup(bot: commands.Bot):
    # 🛡️ 영구 뷰(timeout=None)는 봇이 재시작된 뒤에도 기존에 떠 있던 메시지의 버튼이 계속
    # 눌리도록, 미리 한 번 등록해둬야 한다(custom_id 기준으로 콜백이 다시 연결됨) - party.py 등
    # 기존 코그들과 동일한 패턴.
    bot.add_view(LaunchBrawlSpikeView())
    await bot.add_cog(BrawlActivityTestCog(bot))
