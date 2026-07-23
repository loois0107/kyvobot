import asyncio
import json
import os
import redis.asyncio as aioredis

from discord.ext import commands
from locales import get_locale_message  # 완장의 기존 로케일 로더 함수 명칭에 맞게 자동 연동

class KyvoBaseCog(commands.Cog):
    """
    모든 Cog가 상속하는 공통 베이스 마스터 클래스.
    - i18n: 서버 설정 언어(guild_settings.language) 기반 get_msg
    - Cache-Aside: Redis 경유 guild_settings 조회 (DB 부하 최소화)
    - 캐시 무효화 헬퍼 탑재
    """

    def __init__(self, bot):
        self.bot = bot
        self.supabase = getattr(bot, "supabase", None)

        # Redis: bot에 이미 인스턴스가 있으면 재사용, 없으면 환경변수로 커넥션 생성
        if hasattr(bot, "redis"):
            self.redis = bot.redis
        else:
            redis_url = os.getenv("REDIS_URL")
            self.redis = aioredis.from_url(redis_url, decode_responses=True)

    # ══════════════════════════════════════════════════════════
    #  Cache-Aside 설정 조회 엔진 (자동 캐싱 및 분기 보호)
    # ══════════════════════════════════════════════════════════
    async def get_guild_settings(self, guild_id: int) -> dict:
        """Redis 캐시 우선 조회, 미스 시 Supabase 우회 후 재캐싱. 실패해도 빈 dict 반환"""
        key = f"guild:{guild_id}:settings"

        try:
            cached = await self.redis.get(key)
            if cached:
                return json.loads(cached)
        except Exception as e:
            print(f"[CACHE][WARN] Lookup failed, bypassing to DB: {type(e).__name__}")

        loop = asyncio.get_running_loop()
        try:
            res = await loop.run_in_executor(
                None,
                lambda: self.supabase.table("guild_settings")
                .select("*")
                .eq("guild_id", str(guild_id))
                .maybe_single()
                .execute()
            )
            settings = res.data or {}
        except Exception as e:
            print(f"[DB][ERROR] Failed to fetch guild settings (guild={guild_id}): {type(e).__name__}: {e}", flush=True)
            return {}

        ttl = 300 if settings else 60
        try:
            await self.redis.setex(key, ttl, json.dumps(settings, ensure_ascii=False))
        except Exception as e:
            print(f"[CACHE][WARN] Failed to re-cache settings: {type(e).__name__}")

        return settings

    async def invalidate_settings_cache(self, guild_id: int) -> bool:
        """해당 길드의 설정 캐시를 강제 삭제(폭파)한다."""
        key = f"guild:{guild_id}:settings"
        try:
            deleted = await self.redis.delete(key)
            print(f"[CACHE] Invalidation success: {key} ({deleted} keys removed)", flush=True)
            return True
        except Exception as e:
            print(f"[CACHE][ERROR] Invalidation failed ({key}): {type(e).__name__}")
            return False

    # ══════════════════════════════════════════════════════════
    #  금지어 필터 (automod 채팅 필터 / 익명 제보 필터가 공유하는 단일 소스)
    # ══════════════════════════════════════════════════════════
    async def get_forbidden_words(self, guild_id: int) -> list[str]:
        """settings.automod_settings.forbidden_words를 읽어온다. 모든 Cog가 이 하나의 소스만
        본다 - 채팅 필터와 익명 제보 필터가 서로 다른 리스트를 보는 일이 없게 한다."""
        row = await self.get_guild_settings(guild_id)
        nested_settings = row.get("settings") or {}
        automod_settings = nested_settings.get("automod_settings") or {}
        words = automod_settings.get("forbidden_words")
        return words if isinstance(words, list) else []

    async def check_forbidden_words(self, guild_id: int, content: str) -> str | None:
        """content에 금지어가 하나라도 포함되면 그 단어를, 없으면 None을 반환한다."""
        for word in await self.get_forbidden_words(guild_id):
            if word and word in content:
                return word
        return None

    # ══════════════════════════════════════════════════════════
    #  통합 다국어 메시지 치환 로더 (get_msg)
    # ══════════════════════════════════════════════════════════
    async def get_msg(self, guild_id: int, key: str, **kwargs) -> str:
        """서버 설정 언어에 맞는 메시지를 반환한다. 언어는 캐시된 설정에서 읽어 DB 부하를 차단."""
        settings = await self.get_guild_settings(guild_id)
        lang = settings.get("language", "en")

        template = get_locale_message(lang, key)
        if kwargs:
            try:
                return template.format(**kwargs)
            except (KeyError, IndexError) as e:
                print(f"[LOCALES][WARN] '{key}' format failed: {type(e).__name__}")
                return template
        return template