import discord
from discord import app_commands
from discord.ext import commands
from cogs.base import KyvoBaseCog
from locales import get_locale_message
import aiohttp
import asyncio
import os

DASHBOARD_BASE_URL = (os.getenv("DASHBOARD_BASE_URL") or "").rstrip("/")
# 🛡️ cogs/leveling.py와 동일한 검증 - discord.py는 Button(url=...)의 스킴을 검사하지 않아서,
# 잘못된 값(스킴 누락 등)을 그대로 Discord API에 보내면 응답 자체가 HTTPException으로 실패한다.
DASHBOARD_BASE_URL_VALID = DASHBOARD_BASE_URL.startswith(("http://", "https://"))
ONBOARDING_EMBED_COLOR = 0x5865F2

# 🛡️ [운영자 전용 알림] 서버 초대/퇴장을 서포트 서버 관리자 채널로 알리는 웹훅 - 하드코딩 금지,
# .env에서만 읽는다. 미설정이면 cog_load에서 한 번만 경고를 남기고, 이후 호출부는 매번 조용히
# 스킵한다(길드가 들고날 때마다 반복 로그가 쌓이는 걸 피함) - INTERNAL_API_SECRET(ticket_ai.py)과
# 동일한 패턴.
SERVER_LOG_WEBHOOK_URL = os.getenv("SERVER_LOG_WEBHOOK_URL") or ""
SERVER_LOG_JOIN_COLOR = 0x2ECC71
SERVER_LOG_REMOVE_COLOR = 0xE74C3C
# 이 알림은 초대/퇴장한 길드가 아니라 운영자 본인이 보는 화면이라, 그 길드의 guild_settings.language로
# 번역할 이유가 없다 - ONBOARDING_LANGUAGE_FIELD_TITLE/DESC와 동일한 이유로 get_msg를 거치지 않는
# 고정 텍스트로 둔다.
SERVER_LOG_JOIN_TITLE = "📥 새로운 서버에 봇이 초대되었습니다!"
SERVER_LOG_REMOVE_TITLE = "📤 서버에서 봇이 퇴장되었습니다."

# 🛡️ [항상 이중언어] 이 서버의 최초 language는 guild.preferred_locale로 자동 시딩되는데,
# 이 값은 서버 관리자가 디스코드 서버 설정에서 일부러 "서버 언어"를 지정해야만 정확해서
# (대부분의 서버는 안 건드려서 기본값인 영어로 남는다) 실제 서버 사용 언어와 다를 수 있다.
# "언어를 어떻게 바꾸는지" 안내가 잘못 시딩된 언어로만 나가면, 그 안내 자체를 못 읽어서
# 못 고치는 역설이 생긴다 - 그래서 이 필드만은 guild_settings.language와 무관하게 항상
# 한국어+영어를 동시에 보여준다(get_msg를 거치지 않는 고정 상수). /language 명령어가
# 생긴 뒤로는 대시보드보다 이 명령어를 먼저 안내한다 - 즉시 실행 가능하기 때문.
ONBOARDING_LANGUAGE_FIELD_TITLE = "🌐 Language / 언어"
ONBOARDING_LANGUAGE_FIELD_DESC = (
    "이 봇의 언어는 /language 명령어로 바로 바꿀 수 있어요.\n"
    "You can change the bot's language instantly with the /language command."
)


def resolve_welcome_channel(guild: discord.Guild) -> discord.TextChannel | None:
    """온보딩 메시지를 보낼 채널을 고른다: system_channel(봇이 실제로 쓸 수 있으면) -> 없거나
    권한이 막혔으면 position 순으로 봇이 쓸 수 있는 첫 텍스트채널 -> 그마저 없으면 None(발송 포기)."""
    bot_member = guild.me
    if bot_member is None:
        return None

    def _can_send(channel: discord.TextChannel) -> bool:
        perms = channel.permissions_for(bot_member)
        return perms.view_channel and perms.send_messages and perms.embed_links

    system_channel = guild.system_channel
    if system_channel is not None and _can_send(system_channel):
        return system_channel

    for channel in sorted(guild.text_channels, key=lambda c: c.position):
        if _can_send(channel):
            return channel

    return None


class KyvoOnboarding(KyvoBaseCog):
    async def cog_load(self):
        if not SERVER_LOG_WEBHOOK_URL:
            print("[ONBOARDING][WARN] SERVER_LOG_WEBHOOK_URL not set - guild join/leave "
                  "notifications to the support server are disabled.", flush=True)

    async def build_welcome_embed(self, guild: discord.Guild) -> discord.Embed:
        title = await self.get_msg(guild.id, "onboarding_welcome_title")
        desc = await self.get_msg(guild.id, "onboarding_welcome_desc")

        embed = discord.Embed(title=title, description=desc, color=ONBOARDING_EMBED_COLOR)
        if DASHBOARD_BASE_URL:
            embed.url = f"{DASHBOARD_BASE_URL}/dashboard/{guild.id}"
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)

        for title_key, desc_key in (
            ("onboarding_field_party_title", "onboarding_field_party_desc"),
            ("onboarding_field_ticket_title", "onboarding_field_ticket_desc"),
            ("onboarding_field_leveling_title", "onboarding_field_leveling_desc"),
            ("onboarding_field_automod_title", "onboarding_field_automod_desc"),
        ):
            field_title = await self.get_msg(guild.id, title_key)
            field_desc = await self.get_msg(guild.id, desc_key)
            embed.add_field(name=field_title, value=field_desc, inline=False)

        # 언어 필드는 위 루프와 달리 get_msg(시딩된 언어)를 거치지 않고 항상 고정 이중언어로 표시
        embed.add_field(name=ONBOARDING_LANGUAGE_FIELD_TITLE, value=ONBOARDING_LANGUAGE_FIELD_DESC, inline=False)

        return embed

    async def _seed_guild_language(self, guild: discord.Guild) -> None:
        """신규로 초대된 서버의 guild_settings.language를 디스코드 서버 자체의 언어
        (guild.preferred_locale)로 초기화한다 - 한국어로 설정된 디스코드 서버는 "ko"로,
        그 외는 전부 "en"으로 시작해서, 대시보드에서 아무것도 안 건드린 상태의 기본값이
        무조건 영어였던 문제를 없앤다.

        🛡️ [기존 설정 보호] guild_settings 행이 이미 존재하면(재초대, 또는 대시보드/다른
        커맨드가 먼저 만든 행 등) 절대 건드리지 않는다 - "행이 아예 없을 때"만 신규 서버로
        간주해서 시딩한다. language 값 자체가 비어있는지는 안 본다 - 그것까지 따지면 언제
        만들어졌는지 모르는 행의 다른 설정을 실수로 건드릴 위험이 커진다."""
        guild_id = str(guild.id)
        try:
            existing = await asyncio.to_thread(
                lambda: self.supabase.table("guild_settings")
                .select("guild_id")
                .eq("guild_id", guild_id)
                .maybe_single()
                .execute()
            )
            if existing is not None and existing.data:
                print(f"[ONBOARDING] guild_settings row already exists for guild={guild_id}, "
                      f"leaving language untouched.", flush=True)
                return

            seeded_lang = "ko" if guild.preferred_locale == discord.Locale.korean else "en"
            await asyncio.to_thread(
                lambda: self.supabase.table("guild_settings")
                .insert({"guild_id": guild_id, "language": seeded_lang})
                .execute()
            )
            # 🛡️ 방금 만든 행을 get_msg가 곧바로(환영 메시지 렌더링 시점에) 볼 수 있어야 하므로,
            # 혹시 남아있을 수 있는 캐시(빈 값 등)를 확실히 비운다.
            await self.invalidate_settings_cache(guild.id)
            print(f"[ONBOARDING] Seeded guild_settings.language='{seeded_lang}' for new guild={guild_id} "
                  f"(preferred_locale={guild.preferred_locale}).", flush=True)
        except Exception as e:
            print(f"[ONBOARDING][WARN] Failed to seed language for guild={guild_id}: "
                  f"{type(e).__name__}: {e}", flush=True)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        await self._seed_guild_language(guild)

        embed = await self.build_welcome_embed(guild)

        channel = resolve_welcome_channel(guild)
        if channel is None:
            print(f"[ONBOARDING][WARN] No usable channel found to post the welcome message (guild={guild.id}).", flush=True)
        else:
            try:
                await channel.send(embed=embed)
            except (discord.Forbidden, discord.HTTPException) as e:
                print(f"[ONBOARDING][ERROR] Failed to send welcome message (guild={guild.id}, channel={channel.id}): "
                      f"{type(e).__name__}: {e}", flush=True)

        # 🛡️ 채널 공지가 끝난 뒤에 별도로 시도한다 - 이 블록에서 뭘 하든(audit log 조회 실패,
        # 권한 없음, DM 차단 등) 위 채널 공지 흐름에는 이미 영향을 줄 수 없는 시점이다.
        await self._notify_inviter_dm(guild, embed)

        # 🛡️ 운영자 알림도 맨 마지막에 시도한다 - 여기서 뭐가 실패하든 위의 언어 시딩/채널 공지/
        # DM은 이미 다 끝난 뒤라 전혀 영향받지 않는다.
        await self._send_server_log_webhook(self._build_join_log_embed(guild))

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        """🛡️ [순수 알림 전용] 이 리스너는 운영자에게 퇴장 사실을 알리는 것 하나만 한다 -
        guild_settings 삭제, 캐시 정리, 그 밖의 어떤 데이터 정리 로직도 여기 넣지 않는다.
        (그런 정리가 필요하다면 별도로 신중하게 설계해야 할 완전히 다른 작업이다.)"""
        await self._send_server_log_webhook(self._build_remove_log_embed(guild))

    def _build_join_log_embed(self, guild: discord.Guild) -> discord.Embed:
        owner = guild.owner
        owner_text = f"{owner.mention} (`{guild.owner_id}`)" if owner else f"`{guild.owner_id}`"

        embed = discord.Embed(
            title=SERVER_LOG_JOIN_TITLE,
            color=SERVER_LOG_JOIN_COLOR,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="서버", value=f"{guild.name} (`{guild.id}`)", inline=False)
        embed.add_field(name="멤버 수", value=f"{guild.member_count:,}명", inline=True)
        embed.add_field(name="서버 소유자", value=owner_text, inline=True)
        embed.add_field(name="봇의 총 서버 수", value=f"{len(self.bot.guilds):,}개", inline=True)
        return embed

    def _build_remove_log_embed(self, guild: discord.Guild) -> discord.Embed:
        embed = discord.Embed(
            title=SERVER_LOG_REMOVE_TITLE,
            color=SERVER_LOG_REMOVE_COLOR,
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="서버", value=f"{guild.name} (`{guild.id}`)", inline=False)
        embed.add_field(name="남은 총 서버 수", value=f"{len(self.bot.guilds):,}개", inline=True)
        return embed

    async def _send_server_log_webhook(self, embed: discord.Embed) -> None:
        """서포트 서버 관리자 채널로 길드 join/remove 알림을 보낸다 - 실패해도(URL 미설정,
        형식 오류, 네트워크 문제, 웹훅 삭제 등) 호출부 흐름엔 절대 영향을 주지 않는다."""
        if not SERVER_LOG_WEBHOOK_URL:
            return
        try:
            async with aiohttp.ClientSession() as session:
                webhook = discord.Webhook.from_url(SERVER_LOG_WEBHOOK_URL, session=session)
                await webhook.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException, ValueError) as e:
            print(f"[ONBOARDING][WARN] Failed to send server log webhook: {type(e).__name__}: {e}", flush=True)
        except Exception as e:
            print(f"[ONBOARDING][WARN] Unexpected error sending server log webhook: {type(e).__name__}: {e}", flush=True)

    async def _notify_inviter_dm(self, guild: discord.Guild, embed: discord.Embed) -> None:
        """가능하면 이 서버에 봇을 초대한 사람을 Audit Log(BOT_ADD)에서 찾아 같은 온보딩 임베드를
        DM으로도 보낸다. 채널 공지가 이미 주 통지 수단이라 이건 어디까지나 보너스 - 권한이 없거나,
        조회에 실패하거나, 초대자를 못 찾거나, DM이 막혀 있어도 전부 조용히 넘어간다(재시도 없음).

        🛡️ [한디리 가이드라인] "명령어 사용이 아닌 입장/퇴장 등 이벤트로 유저에게 DM을 보내는 경우,
        관리자가 켜고 끌 수 있어야 하며 기본값은 비허용"이어야 한다. 이 DM은 정확히 그 범주(on_guild_join
        이벤트가 트리거, 명령어 아님)라 guild_settings.settings.inviter_dm_enabled로 게이팅한다 - 키가
        아예 없는(기존/신규 불문) 모든 행은 .get(..., False)로 안전하게 기본 꺼짐이 된다. 이 가드는
        audit_logs 조회나 sleep보다 먼저 와야 "꺼져 있으면 아예 시도하지 않는다"가 성립한다."""
        try:
            row = await self.get_guild_settings(guild.id)
            nested_settings = row.get("settings") or {}
            if not nested_settings.get("inviter_dm_enabled", False):
                print(f"[ONBOARDING] Skipping inviter DM (disabled by guild settings, guild={guild.id}).", flush=True)
                return

            bot_member = guild.me
            if bot_member is None or not bot_member.guild_permissions.view_audit_log:
                print(f"[ONBOARDING] Skipping inviter DM (no view_audit_log permission, guild={guild.id}).", flush=True)
                return

            # Discord가 BOT_ADD 감사 로그 엔트리를 기록할 시간을 짧게 준다 - on_guild_join
            # 발화 시점엔 아직 안 써져 있을 수 있다. 길게 재시도하지 않고 한 번만 조회한다.
            await asyncio.sleep(1.5)

            inviter: discord.User | discord.Member | None = None
            async for entry in guild.audit_logs(action=discord.AuditLogAction.bot_add, limit=5):
                if entry.target and entry.target.id == self.bot.user.id:
                    inviter = entry.user  # audit_logs는 최신순이라 첫 매치가 가장 최근 것
                    break

            if inviter is None:
                print(f"[ONBOARDING] No matching bot_add audit log entry found (guild={guild.id}).", flush=True)
                return

            await inviter.send(embed=embed)
            print(f"[ONBOARDING] Sent onboarding DM to inviter user={inviter.id} (guild={guild.id}).", flush=True)
        except (discord.Forbidden, discord.HTTPException) as e:
            print(f"[ONBOARDING][WARN] Failed to DM this guild's inviter (guild={guild.id}): "
                  f"{type(e).__name__}: {e}", flush=True)
        except Exception as e:
            print(f"[ONBOARDING][WARN] Unexpected error while notifying inviter (guild={guild.id}): "
                  f"{type(e).__name__}: {e}", flush=True)

    # 🛡️ default_permissions(administrator=True)는 has_permissions()와 달리 "권한 없으면 에러"가
    # 아니라 "권한 없는 유저에게는 명령어 자체가 안 보임"이다(Discord 클라이언트가 필터링) - 이
    # 명령어는 관리자용 대시보드 링크일 뿐이라 일반 유저에게 노출될 이유가 없어서 이 방식을 쓴다.
    @app_commands.command(name="dashboard", description="Get a link to this server's admin dashboard.")
    @app_commands.default_permissions(administrator=True)
    async def dashboard(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id

        if DASHBOARD_BASE_URL and DASHBOARD_BASE_URL_VALID:
            button_label = await self.get_msg(guild_id, "dashboard_link_button")
            view = discord.ui.View()
            view.add_item(discord.ui.Button(
                label=button_label,
                style=discord.ButtonStyle.link,
                url=f"{DASHBOARD_BASE_URL}/dashboard/{guild_id}",
            ))
            await interaction.followup.send(view=view, ephemeral=True)
        else:
            # 🛡️ URL을 못 만드는 상황(미설정 또는 스킴 없음)에서도 명령어가 조용히 실패하지
            # 않도록, 버튼 없이 텍스트 응답만이라도 나가게 한다.
            msg = await self.get_msg(guild_id, "dashboard_link_unavailable")
            await interaction.followup.send(msg, ephemeral=True)

    # 🛡️ 대시보드의 "일반 설정" 언어 드롭다운과 완전히 동일한 기능을 디스코드 안에서 제공한다 -
    # 같은 guild_settings.language 컬럼, 같은 Redis 캐시 키(guild:{guild_id}:settings)를 쓰므로
    # 대시보드/이 명령어 어느 쪽에서 바꿔도 서로 꼬이지 않는다. default_permissions는 /dashboard와
    # 동일한 이유로 administrator 전용.
    @app_commands.command(name="language", description="Change this server's language (English or Korean).")
    @app_commands.describe(language="The language Kyvo should reply in from now on.")
    @app_commands.choices(language=[
        app_commands.Choice(name="English", value="en"),
        app_commands.Choice(name="한국어", value="ko"),
    ])
    @app_commands.default_permissions(administrator=True)
    async def language(self, interaction: discord.Interaction, language: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        new_lang = language.value

        try:
            await asyncio.to_thread(
                lambda: self.supabase.table("guild_settings")
                .upsert({"guild_id": str(guild_id), "language": new_lang}, on_conflict="guild_id")
                .execute()
            )
            await self.invalidate_settings_cache(guild_id)
        except Exception as e:
            print(f"[ONBOARDING][ERROR] Failed to save language for guild={guild_id}: "
                  f"{type(e).__name__}: {e}", flush=True)
            # 🛡️ 저장이 실패했으니 new_lang은 반영되지 않았다 - 이 에러 메시지는 (아직 유효한)
            # 서버의 기존 언어로 보여준다(get_msg), 방금 실패한 선택값(get_locale_message)이 아니라.
            msg = await self.get_msg(guild_id, "language_err_save_failed")
            await interaction.followup.send(msg, ephemeral=True)
            return

        # 🛡️ 저장이 방금 성공했으니 new_lang이 곧 이 서버의 현재 언어다 - get_msg로 다시
        # guild_settings를 조회할 필요 없이, 이미 아는 값으로 바로 get_locale_message를 부른다.
        msg = get_locale_message(new_lang, "language_set_success")
        await interaction.followup.send(msg, ephemeral=True)


async def setup(bot):
    await bot.add_cog(KyvoOnboarding(bot))
    print("[⚡ ONBOARDING] Cog extension setup complete.", flush=True)
