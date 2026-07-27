import time
import asyncio
import json
import os
import re
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

# 🛡️ 대시보드에서 설정 가능한 범위 - lib/automodSettings.ts와 반드시 값이 일치해야 한다
# (party_settings 상수들과 동일한 관례). 여기서 벗어난 값은 조용히 기본값으로 되돌린다.
AUTOMOD_SPAM_LIMIT_MIN = 3
AUTOMOD_SPAM_LIMIT_MAX = 20
AUTOMOD_SPAM_LIMIT_DEFAULT = 5

AUTOMOD_SPAM_INTERVAL_MIN_SECONDS = 5
AUTOMOD_SPAM_INTERVAL_MAX_SECONDS = 60
AUTOMOD_SPAM_INTERVAL_DEFAULT_SECONDS = 10

AUTOMOD_TIMEOUT_MIN_SECONDS = 60
AUTOMOD_TIMEOUT_MAX_SECONDS = 3600
AUTOMOD_TIMEOUT_DEFAULT_SECONDS = 600  # 기존 하드코딩값과 동일 - 설정 안 한 길드는 지금과 동작이 똑같다

AUTOMOD_FORBIDDEN_WORD_MAX_LENGTH = 50
AUTOMOD_FORBIDDEN_WORDS_MAX_COUNT = 200

AUTOMOD_MAX_CHARS_MIN = 100
AUTOMOD_MAX_CHARS_MAX = 4000
AUTOMOD_MAX_CHARS_DEFAULT = 800

AUTOMOD_MAX_LINES_MIN = 3
AUTOMOD_MAX_LINES_MAX = 50
AUTOMOD_MAX_LINES_DEFAULT = 12

# 길이/줄수 판정 전에 트리플 백틱 코드 블록 내용을 잘라낸다 - 정상적인 긴 코드 스니펫이 오탐되지
# 않게 하기 위함(닫히지 않은 백틱은 매칭 안 돼서 안전 쪽으로 - 전체가 그대로 카운트된다).
CODE_BLOCK_PATTERN = re.compile(r"```.*?```", re.DOTALL)

# 🛡️ 처벌 로그 채널 게시 - 대시보드에 antinuke_settings.log_channel_id를 설정할 UI가 아직 없어서
# (app/api/settings/[guildId]/route.ts 주석 참고), 실질적으로는 이 이름의 채널이 대부분의 서버에서
# 유일한 실사용 경로가 된다. 예전(초기 커밋)엔 있었다가 로그 배치 큐 도입 때 통째로 삭제됐던 기능을
# 복원한다 - 이번엔 DB 큐(enqueue_log)와 완전히 독립적으로 동작하도록 설계.
AUTOMOD_LOG_CHANNEL_NAME = "automod-logs"
AUTOMOD_LOG_CONTENT_PREVIEW_MAX_LENGTH = 200


def truncate_message_preview(content: str, max_length: int = AUTOMOD_LOG_CONTENT_PREVIEW_MAX_LENGTH) -> str:
    """처벌 로그 임베드에 넣을 삭제된 메시지 미리보기를 자른다 - max_length를 넘으면 "..."을 붙인다."""
    if not content:
        return ""
    if len(content) <= max_length:
        return content
    return content[:max_length] + "..."


def resolve_automod_log_channel(guild: discord.Guild, antinuke_settings: dict | None) -> discord.TextChannel | None:
    """처벌 로그를 올릴 채널을 고른다: 설정된 채널(antinuke_settings.log_channel_id) -> 이름이
    "automod-logs"인 채널 -> guild.system_channel. 셋 다 없으면 None(호출부가 조용히 스킵)."""
    antinuke_settings = antinuke_settings or {}

    configured_id = antinuke_settings.get("log_channel_id")
    if configured_id:
        try:
            channel = guild.get_channel(int(configured_id))
        except (TypeError, ValueError):
            channel = None
        if channel:
            return channel

    named_channel = discord.utils.get(guild.text_channels, name=AUTOMOD_LOG_CHANNEL_NAME)
    if named_channel:
        return named_channel

    return guild.system_channel


def resolve_automod_settings(automod_settings: dict | None) -> dict:
    """대시보드가 저장한 automod_settings를 안전하게 정규화한다 - 값이 없거나(미설정 길드),
    형식이 이상해도(수동 DB 편집 등) 항상 안전한 기본값(기존 하드코딩값과 동일)으로 폴백하고
    절대 크래시하지 않는다. resolve_party_settings와 동일한 방침."""
    automod_settings = automod_settings or {}

    try:
        spam_limit = int(automod_settings.get("spam_limit"))
    except (TypeError, ValueError):
        spam_limit = AUTOMOD_SPAM_LIMIT_DEFAULT
    if not (AUTOMOD_SPAM_LIMIT_MIN <= spam_limit <= AUTOMOD_SPAM_LIMIT_MAX):
        spam_limit = AUTOMOD_SPAM_LIMIT_DEFAULT

    try:
        spam_interval_seconds = int(automod_settings.get("spam_interval_seconds"))
    except (TypeError, ValueError):
        spam_interval_seconds = AUTOMOD_SPAM_INTERVAL_DEFAULT_SECONDS
    if not (AUTOMOD_SPAM_INTERVAL_MIN_SECONDS <= spam_interval_seconds <= AUTOMOD_SPAM_INTERVAL_MAX_SECONDS):
        spam_interval_seconds = AUTOMOD_SPAM_INTERVAL_DEFAULT_SECONDS

    try:
        timeout_seconds = int(automod_settings.get("timeout_seconds"))
    except (TypeError, ValueError):
        timeout_seconds = AUTOMOD_TIMEOUT_DEFAULT_SECONDS
    if not (AUTOMOD_TIMEOUT_MIN_SECONDS <= timeout_seconds <= AUTOMOD_TIMEOUT_MAX_SECONDS):
        timeout_seconds = AUTOMOD_TIMEOUT_DEFAULT_SECONDS

    try:
        max_chars = int(automod_settings.get("max_chars"))
    except (TypeError, ValueError):
        max_chars = AUTOMOD_MAX_CHARS_DEFAULT
    if not (AUTOMOD_MAX_CHARS_MIN <= max_chars <= AUTOMOD_MAX_CHARS_MAX):
        max_chars = AUTOMOD_MAX_CHARS_DEFAULT

    try:
        max_lines = int(automod_settings.get("max_lines"))
    except (TypeError, ValueError):
        max_lines = AUTOMOD_MAX_LINES_DEFAULT
    if not (AUTOMOD_MAX_LINES_MIN <= max_lines <= AUTOMOD_MAX_LINES_MAX):
        max_lines = AUTOMOD_MAX_LINES_DEFAULT

    return {
        "spam_limit": spam_limit,
        "spam_interval_seconds": spam_interval_seconds,
        "timeout_seconds": timeout_seconds,
        "max_chars": max_chars,
        "max_lines": max_lines,
    }


def check_message_shape(content: str, max_chars: int, max_lines: int) -> tuple[str | None, int]:
    """단일 메시지만 보고 즉시 판단 가능한 두 규칙 - 트리플 백틱 코드 블록은 판정에서 제외한다.
    글자 수 위반이 줄 수 위반보다 우선(둘 다 걸리면 "too_long"을 반환) - 임의 순서지만 항상
    결정적이다. 위반 없으면 (None, 0). 두 번째 반환값은 실제 측정치(글자 수 또는 줄 수) -
    spam_reason처럼 로그/경고 메시지에 실제 수치를 보여주기 위함."""
    if not content:
        return None, 0

    stripped = CODE_BLOCK_PATTERN.sub("", content)

    if len(stripped) > max_chars:
        return "too_long", len(stripped)

    line_count = stripped.count("\n") + 1
    if line_count > max_lines:
        return "too_many_lines", line_count

    return None, 0


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

        # 🛡️ settings.automod_settings에서 실제로 읽는다 - 대시보드 UI가 생겨서 더는 하드코딩할
        # 필요가 없다. 미설정 길드는 resolve_automod_settings의 기본값(기존 하드코딩값과 동일)으로
        # 폴백하므로 동작이 그대로 유지된다.
        nested_settings = settings.get("settings") or {}
        automod_settings = resolve_automod_settings(nested_settings.get("automod_settings"))
        limit = automod_settings["spam_limit"]
        window = automod_settings["spam_interval_seconds"]
        timeout_seconds = automod_settings["timeout_seconds"]
        max_chars = automod_settings["max_chars"]
        max_lines = automod_settings["max_lines"]

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
            # 무관하게 항상 실행된다. timeout_seconds는 위에서 이미 automod_settings로부터 계산됨
            # (처벌 지속시간과 플래그 TTL을 반드시 일치시켜야 한다).
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
                await self._post_punishment_log(
                    guild=message.guild, member=message.author, channel=message.channel,
                    action=punishment_log, reason=base_reason, content=message.content,
                    antinuke_settings=nested_settings.get("antinuke_settings") or {},
                )

                # 🛡️ [경고 메시지 중복 방지] 처벌/로그와 동일한 이유로 claimed 블록 안으로 이동 -
                # 슬라이딩 윈도우 카운트는 처벌 후에도 리셋되지 않아, 같은 burst 안의 후속
                # 메시지마다 경고가 반복 전송되던 문제(경고 자체가 채널을 도배)를 막는다.
                warn_msg = await self.get_msg(message.guild.id, "spam_warn", mention=message.author.mention)
                try:
                    await message.channel.send(warn_msg, delete_after=5.0)
                except (discord.Forbidden, discord.HTTPException):
                    pass
            return

        # 1-2. 메시지 형태 규칙 - 글자 수/줄 수 초과(버스트 여부와 무관하게 메시지 하나만 봐도
        # 즉시 판단 가능). 짧은 시간에 반복되면 위의 check_spam이 이미 매번 슬라이딩 윈도우에
        # 등록하고 있으므로(exceeded 여부와 무관하게), 별도 카운터 없이도 기존 스팸 타임아웃
        # 경로로 자연스럽게 escalate된다.
        shape_violation, shape_count = check_message_shape(message.content, max_chars, max_lines)

        if shape_violation:
            try:
                await message.delete()
            except discord.NotFound:
                pass
            except discord.Forbidden:
                pass

            if shape_violation == "too_long":
                reason_key, warn_key, limit_value = "automod_too_long_reason", "automod_too_long_warn", max_chars
            else:
                reason_key, warn_key, limit_value = "automod_many_lines_reason", "automod_many_lines_warn", max_lines

            log_reason = await self.get_msg(message.guild.id, reason_key, count=shape_count, limit=limit_value)
            self.enqueue_log(
                guild_id=message.guild.id,
                user_id=message.author.id,
                action=f"{shape_violation}_delete",
                reason=f"{log_reason} | Channel: #{message.channel.name}",
            )
            await self._post_punishment_log(
                guild=message.guild, member=message.author, channel=message.channel,
                action=f"{shape_violation}_delete", reason=log_reason, content=message.content,
                antinuke_settings=nested_settings.get("antinuke_settings") or {},
            )

            warn_msg = await self.get_msg(message.guild.id, warn_key, mention=message.author.mention)
            try:
                await message.channel.send(warn_msg, delete_after=5.0)
            except (discord.Forbidden, discord.HTTPException):
                pass
            return

        # 2. 금지어 필터링
        # 🛡️ [익명 제보 기능과 공유] settings.automod_settings.forbidden_words가 실제 저장 위치다.
        # KyvoBaseCog.check_forbidden_words()를 거쳐서 익명 제보 필터와 완전히 같은 소스를 본다.
        word = await self.check_forbidden_words(message.guild.id, message.content)

        if word:
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
            await self._post_punishment_log(
                guild=message.guild, member=message.author, channel=message.channel,
                action="bad_word_delete", reason=log_reason, content=message.content,
                antinuke_settings=nested_settings.get("antinuke_settings") or {},
            )

            warn_msg = await self.get_msg(message.guild.id, "bad_word_warn", mention=message.author.mention)
            try:
                await message.channel.send(warn_msg, delete_after=3.0)
            except (discord.Forbidden, discord.HTTPException):
                pass

    # ══════════════════════════════════════════════════════════
    #  ③-2 automod-logs 채널 실시간 게시 - enqueue_log(DB 큐)와 완전히 독립적으로 동작한다.
    #  배치 큐는 DB insert 부하 완화가 목적이었지 채널 알림을 지연시킬 이유가 없어서, 원자적
    #  클레임이 확정되는 시점에 즉시 발송한다.
    # ══════════════════════════════════════════════════════════
    async def _post_punishment_log(self, guild: discord.Guild, member: discord.Member, channel: discord.abc.Messageable,
                                    action: str, reason: str, content: str, antinuke_settings: dict) -> None:
        """이 함수가 무엇을 하다 실패하든(채널 없음/권한 부족/Discord API 오류) enqueue_log가 이미
        큐에 넣은 DB 로그엔 절대 영향을 주지 않는다 - 통째로 여기서 삼킨다(on_member_join에 추가한
        것과 동일한 안전망). 반대 방향도 마찬가지: enqueue_log 자체가 예외를 던지지 않는 구조라
        (큐가 가득 차면 카운터만 증가) 이 함수의 성공/실패가 DB 큐에 영향을 줄 수도 없다."""
        try:
            log_channel = resolve_automod_log_channel(guild, antinuke_settings)
            if log_channel is None:
                return

            title = await self.get_msg(guild.id, "automod_log_title")
            lbl_offender = await self.get_msg(guild.id, "automod_log_field_offender")
            lbl_channel = await self.get_msg(guild.id, "automod_log_field_channel")
            lbl_reason = await self.get_msg(guild.id, "automod_log_field_reason")
            lbl_action = await self.get_msg(guild.id, "automod_log_field_action")
            lbl_content = await self.get_msg(guild.id, "automod_log_field_content")

            embed = discord.Embed(title=title, color=discord.Color.red(), timestamp=datetime.now(timezone.utc))
            embed.add_field(name=lbl_offender, value=f"{member.mention} (`{member.id}`)", inline=True)
            embed.add_field(name=lbl_channel, value=getattr(channel, "mention", str(channel)), inline=True)
            embed.add_field(name=lbl_action, value=f"`{action}`", inline=True)
            embed.add_field(name=lbl_reason, value=reason, inline=False)

            preview = truncate_message_preview(content)
            if preview:
                embed.add_field(name=lbl_content, value=f"```\n{preview}\n```", inline=False)

            await log_channel.send(embed=embed)
        except Exception as e:
            print(f"[AUTOMOD_LOG][ERROR] Failed to post punishment log embed (guild={guild.id}): {type(e).__name__}: {e}", flush=True)

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