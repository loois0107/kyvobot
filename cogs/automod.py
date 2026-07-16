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
    #  ③ 도배 감지 핵심 함수 (ZSET 및 로컬 폴백)
    # ══════════════════════════════════════════════════════════
    async def check_spam(self, guild_id: int, user_id: int, message_id: int,
                         limit: int, window_sec: int) -> tuple[int, bool]:
        """
        Redis ZSET 슬라이딩 윈도우로 도배 여부를 판정한다.
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
    #  ④ Message Event Listener (🔍 촘촘한 디버그 로그 추가형)
    # ══════════════════════════════════════════════════════════
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # 🔍 디버그 관문 0: Cog 수신 자체를 감지하는지 확인 (3순위 의심용)
        print(f"[SPAM][DEBUG] on_message 진입 | Author: {message.author} (Bot: {message.author.bot}) | Content: '{message.content}'", flush=True)

        if message.author.bot or not message.guild:
            return
        
        perms = message.author.guild_permissions
        if perms.administrator or perms.manage_messages:
            # 🔍 디버그 관문 1: 관리자 스킵 여부
            print(f"[SPAM][DEBUG] 관리자 프리패스로 스킵: {message.author}", flush=True)
            return

        # 🔍 디버그 관문 2: 글자 수 검사 (1순위 의심인 'Message Content Intent' 누락 시 0글자로 인식되어 무조건 여기서 스킵됨)
        if len(message.content) < 2 and not message.attachments:
            print(f"[SPAM][DEBUG] 짧은 메시지 스킵 | Content: '{message.content}' (len={len(message.content)})", flush=True)
            return

        settings = await self.get_guild_settings(message.guild.id)
        # 🔍 디버그 관문 3: 서버 세팅값 조회 결과 확인 (2순위 의심용)
        print(f"[SPAM][DEBUG] Settings 조회 결과: {settings}", flush=True)

        if not settings or not settings.get("automod_enabled", True):
            print(f"[SPAM][DEBUG] automod 비활성 상태로 스킵", flush=True)
            return

        limit = settings.get("spam_limit", 5)        # 윈도우당 허용 메시지 수
        window = settings.get("spam_interval", 10)   # 윈도우 크기 (초)

        # 1. 도배 감지 실행
        count, exceeded = await self.check_spam(
            guild_id=message.guild.id,
            user_id=message.author.id,
            message_id=message.id,
            limit=limit,
            window_sec=window,
        )
        # 🔍 디버그 관문 4: Redis 판정 결과 출력
        print(f"[SPAM][DEBUG] ZSET 결과 -> count={count}, limit={limit}, exceeded={exceeded}", flush=True)

        if exceeded:
            # 🔍 디버그 관문 5: 초과하여 진짜 처벌로 들어왔는지 확인
            print(f"[SPAM][DEBUG] 🚨 처벌 진입! (Target: {message.author})", flush=True)

            # 메시지 삭제
            try:
                await message.delete()
                print(f"[SPAM][DEBUG] 메시지 삭제 완료", flush=True)
            except discord.NotFound:
                pass  
            except discord.Forbidden:
                print(f"[SPAM][WARN] 메시지 삭제 권한 없음 (guild={message.guild.id})", flush=True)

            # 타임아웃 처벌 적용 (10분)
            import datetime
            punishment_log = "spam_delete"
            punishment_reason = f"도배 감지 ({count}/{limit} in {window}s)"

            try:
                duration = datetime.timedelta(minutes=10)
                await message.author.timeout(duration, reason=punishment_reason)
                punishment_log = "spam_timeout"
                punishment_reason += " -> 10분 타임아웃 처분"
                print(f"[SPAM][DEBUG] {message.author} 타임아웃 10분 적용 완료", flush=True)
            except discord.Forbidden:
                print(f"[SPAM][WARN] 처벌 권한 없음 (guild={message.guild.id}, user={message.author.id})", flush=True)
                punishment_reason += " (권한 부족으로 처벌 실패)"
            except Exception as e:
                print(f"[SPAM][ERROR] 처벌 적용 중 에러: {type(e).__name__}: {e}", flush=True)

            # 처벌 로그 배치 큐 전송 (클로드 꿀팁 반영: 채널명 추가)
            self.enqueue_log(
                guild_id=message.guild.id,
                user_id=message.author.id,
                action=punishment_log,
                reason=f"{punishment_reason} | 채널: #{message.channel.name}",
            )

            try:
                await message.channel.send(
                    f"⚠️ {message.author.mention}, 도배가 감지되어 메시지가 삭제되고 **10분간 입막음(Timeout)** 처리되었습니다.",
                    delete_after=5.0
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
                    reason=f"Forbidden word detected: {word} | 채널: #{message.channel.name}"
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
    #  ⑤ 초기화 명령어 (관리자 수동 오탐 구제용)
    # ══════════════════════════════════════════════════════════
    @commands.command(name="clearspam")
    @commands.has_permissions(manage_messages=True)
    async def clear_spam(self, ctx, member: discord.Member):
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