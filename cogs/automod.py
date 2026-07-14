import asyncio
import json
import os
from datetime import datetime, timezone
import discord
from discord.ext import commands, tasks
import redis.asyncio as aioredis

class AutoMod(commands.Cog):
    """
    자동 관리(도배 및 금지어 필터링) Cog.
    - Read: Cache-Aside 패턴 (Upstash Redis)으로 Supabase 부하 차단
    - Write: 인메모리 배치 큐를 통해 Supabase에 로그 일괄 삽입 (Bulk Insert)
    """

    # ── 로그 배치 큐 설정 ──────────────────────────────
    LOG_FLUSH_INTERVAL = 5.0    # 큐를 비우는 주기 (초)
    LOG_BATCH_MAX_SIZE = 200    # 1회 Bulk Insert 최대 행 수
    LOG_QUEUE_MAX_SIZE = 5000   # 메모리 폭주 방지용 큐 상한
    LOG_MAX_RETRY = 3           # 삽입 실패 시 재시도 횟수
    # ─────────────────────────────────────────────────

    def __init__(self, bot):
        self.bot = bot
        self.supabase = getattr(bot, "supabase", None)
        
        # Redis 연결 초기화 (봇 메인 객체에 있으면 재사용, 없으면 독립 연결)
        if hasattr(bot, "redis"):
            self.redis = bot.redis
        else:
            redis_url = os.getenv("REDIS_URL")
            self.redis = aioredis.from_url(redis_url, decode_responses=True)

        # 도배 감지용 인메모리 슬라이딩 윈도우 캐시
        self.spam_cache = {}

        # 처벌 로그를 임시 적재할 인메모리 큐 (이벤트 루프 안전)
        self.log_queue = asyncio.Queue(maxsize=self.LOG_QUEUE_MAX_SIZE)
        self.log_dropped_count = 0      # 큐 포화로 유실된 로그 누적 카운터

        # 5초 주기 일괄 삽입 워커 가동
        self.flush_log_queue.start()
        print("[⚡ AUTOMOD] 로딩 시작: Redis 연결 및 로그 배치 큐 가동 완료.", flush=True)

    async def cog_unload(self):
        """Cog 언로드/봇 종료 시 큐에 남은 로그를 최대한 살려서 밀어 넣는다."""
        self.flush_log_queue.cancel()
        try:
            await asyncio.wait_for(self._drain_and_insert(final=True), timeout=10.0)
        except Exception as e:
            print(f"[LOG-QUEUE][ERROR] 종료 플러시 실패: {type(e).__name__}: {e}", flush=True)

    # ══════════════════════════════════════════════════════════
    #  Cache-Aside 설정 조회 레이어 (Read Optimizer)
    # ══════════════════════════════════════════════════════════

    async def _get_cached_guild_settings(self, guild_id: str) -> dict:
        """Redis 캐시를 먼저 확인하고, 없으면 Supabase에서 가져와 캐싱한다 (TTL 5분)."""
        cache_key = f"guild:{guild_id}:settings"
        try:
            cached_data = await self.redis.get(cache_key)
            if cached_data:
                return json.loads(cached_data)
        except Exception as cache_err:
            print(f"[❌ AUTOMOD] Redis 캐시 조회 실패: {cache_err}", flush=True)

        # 캐시 미스 시 Supabase로 이동
        guild_settings = {}
        if hasattr(self.bot, "get_guild_settings"):
            try:
                guild_settings = await self.bot.get_guild_settings(guild_id)
            except Exception as err:
                print(f"[❌ AUTOMOD] bot.get_guild_settings 호출 실패: {err}", flush=True)
        else:
            # fallback 직접 조회
            try:
                if self.supabase:
                    res = self.supabase.table("guild_settings").select("*").eq("guild_id", str(guild_id)).execute()
                    if res.data:
                        guild_settings = res.data[0]
            except Exception as db_err:
                print(f"[❌ AUTOMOD] Supabase 직접 조회 실패: {db_err}", flush=True)

        # 가져온 데이터를 캐시에 5분간 보관
        try:
            await self.redis.setex(cache_key, 300, json.dumps(guild_settings, ensure_ascii=False))
        except Exception as cache_err:
            print(f"[❌ AUTOMOD] Redis 캐시 저장 실패: {cache_err}", flush=True)

        return guild_settings

    # ══════════════════════════════════════════════════════════
    #  메시지 감시 이벤트 리스너 (Spam / Bad Word Filter)
    # ══════════════════════════════════════════════════════════

    @commands.Cog.listener()
    async def on_message(self, message):
        # 봇이 작성한 메시지거나 DM인 경우 패스
        if message.author.bot or message.guild is None:
            return

        # 관리자 및 메시지 관리 권한 소지자는 검사 생략 (Bypass)
        member = message.guild.get_member(message.author.id)
        if member and (member.guild_permissions.manage_messages or member.guild_permissions.administrator):
            return

        # 1. Redis 기반 캐시 레이어에서 서버 설정 로드
        settings = await self._get_cached_guild_settings(str(message.guild.id))
        if not settings:
            return

        # 2. 안티 스팸 (Sliding Window 인메모리 필터)
        user_id = message.author.id
        now = datetime.now(timezone.utc).timestamp()

        if user_id not in self.spam_cache:
            self.spam_cache[user_id] = []

        # 최근 5초 내 메시지만 유지
        self.spam_cache[user_id] = [t for t in self.spam_cache[user_id] if now - t < 5.0]
        self.spam_cache[user_id].append(now)

        # 5초 내 메시지 개수가 5개를 초과할 시 즉각 조치
        if len(self.spam_cache[user_id]) > 5:
            try:
                await message.delete()
            except discord.Forbidden:
                pass

            # [DB 비동기 쓰기] 큐에 던지고 즉시 복귀 (완장 DB 컬럼에 맞춤!)
            self.enqueue_log(
                guild_id=message.guild.id,
                user_id=message.author.id,
                action="spam_delete",
                reason="도배 감지 (슬라이딩 윈도우 초과)"
            )

            try:
                await message.channel.send(
                    f"{message.author.mention}님, 도배가 감지되어 메시지가 삭제되었습니다.",
                    delete_after=3.0
                )
            except discord.Forbidden:
                pass
            return

        # 3. 금지어 필터링
        forbidden_words = settings.get("forbidden_words", [])
        if isinstance(forbidden_words, str):
            forbidden_words = [w.strip() for w in forbidden_words.split(",") if w.strip()]

        for word in forbidden_words:
            if word in message.content:
                try:
                    await message.delete()
                except discord.Forbidden:
                    pass

                # [DB 비동기 쓰기] 큐에 던지고 즉시 복귀 (완장 DB 컬럼에 맞춤!)
                self.enqueue_log(
                    guild_id=message.guild.id,
                    user_id=message.author.id,
                    action="bad_word_delete",
                    reason=f"금지어 감지: {word}"
                )

                try:
                    await message.channel.send(
                        f"{message.author.mention}님, 금지어가 포함되어 메시지가 삭제되었습니다.",
                        delete_after=3.0
                    )
                except discord.Forbidden:
                    pass
                break

    # ══════════════════════════════════════════════════════════
    #  로그 배치 큐 비동기 처리 엔진 (Write Optimizer)
    # ══════════════════════════════════════════════════════════

    def enqueue_log(self, guild_id: int, user_id: int, action: str, reason: str) -> None:
        """
        처벌 로그를 DB에 즉시 쓰지 않고 인메모리 큐에 적재한다.
        실제 Supabase 스키마 구조와 100% 싱크를 맞춤.
        """
        payload = {
            "guild_id": str(guild_id),
            "user_id": str(user_id),
            "action_type": action,   # 완장 DB 컬럼명인 'action_type'으로 매핑!
            "reason": reason,
            "moderator_id": str(self.bot.user.id) if self.bot.user else None, # 처벌한 봇 ID 자동 삽입!
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self.log_queue.put_nowait(payload)
        except asyncio.QueueFull:
            self.log_dropped_count += 1
            print(
                f"[LOG-QUEUE][WARN] 큐 포화(maxsize={self.LOG_QUEUE_MAX_SIZE}) "
                f"→ 로그 1건 폐기 (누적 유실 {self.log_dropped_count}건)",
                flush=True,
            )

    @tasks.loop(seconds=LOG_FLUSH_INTERVAL)
    async def flush_log_queue(self):
        """5초마다 큐를 비워 Supabase에 일괄 삽입하는 백그라운드 워커."""
        try:
            await self._drain_and_insert()
        except Exception as e:
            print(f"[LOG-QUEUE][ERROR] 플러시 루프 예외: {type(e).__name__}: {e}", flush=True)

    @flush_log_queue.before_loop
    async def before_flush_log_queue(self):
        """봇 게이트웨이 준비 완료 후 플러셔를 시작한다."""
        await self.bot.wait_until_ready()
        print("[LOG-QUEUE] 배치 플러셔 가동 (interval=5s)", flush=True)

    async def _drain_and_insert(self, final: bool = False) -> None:
        """큐에서 최대 LOG_BATCH_MAX_SIZE 만큼 꺼내 Bulk Insert 한다."""
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
        """동기 Supabase 클라이언트를 스레드 풀에 격리해 호출한다. 성공 여부 반환."""
        rows = [{k: v for k, v in row.items() if not k.startswith("_")} for row in batch]
        
        # Supabase 클라이언트 검증
        if not self.supabase:
            self.supabase = getattr(self.bot, "supabase", None)
            if not self.supabase:
                print("[LOG-QUEUE][ERROR] Supabase 클라이언트를 찾을 수 없습니다.", flush=True)
                return False

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._sync_bulk_insert, rows)
            print(f"[LOG-QUEUE] Supabase 일괄 삽입 성공: {len(rows)}건 "
                  f"(잔여 큐 {self.log_queue.qsize()})", flush=True)
            return True
        except Exception as e:
            print(f"[LOG-QUEUE][ERROR] 일괄 삽입 실패({len(rows)}건): "
                  f"{type(e).__name__}: {e}", flush=True)
            return False

    def _sync_bulk_insert(self, rows: list):
        """스레드 풀 전용 동기 삽입. supabase-py는 리스트를 넘기면 Bulk Insert로 동작한다."""
        return self.supabase.table("automod_logs").insert(rows).execute()

    async def _requeue_failed(self, batch: list) -> None:
        """삽입 실패한 로그를 재시도 카운트와 함께 큐에 되돌린다. 한도 초과 시 Redis DLQ로 대피."""
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
        """최종 실패 로그를 Redis 리스트에 백업한다. Redis마저 죽으면 콘솔에만 남기고 포기한다."""
        try:
            await self.redis.rpush("kyvo:log:dlq", json.dumps(row, ensure_ascii=False))
            await self.redis.ltrim("kyvo:log:dlq", -10000, -1)  # DLQ 무한 증식 방지
            print(f"[LOG-QUEUE][DLQ] 재시도 한도 초과 → Redis 대피 완료 "
                  f"(guild={row.get('guild_id')})", flush=True)
        except Exception as e:
            self.log_dropped_count += 1
            print(f"[LOG-QUEUE][FATAL] DLQ 백업 실패, 로그 유실: "
                  f"{type(e).__name__}: {e} | payload={row}", flush=True)

    # 기존 호출부 호환용 래퍼 (레거시 코드가 혹시 await로 부르고 있어도 크래시 안 남)
    async def _log_to_supabase(self, *args, **kwargs) -> None:
        """[Deprecated] 즉시 쓰기 → 배치 큐 적재로 리다이렉트."""
        self.enqueue_log(*args, **kwargs)

    # ══════════════════════════════════════════════════════════
    #  모니터링 명령어 (Admin Only)
    # ══════════════════════════════════════════════════════════

    @commands.command(name="logqueue")
    @commands.has_permissions(administrator=True)
    async def logqueue_status(self, ctx):
        """현재 로그 배치 큐의 적재량과 유실 카운트를 확인한다."""
        try:
            dlq_size = await self.redis.llen("kyvo:log:dlq")
        except Exception:
            dlq_size = "N/A (Redis 오류)"

        embed = discord.Embed(title="📊 로그 배치 큐 상태", color=0x5865F2)
        embed.add_field(name="대기 중", value=f"{self.log_queue.qsize()} 건", inline=True)
        embed.add_field(name="유실 누적", value=f"{self.log_dropped_count} 건", inline=True)
        embed.add_field(name="Redis DLQ", value=f"{dlq_size} 건", inline=True)
        embed.add_field(name="플러시 주기", value=f"{self.LOG_FLUSH_INTERVAL}초", inline=False)
        await ctx.send(embed=embed)

async def setup(bot):
    await bot.add_cog(AutoMod(bot))
    print("[⚡ AUTOMOD] setup 함수 실행 완료!", flush=True)