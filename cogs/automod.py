import time
import asyncio
import json
import os
from datetime import datetime, timezone, timedelta
import discord
from discord.ext import commands, tasks
import redis.asyncio as aioredis

# ⚡ [아키텍처 혁신] 모든 Cog의 기저가 되는 공통 마스터 베이스 콕 임포트
from cogs.base import KyvoBaseCog

# ══════════════════════════════════════════════════════════
#  ① 슬라이딩 윈도우 Lua 스크립트 (AutoMod 고유 자산 유지)
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

class AutoMod(KyvoBaseCog):
    """
    KyvoBaseCog를 상속받아 무거운 캐시 및 다국어 헬퍼(get_msg)를 상속받고,
    자체적인 도배 방지 및 금지어 필터링 기능만 격리 수행하는 슬림화된 AutoMod 엔진.
    """
    def __init__(self, bot):
        # ⚡ [중요] 부모 클래스(KyvoBaseCog)의 __init__을 호출하여 bot, supabase, redis 자동 세팅 완료!
        super().__init__(bot)

        # AutoMod 모듈 전용 고유 비즈니스 상태 레이어 초기화
        self.spam_cache = {}
        self.spam_script = self.redis.register_script(SLIDING_WINDOW_LUA)
        self.log_queue: asyncio.Queue = asyncio.Queue(maxsize=5000)
        self.log_dropped_count = 0

        self.flush_log_queue.start()
        print("[⚡ AUTOMOD] Initialization complete. Multi-language support active via Base Cog.", flush=True)

    async def cog_unload(self):
        self.flush_log_queue.cancel()
        try:
            await asyncio.wait_for(self._drain_and_insert(final=True), timeout=10.0)
        except Exception as e:
            print(f"[LOG-QUEUE][ERROR] Graceful flush failed: {type(e).__name__}: {e}", flush=True)

    # 💡 [중복 소각 완료] 기존의 get_msg, get_guild_settings, invalidate_settings_cache 
    #    및 LOCALES 딕셔너리는 이제 부모 클래스(KyvoBaseCog)가 완벽하게 처리하므로 통째로 삭제됨!

    # ══════════════════════════════════════════════════════════
    #  ② Spam Detection Core
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
    #  ③ Message Event Listener (부모 Cog의 상속 함수 완벽 연동)
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

        # ⚡ 부모 클래스로부터 상속받은 캐시-aside 설정 로더 가동
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

            # ⚡ 부모 클래스의 통합 다국어 로더(get_msg)를 호출하여 실시간 문자열 치환
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

            # ⚡ 부모 클래스의 get_msg 기반으로 유저 경고 메시지 출력
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

                # ⚡ 금지어 사유 및 알림도 부모 클래스의 상속 get_msg로 일원화
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
    #  ④ Log Batch Queue Async Engine (AutoMod 고유 자산 유지)
    # ══════════════════════════════════════════════════════════
    def enqueue_log(self, guild_id: int, user_id: int, action: str, reason: str) -> None:
        # 🛡️ [스키마 정합성 수정] 실제 automod_logs 테이블 컬럼은 "action"(NOT NULL)이지 "action_type"이
        # 아니고, "moderator_id" 컬럼은 아예 존재하지 않는다 - 둘 다 insert마다 실패를 유발해서
        # 7/16~7/21 사이 처벌 로그 23건이 DB에 한 번도 안 들어가고 DLQ에만 쌓였다.
        payload = {
            "guild_id": str(guild_id),
            "user_id": str(user_id),
            "action": action,
            "reason": reason,
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
    #  ⑤ Admin Commands (상속 함수 연동형 리팩터링 완료)
    # ══════════════════════════════════════════════════════════
    @commands.command(name="reload_settings", aliases=["refresh_settings", "설정새로고침"])
    @commands.has_permissions(administrator=True)
    async def reload_settings(self, ctx):
        # ⚡ 부모 클래스에서 상속받은 캐시 폭파(invalidate) 메서드 호출
        ok = await self.invalidate_settings_cache(ctx.guild.id)
        if ok:
            await ctx.send("✅ **Settings cache purged via Base Engine.**")
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