import discord
from discord.ext import commands
import re
import datetime
from collections import defaultdict
import os
import json
import redis.asyncio as aioredis

class AutoMod(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.spam_cache = {}
        self.invite_regex = re.compile(r"(discord\.gg|discord\.com/invite)/[a-zA-Z0-9]+")
        self.DEFAULT_SPAM_MAX_MSGS = 4
        self.MAX_MENTIONS = 5
        self.default_banned_words = [r"\bnigger\b", r"\bretard\b", r"scam\.link", r"free\-nitro"]

        # 🚀 실시간 진단 로그
        print("[⚡ AUTOMOD] 로딩 시작: Redis 연결을 시도합니다.", flush=True)
        redis_url = os.getenv("REDIS_URL")
        print(f"[⚡ AUTOMOD] 읽어온 REDIS_URL 존재 여부: {bool(redis_url)}", flush=True)
        
        try:
            self.redis = aioredis.from_url(redis_url, decode_responses=True)
            print("[⚡ AUTOMOD] Redis 클라이언트 객체 생성 완료!", flush=True)
        except Exception as init_err:
            print(f"[❌ AUTOMOD INIT ERROR] Redis 초기화 실패: {init_err}", flush=True)

    async def _get_cached_guild_settings(self, guild_id: str) -> dict:
        cache_key = f"guild:{guild_id}:settings"
        print(f"[🔎 AUTOMOD] Redis 캐시 조회 시작 -> Key: {cache_key}", flush=True)
        
        try:
            cached_data = await self.redis.get(cache_key)
            if cached_data:
                print("[🎯 AUTOMOD] Redis 캐시 적중! Supabase 패스합니다.", flush=True)
                return json.loads(cached_data)
            print("[📭 AUTOMOD] Redis 캐시 비어있음. Supabase로 이동합니다.", flush=True)
        except Exception as cache_err:
            print(f"[❌ REDIS CACHE ERROR] 읽기 실패 (Supabase로 우회): {cache_err}", flush=True)

        guild_settings = await self.bot.get_guild_settings(guild_id)

        try:
            await self.redis.setex(cache_key, 300, json.dumps(guild_settings))
            print("[💾 AUTOMOD] Supabase 데이터를 Redis 캐시에 포스트잇 완료!", flush=True)
        except Exception as cache_err:
            print(f"[❌ REDIS CACHE ERROR] 쓰기 실패: {cache_err}", flush=True)

        return guild_settings

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
            print(f"[AUTOMOD EXCEPTION] Failed logging infraction data: {e}", flush=True)

    async def _execute_punishment(self, message: discord.Message, reason: str):
        guild = message.guild
        user = message.author
        
        timeout_until = getattr(user, 'communication_disabled_until', None) or getattr(user, 'timed_out_until', None)
        if timeout_until and timeout_until > discord.utils.utcnow():
            return
        
        try:
            await message.delete()
        except discord.NotFound:
            return
        except discord.Forbidden:
            print(f"[AUTOMOD WARNING] Missing permission to delete message in guild {guild.id}", flush=True)
            return

        duration = datetime.timedelta(minutes=10)
        try:
            await user.timeout(duration, reason=f"[KyvoBot AutoMod] {reason}")
            action_taken = "TIMEOUT_10M"
        except discord.Forbidden:
            action_taken = "DELETE_ONLY"
            print(f"[AUTOMOD WARNING] Insufficient hierarchy to timeout user {user.id} in guild {guild.id}", flush=True)

        await self._log_to_supabase(guild.id, user.id, action_taken, reason)

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
            await message.channel.send(
                f"⚠️ {user.mention}님, DM이 차단되어 채널로 경고합니다. 보안 정책 위반 메시지가 삭제되었습니다. (`{action_taken}`)",
                delete_after=3.0
            )

        try:
            guild_settings = await self._get_cached_guild_settings(str(guild.id))
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
            print(f"[LOG TRANSMISSION ERROR] Failed sending staff log: {log_err}", flush=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        if message.author.guild_permissions.manage_messages or message.author.guild_permissions.administrator:
            return

        # ⚡ 부계정이 말 거는 순간 무조건 실행 확인 로그 찍기
        print(f"[📥 MESSAGE RECEIVED] 유저: {message.author.name}, 내용: {message.content}", flush=True)

        guild_id_str = str(message.guild.id)
        guild_settings = await self._get_cached_guild_settings(guild_id_str)
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

        # PATTERN 3: 텍스트 도배 방지
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
        db_banned_words = guild_settings.get("banned_words", [])
        all_banned_patterns = self.default_banned_words + [re.escape(word) for word in db_banned_words if word]

        for pattern in all_banned_patterns:
            if re.search(pattern, message.content, re.IGNORECASE):
                await self._execute_punishment(message, "Toxicity Data Block or Restricted Phrase Matched")
                return

async def setup(bot):
    print("[⚡ AUTOMOD] setup 함수 실행 완료!", flush=True)
    await bot.add_cog(AutoMod(bot))