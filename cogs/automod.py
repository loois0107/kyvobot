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
        self.punished_cache = {}  # Redis 장애 시 폴백용 (guild_id, user_id) -> 만료 시각(epoch)
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
    #  ②-2 중복 처벌 방지 플래그 (Redis 우선, check_spam과 동일한 폴백 구조)
    #  discord.py의 Member.timeout()은 self를 제자리 갱신하지 않고 새 Member를 리턴만 하기 때문에
    #  (반환값을 버림), message.author.is_timed_out()은 같은 burst 안에서 절대 갱신되지 않는다.
    #  그래서 디스코드 캐시에 의존하지 않고 우리가 직접 Redis에 처벌 상태를 기록해서 판단한다.
    # ══════════════════════════════════════════════════════════
    async def try_claim_punishment(self, guild_id: int, user_id: int, duration_sec: int) -> bool:
        """True면 이 호출이 처벌을 원자적으로 선점한 것(=진행), False면 이미 누가 선점함(=스킵).
        GET으로 먼저 확인하고 나중에 SET하는 방식은 확인과 기록 사이에 시간차가 생겨서, 거의 동시에
        도착한 메시지 두 개가 둘 다 "아직 처벌 안 됨"을 보고 통과하는 경쟁 조건이 실제로 발생했다
        (7:21 테스트에서 1초 안에 로그 2건). SET NX는 확인+기록이 Redis 서버에서 원자적으로 처리되므로
        동시에 여러 개가 들어와도 정확히 하나만 성공한다."""
        key = f"punished:{guild_id}:{user_id}"
        try:
            acquired = await self.redis.set(key, "1", ex=duration_sec, nx=True)
            return bool(acquired)
        except Exception as e:
            print(f"[SPAM][WARN] Redis punished-flag claim failed, falling back to local: {type(e).__name__}: {e}", flush=True)
            return self._try_claim_punishment_local(guild_id, user_id, duration_sec)

    def _try_claim_punishment_local(self, guild_id: int, user_id: int, duration_sec: int) -> bool:
        key = (guild_id, user_id)
        now = time.time()
        expiry = self.punished_cache.get(key)
        if expiry is not None and now < expiry:
            return False
        self.punished_cache[key] = now + duration_sec
        return True

    async def release_punishment_claim(self, guild_id: int, user_id: int) -> None:
        """타임아웃 API 호출 자체가 실패했을 때 선점을 되돌린다 - 실제로 제재되지 않았으므로
        다음 메시지가 다시 처벌을 시도할 수 있어야 한다."""
        key = f"punished:{guild_id}:{user_id}"
        try:
            await self.redis.delete(key)
        except Exception as e:
            print(f"[SPAM][WARN] Redis punished-flag release failed: {type(e).__name__}: {e}", flush=True)
        self.punished_cache.pop((guild_id, user_id), None)

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
        if not settings:
            return

        # 🛡️ [죽은 참조 정리] automod_enabled/spam_limit/spam_interval은 guild_settings에 대응하는
        # 컬럼이 실제로 존재하지 않아 대시보드로 설정할 방법이 없다(직접 조회로 확인됨). settings.get()으로
        # 읽는 척하지 않고, 설정 UI가 실제로 생기기 전까지는 고정값을 명시적으로 쓴다.
        limit = 5
        window = 10

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

            # 🛡️ [중복 처벌 방지 - 원자적 선점] 슬라이딩 윈도우 카운트는 처벌 발동 후에도 리셋되지
            # 않아서, 같은 burst 안에서 도착하는 후속 메시지마다 재타임아웃 시도 + 로그 기록이 매번
            # 다시 발동했었다. GET으로 먼저 확인하고 나중에 SET하는 방식은 그 사이 시간차 때문에
            # 여전히 중복이 발생했다(1초 안에 로그 2건) - SET NX로 확인+선점을 원자적으로 한 번에
            # 처리해서 동시에 도착해도 정확히 하나만 통과하도록 바꾼다. 메시지 삭제는 이 체크와
            # 무관하게 항상 실행된다.
            timeout_seconds = 600  # 10분 - 처벌 지속시간과 플래그 TTL을 반드시 일치시켜야 한다
            claimed = await self.try_claim_punishment(message.guild.id, message.author.id, timeout_seconds)

            if claimed:
                # ⚡ 부모 클래스의 통합 다국어 로더(get_msg)를 호출하여 실시간 문자열 치환
                base_reason = await self.get_msg(message.guild.id, "spam_reason", count=count, limit=limit, window=window)
                punishment_log = "spam_delete"

                try:
                    duration = timedelta(seconds=timeout_seconds)
                    await message.author.timeout(duration, reason=base_reason)
                    punishment_log = "spam_timeout"
                    suffix = await self.get_msg(message.guild.id, "spam_timeout_applied")
                    base_reason += suffix
                except discord.Forbidden:
                    suffix = await self.get_msg(message.guild.id, "spam_failed_permission")
                    base_reason += suffix
                    # 🛡️ 실제로 제재되지 않았으므로 선점을 풀어서 다음 메시지가 다시 시도할 수 있게 한다.
                    await self.release_punishment_claim(message.guild.id, message.author.id)
                except Exception as e:
                    print(f"[SPAM][ERROR] Failed to apply punishment: {type(e).__name__}: {e}", flush=True)
                    await self.release_punishment_claim(message.guild.id, message.author.id)

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
        # 🛡️ [죽은 참조 정리] guild_settings에 forbidden_words/banned_words 컬럼이 없어서(직접 조회로
        # 확인됨) 대시보드에서 채울 방법이 없다. settings.get()으로 읽는 척하지 않고 빈 리스트를 명시한다 -
        # 실제 컬럼이 생기면 이 한 줄만 바꾸면 된다.
        forbidden_words: list[str] = []

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