import discord
from discord.ext import commands
import re
import datetime
from collections import defaultdict

class AutoMod(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.spam_cache = {}
        self.invite_regex = re.compile(r"(discord\.gg|discord\.com/invite)/[a-zA-Z0-9]+")
        self.DEFAULT_SPAM_MAX_MSGS = 4
        self.MAX_MENTIONS = 5
        self.banned_words = [r"\bnigger\b", r"\bretard\b", r"scam\.link", r"free\-nitro"]

    async def _log_to_supabase(self, guild_id: int, user_id: int, action: str, reason: str):
        try:
            data = {
                "guild_id": str(guild_id),
                "user_id": str(user_id),
                "action": action,
                "reason": reason,
                "created_at": discord.utils.utcnow().isoformat()
            }
            await self.bot.loop.run_in_executor(
                None, 
                lambda: self.bot.supabase.table("automod_logs").insert(data).execute()
            )
        except Exception as e:
            print(f"[AUTOMOD EXCEPTION] Failed logging infraction data: {e}")

    async def _execute_punishment(self, message: discord.Message, reason: str):
        guild = message.guild
        user = message.author
        
        # 🛡️ [방어선 1] 라이브러리 버전별(discord.py / pycord) 타임아웃 속성 호환성 예외 처리
        timeout_until = getattr(user, 'communication_disabled_until', None) or getattr(user, 'timed_out_until', None)
        if timeout_until and timeout_until > discord.utils.utcnow():
            return
        
        try:
            await message.delete()
        except discord.NotFound:
            return
        except discord.Forbidden:
            print(f"[AUTOMOD WARNING] Missing permission to delete message in guild {guild.id}")
            return

        duration = datetime.timedelta(minutes=10)
        try:
            await user.timeout(duration, reason=f"[KyvoBot AutoMod] {reason}")
            action_taken = "TIMEOUT_10M"
        except discord.Forbidden:
            action_taken = "DELETE_ONLY"
            print(f"[AUTOMOD WARNING] Insufficient hierarchy to timeout user {user.id} in guild {guild.id}")

        await self._log_to_supabase(guild.id, user.id, action_taken, reason)

        # ✉️ 은밀한 DM 경고장 작성
        private_embed = discord.Embed(
            title="🛡️ AutoMod Infraction Intercepted",
            description=f"Hello {user.name}, your message in **{guild.name}** violated server protective policies and was removed.",
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow()
        )
        private_embed.add_field(name="Reason", value=f"```\n{reason}\n```", inline=False)
        private_embed.add_field(name="Action Applied", value=f"` {action_taken} `", inline=True)
        private_embed.set_footer(text="Guild Protective Layer • Powered by KyvoBot")
        
        try:
            await user.send(embed=private_embed)
        except discord.Forbidden:
            # DM이 차단된 유저만 채널에 임시 경고 (3초 후 자동 삭제)
            await message.channel.send(
                f"⚠️ {user.mention}님, DM이 차단되어 채널로 경고합니다. 보안 정책 위반 메시지가 삭제되었습니다. (`{action_taken}`)",
                delete_after=3.0
            )

        # 🚨 관리자 로그 채널 전송 시스템
        try:
            guild_settings = await self.bot.get_guild_settings(str(guild.id))
            antinuke_config = guild_settings.get("antinuke_settings", {})
            configured_log_id = antinuke_config.get("log_channel_id")

            log_channel = None
            if configured_log_id:
                log_channel = guild.get_channel(int(configured_log_id))
            if not log_channel:
                log_channel = discord.utils.get(guild.text_channels, name="automod-logs")
            if not log_channel:
                log_channel = guild.system_channel

            if log_channel:
                staff_embed = discord.Embed(
                    title="🚨 AutoMod Security Enforcement Log",
                    color=discord.Color.dark_red(),
                    timestamp=discord.utils.utcnow()
                )
                staff_embed.set_thumbnail(url=user.display_avatar.url)
                staff_embed.add_field(name="Offender Target", value=f"{user.mention}\n`Name: {user.name}`\n`ID: {user.id}`", inline=True)
                staff_embed.add_field(name="Channel Layer", value=message.channel.mention, inline=True)
                staff_embed.add_field(name="Infraction Trigger", value=f"```\n{reason}\n```", inline=False)
                staff_embed.add_field(name="Interception Result", value=f"` {action_taken} `", inline=True)
                
                content_preview = message.content[:500] + "..." if len(message.content) > 500 else message.content
                if content_preview:
                    staff_embed.add_field(name="Raw Content Truncation", value=f"```\n{content_preview}\n```", inline=False)
                
                await log_channel.send(embed=staff_embed)
        except Exception as log_err:
            print(f"[LOG TRANSMISSION ERROR] Failed sending staff log: {log_err}")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        if message.author.guild_permissions.manage_messages or message.author.guild_permissions.administrator:
            return

        guild_id_str = str(message.guild.id)
        guild_settings = await self.bot.get_guild_settings(guild_id_str)
        antinuke_config = guild_settings.get("antinuke_settings", {})

        # PATTERN 1: 멘션 도배 방지
        total_mentions = len(message.mentions) + len(message.role_mentions)
        if total_mentions > self.MAX_MENTIONS:
            await self._execute_punishment(message, f"Mass Mention Trigger ({total_mentions} mentions)")
            return

        # PATTERN 2: 서버 초대 링크 차단
        if self.invite_regex.search(message.content):
            await self._execute_punishment(message, "Unauthorized External Discord Invite Link Shared")
            return

        # PATTERN 3: 텍스트 도배 방지 (초고속 난사 방어선)
        spam_window = float(antinuke_config.get("anti_spam_speed", 3.0))
        current_time = datetime.datetime.now(datetime.timezone.utc).timestamp()
        user_id = message.author.id

        user_timestamps = self.spam_cache.get(user_id, [])
        user_timestamps = [t for t in user_timestamps if current_time - t < spam_window]
        user_timestamps.append(current_time)

        if not user_timestamps:
            self.spam_cache.pop(user_id, None)
        else:
            self.spam_cache[user_id] = user_timestamps

        if len(user_timestamps) >= self.DEFAULT_SPAM_MAX_MSGS:
            self.spam_cache.pop(user_id, None)
            
            await self._execute_punishment(
                message, 
                f"Rapid Rate Limit Breach ({len(user_timestamps)} messages within {spam_window}s)"
            )
            return

        # PATTERN 4: 금지어 필터링
        for pattern in self.banned_words:
            if re.search(pattern, message.content, re.IGNORECASE):
                await self._execute_punishment(message, "Toxicity Data Block or Restricted Phrase Matched")
                return

async def setup(bot):
    await bot.add_cog(AutoMod(bot))