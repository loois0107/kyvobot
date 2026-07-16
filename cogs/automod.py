import time
import asyncio
import json
import os
from datetime import datetime, timezone, timedelta
import discord
from discord.ext import commands, tasks
import redis.asyncio as aioredis

# ══════════════════════════════════════════════════════════
#  ① 다국어 언어 사전 (LOCALES) - 한국어 및 영어 지원
# ══════════════════════════════════════════════════════════
LOCALES = {
    "en": {
        "spam_warn": "⚠️ {mention}, spam detected. Your messages have been deleted and you have been **timed out for 10 minutes**.",
        "spam_reason": "Spam detected ({count}/{limit} in {window}s)",
        "spam_timeout_applied": " -> 10m Timeout applied",
        "spam_failed_permission": " (Punishment failed due to missing permission)",
        "bad_word_warn": "{mention}, forbidden word detected. Your message has been deleted.",
        "bad_word_reason": "Forbidden word detected: {word}",
        "clear_spam_success": "✅ Successfully cleared spam counter for {mention}.",
        "clear_spam_fail": "⚠️ Failed to connect to Redis. Counter will auto-reset after window expiration.",
    },
    "ko": {
        "spam_warn": "⚠️ {mention}님, 도배가 감지되어 메시지가 삭제되고 **10분간 입막음(타임아웃)** 처리되었습니다.",
        "spam_reason": "도배 감지 ({count}/{limit}명 중 {window}초 내)",
        "spam_timeout_applied": " -> 10분 타임아웃 처분 완료",
        "spam_failed_permission": " (권한 부족으로 처벌 실패)",
        "bad_word_warn": "{mention}님, 금지어가 감지되어 메시지가 삭제되었습니다.",
        "bad_word_reason": "금지어 감지: {word}",
        "clear_spam_success": "✅ {mention}님의 도배 카운터를 즉시 초기화했습니다.",
        "clear_spam_fail": "⚠️ Redis 연결 실패. 윈도우 만료 후 자동 초기화됩니다.",
    }
}

# ══════════════════════════════════════════════════════════
#  ② 슬라이딩 윈도우 Lua 스크립트
# ══════════════════════════════════════════════════════════
SLIDING_WINDOW_LUA = """
local key    = KEYS[1]
local now    = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit  = tonumber(ARGV[3])
local member = ARGV[4]

redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
redis.call('ZADD', key, now, member)
local count = redis.call('ZCARD', key)
redis.call('PEXPIRE', key, window + 1000)

if count > limit then
    return {count, 1}
end
return {count, 0}
"""

class AutoMod(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.supabase = getattr(bot, "supabase", None)
        
        if hasattr(bot, "redis"):
            self.redis = bot.redis
        else:
            redis_url = os.getenv("REDIS_URL")
            self.redis = aioredis.from_url(redis_url, decode_responses=True)

        self.spam_cache = {}
        self.spam_script = self.redis.register_script(SLIDING_WINDOW_LUA)
        self.log_queue: asyncio.Queue = asyncio.Queue(maxsize=5000)
        self.log_dropped_count = 0

        self.flush_log_queue.start()
        print("[⚡ AUTOMOD] Initialization complete. Multi-language support active.", flush=True)

    async def cog_unload(self):
        self.flush_log_queue.cancel()
        try:
            await asyncio.wait_for(self._drain_and_insert(final=True), timeout=10.0)
        except Exception as e:
            print(f"[LOG-QUEUE][ERROR] Graceful flush failed: {type(e).__name__}: {e}", flush=True)

    # ══════════════════════════════════════════════════════════
    #  ③ 다국어 텍스트 추출 헬퍼 함수 (i18n 핵심)
    # ══════════════════════════════════════════════════════════
    async def get_msg(self, guild_id: int, key: string, **kwargs) -> str:
        """서버 설정 언어(ko/en)에 맞는 메시지를 찾아 포맷팅해 반환합니다."""
        settings = await self.get_guild_settings(guild_id)
        # DB에 language 설정이 없으면 기본적으로 'ko'를 사용합니다 (국내 중심)
        lang = settings.get("language", "ko")
        
        # 만약 없는 언어나 키를 요청할 경우 영어를 폴백으로 작동
        lang_dict = LOCALES.get(lang, LOCALES["en"])
        template = lang_dict.get(key, LOCALES["en"].get(key, f"Missing [{key}]"))
        
        return template.format(**kwargs)

    # ══════════════════════════════════════════════════════════
    #  Cache-Aside Guild Settings Layer
    # ══════════════════════════════════════════════════════════
    async def get_guild_settings(self, guild_id: int) -> dict:
        key = f"guild:{guild_id}:settings"
        try:
            cached = await self.redis.get(key)
            if cached:
                return json.loads(cached)
        except Exception as e:
            print(f"[CACHE][WARN] Lookup failed: {type(e).__name__}: {e}", flush=True)

        loop = asyncio.get_running_loop()
        try:
            res = await loop.run_in_executor(
                None,
                lambda: self.supabase.table("guild_settings")
                        .select("*").eq("guild_id", str(guild_id))
                        .maybe_single().execute(),
            )
            settings = res.data or {}
        except Exception as e:
            print(f"[DB][ERROR] Failed to fetch guild settings (guild={guild_id}): {type(e).__name__}: {e}", flush=True)
            return {}

        ttl = 300 if settings else 60
        try:
            await self.redis.setex(key, ttl, json.dumps(settings, ensure_ascii=False))
        except Exception as e:
            print(f"[CACHE][WARN] Failed to re-cache settings: {type(e).__name__}: {e}", flush=True)

        return settings

    async def invalidate_settings_cache(self, guild_id: int) -> bool:
        key = f"guild:{guild_id}:settings"
        try:
            await self.redis.delete(key)
            return True
        except Exception as e:
            print(f"[CACHE][ERROR] Invalidation failed: {type(e).__name__}: {e}", flush=True)
            return False

    # ══════════════════════════════════════════════════════════
    #  Spam Detection Core
    # ══════════════════════════════════════════════════════════
    async def check_spam(self, guild_id: int, user_id: int, message_id: int,
                         limit: int, window_sec: int) -> tuple[int, bool]:
        key = f"spam:{guild_id}:{user_id}"
        now_ms = int(time.time() * 1000)
        window_ms = window_sec * 1000

        try:
            count, exceeded = await self.spam_script(
                keys=[key],
                args=[now_ms, window_ms, limit, str(message_id)],
            )
            return int(count), bool(exceeded)
        except Exception as e:
            print(f"[SPAM][WARN] Redis evaluation failed, falling back to local: {type(e).__name__}: {e}", flush=True)
            return self._check_spam_local(guild_id, user_id, limit, window_sec)

    def _check_spam_local(self, guild_id: int, user_id: int,
                          limit: int, window_sec: int) -> tuple[int, bool]:
        key = (guild_id, user_id)
        now = time.time()
        cutoff = now - window_sec

        timestamps = [t for t in self.spam_cache.get(key, []) if t > cutoff]
        timestamps.append(now)
        self.spam_cache[key] = timestamps

        count = len(timestamps)
        return count, count > limit

    # ══════════════════════════════════════════════════════════
    #  ④ Message Event Listener (i18n 다국어 출력 적용)
    # ══════════════════════════════════════════════════════════
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        perms = message.author.guild_permissions
        if perms.administrator or perms.manage_messages:
            return

        if not message.content and not message.attachments:
            return

        settings = await self.get_guild_settings(message.guild.id)
        if not settings or not settings.get("automod_enabled", True):
            return

        limit = settings.get("spam_limit", 5)
        window = settings.get("spam_interval", 10)

        count, exceeded = await self.check_spam(
            guild_id=message.guild.id,
            user_id=message.author.id,
            message_id=message.id,
            limit=limit,
            window_sec=window,
        )

        if exceeded:
            try:
                await message.delete()
            except discord.NotFound:
                pass  
            except discord.Forbidden:
                print(f"[SPAM][WARN] Missing permission to delete message (guild={message.guild.id})", flush=True)

            # i18n 기반 처벌 사유 문자열 조립
            base_reason = await self.get_msg(message.guild.id, "spam_reason", count=count, limit=limit, window=window)
            punishment_log = "spam_delete"

            try:
                duration = timedelta(minutes=10)
                await message.author.timeout(duration, reason=base_reason)
                punishment_log = "spam_timeout"
                suffix = await self.get_msg(message.guild.id, "spam_timeout_applied")
                base_reason += suffix
            except discord.Forbidden:
                suffix = await self.get_msg(message.guild.id, "spam_failed_permission")
                base_reason += suffix
            except Exception as e:
                print(f"[SPAM][ERROR] Failed to apply punishment: {type(e).__name__}: {e}", flush=True)

            self.enqueue_log(
                guild_id=message.guild.id,
                user_id=message.author.id,
                action=punishment_log,
                reason=f"{base_reason} | Channel: #{message.channel.name}",
            )

            # 유저 대상 경고문 전송 (i18n 적용)
            warn_msg = await self.get_msg(message.guild.id, "spam_warn", mention=message.author.mention)
            try:
                await message.channel.send(warn_msg, delete_after=5.0)
            except (discord.Forbidden, discord.HTTPException):
                pass
            return

        # 2. 금지어 필터링
        forbidden_words = settings.get("forbidden_words", [])
        if isinstance(forbidden_words, str):
            forbidden_words = [w.strip() for w in forbidden_words.split(",") if w.strip()]

        for word in forbidden_words:
            if word in message.content:
                try:
                    await message.delete()
                except discord.NotFound:
                    pass
                except discord.Forbidden:
                    pass

                log_reason = await self.get_msg(message.guild.id, "bad_word_reason", word=word)
                self.enqueue_log(
                    guild_id=message.guild.id,
                    user_id=message.author.id,
                    action="bad_word_delete",
                    reason=f"{log_reason} | Channel: #{message.channel.name}"
                )

                warn_msg = await self.get_msg(message.guild.id, "bad_word_warn", mention=message.author.mention)
                try:
                    await message.channel.send(warn_msg, delete_after=3.0)
                except (discord.Forbidden, discord.HTTPException):
                    pass
                break

    # ══════════════════════════════════════════════════════════
    #  Log Batch Queue Async Engine
    # ══════════════════════════════════════════════════════════
    def enqueue_log(self, guild_id: int, user_id: int, action: str, reason: str) -> None:
        payload = {
            "guild_id": str(guild_id),
            "user_id": str(user_id),
            "action_type": action,
            "reason": reason,
            "moderator_id": str(self.bot.user.id) if self.bot.user else None,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self.log_queue.put_nowait(payload)
        except asyncio.QueueFull:
            self.log_dropped_count += 1

    @tasks.loop(seconds=5.0)
    async def flush_log_queue(self):
        try:
            await self._drain_and_insert()
        except Exception as e:
            print(f"[LOG-QUEUE][ERROR] Flush loop exception: {type(e).__name__}: {e}", flush=True)

    @flush_log_queue.before_loop
    async def before_flush_log_queue(self):
        await self.bot.wait_until_ready()

    async def _drain_and_insert(self, final: bool = False) -> None:
        if self.log_queue.empty():
            return

        batch = []
        limit = self.log_queue.qsize() if final else 200
        while len(batch) < limit:
            try:
                batch.append(self.log_queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        if not batch:
            return

        ok = await self._bulk_insert_supabase(batch)
        if not ok:
            await self._requeue_failed(batch)

    async def _bulk_insert_supabase(self, batch: list) -> bool:
        rows = [{k: v for k, v in row.items() if not k.startswith("_")} for row in batch]
        if not self.supabase:
            self.supabase = getattr(self.bot, "supabase", None)
            if not self.supabase:
                return False

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._sync_bulk_insert, rows)
            return True
        except Exception as e:
            print(f"[LOG-QUEUE][ERROR] Bulk insert failed: {type(e).__name__}: {e}", flush=True)
            return False

    def _sync_bulk_insert(self, rows: list):
        return self.supabase.table("automod_logs").insert(rows).execute()

    async def _requeue_failed(self, batch: list) -> None:
        for row in batch:
            row["_retry"] = row.get("_retry", 0) + 1
            if row["_retry"] > 3:
                await self._push_to_dead_letter(row)
                continue
            try:
                self.log_queue.put_nowait(row)
            except asyncio.QueueFull:
                await self._push_to_dead_letter(row)

    async def _push_to_dead_letter(self, row: dict) -> None:
        try:
            await self.redis.rpush("kyvo:log:dlq", json.dumps(row, ensure_ascii=False))
            await self.redis.ltrim("kyvo:log:dlq", -10000, -1)
        except Exception as e:
            self.log_dropped_count += 1

    async def _log_to_supabase(self, *args, **kwargs) -> None:
        self.enqueue_log(*args, **kwargs)

    # ══════════════════════════════════════════════════════════
    #  ⑤ Admin Commands
    # ══════════════════════════════════════════════════════════
    @commands.command(name="reload_settings", aliases=["refresh_settings", "설정새로고침"])
    @commands.has_permissions(administrator=True)
    async def reload_settings(self, ctx):
        ok = await self.invalidate_settings_cache(ctx.guild.id)
        if ok:
            await ctx.send("✅ **Settings cache purged.**")
        else:
            await ctx.send("⚠️ **Failed to connect to Redis.**")

    @commands.command(name="logqueue")
    @commands.has_permissions(administrator=True)
    async def logqueue_status(self, ctx):
        try:
            dlq_size = await self.redis.llen("kyvo:log:dlq")
        except Exception:
            dlq_size = "N/A"

        embed = discord.Embed(title="📊 Log Batch Queue Metrics", color=0x5865F2)
        embed.add_field(name="Queued Logs", value=f"{self.log_queue.qsize()} items", inline=True)
        embed.add_field(name="Dropped Logs", value=f"{self.log_dropped_count} items", inline=True)
        embed.add_field(name="Redis DLQ Size", value=f"{dlq_size} items", inline=True)
        await ctx.send(embed=embed)

    @commands.command(name="clearspam")
    @commands.has_permissions(manage_messages=True)
    async def clear_spam(self, ctx, member: discord.Member):
        key = f"spam:{ctx.guild.id}:{member.id}"
        try:
            await self.redis.delete(key)
            self.spam_cache.pop((ctx.guild.id, member.id), None)
            success_msg = await self.get_msg(ctx.guild.id, "clear_spam_success", mention=member.mention)
            await ctx.send(success_msg)
        except Exception as e:
            print(f"[SPAM][ERROR] Failed to clear spam: {type(e).__name__}: {e}", flush=True)
            fail_msg = await self.get_msg(ctx.guild.id, "clear_spam_fail")
            await ctx.send(fail_msg)

async def setup(bot):
    await bot.add_cog(AutoMod(bot))
    print("[⚡ AUTOMOD] Cog extension setup complete.", flush=True)