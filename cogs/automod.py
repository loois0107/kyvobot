import time
import asyncio
import json
import os
from datetime import datetime, timezone
import discord
from discord.ext import commands, tasks
import redis.asyncio as aioredis

# ══════════════════════════════════════════════════════════
#  ① 슬라이딩 윈도우 Lua 스크립트 (Redis 서버에서 원자적으로 실행)
# ══════════════════════════════════════════════════════════
SLIDING_WINDOW_LUA = """
local key    = KEYS[1]
local now    = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit  = tonumber(ARGV[3])
local member = ARGV[4]

-- 1) 윈도우 밖으로 밀려난 오래된 기록 제거
redis.call('ZREMRANGEBYSCORE', key, 0, now - window)

-- 2) 현재 메시지 기록
redis.call('ZADD', key, now, member)

-- 3) 윈도우 내 메시지 수 집계
local count = redis.call('ZCARD', key)

-- 4) 키 자동 만료 (윈도우 + 1초 여유). 죽은 유저의 키가 영원히 남지 않게 한다.
redis.call('PEXPIRE', key, window + 1000)

if count > limit then
    return {count, 1}
end
return {count, 0}
"""

class AutoMod(commands.Cog):
    """
    AutoMod (Anti-Spam & Bad Word Filtering) Cog.
    - Read: Cache-Aside Pattern (Upstash Redis) with isolated thread-pool DB query & negative caching.
    - Write: In-memory Batch Queue for Supabase Bulk Insertion.
    """

    # ── Log Batch Queue Settings ───────────────────────
    LOG_FLUSH_INTERVAL = 5.0    # Queue flush interval (seconds)
    LOG_BATCH_MAX_SIZE = 200    # Max rows per bulk insert
    LOG_QUEUE_MAX_SIZE = 5000   # Max queue capacity to prevent memory overflow
    LOG_MAX_RETRY = 3           # Max retries on database insert failure
    # ─────────────────────────────────────────────────

    # ══════════════════════════════════════════════════════════
    #  ② 생성자 및 Lua 스크립트 등록
    # ══════════════════════════════════════════════════════════
    def __init__(self, bot):
        self.bot = bot
        self.supabase = getattr(bot, "supabase", None)
        
        # Initialize Redis connection (with robust environment variable fallback)
        if hasattr(bot, "redis"):
            self.redis = bot.redis
        else:
            redis_url = os.getenv("REDIS_URL")
            self.redis = aioredis.from_url(redis_url, decode_responses=True)

        # Redis 장애 시에만 사용하는 로컬 폴백 캐시 (평시엔 비어 있음)
        self.spam_cache = {}

        # Lua 스크립트를 Redis에 등록 (EVALSHA로 재사용되어 대역폭 절약)
        self.spam_script = self.redis.register_script(SLIDING_WINDOW_LUA)

        # Async queue to temporarily buffer infraction logs
        self.log_queue: asyncio.Queue = asyncio.Queue(maxsize=self.LOG_QUEUE_MAX_SIZE)
        self.log_dropped_count = 0      # Cumulative counter for dropped logs

        # Start the background batch flusher task
        self.flush_log_queue.start()
        print("[⚡ AUTOMOD] Initialization complete. Redis connected & Batch Queue active.", flush=True)

    async def cog_unload(self):
        """Flush remaining logs to DB gracefully upon cog unload or bot shutdown."""
        self.flush_log_queue.cancel()
        try:
            await asyncio.wait_for(self._drain_and_insert(final=True), timeout=10.0)
        except Exception as e:
            print(f"[LOG-QUEUE][ERROR] Graceful flush failed: {type(e).__name__}: {e}", flush=True)

    # ══════════════════════════════════════════════════════════
    #  Cache-Aside Guild Settings Layer (Read Optimizer)
    # ══════════════════════════════════════════════════════════

    async def get_guild_settings(self, guild_id: int) -> dict:
        """
        Cache-Aside Lookup.
        Fetches settings from Redis first. On cache miss, queries Supabase via an isolated 
        thread pool and re-caches the data. Implements negative caching for empty settings.
        """
        key = f"guild:{guild_id}:settings"

        # 1) Attempt Cache Lookup
        try:
            cached = await self.redis.get(key)
            if cached:
                return json.loads(cached)
        except Exception as e:
            print(f"[CACHE][WARN] Lookup failed, bypassing to DB: {type(e).__name__}: {e}", flush=True)

        # 2) Cache Miss -> Query Supabase inside an isolated Thread Pool
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
            print(f"[DB][ERROR] Failed to fetch guild settings (guild={guild_id}): "
                  f"{type(e).__name__}: {e}", flush=True)
            return {}  # Return empty dict if DB is down to keep the bot running gracefully

        # 3) Re-cache Data. Apply a shorter TTL (60s) for empty settings to mitigate cache stampede.
        ttl = 300 if settings else 60
        try:
            await self.redis.setex(key, ttl, json.dumps(settings, ensure_ascii=False))
        except Exception as e:
            print(f"[CACHE][WARN] Failed to re-cache settings: {type(e).__name__}: {e}", flush=True)

        return settings

    async def invalidate_settings_cache(self, guild_id: int) -> bool:
        """Forcefully purges the configuration cache for the given guild."""
        key = f"guild:{guild_id}:settings"
        try:
            deleted = await self.redis.delete(key)
            print(f"[CACHE] Invalidation success: {key} ({deleted} keys removed)", flush=True)
            return True
        except Exception as e:
            print(f"[CACHE][ERROR] Invalidation failed ({key}): {type(e).__name__}: {e}", flush=True)
            return False

    # ══════════════════════════════════════════════════════════
    #  ③ 도배 감지 핵심 함수 (ZSET 및 로컬 폴백)
    # ══════════════════════════════════════════════════════════
    async def check_spam(self, guild_id: int, user_id: int, message_id: int,
                         limit: int, window_sec: int) -> tuple[int, bool]:
        """
        Redis ZSET 슬라이딩 윈도우로 도배 여부를 판정한다.
        Redis 장애 시 로컬 딕셔너리로 자동 폴백하여 봇이 절대 멈추지 않는다.

        Returns:
            (윈도우 내 메시지 수, 제한 초과 여부)
        """
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
            print(f"[SPAM][WARN] Redis 판정 실패, 로컬 폴백: "
                  f"{type(e).__name__}: {e}", flush=True)
            return self._check_spam_local(guild_id, user_id, limit, window_sec)

    def _check_spam_local(self, guild_id: int, user_id: int,
                          limit: int, window_sec: int) -> tuple[int, bool]:
        """[폴백] Redis가 죽었을 때만 동작하는 인메모리 슬라이딩 윈도우."""
        key = (guild_id, user_id)
        now = time.time()
        cutoff = now - window_sec

        timestamps = [t for t in self.spam_cache.get(key, []) if t > cutoff]
        timestamps.append(now)
        self.spam_cache[key] = timestamps

        count = len(timestamps)
        return count, count > limit

    # ══════════════════════════════════════════════════════════
    #  ④ Message Event Listener (도배 검사 후 금지어 검사 실행)
    # ══════════════════════════════════════════════════════════
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # 관리자 및 봇 프리패스 (기존 로직 유지)
        if message.author.bot or not message.guild:
            return
        
        perms = message.author.guild_permissions
        if perms.administrator or perms.manage_messages:
            return

        # ⚡ [Upstash 비용 구세주] Redis 호출 전 조기 탈출로 커맨드 절약
        if len(message.content) < 2 and not message.attachments:
            return

        settings = await self.get_guild_settings(message.guild.id)
        if not settings or not settings.get("automod_enabled", True):
            return

        limit = settings.get("spam_limit", 5)        # 윈도우당 허용 메시지 수
        window = settings.get("spam_interval", 10)   # 윈도우 크기 (초)

        # 1. 도배 감지 실행 (Lua Script 호출 및 Redis 캐싱)
        count, exceeded = await self.check_spam(
            guild_id=message.guild.id,
            user_id=message.author.id,
            message_id=message.id,
            limit=limit,
            window_sec=window,
        )

        if exceeded:
            # ── 처벌 실행 ──────────────────────────────────
            try:
                await message.delete()
            except discord.NotFound:
                pass  # 이미 삭제됨
            except discord.Forbidden:
                print(f"[SPAM][WARN] 메시지 삭제 권한 없음 (guild={message.guild.id})", flush=True)

            # 처벌 로그 배치 큐 전송 (데이터베이스 무결성을 위해 channel_id 제거)
            self.enqueue_log(
                guild_id=message.guild.id,
                user_id=message.author.id,
                action="spam_delete",
                reason=f"도배 감지 ({count}/{limit} in {window}s) | 채널: #{message.channel.name}"
            )

            try:
                await message.channel.send(
                    f"{message.author.mention}, spam detected. Your message has been deleted.",
                    delete_after=3.0
                )
            except discord.Forbidden:
                pass
            return

        # 2. 금지어 필터링 실행 (도배에 안 걸렸을 때만 순차 진행)
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

                self.enqueue_log(
                    guild_id=message.guild.id,
                    user_id=message.author.id,
                    action="bad_word_delete",
                    reason=f"Forbidden word detected: {word}"
                )

                try:
                    await message.channel.send(
                        f"{message.author.mention}, forbidden word detected. Your message has been deleted.",
                        delete_after=3.0
                    )
                except discord.Forbidden:
                    pass
                break

    # ══════════════════════════════════════════════════════════
    #  Log Batch Queue Async Engine (Write Optimizer)
    # ══════════════════════════════════════════════════════════

    def enqueue_log(self, guild_id: int, user_id: int, action: str, reason: str) -> None:
        """Pushes infraction logs into the in-memory queue instead of writing directly to the DB."""
        payload = {
            "guild_id": str(guild_id),
            "user_id": str(user_id),
            "action_type": action,   # Mapped to Supabase column 'action_type'
            "reason": reason,
            "moderator_id": str(self.bot.user.id) if self.bot.user else None, # Automate system bot ID
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self.log_queue.put_nowait(payload)
        except asyncio.QueueFull:
            self.log_dropped_count += 1
            print(
                f"[LOG-QUEUE][WARN] Queue saturated (maxsize={self.LOG_QUEUE_MAX_SIZE}) "
                f"-> 1 log discarded (Cumulative dropped: {self.log_dropped_count})",
                flush=True,
            )

    @tasks.loop(seconds=LOG_FLUSH_INTERVAL)
    async def flush_log_queue(self):
        """Background worker loop that drains the queue and pushes bulk entries every 5 seconds."""
        try:
            await self._drain_and_insert()
        except Exception as e:
            print(f"[LOG-QUEUE][ERROR] Flush loop exception: {type(e).__name__}: {e}", flush=True)

    @flush_log_queue.before_loop
    async def before_flush_log_queue(self):
        """Wait until the Discord gateway client connection is stable before running."""
        await self.bot.wait_until_ready()
        print("[LOG-QUEUE] Background batch flusher activated (Interval: 5s)", flush=True)

    async def _drain_and_insert(self, final: bool = False) -> None:
        """Drains up to LOG_BATCH_MAX_SIZE entries from the queue for bulk insertion."""
        if self.log_queue.empty():
            return

        batch = []
        limit = self.log_queue.qsize() if final else self.LOG_BATCH_MAX_SIZE
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
        """Executes synchronous Supabase SDK insertion isolated inside a thread pool."""
        rows = [{k: v for k, v in row.items() if not k.startswith("_")} for row in batch]
        
        if not self.supabase:
            self.supabase = getattr(self.bot, "supabase", None)
            if not self.supabase:
                print("[LOG-QUEUE][ERROR] Supabase client instance not found.", flush=True)
                return False

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._sync_bulk_insert, rows)
            print(f"[LOG-QUEUE] Supabase bulk insert succeeded: {len(rows)} rows "
                  f"(Remaining queue: {self.log_queue.qsize()})", flush=True)
            return True
        except Exception as e:
            print(f"[LOG-QUEUE][ERROR] Bulk insert failed for {len(rows)} rows: "
                  f"{type(e).__name__}: {e}", flush=True)
            return False

    def _sync_bulk_insert(self, rows: list):
        """Synchronous target execution for the database thread pool."""
        return self.supabase.table("automod_logs").insert(rows).execute()

    async def _requeue_failed(self, batch: list) -> None:
        """Re-enqueues failed rows with an incremented retry count; moves to DLQ if max retries exceeded."""
        for row in batch:
            row["_retry"] = row.get("_retry", 0) + 1

            if row["_retry"] > self.LOG_MAX_RETRY:
                await self._push_to_dead_letter(row)
                continue

            try:
                self.log_queue.put_nowait(row)
            except asyncio.QueueFull:
                await self._push_to_dead_letter(row)

    async def _push_to_dead_letter(self, row: dict) -> None:
        """Evacuates unprocessable logs into the Upstash Redis list (DLQ) for disaster recovery."""
        try:
            await self.redis.rpush("kyvo:log:dlq", json.dumps(row, ensure_ascii=False))
            await self.redis.ltrim("kyvo:log:dlq", -10000, -1)  # Bound DLQ growth
            print(f"[LOG-QUEUE][DLQ] Retry limit exceeded -> Evacuated to Redis DLQ "
                  f"(Guild: {row.get('guild_id')})", flush=True)
        except Exception as e:
            self.log_dropped_count += 1
            print(f"[LOG-QUEUE][FATAL] DLQ backup fallback failed. Log lost: "
                  f"{type(e).__name__}: {e} | payload={row}", flush=True)

    # Legacy compatibility wrapper
    async def _log_to_supabase(self, *args, **kwargs) -> None:
        """[Deprecated] Redirects legacy immediate writes to the async batch queue."""
        self.enqueue_log(*args, **kwargs)

    # ══════════════════════════════════════════════════════════
    #  Management & Monitoring Commands (Admin Only)
    # ══════════════════════════════════════════════════════════

    @commands.command(name="reload_settings", aliases=["refresh_settings", "설정새로고침"])
    @commands.has_permissions(administrator=True)
    async def reload_settings(self, ctx):
        """[Admin] Forcefully purge the settings cache for this server."""
        ok = await self.invalidate_settings_cache(ctx.guild.id)
        if ok:
            await ctx.send("✅ **Settings cache purged.** The latest configuration will be loaded on the next message.")
        else:
            await ctx.send("⚠️ **Failed to connect to Redis.** Changes will apply automatically within 5 minutes via natural TTL.")

    @commands.command(name="logqueue")
    @commands.has_permissions(administrator=True)
    async def logqueue_status(self, ctx):
        """Checks the live metrics and memory overhead of the log batch queue."""
        try:
            dlq_size = await self.redis.llen("kyvo:log:dlq")
        except Exception:
            dlq_size = "N/A (Redis Error)"

        embed = discord.Embed(title="📊 Log Batch Queue Metrics", color=0x5865F2)
        embed.add_field(name="Queued Logs", value=f"{self.log_queue.qsize()} items", inline=True)
        embed.add_field(name="Dropped Logs", value=f"{self.log_dropped_count} items", inline=True)
        embed.add_field(name="Redis DLQ Size", value=f"{dlq_size} items", inline=True)
        embed.add_field(name="Flush Interval", value=f"{self.LOG_FLUSH_INTERVAL}s", inline=False)
        await ctx.send(embed=embed)

    # ══════════════════════════════════════════════════════════
    #  ⑤ 초기화 명령어 (관리자 수동 오탐 구제용)
    # ══════════════════════════════════════════════════════════
    @commands.command(name="clearspam")
    @commands.has_permissions(manage_messages=True)
    async def clear_spam(self, ctx, member: discord.Member):
        """[관리자] 특정 유저의 도배 카운터를 즉시 초기화한다 (오탐 구제용)."""
        key = f"spam:{ctx.guild.id}:{member.id}"
        try:
            await self.redis.delete(key)
            self.spam_cache.pop((ctx.guild.id, member.id), None)
            await ctx.send(f"✅ {member.mention} 의 도배 카운터를 초기화했습니다.")
        except Exception as e:
            print(f"[SPAM][ERROR] 카운터 초기화 실패: {type(e).__name__}: {e}", flush=True)
            await ctx.send("⚠️ Redis 연결 실패. 윈도우 만료 후 자동 초기화됩니다.")

async def setup(bot):
    await bot.add_cog(AutoMod(bot))
    print("[⚡ AUTOMOD] Cog extension setup complete.", flush=True)