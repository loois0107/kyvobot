import asyncio
import json
import os
from datetime import datetime, timezone
import discord
from discord.ext import commands, tasks
import redis.asyncio as aioredis

class AutoMod(commands.Cog):
    """
    AutoMod (Anti-Spam & Bad Word Filtering) Cog.
    - Read: Cache-Aside Pattern (Upstash Redis) to prevent Supabase load.
    - Write: In-memory Batch Queue for Supabase Bulk Insertion.
    """

    # ── Log Batch Queue Settings ───────────────────────
    LOG_FLUSH_INTERVAL = 5.0    # Queue flush interval (seconds)
    LOG_BATCH_MAX_SIZE = 200    # Max rows per bulk insert
    LOG_QUEUE_MAX_SIZE = 5000   # Max queue capacity to prevent memory overflow
    LOG_MAX_RETRY = 3           # Max retries on database insert failure
    # ─────────────────────────────────────────────────

    def __init__(self, bot):
        self.bot = bot
        self.supabase = getattr(bot, "supabase", None)
        
        # Initialize Redis connection
        if hasattr(bot, "redis"):
            self.redis = bot.redis
        else:
            redis_url = os.getenv("REDIS_URL")
            self.redis = aioredis.from_url(redis_url, decode_responses=True)

        # In-memory sliding window cache for spam detection
        self.spam_cache = {}

        # Async queue to temporarily buffer infraction logs
        self.log_queue = asyncio.Queue(maxsize=self.LOG_QUEUE_MAX_SIZE)
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

    async def _get_cached_guild_settings(self, guild_id: str) -> dict:
        """Fetch guild settings from Redis cache first; fallback to Supabase on cache miss (TTL: 5m)."""
        cache_key = f"guild:{guild_id}:settings"
        try:
            cached_data = await self.redis.get(cache_key)
            if cached_data:
                return json.loads(cached_data)
        except Exception as cache_err:
            print(f"[❌ AUTOMOD] Redis cache lookup failed: {cache_err}", flush=True)

        # Fallback to Supabase on cache miss
        guild_settings = {}
        if hasattr(self.bot, "get_guild_settings"):
            try:
                guild_settings = await self.bot.get_guild_settings(guild_id)
            except Exception as err:
                print(f"[❌ AUTOMOD] bot.get_guild_settings failed: {err}", flush=True)
        else:
            try:
                if self.supabase:
                    res = self.supabase.table("guild_settings").select("*").eq("guild_id", str(guild_id)).execute()
                    if res.data:
                        guild_settings = res.data[0]
            except Exception as db_err:
                print(f"[❌ AUTOMOD] Supabase fallback query failed: {db_err}", flush=True)

        # Cache the fetched data for 5 minutes
        try:
            await self.redis.setex(cache_key, 300, json.dumps(guild_settings, ensure_ascii=False))
        except Exception as cache_err:
            print(f"[❌ AUTOMOD] Redis cache save failed: {cache_err}", flush=True)

        return guild_settings

    # ══════════════════════════════════════════════════════════
    #  Message Event Listener (Spam / Bad Word Filter)
    # ══════════════════════════════════════════════════════════

    @commands.Cog.listener()
    async def on_message(self, message):
        # Bypass bots and DM channels
        if message.author.bot or message.guild is None:
            return

        # Bypass Administrators and Moderators
        member = message.guild.get_member(message.author.id)
        if member and (member.guild_permissions.manage_messages or member.guild_permissions.administrator):
            return

        # 1. Fetch server settings from Cache-Aside layer
        settings = await self._get_cached_guild_settings(str(message.guild.id))
        if not settings:
            return

        # 2. Anti-Spam (In-memory Sliding Window Filter)
        user_id = message.author.id
        now = datetime.now(timezone.utc).timestamp()

        if user_id not in self.spam_cache:
            self.spam_cache[user_id] = []

        # Retain messages sent within the last 5.0 seconds
        self.spam_cache[user_id] = [t for t in self.spam_cache[user_id] if now - t < 5.0]
        self.spam_cache[user_id].append(now)

        # Trigger action if rate exceeds 5 messages per 5 seconds
        if len(self.spam_cache[user_id]) > 5:
            try:
                await message.delete()
            except discord.Forbidden:
                pass

            # [Async Non-blocking Write] Enqueue log and resume immediately
            self.enqueue_log(
                guild_id=message.guild.id,
                user_id=message.author.id,
                action="spam_delete",
                reason="Spam detected (Exceeded sliding window limit)"
            )

            try:
                await message.channel.send(
                    f"{message.author.mention}, spam detected. Your message has been deleted.",
                    delete_after=3.0
                )
            except discord.Forbidden:
                pass
            return

        # 3. Bad Word Filtering
        forbidden_words = settings.get("forbidden_words", [])
        if isinstance(forbidden_words, str):
            forbidden_words = [w.strip() for w in forbidden_words.split(",") if w.strip()]

        for word in forbidden_words:
            if word in message.content:
                try:
                    await message.delete()
                except discord.Forbidden:
                    pass

                # [Async Non-blocking Write] Enqueue log and resume immediately
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
        """
        Pushes infraction logs into the in-memory queue instead of writing directly to the DB.
        Synchronous execution ensures the main event loop is never blocked.
        """
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
    #  Monitoring Command (Admin Only)
    # ══════════════════════════════════════════════════════════

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

async def setup(bot):
    await bot.add_cog(AutoMod(bot))
    print("[⚡ AUTOMOD] Cog extension setup complete.", flush=True)