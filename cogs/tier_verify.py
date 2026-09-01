import time
import uuid
import asyncio
import os
from collections import deque
from urllib.parse import quote
from datetime import datetime, timezone

import discord
from discord import app_commands
import aiohttp

from cogs.base import KyvoBaseCog

# 🛡️ [Fail-Fast] 이 코그는 RIOT_API_KEY 없이는 아무 것도 할 수 없다. 조용히 죽거나 매 호출마다
# "키 없음" 에러를 반복하는 대신, 로드 시점에 즉시 예외를 던져서 main.py의 기존 per-extension
# try/except([CRITICAL LAYER ERROR] 로그)에 걸리게 한다 - 이 코그만 로드 실패하고 다른 코그는
# 정상 작동한다 (party/giveaway/reaction_roles와 동일한 격리 철학).
RIOT_API_KEY = os.environ.get("RIOT_API_KEY")
if not RIOT_API_KEY:
    raise RuntimeError(
        "[TIER_VERIFY] RIOT_API_KEY environment variable is not set. "
        "Get a Personal API Key from the Riot Developer Portal and set it before starting the bot."
    )

# ══════════════════════════════════════════════════════════
#  Riot 플랫폼 라우팅(길드 설정값) <-> 지역 라우팅(Account-v1 호출용) 매핑
# ══════════════════════════════════════════════════════════
PLATFORM_TO_REGIONAL = {
    "na1": "americas", "br1": "americas", "la1": "americas", "la2": "americas", "oc1": "americas",
    "kr": "asia", "jp1": "asia",
    "euw1": "europe", "eun1": "europe", "tr1": "europe", "ru": "europe",
}
PLATFORM_CHOICES = list(PLATFORM_TO_REGIONAL.keys())

# ══════════════════════════════════════════════════════════
#  Rate Limit - Personal Key 기준 1초/20회 + 2분/100회를 하나의 Lua 스크립트로 원자적 검사.
#  automod의 SLIDING_WINDOW_LUA와 같은 ZSET 슬라이딩 윈도우 정신이지만, 이건 (guild, user)별이
#  아니라 API 키 하나에 걸리는 "봇 전체 공용" 리밋이라 스코프가 다르다. 두 윈도우 중 하나라도
#  꽉 차 있으면 아예 슬롯을 예약하지 않는다(호출하지도 않을 요청 자리를 카운트에 넣지 않기 위함).
# ══════════════════════════════════════════════════════════
RIOT_DUAL_WINDOW_LUA = """
local short_key = KEYS[1]
local long_key = KEYS[2]
local now = tonumber(ARGV[1])
local short_window = tonumber(ARGV[2])
local short_limit = tonumber(ARGV[3])
local long_window = tonumber(ARGV[4])
local long_limit = tonumber(ARGV[5])
local member = ARGV[6]

redis.call('ZREMRANGEBYSCORE', short_key, 0, now - short_window)
redis.call('ZREMRANGEBYSCORE', long_key, 0, now - long_window)

local short_count = redis.call('ZCARD', short_key)
local long_count = redis.call('ZCARD', long_key)

if short_count >= short_limit or long_count >= long_limit then
    return 0
end

redis.call('ZADD', short_key, now, member)
redis.call('ZADD', long_key, now, member)
redis.call('PEXPIRE', short_key, short_window + 1000)
redis.call('PEXPIRE', long_key, long_window + 1000)

return 1
"""

RIOT_RL_SHORT_KEY = "riot:ratelimit:short"
RIOT_RL_LONG_KEY = "riot:ratelimit:long"
RIOT_SHORT_WINDOW_MS = 1000
RIOT_SHORT_LIMIT = 20
RIOT_LONG_WINDOW_MS = 120_000
RIOT_LONG_LIMIT = 100
RIOT_SLOT_WAIT_TIMEOUT_SECONDS = 3.0
RIOT_HTTP_TIMEOUT_SECONDS = 8.0
RIOT_MAX_RETRIES = 2
RIOT_MAX_RETRY_AFTER_WAIT_SECONDS = 5.0


def normalize_tier(raw_tier: str) -> str:
    """Riot League-v4 응답의 tier 문자열(예: 'GOLD', 'GRANDMASTER')을 party_tier_roles/
    party.TIER_CHOICES 표기('Gold', 'Grandmaster')로 정규화한다. Riot의 공식 tier enum은
    항상 공백 없는 단일 대문자 단어이므로 capitalize()만으로 TIER_CHOICES와 정확히 일치한다."""
    return raw_tier.capitalize()


# ══════════════════════════════════════════════════════════
#  Riot API 에러 taxonomy - 절대 조용히 삼키지 않고, 상황별로 분기해서 유저에게 명확히 알린다.
# ══════════════════════════════════════════════════════════
class RiotAPIError(Exception):
    pass


class RiotNotFoundError(RiotAPIError):
    pass


class RiotRateLimitedError(RiotAPIError):
    def __init__(self, retry_after: float | None = None):
        super().__init__(f"Rate limited (retry_after={retry_after})")
        self.retry_after = retry_after


class RiotAuthError(RiotAPIError):
    def __init__(self, status: int):
        super().__init__(f"Auth error (status={status})")
        self.status = status


class RiotServerError(RiotAPIError):
    def __init__(self, status: int):
        super().__init__(f"Server error (status={status})")
        self.status = status


class RiotTimeoutError(RiotAPIError):
    pass


class RiotVerifyModal(discord.ui.Modal):
    def __init__(self, cog: "KyvoTierVerify", title: str, gamename_label: str, tagline_label: str):
        super().__init__(title=title[:45])
        self.cog = cog
        self.gamename_input = discord.ui.TextInput(label=gamename_label[:45], max_length=20, required=True)
        self.tagline_input = discord.ui.TextInput(label=tagline_label[:45], max_length=10, required=True)
        self.add_item(self.gamename_input)
        self.add_item(self.tagline_input)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.handle_verify_submit(interaction, self.gamename_input.value, self.tagline_input.value)


class KyvoTierVerify(KyvoBaseCog):
    def __init__(self, bot):
        super().__init__(bot)
        self.dual_window_script = self.redis.register_script(RIOT_DUAL_WINDOW_LUA)
        # Redis 장애 시에만 쓰는 로컬 폴백 (automod/leveling과 동일한 정신) - 단일 프로세스 가정.
        self.local_short_calls: deque = deque()
        self.local_long_calls: deque = deque()

    async def _db_call(self, fn):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.bot.db_executor, fn)

    # ══════════════════════════════════════════════════════════
    #  Rate Limit 슬롯 확보
    # ══════════════════════════════════════════════════════════
    async def _acquire_riot_slot(self) -> bool:
        now_ms = time.time() * 1000
        member = f"{now_ms}-{uuid.uuid4().hex}"
        try:
            result = await self.dual_window_script(
                keys=[RIOT_RL_SHORT_KEY, RIOT_RL_LONG_KEY],
                args=[now_ms, RIOT_SHORT_WINDOW_MS, RIOT_SHORT_LIMIT, RIOT_LONG_WINDOW_MS, RIOT_LONG_LIMIT, member],
            )
            return bool(int(result))
        except Exception as e:
            print(f"[TIER_VERIFY][WARN] Redis rate-limit check failed, falling back to local: {type(e).__name__}: {e}", flush=True)
            return self._acquire_riot_slot_local()

    def _acquire_riot_slot_local(self) -> bool:
        now = time.monotonic()
        short_cutoff = now - (RIOT_SHORT_WINDOW_MS / 1000)
        long_cutoff = now - (RIOT_LONG_WINDOW_MS / 1000)
        while self.local_short_calls and self.local_short_calls[0] < short_cutoff:
            self.local_short_calls.popleft()
        while self.local_long_calls and self.local_long_calls[0] < long_cutoff:
            self.local_long_calls.popleft()
        if len(self.local_short_calls) >= RIOT_SHORT_LIMIT or len(self.local_long_calls) >= RIOT_LONG_LIMIT:
            return False
        self.local_short_calls.append(now)
        self.local_long_calls.append(now)
        return True

    async def _wait_for_riot_slot(self) -> bool:
        deadline = time.monotonic() + RIOT_SLOT_WAIT_TIMEOUT_SECONDS
        while True:
            if await self._acquire_riot_slot():
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.1)

    # ══════════════════════════════════════════════════════════
    #  Riot API 호출 - 상태 코드별 명확한 예외 분기, 429는 Retry-After 존중해서 1회 재시도.
    # ══════════════════════════════════════════════════════════
    async def _riot_request(self, session: aiohttp.ClientSession, url: str, extra_headers: dict | None = None):
        headers = {"X-Riot-Token": RIOT_API_KEY}
        if extra_headers:
            headers.update(extra_headers)

        for attempt in range(RIOT_MAX_RETRIES):
            if not await self._wait_for_riot_slot():
                raise RiotRateLimitedError()

            try:
                async with session.get(
                    url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=RIOT_HTTP_TIMEOUT_SECONDS),
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    if resp.status == 404:
                        raise RiotNotFoundError()
                    if resp.status in (401, 403):
                        raise RiotAuthError(resp.status)
                    if resp.status == 429:
                        retry_after_header = resp.headers.get("Retry-After")
                        retry_after = float(retry_after_header) if retry_after_header else 1.0
                        if attempt < RIOT_MAX_RETRIES - 1:
                            wait_s = min(retry_after, RIOT_MAX_RETRY_AFTER_WAIT_SECONDS)
                            print(f"[TIER_VERIFY][WARN] Riot API 429, retrying after {wait_s}s (url={url})", flush=True)
                            await asyncio.sleep(wait_s)
                            continue
                        raise RiotRateLimitedError(retry_after)
                    if 500 <= resp.status < 600:
                        raise RiotServerError(resp.status)
                    raise RiotAPIError(f"Unexpected status {resp.status} from {url}")
            except asyncio.TimeoutError:
                raise RiotTimeoutError()
        raise RiotRateLimitedError()

    # ══════════════════════════════════════════════════════════
    #  /riot_region_set - 관리자 전용, 길드의 LoL 플랫폼 리전 설정 (guild_settings JSON)
    # ══════════════════════════════════════════════════════════
    @app_commands.command(name="riot_region_set", description="Set this server's League of Legends region for account verification.")
    @app_commands.describe(region="The LoL platform region this server's players play on.")
    @app_commands.choices(region=[app_commands.Choice(name=r, value=r) for r in PLATFORM_CHOICES])
    @app_commands.default_permissions(administrator=True)
    async def riot_region_set(self, interaction: discord.Interaction, region: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id

        row = await self.get_guild_settings(guild_id)
        nested_settings = row.get("settings") or {}
        riot_set = nested_settings.get("riot_settings") or {}
        riot_set["platform_region"] = region.value
        nested_settings["riot_settings"] = riot_set

        await self.bot.bulk_update_guild_settings(guild_id, nested_settings)
        await self.invalidate_settings_cache(guild_id)

        msg = await self.get_msg(guild_id, "riot_region_set_success", region=region.value)
        await interaction.followup.send(msg, ephemeral=True)

    async def _get_platform_region(self, guild_id: int) -> str | None:
        row = await self.get_guild_settings(guild_id)
        nested_settings = row.get("settings") or {}
        riot_set = nested_settings.get("riot_settings") or {}
        return riot_set.get("platform_region")

    # ══════════════════════════════════════════════════════════
    #  /tier_verify
    # ══════════════════════════════════════════════════════════
    @app_commands.command(name="tier_verify", description="Verify your League of Legends account and get a rank-based role (requires /riot_region_set first).")
    async def tier_verify(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        title = await self.get_msg(guild_id, "tier_verify_modal_title")
        gamename_label = await self.get_msg(guild_id, "tier_verify_modal_gamename_label")
        tagline_label = await self.get_msg(guild_id, "tier_verify_modal_tagline_label")
        modal = RiotVerifyModal(self, title, gamename_label, tagline_label)
        await interaction.response.send_modal(modal)

    async def handle_verify_submit(self, interaction: discord.Interaction, game_name: str, tag_line: str) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id

        game_name = game_name.strip()
        tag_line = tag_line.strip().lstrip("#").strip()
        if not game_name or not tag_line:
            msg = await self.get_msg(guild_id, "tier_verify_err_invalid_input")
            await interaction.followup.send(msg, ephemeral=True)
            return

        platform_region = await self._get_platform_region(guild_id)
        if not platform_region:
            msg = await self.get_msg(guild_id, "tier_verify_err_region_not_set")
            await interaction.followup.send(msg, ephemeral=True)
            return

        regional_route = PLATFORM_TO_REGIONAL.get(platform_region)
        if regional_route is None:
            # 설정값이 예전 버전/수동 조작 등으로 알 수 없는 값이 된 경우 - 조용히 넘어가지 않는다.
            print(f"[TIER_VERIFY][ERROR] Unknown platform_region '{platform_region}' for guild={guild_id}", flush=True)
            msg = await self.get_msg(guild_id, "tier_verify_err_region_not_set")
            await interaction.followup.send(msg, ephemeral=True)
            return

        account_url = (
            f"https://{regional_route}.api.riotgames.com/riot/account/v1/accounts/"
            f"by-riot-id/{quote(game_name)}/{quote(tag_line)}"
        )

        async with aiohttp.ClientSession() as session:
            try:
                account_data = await self._riot_request(session, account_url)
            except RiotNotFoundError:
                msg = await self.get_msg(guild_id, "tier_verify_err_account_not_found", gameName=game_name, tagLine=tag_line)
                await interaction.followup.send(msg, ephemeral=True)
                return
            except RiotAuthError as e:
                print(f"[TIER_VERIFY][CRITICAL] Riot API auth failure (status={e.status}) - "
                      f"RIOT_API_KEY may be invalid/expired (guild={guild_id})", flush=True)
                msg = await self.get_msg(guild_id, "tier_verify_err_auth")
                await interaction.followup.send(msg, ephemeral=True)
                return
            except RiotRateLimitedError:
                print(f"[TIER_VERIFY][WARN] Rate limited during account lookup (guild={guild_id}, user={interaction.user.id})", flush=True)
                msg = await self.get_msg(guild_id, "tier_verify_err_rate_limited")
                await interaction.followup.send(msg, ephemeral=True)
                return
            except RiotServerError as e:
                print(f"[TIER_VERIFY][WARN] Riot server error during account lookup (status={e.status})", flush=True)
                msg = await self.get_msg(guild_id, "tier_verify_err_server_error")
                await interaction.followup.send(msg, ephemeral=True)
                return
            except RiotTimeoutError:
                print(f"[TIER_VERIFY][WARN] Timeout during account lookup (guild={guild_id})", flush=True)
                msg = await self.get_msg(guild_id, "tier_verify_err_timeout")
                await interaction.followup.send(msg, ephemeral=True)
                return
            except Exception as e:
                print(f"[TIER_VERIFY][ERROR] Unexpected error during account lookup: {type(e).__name__}: {e}", flush=True)
                await interaction.followup.send("❌ An unexpected error occurred.", ephemeral=True)
                return

            puuid = account_data.get("puuid")
            if not puuid:
                print(f"[TIER_VERIFY][ERROR] Account lookup succeeded but response had no puuid (guild={guild_id}): {account_data}", flush=True)
                await interaction.followup.send("❌ An unexpected error occurred.", ephemeral=True)
                return

            league_url = f"https://{platform_region}.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}"
            try:
                entries = await self._riot_request(session, league_url)
            except RiotAuthError as e:
                print(f"[TIER_VERIFY][CRITICAL] Riot API auth failure (status={e.status}) during league lookup "
                      f"(guild={guild_id})", flush=True)
                msg = await self.get_msg(guild_id, "tier_verify_err_auth")
                await interaction.followup.send(msg, ephemeral=True)
                return
            except RiotRateLimitedError:
                print(f"[TIER_VERIFY][WARN] Rate limited during league lookup (guild={guild_id}, user={interaction.user.id})", flush=True)
                msg = await self.get_msg(guild_id, "tier_verify_err_rate_limited")
                await interaction.followup.send(msg, ephemeral=True)
                return
            except RiotServerError as e:
                print(f"[TIER_VERIFY][WARN] Riot server error during league lookup (status={e.status})", flush=True)
                msg = await self.get_msg(guild_id, "tier_verify_err_server_error")
                await interaction.followup.send(msg, ephemeral=True)
                return
            except RiotTimeoutError:
                print(f"[TIER_VERIFY][WARN] Timeout during league lookup (guild={guild_id})", flush=True)
                msg = await self.get_msg(guild_id, "tier_verify_err_timeout")
                await interaction.followup.send(msg, ephemeral=True)
                return
            except RiotNotFoundError:
                entries = []  # by-puuid는 언랭이면 보통 빈 배열이지만 혹시 몰라 안전하게 처리
            except Exception as e:
                print(f"[TIER_VERIFY][ERROR] Unexpected error during league lookup: {type(e).__name__}: {e}", flush=True)
                await interaction.followup.send("❌ An unexpected error occurred.", ephemeral=True)
                return

        solo_entry = next((e for e in (entries or []) if e.get("queueType") == "RANKED_SOLO_5x5"), None)
        tier = normalize_tier(solo_entry["tier"]) if solo_entry else None
        rank = solo_entry.get("rank", "") if solo_entry else None
        lp = solo_entry.get("leaguePoints", 0) if solo_entry else None

        try:
            await self._db_call(
                lambda: self.bot.supabase.table("riot_verifications").upsert({
                    "guild_id": str(guild_id), "user_id": str(interaction.user.id),
                    "game_name": game_name, "tag_line": tag_line, "puuid": puuid,
                    "tier": tier, "rank": rank, "league_points": lp,
                    "verified_at": datetime.now(timezone.utc).isoformat(),
                }, on_conflict="guild_id,user_id").execute()
            )
        except Exception as e:
            print(f"[TIER_VERIFY][ERROR] Failed to save verification record (guild={guild_id}, user={interaction.user.id}): "
                  f"{type(e).__name__}: {e}", flush=True)

        if solo_entry is None:
            msg = await self.get_msg(guild_id, "tier_verify_err_unranked", gameName=game_name, tagLine=tag_line)
            await interaction.followup.send(msg, ephemeral=True)
            return

        status, role = await self._apply_tier_role(interaction, guild_id, tier)
        lp_str = str(lp) if lp is not None else "0"
        rank_str = rank or ""

        if status == "success":
            msg = await self.get_msg(guild_id, "tier_verify_success", gameName=game_name, tagLine=tag_line,
                                      tier=tier, rank=rank_str, lp=lp_str, role=role.name)
        elif status == "hierarchy_fail":
            msg = await self.get_msg(guild_id, "party_tier_err_hierarchy")
        else:
            msg = await self.get_msg(guild_id, "tier_verify_success_no_role_mapped", gameName=game_name, tagLine=tag_line,
                                      tier=tier, rank=rank_str, lp=lp_str)
        await interaction.followup.send(msg, ephemeral=True)

    # ══════════════════════════════════════════════════════════
    #  party_tier_roles의 배타적 스왑 로직 재사용 (party.py tier_set과 동일한 흐름을 복제).
    #  반환: ("success", role) / ("hierarchy_fail", None) / ("not_configured", None)
    # ══════════════════════════════════════════════════════════
    async def _apply_tier_role(self, interaction: discord.Interaction, guild_id: int, tier: str) -> tuple[str, discord.Role | None]:
        try:
            res = await self._db_call(
                lambda: self.bot.supabase.table("party_tier_roles").select("*").eq("guild_id", str(guild_id)).execute()
            )
            all_rows = res.data or []
        except Exception as e:
            print(f"[TIER_VERIFY][ERROR] Failed to fetch tier role mappings (guild={guild_id}): {type(e).__name__}: {e}", flush=True)
            return "not_configured", None

        target_row = next((r for r in all_rows if r["tier"] == tier), None)
        if target_row is None:
            return "not_configured", None

        new_role = interaction.guild.get_role(int(target_row["role_id"]))
        if new_role is None:
            print(f"[TIER_VERIFY][ERROR] Tier role {target_row['role_id']} for tier '{tier}' no longer exists "
                  f"(guild={guild_id})", flush=True)
            return "not_configured", None

        # 🛡️ TOCTOU 재확인: 매핑을 만든 뒤 봇 권한/위계가 바뀌었을 수 있다.
        bot_member = interaction.guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_roles or bot_member.top_role <= new_role:
            print(f"[TIER_VERIFY][ERROR] Cannot assign tier role '{new_role.name}' - permission/hierarchy issue "
                  f"(guild={guild_id})", flush=True)
            return "hierarchy_fail", None

        other_tier_role_ids = {r["role_id"] for r in all_rows if r["tier"] != tier}
        roles_to_remove = [r for r in interaction.user.roles if str(r.id) in other_tier_role_ids]

        if roles_to_remove:
            try:
                await interaction.user.remove_roles(*roles_to_remove, reason="[KYVO TIER VERIFY] replaced by verified tier")
            except discord.Forbidden:
                print(f"[TIER_VERIFY][ERROR] Forbidden while removing old tier roles from user={interaction.user.id} "
                      f"(guild={guild_id})", flush=True)
            except discord.HTTPException as e:
                print(f"[TIER_VERIFY][ERROR] HTTPException while removing old tier roles: {type(e).__name__}: {e}", flush=True)

        if new_role not in interaction.user.roles:
            try:
                await interaction.user.add_roles(new_role, reason="[KYVO TIER VERIFY]")
            except discord.Forbidden:
                print(f"[TIER_VERIFY][ERROR] Forbidden while granting tier role '{new_role.name}' to "
                      f"user={interaction.user.id} (guild={guild_id})", flush=True)
                return "hierarchy_fail", None
            except discord.HTTPException as e:
                print(f"[TIER_VERIFY][ERROR] HTTPException while granting tier role: {type(e).__name__}: {e}", flush=True)
                return "hierarchy_fail", None

        return "success", new_role


async def setup(bot):
    cog = KyvoTierVerify(bot)
    await bot.add_cog(cog)
    print("[⚡ TIER_VERIFY] Cog extension setup complete.", flush=True)
