import discord
from discord import app_commands
from discord.ext import commands
from cogs.base import KyvoBaseCog
import asyncio
import os

DASHBOARD_BASE_URL = (os.getenv("DASHBOARD_BASE_URL") or "").rstrip("/")
# 🛡️ cogs/leveling.py와 동일한 검증 - discord.py는 Button(url=...)의 스킴을 검사하지 않아서,
# 잘못된 값(스킴 누락 등)을 그대로 Discord API에 보내면 응답 자체가 HTTPException으로 실패한다.
DASHBOARD_BASE_URL_VALID = DASHBOARD_BASE_URL.startswith(("http://", "https://"))
ONBOARDING_EMBED_COLOR = 0x5865F2


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
            ("onboarding_field_language_title", "onboarding_field_language_desc"),
        ):
            field_title = await self.get_msg(guild.id, title_key)
            field_desc = await self.get_msg(guild.id, desc_key)
            embed.add_field(name=field_title, value=field_desc, inline=False)

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

        channel = resolve_welcome_channel(guild)
        if channel is None:
            print(f"[ONBOARDING][WARN] No usable channel found to post the welcome message (guild={guild.id}).", flush=True)
            return

        embed = await self.build_welcome_embed(guild)
        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException) as e:
            print(f"[ONBOARDING][ERROR] Failed to send welcome message (guild={guild.id}, channel={channel.id}): "
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


async def setup(bot):
    await bot.add_cog(KyvoOnboarding(bot))
    print("[⚡ ONBOARDING] Cog extension setup complete.", flush=True)
