import os

import discord
from discord import app_commands
from discord.ext import commands

from cogs.base import KyvoBaseCog
from cogs.i18n_commands import COMMAND_KO

# 🛡️ onboarding.py의 dashboard 커맨드와 동일한 패턴(각 cog가 자기 완결적이도록 복제) -
# Button(url=...)의 스킴을 discord.py가 검사하지 않아서, 잘못된 값(스킴 누락 등)을 그대로
# Discord API에 보내면 응답 자체가 HTTPException으로 실패한다.
DASHBOARD_BASE_URL = (os.getenv("DASHBOARD_BASE_URL") or "").rstrip("/")
DASHBOARD_BASE_URL_VALID = DASHBOARD_BASE_URL.startswith(("http://", "https://"))
HELP_EMBED_COLOR = 0x5865F2

# 카테고리별 대표 명령어 - 대시보드 사이드바(kyvobot-dashboard의
# app/dashboard/[guildId]/layout.tsx)가 이미 쓰고 있는 4개 카테고리(Community Management /
# Party & Games / Economy & Engagement / Integrations & AI Support)와 동일한 분류를 따른다.
# 각 항목은 app_commands 트리에 등록된 qualified_name 그대로 - 그룹 서브커맨드는 여기 없이
# 그룹 이름만 대표로 올린다(예: "shop"은 shop 그룹 전체를 가리킴, "shop add"처럼 특정
# 서브커맨드를 콕 집지 않음).
HELP_CATEGORIES: list[tuple[str, list[str]]] = [
    ("help_category_community", ["anonymous_report", "reaction_role_add", "cc_add"]),
    ("help_category_party", ["party_recruit", "scrim_start", "gg", "tier_set"]),
    ("help_category_economy", ["balance", "daily", "shop", "level", "giveaway"]),
    ("help_category_integrations", ["twitch_channel_set", "ticket-setup"]),
]


def _resolve_live_command(bot: commands.Bot, qualified_name: str):
    """qualified_name("shop"처럼 그룹, 또는 "shop view"처럼 그룹 서브커맨드)으로 실제 등록된
    app_commands.Command/Group 객체를 찾는다. 모든 cog가 이미 로드된 뒤(=/help가 실제로
    호출되는 시점)에만 쓰이므로, main.py의 extensions 리스트에서 cogs.help의 위치는
    상관없다."""
    parts = qualified_name.split(" ")
    cmd = bot.tree.get_command(parts[0])
    for part in parts[1:]:
        if cmd is None or not hasattr(cmd, "get_command"):
            return None
        cmd = cmd.get_command(part)
    return cmd


def _describe_command(bot: commands.Bot, qualified_name: str, lang: str) -> tuple[str, bool]:
    """(설명 텍스트, 관리자 전용 여부)를 반환한다.

    한국어는 cogs/i18n_commands.py의 COMMAND_KO를 그대로 재사용한다 - 새로 번역하지 않는다.
    그 테이블은 원래 "유저 개인의 Discord 클라이언트 로케일" 기준으로 만들어졌지만, 여기서는
    "서버 설정 언어"(guild_settings.language, get_msg와 동일 기준) 기준으로 재사용한다 - 둘 다
    그냥 한국어 문자열이라 내용상 문제없다.

    영어는 실제 등록된 명령어의 description을 그대로 가져온다 - 별도로 하드코딩하면 원본
    description이 나중에 바뀔 때 여기가 따로 놀 수 있어서, 항상 살아있는 값을 참조한다.

    관리자 전용 여부는 (A) 단일 기준 - default_permissions가 걸려 있으면(요구 권한이
    administrator든 manage_guild든 상관없이) 무조건 "관리자 전용"으로 취급한다. 그룹
    서브커맨드는 discord.py가 default_permissions를 무시하므로("Due to a Discord limitation,
    this decorator does nothing in subcommands") 그룹 자체의 값만 의미가 있다 - 이 파일이
    대표로 올리는 항목들은 전부 그룹 아니면 최상위 명령어라 문제없다.
    """
    cmd = _resolve_live_command(bot, qualified_name)
    is_gated = bool(cmd is not None and getattr(cmd, "default_permissions", None) is not None)

    if lang == "ko":
        entry = COMMAND_KO.get(qualified_name)
        if entry:
            return entry["description"], is_gated

    return (cmd.description if cmd else ""), is_gated


class KyvoHelp(KyvoBaseCog):
    async def build_help_embed(self, guild: discord.Guild, is_admin: bool) -> discord.Embed:
        guild_id = guild.id
        settings = await self.get_guild_settings(guild_id)
        lang = settings.get("language", "en")

        title = await self.get_msg(guild_id, "help_title")
        intro = await self.get_msg(guild_id, "help_intro")
        # 관리자에겐 /dashboard로, 일반 유저에겐 아래 버튼(프로필 페이지)으로 안내한다 - 일반
        # 유저는 애초에 /dashboard를 실행할 권한이 없어서(default_permissions administrator=True),
        # "언제든 /dashboard를 실행하세요"라고 안내하면 본인은 못 쓰는 명령어를 안내받는 셈이 된다.
        quickstart_key = "help_quickstart_admin" if is_admin else "help_quickstart_member"
        quickstart = await self.get_msg(guild_id, quickstart_key)
        embed = discord.Embed(title=title, description=f"{intro}\n\n{quickstart}", color=HELP_EMBED_COLOR)

        for category_key, qualified_names in HELP_CATEGORIES:
            lines = []
            for qualified_name in qualified_names:
                desc_text, gated = _describe_command(self.bot, qualified_name, lang)
                if gated and not is_admin:
                    continue
                lines.append(f"`/{qualified_name}` — {desc_text}")

            if not lines:
                # 이 카테고리의 대표 명령어가 전부 관리자 전용인데 지금 보는 사람은 관리자가
                # 아니면(예: 연동&AI지원), 필드 자체를 통째로 숨긴다 - 빈 필드를 보여주지 않는다.
                continue

            category_title = await self.get_msg(guild_id, category_key)
            embed.add_field(name=category_title, value="\n".join(lines), inline=False)

        footer = await self.get_msg(guild_id, "help_footer")
        embed.set_footer(text=footer)
        return embed

    @app_commands.command(name="help", description="See what Kyvo can do, organized by category.")
    async def help_command(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        is_admin = interaction.user.guild_permissions.manage_guild

        embed = await self.build_help_embed(guild, is_admin)

        view = None
        if DASHBOARD_BASE_URL and DASHBOARD_BASE_URL_VALID:
            # 관리자는 서버 관리자 대시보드로, 일반 유저는 본인 프로필 페이지로 - 관리자 대시보드는
            # 어차피 requireGuildAdmin(대시보드 쪽)에 막혀서 일반 유저가 눌러봤자 403만 볼 뿐이다.
            if is_admin:
                button_label = await self.get_msg(guild.id, "dashboard_link_button")
                target_path = f"dashboard/{guild.id}"
            else:
                button_label = await self.get_msg(guild.id, "help_profile_button")
                target_path = f"profile/{guild.id}"

            view = discord.ui.View()
            view.add_item(discord.ui.Button(
                label=button_label,
                style=discord.ButtonStyle.link,
                url=f"{DASHBOARD_BASE_URL}/{target_path}",
            ))

        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


async def setup(bot):
    await bot.add_cog(KyvoHelp(bot))
    print("[⚡ HELP] Cog extension setup complete.", flush=True)
