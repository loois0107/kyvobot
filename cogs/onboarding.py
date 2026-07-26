import discord
from discord.ext import commands
from cogs.base import KyvoBaseCog
import os

DASHBOARD_BASE_URL = (os.getenv("DASHBOARD_BASE_URL") or "").rstrip("/")
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
        ):
            field_title = await self.get_msg(guild.id, title_key)
            field_desc = await self.get_msg(guild.id, desc_key)
            embed.add_field(name=field_title, value=field_desc, inline=False)

        return embed

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
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


async def setup(bot):
    await bot.add_cog(KyvoOnboarding(bot))
    print("[⚡ ONBOARDING] Cog extension setup complete.", flush=True)
