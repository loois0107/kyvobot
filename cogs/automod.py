import time
import asyncio
import json
import os
from datetime import datetime, timezone, timedelta
import discord
from discord.ext import commands, tasks
import redis.asyncio as aioredis

# ══════════════════════════════════════════════════════════
#  ① Sliding Window Lua Script (Executed atomically on Redis)
# ══════════════════════════════════════════════════════════
SLIDING_WINDOW_LUA = """
local key    = KEYS[1]
local now    = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit  = tonumber(ARGV[3])
local member = ARGV[4]

-- 1) Remove old logs that fell out of the window
redis.call('ZREMRANGEBYSCORE', key, 0, now - window)

-- 2) Record the current message timestamp
redis.call('ZADD', key, now, member)

-- 3) Count remaining active logs in the window
local count = redis.call('ZCARD', key)

-- 4) Set auto-expiration on the key (window + 1s buffer) to prevent memory leak
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
    #  ② Constructor & Lua Script Registration
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

        # Local fallback cache used only when Redis is unavailable
        self.spam_cache = {}

        # Register Lua script to Redis for bandwidth optimization (reused via EVALSHA)
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
            return {}

        # 3) Re-cache Data
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
    #  ③ Spam Detection Core (ZSET with Local Fallback)
    # ══════════════════════════════════════════════════════════
    async def check_spam(self, guild_id: int, user_id: int, message_id: int,
                         limit: int, window_sec: int) -> tuple[int, bool]:
        """
        Evaluates spam state using Redis ZSET sliding window atomically.
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
            print(f"[SPAM][WARN] Redis evaluation failed, falling back to local: "
                  f"{type(e).__name__}: {e}", flush=True)
            return self._check_spam_local(guild_id, user_id, limit, window_sec)

    def _check_spam_local(self, guild_id: int, user_id: int,
                          limit: int, window_sec: int) -> tuple[int, bool]:
        """[Fallback] Memory-based sliding window running only when Redis is down."""
        key = (guild_id, user_id)
        now = time.time()
        cutoff = now - window_sec

        timestamps = [t for t in self.spam_cache.get(key, []) if t > cutoff]
        timestamps.append(now)
        self.spam_cache[key] = timestamps

        count = len(timestamps)
        return count, count > limit

    # ══════════════════════════════════════════════════════════
    #  ④ Message Event Listener (Clean English Debug Logs)
    # ══════════════════════════════════════════════════════════
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # 🔍 Debug Gate 0: Filter out bots and DM channels to prevent log pollution
        if message.author.bot or not message.guild:
            return

        print(f"[SPAM][DEBUG] on_message entered | Author: {message.author} | Content: '{message.content}'", flush=True)
        
        perms = message.author.guild_permissions
        if perms.administrator or perms.manage_messages:
            # 🔍 Debug Gate 1: Admin Bypass
            print(f"[SPAM][DEBUG] Bypassed admin/moderator: {message.author}", flush=True)
            return

        # ⚡ Skip evaluation only if message content is completely empty and lacks attachments
        if not message.content and not message.attachments:
            print(f"[SPAM][DEBUG] Bypassed empty message with no attachments", flush=True)
            return

        settings = await self.get_guild_settings(message.guild.id)
        # 🔍 Debug Gate 2: Configuration Lookup
        print(f"[SPAM][DEBUG] Settings loaded: {settings}", flush=True)

        if not settings or not settings.get("automod_enabled", True):
            print(f"[SPAM][DEBUG] Bypassed because automod is disabled", flush=True)
            return

        limit = settings.get("spam_limit", 5)        # Max allowed messages per window
        window = settings.get("spam_interval", 10)   # Sliding window size (seconds)

        # 1. Execute Spam Detection
        count, exceeded = await self.check_spam(
            guild_id=message.guild.id,
            user_id=message.author.id,
            message_id=message.id,
            limit=limit,
            window_sec=window,
        )
        # 🔍 Debug Gate 3: Spam check evaluation output
        print(f"[SPAM][DEBUG] ZSET Result -> count={count}, limit={limit}, exceeded={exceeded}", flush=True)

        if exceeded:
            # 🔍 Debug Gate 4: Punishment entry
            print(f"[SPAM][DEBUG] 🚨 Punishment triggered! (Target: {message.author})", flush=True)

            # Delete spam message
            try:
                await message.delete()
                print(f"[SPAM][DEBUG] Message deleted successfully.", flush=True)
            except discord.NotFound:
                pass  
            except discord.Forbidden:
                print(f"[SPAM][WARN] Missing permission to delete message (guild={message.guild.id})", flush=True)

            # Apply 10-minute timeout
            punishment_log = "spam_delete"
            punishment_reason = f"Spam detected ({count}/{limit} in {window}s)"

            try:
                duration = timedelta(minutes=10)
                await message.author.timeout(duration, reason=punishment_reason)
                punishment_log = "spam_timeout"
                punishment_reason += " -> 10m Timeout applied"
                print(f"[SPAM][DEBUG] Timeout (10m) applied to {message.author}", flush=True)
            except discord.Forbidden:
                print(f"[SPAM][WARN] Missing permission to punish (guild={message.guild.id}, user={message.author.id})", flush=True)
                punishment_reason += " (Punishment failed due to missing permission)"
            except Exception as e:
                print(f"[SPAM][ERROR] Failed to apply punishment: {type(e).__name__}: {e}", flush=True)

            # Enqueue infraction log (with Channel Name)
            self.enqueue_log(
                guild_id=message.guild.id,
                user_id=message.author.id,
                action=punishment_log,
                reason=f"{punishment_reason} | Channel: #{message.channel.name}",
            )

            # Send ephemeral-style warning message
            try:
                await message.channel.send(
                    f"⚠️ {message.author.mention}, spam detected. Your messages have been deleted and you have been **timed out for 10 minutes**.",
                    delete_after=5.0
                )
            except (discord.Forbidden, discord.HTTPException):
                pass
            return

        # 2. Execute Bad Word Filter (Evaluated only if user did not trigger spam)
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
                    reason=f"Forbidden word detected: {word} | Channel: #{message.channel.name}"
                )

                try:
                    await message.channel.send(
                        f"{message.author.mention}, forbidden word detected. Your message has been deleted.",
                        delete_after=3.0
                    )
                except (discord.Forbidden, discord.HTTPException):
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
            "action_type": action,
            "reason": reason,
            "moderator_id": str(self.bot.user.id) if self.bot.user else None,
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
        try:
            await self._drain_and_insert()
        except Exception as e:
            print(f"[LOG-QUEUE][ERROR] Flush loop exception: {type(e).__name__}: {e}", flush=True)

    @flush_log_queue.before_loop
    async def before_flush_log_queue(self):
        await self.bot.wait_until_ready()
        print("[LOG-QUEUE] Background batch flusher activated (Interval: 5s)", flush=True)

    async def _drain_and_insert(self, final: bool = False) -> None:
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
        return self.supabase.table("automod_logs").insert(rows).execute()

    async def _requeue_failed(self, batch: list) -> None:
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
        try:
            await self.redis.rpush("kyvo:log:dlq", json.dumps(row, ensure_ascii=False))
            await self.redis.ltrim("kyvo:log:dlq", -10000, -1)
            print(f"[LOG-QUEUE][DLQ] Retry limit exceeded -> Evacuated to Redis DLQ "
                  f"(Guild: {row.get('guild_id')})", flush=True)
        except Exception as e:
            self.log_dropped_count += 1
            print(f"[LOG-QUEUE][FATAL] DLQ backup fallback failed. Log lost: "
                  f"{type(e).__name__}: {e} | payload={row}", flush=True)

    async def _log_to_supabase(self, *args, **kwargs) -> None:
        self.enqueue_log(*args, **kwargs)

    # ══════════════════════════════════════════════════════════
    #  Management & Monitoring Commands (Admin Only)
    # ══════════════════════════════════════════════════════════

    @commands.command(name="reload_settings", aliases=["refresh_settings", "설정새로고침"])
    @commands.has_permissions(administrator=True)
    async def reload_settings(self, ctx):
        ok = await self.invalidate_settings_cache(ctx.guild.id)
        if ok:
            await ctx.send("✅ **Settings cache purged.** The latest configuration will be loaded on the next message.")
        else:
            await ctx.send("⚠️ **Failed to connect to Redis.** Changes will apply automatically within 5 minutes via natural TTL.")

    @commands.command(name="logqueue")
    @commands.has_permissions(administrator=True)
    async def logqueue_status(self, ctx):
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
    #  ⑤ Maintenance Commands (Admin Only)
    # ══════════════════════════════════════════════════════════
    @commands.command(name="clearspam")
    @commands.has_permissions(manage_messages=True)
    async def clear_spam(self, ctx, member: discord.Member):
        """[Admin] Manually clear a specific user's spam counter to resolve false-positives."""
        key = f"spam:{ctx.guild.id}:{member.id}"
        try:
            await self.redis.delete(key)
            self.spam_cache.pop((ctx.guild.id, member.id), None)
            await ctx.send(f"✅ Successfully cleared spam counter for {member.mention}.")
        except Exception as e:
            print(f"[SPAM][ERROR] Failed to clear spam counter: {type(e).__name__}: {e}", flush=True)
            await ctx.send("⚠️ Failed to connect to Redis. Counter will auto-reset after window expiration.")

async def setup(bot):
    await bot.add_cog(AutoMod(bot))
    print("[⚡ AUTOMOD] Cog extension setup complete.", flush=True)