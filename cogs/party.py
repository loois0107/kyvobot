import discord
from discord import app_commands
from discord.ext import commands, tasks
from cogs.base import KyvoBaseCog
import asyncio
import os
import hmac
from datetime import datetime, timezone, timedelta
from aiohttp import web

# 🛡️ 대시보드 tier-roles 일괄 편집기가 재매핑 시 "이전 역할 보유자 정리"를 이 봇에게 위임할 때
# 쓰는 내부 전용 시크릿. 없어도 party 코그 자체는 정상 동작한다(슬래시 커맨드 쪽 정리는 이 시크릿과
# 무관하게 항상 작동) - 대시보드發 정리 요청 라우트만 등록을 건너뛴다.
INTERNAL_API_SECRET = os.environ.get("INTERNAL_API_SECRET")

PARTY_MIN_NEEDED_COUNT = 2
PARTY_MAX_NEEDED_COUNT = 10
# 대시보드에서 party_settings를 설정 안 한 길드를 위한 기본값(폴백) - 상수 자체는 안 지운다.
PARTY_CARD_LIFETIME_MINUTES = 60
PARTY_CHANNEL_LIFETIME_HOURS = 6
PARTY_CHECK_INTERVAL_SECONDS = 30

# 대시보드 party-settings 페이지가 저장을 거부해야 하는 범위 - giveaway.py의
# GIVEAWAY_MIN/MAX_DURATION_MINUTES와 동일한 정신(너무 짧으면 기능이 무의미, 너무 길면 좀비 방치).
PARTY_CARD_LIFETIME_MIN_MINUTES = 5
PARTY_CARD_LIFETIME_MAX_MINUTES = 1440  # 24시간
PARTY_CHANNEL_LIFETIME_MIN_HOURS = 1
PARTY_CHANNEL_LIFETIME_MAX_HOURS = 48

# 참여 버튼의 custom_id - 재시작 후에도 discord.py가 이 문자열만으로 콜백을 다시 찾아
# 연결할 수 있어야 하므로 고정 문자열이어야 한다 (recruitment_id는 여기 넣지 않는다 -
# 아래 PartyCardView docstring 참고, giveaway/anonymous_reports와 동일한 이유).
PARTY_JOIN_CUSTOM_ID = "kyvo_party:join"

# 라이엇 API 연동 전까지 쓰는 자기신고 티어 목록 (LoL 랭크 체계)
TIER_CHOICES = [
    "Iron", "Bronze", "Silver", "Gold", "Platinum",
    "Emerald", "Diamond", "Master", "Grandmaster", "Challenger",
]

# 🛡️ 부여했을 때 2차 확인을 받아야 하는 위험 권한 - custom_commands.py/reaction_roles.py와 동일한
# 목록/정신. 티어 역할도 결국 "역할을 부여 가능하게 만드는" 관리자 명령어라 같은 위협 모델이다.
DANGEROUS_ROLE_PERMISSIONS = (
    "manage_roles", "manage_guild", "manage_channels",
    "ban_members", "kick_members", "manage_webhooks", "manage_messages",
)


def _get_dangerous_permissions(role: discord.Role) -> list[str]:
    perms = role.permissions
    return [name for name in DANGEROUS_ROLE_PERMISSIONS if getattr(perms, name, False)]


def clean_hex_color(hex_str, fallback: str) -> str:
    """leveling.py의 동명 함수와 동일한 안전 파싱 - 형식이 이상하면(길이가 안 맞는 등) 조용히
    폴백값을 쓴다. 여기서도 복제해서 쓰는 이유는 각 코그가 자기 완결적이어야 한다는 이번 세션의
    관례를 따르기 위함이다."""
    if not hex_str:
        return fallback
    clean = str(hex_str).strip()
    if not clean.startswith('#'):
        clean = f"#{clean}"
    return clean if len(clean) in (4, 7, 9) else fallback


def resolve_party_settings(party_settings: dict | None) -> dict:
    """대시보드가 저장한 party_settings를 안전하게 정규화한다 - 값이 없거나(미설정 길드),
    형식이 이상해도(수동 DB 편집 등) 항상 안전한 기본값으로 폴백하고 절대 크래시하지 않는다.
    범위를 벗어난 숫자값도 여기서 조용히 기본값으로 되돌린다 - 실제 저장 시점 검증(대시보드
    API)이 이 범위를 이미 강제하므로, 여기서 걸리는 건 그 검증을 우회한 비정상 데이터뿐이다."""
    party_settings = party_settings or {}

    card_color = clean_hex_color(party_settings.get("card_color"), "#5865F2")
    card_description = str(party_settings.get("card_description") or "").strip()

    try:
        card_lifetime_minutes = int(party_settings.get("card_lifetime_minutes"))
    except (TypeError, ValueError):
        card_lifetime_minutes = PARTY_CARD_LIFETIME_MINUTES
    if not (PARTY_CARD_LIFETIME_MIN_MINUTES <= card_lifetime_minutes <= PARTY_CARD_LIFETIME_MAX_MINUTES):
        card_lifetime_minutes = PARTY_CARD_LIFETIME_MINUTES

    try:
        channel_lifetime_hours = int(party_settings.get("channel_lifetime_hours"))
    except (TypeError, ValueError):
        channel_lifetime_hours = PARTY_CHANNEL_LIFETIME_HOURS
    if not (PARTY_CHANNEL_LIFETIME_MIN_HOURS <= channel_lifetime_hours <= PARTY_CHANNEL_LIFETIME_MAX_HOURS):
        channel_lifetime_hours = PARTY_CHANNEL_LIFETIME_HOURS

    game_name = str(party_settings.get("game_name") or "").strip()[:256]  # 디스코드 author name 256자 제한
    card_thumbnail_url = clean_thumbnail_url(party_settings.get("card_thumbnail_url"))

    return {
        "card_color": card_color,
        "card_description": card_description,
        "card_lifetime_minutes": card_lifetime_minutes,
        "channel_lifetime_hours": channel_lifetime_hours,
        "game_name": game_name,
        "card_thumbnail_url": card_thumbnail_url,
    }


def clean_thumbnail_url(raw) -> str:
    """형식이 http(s)로 시작하지 않으면(수동 DB 편집, javascript: 등) 조용히 빈 문자열로 되돌린다 -
    resolve_party_settings와 게임 프리셋 둘 다 이 검증을 공유한다."""
    url = str(raw or "").strip()
    if not url.startswith(("http://", "https://")):
        return ""
    return url


def select_party_card_design(base_party_settings: dict, preset_row: dict | None) -> dict:
    """/party_recruit에서 게임 프리셋을 골랐으면(row.selected_game이 있고 실제로 조회에 성공하면)
    그 프리셋의 디자인 필드를 쓰고, 아니면(선택 안 함/프리셋이 그 사이 삭제/수정됨) 길드 기본값
    (base_party_settings)으로 안전하게 폴백한다. 타이머(card_lifetime_minutes 등)는 프리셋과
    무관하게 항상 길드 공통값이라 이 함수의 반환값에 포함하지 않는다."""
    if preset_row is None:
        return {
            "card_color": base_party_settings["card_color"],
            "card_description": base_party_settings["card_description"],
            "card_thumbnail_url": base_party_settings["card_thumbnail_url"],
            "game_name": base_party_settings["game_name"],
        }

    return {
        "card_color": clean_hex_color(preset_row.get("card_color"), base_party_settings["card_color"]),
        "card_description": str(preset_row.get("card_description") or "").strip(),
        "card_thumbnail_url": clean_thumbnail_url(preset_row.get("card_thumbnail_url")),
        "game_name": str(preset_row.get("game_name") or "").strip()[:256],
    }


PARTY_MIN_TIER_ANY_VALUE = "Any"  # /party_recruit의 min_tier 드롭다운 전용 선택지 - TIER_CHOICES엔 없음
PARTY_LOOKING_FOR_ROLE_MAX_LENGTH = 100  # lanes 필드와 동일한 자유 텍스트 길이 상한


def normalize_min_tier(min_tier: str | None) -> str | None:
    """"Any"나 빈 값, TIER_CHOICES에 없는 값(수동 DB 편집 등)은 전부 None(조건 없음)으로
    정규화한다 - 강제 검증은 안 하지만, 나중에 실제 검증을 얹을 때 riot_verifications.tier와
    바로 비교 가능하도록 저장값 자체는 TIER_CHOICES와 정확히 일치하는 문자열이거나 None만
    허용한다."""
    if not min_tier:
        return None
    cleaned = str(min_tier).strip()
    if cleaned == PARTY_MIN_TIER_ANY_VALUE or cleaned not in TIER_CHOICES:
        return None
    return cleaned


def resolve_looking_for_line(min_tier: str | None, looking_for_role: str | None) -> str:
    """카드에 보여줄 "Looking For" 한 줄을 만든다. 둘 다 없으면 빈 문자열을 반환하고,
    호출부는 이걸 "필드 자체를 생략하라"는 신호로 쓴다. looking_for_role은 여전히 정보 표시일
    뿐이지만, min_tier는 handle_join에서 실제로 강제 검증된다(meets_min_tier_requirement 참고)."""
    parts = []
    tier = normalize_min_tier(min_tier)
    if tier:
        parts.append(f"{tier}+")
    role = str(looking_for_role or "").strip()[:PARTY_LOOKING_FOR_ROLE_MAX_LENGTH]
    if role:
        parts.append(role)
    return " · ".join(parts)


def _tier_rank(tier: str | None) -> int | None:
    """TIER_CHOICES 상의 서수 위치를 반환한다. 리스트에 없거나(미인증/손상 데이터) None이면
    None을 반환한다 - 절대 크래시하지 않는다."""
    if not tier:
        return None
    try:
        return TIER_CHOICES.index(tier)
    except ValueError:
        return None


def meets_min_tier_requirement(min_tier: str | None, verified_tier: str | None) -> bool:
    """min_tier가 없으면(무관 모집) 항상 True. 있으면 verified_tier의 서수가 min_tier 이상이어야
    True - verified_tier가 없거나(미인증) TIER_CHOICES에 없는 손상 값이면 조건 미충족으로 취급."""
    required_rank = _tier_rank(min_tier)
    if required_rank is None:
        return True

    actual_rank = _tier_rank(verified_tier)
    if actual_rank is None:
        return False

    return actual_rank >= required_rank


def reorder_favorite_first(names: list[str], favorite: str | None) -> list[str]:
    """자동완성 후보 목록에서 즐겨찾기를 맨 앞으로 올린다. 즐겨찾기가 없거나(None) 지금 후보
    목록에 없으면(현재 입력과 안 맞거나, 관리자가 그 사이 프리셋을 지웠으면) 원래 순서 그대로."""
    if not favorite or favorite not in names:
        return names
    return [favorite] + [n for n in names if n != favorite]


def resolve_effective_game(explicit_game: str | None, favorite_game: str | None) -> str | None:
    """game 파라미터를 생략했을 때(explicit_game이 falsy)만 즐겨찾기로 대체한다. 유저가 명시적으로
    고른 값은 절대 덮어쓰지 않는다."""
    return explicit_game if explicit_game else favorite_game


class RoleWarningConfirmView(discord.ui.View):
    """위험 권한 역할을 티어 역할로 등록하기 전 마지막 확인. custom_commands.py/reaction_roles.py의
    동명 View와 동일한 패턴(각 cog가 자기 완결적이도록 복제)."""

    def __init__(self, author_id: int, confirm_label: str, cancel_label: str):
        super().__init__(timeout=60)
        self.author_id = author_id
        self.confirmed: bool | None = None

        confirm_btn = discord.ui.Button(label=confirm_label, style=discord.ButtonStyle.danger)
        confirm_btn.callback = self._on_confirm
        self.add_item(confirm_btn)

        cancel_btn = discord.ui.Button(label=cancel_label, style=discord.ButtonStyle.secondary)
        cancel_btn.callback = self._on_cancel
        self.add_item(cancel_btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ This confirmation is not for you.", ephemeral=True)
            return False
        return True

    def _disable_all(self):
        for item in self.children:
            item.disabled = True

    async def _on_confirm(self, interaction: discord.Interaction):
        self.confirmed = True
        self._disable_all()
        await interaction.response.edit_message(view=self)
        self.stop()

    async def _on_cancel(self, interaction: discord.Interaction):
        self.confirmed = False
        self._disable_all()
        await interaction.response.edit_message(view=self)
        self.stop()

    async def on_timeout(self):
        self._disable_all()


class PartyRecruitmentModal(discord.ui.Modal):
    """/party_recruit의 입력 모달. 디스코드 Modal은 TextInput만 지원하고 Select는 못 넣으므로
    라인 선택도 자유 텍스트로 받는다 (다중선택 UI가 꼭 필요해지면 모달 뒤에 별도 Select 단계를
    추가하는 확장 경로로 남겨둔다)."""

    def __init__(self, cog: "KyvoParty", title: str, queue_label: str, lanes_label: str, count_label: str,
                 looking_for_role_label: str, selected_game: str | None = None, min_tier: str | None = None):
        super().__init__(title=title[:45])
        self.cog = cog
        self.selected_game = selected_game
        self.min_tier = min_tier
        self.queue_input = discord.ui.TextInput(label=queue_label[:45], max_length=50, required=True)
        self.lanes_input = discord.ui.TextInput(label=lanes_label[:45], max_length=100, required=False)
        self.count_input = discord.ui.TextInput(label=count_label[:45], max_length=3, required=True)
        self.looking_for_role_input = discord.ui.TextInput(
            label=looking_for_role_label[:45], max_length=PARTY_LOOKING_FOR_ROLE_MAX_LENGTH, required=False,
        )
        self.add_item(self.queue_input)
        self.add_item(self.lanes_input)
        self.add_item(self.count_input)
        self.add_item(self.looking_for_role_input)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.handle_recruitment_submit(interaction, self.queue_input.value, self.lanes_input.value,
                                                   self.count_input.value, self.selected_game,
                                                   self.min_tier, self.looking_for_role_input.value)


class PartyCardView(discord.ui.View):
    """모집 카드 메시지에 붙는 영구(Persistent) View.

    giveaway의 GiveawayEntryView와 완전히 같은 이유로 timeout=None + 고정 custom_id로 만든다 -
    모집 카드가 얼마나 오래 떠있을지 알 수 없고 그 사이 봇이 재배포될 수 있다. 등록된 이 View
    인스턴스 하나를 "모든" 모집 카드가 공유하므로, 어떤 모집인지는 self에 저장하지 않고 매
    클릭마다 interaction.message.id(=party_recruitments.message_id)로 DB에서 다시 찾는다.
    """

    def __init__(self, cog: "KyvoParty", join_label: str = "Join Party"):
        super().__init__(timeout=None)
        self.cog = cog

        btn = discord.ui.Button(label=join_label, style=discord.ButtonStyle.success,
                                 custom_id=PARTY_JOIN_CUSTOM_ID, emoji="🎮")
        btn.callback = self._on_join
        self.add_item(btn)

    async def _on_join(self, interaction: discord.Interaction):
        await self.cog.handle_join(interaction)


class KyvoParty(KyvoBaseCog):
    def __init__(self, bot):
        super().__init__(bot)
        self.check_party_timers.start()
        # 파티 음성 채널 추적 - voice.py의 Join to Create 메커니즘(track/untrack/schedule_deletion)을
        # 재사용하되, redis_key/tracked_dict는 별도 네임스페이스(kyvo:party_voice:*)로 분리한다.
        self.tracked_party_voice_channels: dict[int, int] = {}
        self._voice_recovered = False

    async def cog_load(self):
        if INTERNAL_API_SECRET:
            self.bot.web_app.router.add_post("/internal/tier-roles/cleanup", self.handle_tier_cleanup_webhook)
            print("[⚡ PARTY] Internal tier-role cleanup route registered at /internal/tier-roles/cleanup.", flush=True)
        else:
            print("[PARTY][WARN] INTERNAL_API_SECRET not set - dashboard-triggered stale tier-role "
                  "cleanup is disabled (the /tier_role_set command's own cleanup still works).", flush=True)

    async def cog_unload(self):
        self.check_party_timers.cancel()

    # ══════════════════════════════════════════════════════════
    #  파티 음성 채널 수명주기 - voice.py의 Join to Create 메커니즘을 공유하되, 소유권은
    #  분리한다(redis_key = kyvo:party_voice:{guild_id}, 이 cog 자신의 tracked dict).
    # ══════════════════════════════════════════════════════════
    def _party_voice_redis_key(self, guild_id: int) -> str:
        return f"kyvo:party_voice:{guild_id}"

    def _get_voice_cog(self):
        voice_cog = self.bot.get_cog("KyvoVoice")
        if voice_cog is None:
            print("[PARTY][WARN] KyvoVoice cog not loaded - party voice channel tracking/cleanup unavailable", flush=True)
        return voice_cog

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if before.channel is None or before.channel.id not in self.tracked_party_voice_channels:
            return
        if len(before.channel.members) != 0:
            return
        voice_cog = self._get_voice_cog()
        if voice_cog is None:
            return
        guild_id = self.tracked_party_voice_channels.get(before.channel.id, before.channel.guild.id)
        asyncio.create_task(voice_cog.schedule_deletion(
            before.channel, self._party_voice_redis_key(guild_id), self.tracked_party_voice_channels,
            reason="[KYVO PARTY] empty voice channel cleanup",
        ))

    @commands.Cog.listener()
    async def on_ready(self):
        # on_ready는 재연결마다 다시 불릴 수 있어, 프로세스 생애주기당 한 번만 복구를 수행한다
        # (voice.py의 콜드 스타트 복구와 동일한 패턴, 네임스페이스만 분리).
        if self._voice_recovered:
            return
        self._voice_recovered = True

        voice_cog = self._get_voice_cog()
        if voice_cog is None:
            return

        for guild in self.bot.guilds:
            redis_key = self._party_voice_redis_key(guild.id)
            try:
                channel_ids = await self.redis.smembers(redis_key)
            except Exception as e:
                print(f"[PARTY][WARN] Redis SMEMBERS failed during party-voice cold-start recovery "
                      f"(guild={guild.id}): {type(e).__name__}: {e}", flush=True)
                continue

            for cid_str in channel_ids:
                try:
                    channel_id = int(cid_str)
                except (TypeError, ValueError):
                    continue

                channel = guild.get_channel(channel_id)
                if channel is None:
                    await voice_cog.untrack_channel(redis_key, guild.id, channel_id, self.tracked_party_voice_channels)
                    print(f"[PARTY][RECOVERY] Stale party-voice tracking entry for missing channel "
                          f"{channel_id} cleaned up (guild={guild.id})", flush=True)
                    continue

                self.tracked_party_voice_channels[channel_id] = guild.id
                print(f"[PARTY][RECOVERY] Reattached tracking for party voice channel {channel_id} "
                      f"(guild={guild.id}, members={len(channel.members)})", flush=True)

                if len(channel.members) == 0:
                    asyncio.create_task(voice_cog.schedule_deletion(
                        channel, redis_key, self.tracked_party_voice_channels,
                        reason="[KYVO PARTY] empty voice channel cleanup",
                    ))

    async def _db_call(self, fn):
        """supabase-py는 동기 클라이언트라 이벤트 루프를 막지 않도록 executor로 감싼다."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.bot.db_executor, fn)

    # ══════════════════════════════════════════════════════════
    #  재매핑 시 이전 역할 정리 - /tier_role_set과 대시보드(내부 웹훅) 둘 다 이 함수 하나를 공유한다.
    #  절대 응답을 블로킹하지 않도록 항상 asyncio.create_task로 백그라운드 실행하는 걸 전제로 짰다
    #  (멤버가 수백 명이면 개별 remove_roles 호출이 rate limit에 걸려 수 초~수십 초 걸릴 수 있음).
    #  개별 멤버 실패는 로그만 남기고 나머지 멤버 정리를 계속 진행한다.
    # ══════════════════════════════════════════════════════════
    async def _cleanup_stale_tier_role(self, guild: discord.Guild, old_role_id: int, tier: str) -> None:
        old_role = guild.get_role(old_role_id)
        if old_role is None:
            print(f"[PARTY][WARN] Stale '{tier}' role {old_role_id} no longer exists, nothing to clean up "
                  f"(guild={guild.id})", flush=True)
            return

        members_with_role = list(old_role.members)
        if not members_with_role:
            return

        print(f"[PARTY] Cleaning up stale '{tier}' role '{old_role.name}' from {len(members_with_role)} "
              f"member(s) (guild={guild.id})", flush=True)

        success_count = 0
        fail_count = 0
        for member in members_with_role:
            try:
                await member.remove_roles(old_role, reason=f"[KYVO TIER REMAP] '{tier}' tier role changed, removing stale assignment")
                success_count += 1
            except discord.Forbidden:
                fail_count += 1
                print(f"[PARTY][ERROR] Forbidden while removing stale '{tier}' role from user={member.id} "
                      f"(guild={guild.id})", flush=True)
            except discord.HTTPException as e:
                fail_count += 1
                print(f"[PARTY][ERROR] HTTPException while removing stale '{tier}' role from user={member.id}: "
                      f"{type(e).__name__}: {e} (guild={guild.id})", flush=True)

        print(f"[PARTY] Stale '{tier}' role cleanup complete (guild={guild.id}): "
              f"{success_count} removed, {fail_count} failed", flush=True)

    async def handle_tier_cleanup_webhook(self, request: web.Request) -> web.Response:
        secret_header = request.headers.get("X-Internal-Secret", "")
        if not secret_header or not hmac.compare_digest(secret_header, INTERNAL_API_SECRET):
            print("[PARTY][WARN] Rejected tier-role cleanup request with invalid/missing internal secret", flush=True)
            return web.Response(status=403)

        try:
            body = await request.json()
        except Exception:
            return web.Response(status=400)

        guild_id = body.get("guild_id")
        tier = body.get("tier")
        old_role_id = body.get("old_role_id")
        if not guild_id or not tier or not old_role_id:
            return web.Response(status=400, text="guild_id, tier, old_role_id are required")

        guild = self.bot.get_guild(int(guild_id))
        if guild is None:
            print(f"[PARTY][WARN] Tier-role cleanup requested for guild {guild_id} but bot isn't in it (or cache empty)", flush=True)
            return web.Response(status=404)

        asyncio.create_task(self._cleanup_stale_tier_role(guild, int(old_role_id), str(tier)))
        return web.Response(status=202)

    # ══════════════════════════════════════════════════════════
    #  게임별 디자인 프리셋 (party_game_presets) - 대시보드가 관리(추가/수정/삭제)하고,
    #  봇은 /party_recruit의 자동완성 + 카드 렌더링 시점 조회에만 쓴다. party_tier_roles와
    #  동일한 패턴: 길드당 여러 행, 자연키(guild_id, game_name)에 unique 제약.
    # ══════════════════════════════════════════════════════════
    async def _get_game_presets(self, guild_id: int) -> list[dict]:
        try:
            res = await self._db_call(
                lambda: self.bot.supabase.table("party_game_presets").select("*")
                        .eq("guild_id", str(guild_id)).execute()
            )
            return res.data or []
        except Exception as e:
            print(f"[PARTY][ERROR] Failed to fetch game presets (guild={guild_id}): {type(e).__name__}: {e}", flush=True)
            return []

    async def _get_game_preset(self, guild_id: int, game_name: str) -> dict | None:
        try:
            res = await self._db_call(
                lambda: self.bot.supabase.table("party_game_presets").select("*")
                        .eq("guild_id", str(guild_id)).eq("game_name", game_name).limit(1).execute()
            )
            return res.data[0] if res.data else None
        except Exception as e:
            print(f"[PARTY][ERROR] Failed to fetch game preset '{game_name}' (guild={guild_id}): "
                  f"{type(e).__name__}: {e}", flush=True)
            return None

    async def _game_preset_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        presets = await self._get_game_presets(interaction.guild_id)
        current_lower = current.lower()
        matches = [p["game_name"] for p in presets if current_lower in p["game_name"].lower()]
        favorite = await self._get_favorite_game(interaction.guild_id, interaction.user.id)
        matches = reorder_favorite_first(matches, favorite)
        return [app_commands.Choice(name=name, value=name) for name in matches[:25]]  # 디스코드 자동완성 응답 상한

    # ══════════════════════════════════════════════════════════
    #  즐겨찾기 게임 (user_party_preferences) - /profile 대시보드가 직접 관리한다(봇 웹훅 불필요 -
    #  단순 개인 설정 저장이라 party_tier_roles처럼 Discord 부작용이 없다). 봇은 조회만 한다:
    #  자동완성 순서 조정(위) + /party_recruit에서 game 파라미터 생략 시 대체(아래).
    # ══════════════════════════════════════════════════════════
    async def _get_favorite_game(self, guild_id: int, user_id: int) -> str | None:
        try:
            res = await self._db_call(
                lambda: self.bot.supabase.table("user_party_preferences").select("favorite_game_name")
                        .eq("guild_id", str(guild_id)).eq("user_id", str(user_id)).limit(1).execute()
            )
            return res.data[0]["favorite_game_name"] if res.data else None
        except Exception as e:
            print(f"[PARTY][ERROR] Failed to fetch favorite game (guild={guild_id}, user={user_id}): "
                  f"{type(e).__name__}: {e}", flush=True)
            return None

    # ══════════════════════════════════════════════════════════
    #  /party_recruit
    # ══════════════════════════════════════════════════════════
    @app_commands.command(name="party_recruit", description="Open a party recruitment post.")
    @app_commands.describe(
        game="Optional - pick a saved game preset for this recruitment card's look",
        min_tier="Optional - shown on the card as an informational preference, never enforced",
    )
    @app_commands.autocomplete(game=_game_preset_autocomplete)
    @app_commands.choices(min_tier=[app_commands.Choice(name=t, value=t) for t in TIER_CHOICES]
                           + [app_commands.Choice(name=PARTY_MIN_TIER_ANY_VALUE, value=PARTY_MIN_TIER_ANY_VALUE)])
    async def party_recruit(self, interaction: discord.Interaction, game: str = None, min_tier: str = None):
        guild_id = interaction.guild_id

        # 🛡️ game을 생략했으면 저장된 즐겨찾기로 조용히 대체한다 - Discord 슬래시 커맨드는 유저별
        # 동적 기본값을 UI 레벨에서 지원하지 않아서, 이게 실질적으로 "기본값 자동 채움"에 해당한다.
        favorite_game = None if game else await self._get_favorite_game(guild_id, interaction.user.id)
        used_favorite = not game and favorite_game is not None
        game = resolve_effective_game(game, favorite_game)

        # 자동완성은 후보만 제안할 뿐 강제하지 않는다 - 존재하지 않는 값이면 모달을 열기 전에
        # 바로 거부한다(모달 다 채운 뒤에야 알게 되는 것보다 낫다).
        if game:
            preset = await self._get_game_preset(guild_id, game)
            if preset is None:
                if used_favorite:
                    # 저장된 즐겨찾기가 그 사이 삭제된 경우 - 유저가 직접 고른 값이 아니므로
                    # 명령어를 실패시키지 않고 "게임 미지정"으로 조용히 넘어간다.
                    game = None
                else:
                    msg = await self.get_msg(guild_id, "party_err_unknown_preset", game=game)
                    await interaction.response.send_message(msg, ephemeral=True)
                    return

        title = await self.get_msg(guild_id, "party_modal_title")
        queue_label = await self.get_msg(guild_id, "party_modal_queue_label")
        lanes_label = await self.get_msg(guild_id, "party_modal_lanes_label")
        count_label = await self.get_msg(guild_id, "party_modal_needed_count_label")
        looking_for_role_label = await self.get_msg(guild_id, "party_modal_looking_for_role_label")
        modal = PartyRecruitmentModal(self, title, queue_label, lanes_label, count_label, looking_for_role_label,
                                       selected_game=game, min_tier=min_tier)
        await interaction.response.send_modal(modal)

    async def handle_recruitment_submit(self, interaction: discord.Interaction, queue_type: str, lanes: str,
                                         count_str: str, selected_game: str | None = None,
                                         min_tier: str | None = None, looking_for_role: str | None = None) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id

        try:
            needed_count = int(count_str.strip())
        except (ValueError, AttributeError):
            needed_count = -1

        if not (PARTY_MIN_NEEDED_COUNT <= needed_count <= PARTY_MAX_NEEDED_COUNT):
            msg = await self.get_msg(guild_id, "party_err_invalid_count",
                                      min=PARTY_MIN_NEEDED_COUNT, max=PARTY_MAX_NEEDED_COUNT)
            await interaction.followup.send(msg, ephemeral=True)
            return

        # 🛡️ 대시보드에서 설정한 마감시간을 쓴다 - 여기서 계산한 절대시각이 그대로 DB에 저장되므로,
        # 나중에 관리자가 설정을 바꿔도 이미 만들어진 이 모집엔 소급 적용되지 않는다(의도된 동작).
        guild_settings_row = await self.get_guild_settings(guild_id)
        party_settings = resolve_party_settings((guild_settings_row.get("settings") or {}).get("party_settings"))
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=party_settings["card_lifetime_minutes"])

        # 1) DB에 먼저 기록한다 (message_id는 메시지를 보내야 알 수 있어 아직 비워둔다).
        try:
            insert_res = await self._db_call(
                lambda: self.bot.supabase.table("party_recruitments").insert({
                    "guild_id": str(guild_id),
                    "channel_id": str(interaction.channel.id),
                    "leader_id": str(interaction.user.id),
                    "queue_type": queue_type.strip(),
                    "lanes": lanes.strip() if lanes else None,
                    "needed_count": needed_count,
                    "expires_at": expires_at.isoformat(),
                    "selected_game": selected_game or None,
                    "min_tier": normalize_min_tier(min_tier),
                    "looking_for_role": (looking_for_role or "").strip()[:PARTY_LOOKING_FOR_ROLE_MAX_LENGTH] or None,
                }).execute()
            )
            recruitment_row = insert_res.data[0] if insert_res.data else None
        except Exception as e:
            print(f"[PARTY][ERROR] Insert failed: {type(e).__name__}: {e}", flush=True)
            recruitment_row = None

        if not recruitment_row:
            msg = await self.get_msg(guild_id, "party_err_save_failed")
            await interaction.followup.send(msg, ephemeral=True)
            return

        recruitment_id = recruitment_row["id"]

        # 모집자 본인도 참가자 1명으로 자동 등록한다 (needed_count는 모집자 포함 총원).
        try:
            await self._db_call(
                lambda: self.bot.supabase.table("party_participants").insert({
                    "recruitment_id": recruitment_id, "user_id": str(interaction.user.id),
                }).execute()
            )
        except Exception as e:
            print(f"[PARTY][ERROR] Failed to auto-register leader as participant "
                  f"(recruitment={recruitment_id}): {type(e).__name__}: {e}", flush=True)

        # 2) 티어 자기신고 역할 멘션 준비 - "실제로 알림이 갈지"만 확인한다(권한 남용 방지 체크와는 다름).
        tier_role = await self._resolve_leader_tier_role(guild_id, interaction.user)
        content = None
        allowed_mentions = discord.AllowedMentions.none()
        mention_notice = None
        if tier_role is not None:
            content = tier_role.mention
            allowed_mentions = discord.AllowedMentions(roles=True)
            bot_member = interaction.guild.me
            can_mention = tier_role.mentionable or (bot_member is not None and bot_member.guild_permissions.mention_everyone)
            if not can_mention:
                mention_notice = await self.get_msg(guild_id, "party_mention_notice_unmentionable", role=tier_role.name)

        embed = await self._build_card_embed(recruitment_row, current_count=1)
        join_label = await self.get_msg(guild_id, "party_btn_join")
        view = PartyCardView(self, join_label)

        try:
            card_message = await interaction.channel.send(content=content, embed=embed, view=view, allowed_mentions=allowed_mentions)
        except Exception as e:
            # 🛡️ 썸네일 URL이 형식은 멀쩡한데 디스코드가 거부하는 극단적 케이스까지 대비한
            # 마지막 안전장치 - 썸네일 없이 한 번만 재시도한다. 이거 하나 때문에 모집 카드
            # 전체가 안 뜨는 일은 없어야 한다.
            print(f"[PARTY][WARN] Failed to post recruitment card {recruitment_id}, retrying without thumbnail: "
                  f"{type(e).__name__}: {e}", flush=True)
            try:
                fallback_embed = await self._build_card_embed(recruitment_row, current_count=1, skip_thumbnail=True)
                card_message = await interaction.channel.send(content=content, embed=fallback_embed, view=view, allowed_mentions=allowed_mentions)
            except Exception as e2:
                print(f"[PARTY][ERROR] Failed to post recruitment card {recruitment_id} even without thumbnail: "
                      f"{type(e2).__name__}: {e2}", flush=True)
                msg = await self.get_msg(guild_id, "party_err_save_failed")
                await interaction.followup.send(msg, ephemeral=True)
                return

        # 3) message_id 기록 - 이게 있어야 버튼 클릭 시 이 행을 다시 찾을 수 있다.
        try:
            await self._db_call(
                lambda: self.bot.supabase.table("party_recruitments")
                        .update({"message_id": str(card_message.id)}).eq("id", recruitment_id).execute()
            )
        except Exception as e:
            print(f"[PARTY][ERROR] Failed to record message_id for recruitment {recruitment_id}: "
                  f"{type(e).__name__}: {e}", flush=True)
            msg = await self.get_msg(guild_id, "party_err_save_failed")
            await interaction.followup.send(msg, ephemeral=True)
            return

        confirm_msg = await self.get_msg(guild_id, "party_create_success")
        if mention_notice:
            confirm_msg = f"{confirm_msg}\n{mention_notice}"
        await interaction.followup.send(confirm_msg, ephemeral=True)

    async def _resolve_leader_tier_role(self, guild_id, member: discord.Member) -> discord.Role | None:
        """모집자가 자기신고한 티어 역할을 찾는다 - 확인하신 대로 "조건 맞는 역할"은
        티어 자기신고 역할 기준이다."""
        try:
            res = await self._db_call(
                lambda: self.bot.supabase.table("party_tier_roles").select("role_id").eq("guild_id", str(guild_id)).execute()
            )
            tier_role_ids = {row["role_id"] for row in (res.data or [])}
        except Exception as e:
            print(f"[PARTY][ERROR] Failed to fetch tier role mappings (guild={guild_id}): {type(e).__name__}: {e}", flush=True)
            return None

        for role in member.roles:
            if str(role.id) in tier_role_ids:
                return role
        return None

    async def _build_card_embed(self, row: dict, current_count: int, finished: bool = False,
                                 party_channel: discord.TextChannel | None = None, expired: bool = False,
                                 cancelled: bool = False, skip_thumbnail: bool = False) -> discord.Embed:
        guild_id = int(row["guild_id"])
        title = await self.get_msg(guild_id, "party_card_title", queue_type=row["queue_type"])
        leader_label = await self.get_msg(guild_id, "party_field_leader")
        queue_label = await self.get_msg(guild_id, "party_field_queue_type")
        count_label = await self.get_msg(guild_id, "party_field_count")
        expires_label = await self.get_msg(guild_id, "party_field_expires")
        looking_for_label = await self.get_msg(guild_id, "party_field_looking_for")

        game_name = ""
        card_thumbnail_url = ""

        if finished:
            description = await self.get_msg(guild_id, "party_full_announcement",
                                               channel=party_channel.mention if party_channel else "")
            color = discord.Color.green()
        elif expired:
            description = await self.get_msg(guild_id, "party_expired_notice")
            color = discord.Color.greyple()
        elif cancelled:
            description = await self.get_msg(guild_id, "party_cancelled_notice")
            color = discord.Color.red()
        else:
            # 🛡️ "모집 중" 상태만 대시보드 커스터마이징 대상 - 마감/꽉참/취소 색상은 그대로 고정.
            guild_settings_row = await self.get_guild_settings(guild_id)
            party_settings = resolve_party_settings((guild_settings_row.get("settings") or {}).get("party_settings"))

            # /party_recruit에서 게임 프리셋을 골랐으면 그 디자인을 쓰고, 없으면(선택 안 함/그 사이
            # 삭제·수정됨) 길드 기본값으로 안전하게 폴백한다 - 타이머는 프리셋과 무관하게 항상
            # party_settings(길드 공통값)에서만 가져온다.
            preset_row = None
            if row.get("selected_game"):
                preset_row = await self._get_game_preset(guild_id, row["selected_game"])
            design = select_party_card_design(party_settings, preset_row)

            description = design["card_description"] or None
            color = discord.Colour.from_str(design["card_color"])
            game_name = design["game_name"]
            card_thumbnail_url = design["card_thumbnail_url"]

        embed = discord.Embed(title=title, description=description, color=color)

        if game_name:
            embed.set_author(name=f"🎮 {game_name}")

        # 🛡️ 형식이 이상하면(resolve_party_settings가 이미 걸렀지만 이중 방어) 아예 시도 안 하고
        # 로그만 남긴다 - 썸네일 하나 때문에 카드 전체가 안 뜨는 일이 없어야 한다. 실제로 이미지가
        # 아니거나 접근 불가능한 링크는(형식은 정상) 디스코드 클라이언트가 알아서 빈 칸으로
        # 표시할 뿐 전송 자체는 항상 성공한다 - 그 케이스는 여기서 막을 필요/방법이 없다.
        if card_thumbnail_url and not skip_thumbnail:
            if card_thumbnail_url.startswith(("http://", "https://")):
                embed.set_thumbnail(url=card_thumbnail_url)
            else:
                print(f"[PARTY][WARN] card_thumbnail_url has an invalid format, skipping thumbnail "
                      f"(guild={guild_id}, url={card_thumbnail_url})", flush=True)

        embed.add_field(name=leader_label, value=f"<@{row['leader_id']}>", inline=True)
        # Queue Type과 Lanes를 하나로 합쳐서 필드 수를 줄인다(5개->4개) - 촘촘했던 인라인 그리드가
        # 자연스럽게 2열로 정리된다.
        queue_value = row["queue_type"]
        if row.get("lanes"):
            queue_value = f"{queue_value} · {row['lanes']}"
        embed.add_field(name=queue_label, value=queue_value, inline=True)

        # 🛡️ 순수 정보 표시용 - 참여 버튼은 이 값과 무관하게 항상 클릭 가능하다(강제 검증 없음).
        # 둘 다 없으면 필드 자체를 생략해서, 안 쓰는 모집은 지금까지와 완전히 같은 카드로 보인다.
        looking_for_line = resolve_looking_for_line(row.get("min_tier"), row.get("looking_for_role"))
        if looking_for_line:
            embed.add_field(name=looking_for_label, value=looking_for_line, inline=True)

        embed.add_field(name=count_label, value=f"`{current_count}/{row['needed_count']}`", inline=True)
        if not finished and not expired and not cancelled:
            ends_at = datetime.fromisoformat(row["expires_at"])
            ts = int(ends_at.timestamp())
            embed.add_field(name=expires_label, value=f"<t:{ts}:R>", inline=True)
        return embed

    # ══════════════════════════════════════════════════════════
    #  참여 처리 - INSERT(유니크 제약으로 이중 참여 방지)가 먼저, 인원 확인은 그다음.
    #  giveaway_entries/handle_entry와 동일한 설계 원칙.
    # ══════════════════════════════════════════════════════════
    async def handle_join(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        user_id = interaction.user.id
        message_id = str(interaction.message.id)

        try:
            res = await self._db_call(
                lambda: self.bot.supabase.table("party_recruitments").select("*").eq("message_id", message_id).execute()
            )
            row = res.data[0] if res.data else None
        except Exception as e:
            print(f"[PARTY][ERROR] Failed to look up recruitment for message {message_id}: {type(e).__name__}: {e}", flush=True)
            await interaction.followup.send("❌ An error occurred while processing your request.", ephemeral=True)
            return

        if not row or row["status"] in ("closed", "expired"):
            msg = await self.get_msg(guild_id, "party_err_expired")
            await interaction.followup.send(msg, ephemeral=True)
            return
        if row["status"] == "full":
            msg = await self.get_msg(guild_id, "party_err_full")
            await interaction.followup.send(msg, ephemeral=True)
            return

        ends_at = datetime.fromisoformat(row["expires_at"])
        if ends_at <= datetime.now(timezone.utc):
            msg = await self.get_msg(guild_id, "party_err_expired")
            await interaction.followup.send(msg, ephemeral=True)
            return

        recruitment_id = row["id"]

        # (사전 체크, UX 응답 속도용 - 진짜 방어선은 아래 INSERT의 UNIQUE 제약)
        try:
            existing = await self._db_call(
                lambda: self.bot.supabase.table("party_participants").select("user_id")
                        .eq("recruitment_id", recruitment_id).eq("user_id", str(user_id)).execute()
            )
            if existing.data:
                msg = await self.get_msg(guild_id, "party_err_already_joined")
                await interaction.followup.send(msg, ephemeral=True)
                return
        except Exception as e:
            print(f"[PARTY][WARN] Pre-check for existing participant failed (recruitment={recruitment_id}): "
                  f"{type(e).__name__}: {e}", flush=True)

        # 🛡️ min_tier 강제 검증 - looking_for_role과 달리 이건 정보 표시가 아니라 실제 참여 차단이다.
        # 관리자 예외 없음(서버 관리 권한과 무관한, 모집자 개인의 실력 조건 선호). 리더 본인은
        # handle_recruitment_submit에서 party_participants에 직접 INSERT되어 여기(handle_join)를
        # 아예 거치지 않으므로 별도 예외 처리가 필요 없다. DB 조회 실패 시에는 열어주지 않고
        # 막는다(fail-closed) - 조건을 실제로 확인 못 했는데 통과시키면 강제 검증의 의미가 없다.
        if row.get("min_tier"):
            try:
                verif_res = await self._db_call(
                    lambda: self.bot.supabase.table("riot_verifications").select("tier")
                            .eq("guild_id", str(guild_id)).eq("user_id", str(user_id)).execute()
                )
                verified_row = verif_res.data[0] if verif_res.data else None
            except Exception as e:
                print(f"[PARTY][ERROR] Tier verification lookup failed for user={user_id} (guild={guild_id}): "
                      f"{type(e).__name__}: {e}", flush=True)
                await interaction.followup.send("❌ An error occurred while checking your verified tier.", ephemeral=True)
                return

            verified_tier = verified_row["tier"] if verified_row else None

            if not meets_min_tier_requirement(row["min_tier"], verified_tier):
                if verified_tier is None:
                    msg = await self.get_msg(guild_id, "party_err_tier_not_verified", min_tier=row["min_tier"])
                else:
                    msg = await self.get_msg(guild_id, "party_err_tier_too_low",
                                              current_tier=verified_tier, min_tier=row["min_tier"])
                await interaction.followup.send(msg, ephemeral=True)
                return

        try:
            await self._db_call(
                lambda: self.bot.supabase.table("party_participants").insert({
                    "recruitment_id": recruitment_id, "user_id": str(user_id),
                }).execute()
            )
        except Exception as e:
            err_str = str(e)
            if "duplicate key" in err_str or "23505" in err_str:
                msg = await self.get_msg(guild_id, "party_err_already_joined")
            else:
                print(f"[PARTY][ERROR] Entry insert failed for user={user_id} recruitment={recruitment_id}: "
                      f"{type(e).__name__}: {e}", flush=True)
                msg = await self.get_msg(guild_id, "party_err_save_failed")
            await interaction.followup.send(msg, ephemeral=True)
            return

        msg = await self.get_msg(guild_id, "party_join_success")
        await interaction.followup.send(msg, ephemeral=True)

        try:
            count_res = await self._db_call(
                lambda: self.bot.supabase.table("party_participants").select("user_id")
                        .eq("recruitment_id", recruitment_id).execute()
            )
            participant_ids = [r["user_id"] for r in (count_res.data or [])]
        except Exception as e:
            print(f"[PARTY][ERROR] Failed to re-count participants for recruitment {recruitment_id}: "
                  f"{type(e).__name__}: {e}", flush=True)
            return

        if len(participant_ids) >= row["needed_count"]:
            # 🛡️ 원자적 클레임: status='recruiting' 조건이 걸린 채로 UPDATE해서, 실제로 행을
            # 건드린 경우(=우리가 선점)에만 채널을 만든다. 동시에 마지막 자리를 두 명이 채워도
            # 채널이 중복 생성되지 않는다 (giveaway.py의 마감 클레임과 동일한 정신).
            try:
                claim_res = await self._db_call(
                    lambda: self.bot.supabase.table("party_recruitments")
                            .update({"status": "full"}).eq("id", recruitment_id).eq("status", "recruiting").execute()
                )
            except Exception as e:
                print(f"[PARTY][ERROR] Failed to claim recruitment {recruitment_id} for channel creation: "
                      f"{type(e).__name__}: {e}", flush=True)
                return

            if claim_res.data:
                await self._create_party_channel(row, participant_ids, interaction.message)
        else:
            try:
                updated_embed = await self._build_card_embed(row, current_count=len(participant_ids))
                await interaction.message.edit(embed=updated_embed)
            except Exception as e:
                print(f"[PARTY][WARN] Failed to update recruitment card {recruitment_id}, retrying without "
                      f"thumbnail: {type(e).__name__}: {e}", flush=True)
                try:
                    fallback_embed = await self._build_card_embed(row, current_count=len(participant_ids), skip_thumbnail=True)
                    await interaction.message.edit(embed=fallback_embed)
                except Exception as e2:
                    print(f"[PARTY][ERROR] Failed to update recruitment card {recruitment_id} even without "
                          f"thumbnail: {type(e2).__name__}: {e2}", flush=True)

    async def _create_party_channel(self, row: dict, participant_ids: list[str], card_message: discord.Message) -> None:
        guild_id = int(row["guild_id"])
        recruitment_id = row["id"]
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            print(f"[PARTY][ERROR] Guild {guild_id} not in cache, cannot create party channel "
                  f"(recruitment={recruitment_id})", flush=True)
            return

        # 비공개 채널: @everyone 차단, 모집자+참여자만 허용. 매번 참가자 조합이 달라서 카테고리
        # 레벨 공유 설정 대신 채널 생성 시점에 개별 오버라이드를 명시한다.
        overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
        bot_member = guild.me
        if bot_member is not None:
            overwrites[bot_member] = discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True)

        mentions = []
        for uid in participant_ids:
            member = guild.get_member(int(uid))
            if member is not None:
                overwrites[member] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
                mentions.append(member.mention)
            else:
                print(f"[PARTY][WARN] Participant {uid} not a cached guild member, cannot grant party "
                      f"channel access (recruitment={recruitment_id})", flush=True)

        safe_name = "".join(c for c in row["queue_type"].lower() if c.isalnum() or c in "-_")[:80] or "party"
        channel_name = f"party-{safe_name}"

        try:
            party_channel = await guild.create_text_channel(channel_name, overwrites=overwrites, reason="[KYVO PARTY]")
        except Exception as e:
            print(f"[PARTY][ERROR] Failed to create party channel for recruitment {recruitment_id}: "
                  f"{type(e).__name__}: {e}", flush=True)
            return

        guild_settings_row = await self.get_guild_settings(guild_id)
        party_settings = resolve_party_settings((guild_settings_row.get("settings") or {}).get("party_settings"))
        channel_expires_at = datetime.now(timezone.utc) + timedelta(hours=party_settings["channel_lifetime_hours"])
        try:
            await self._db_call(
                lambda: self.bot.supabase.table("party_recruitments").update({
                    "party_channel_id": str(party_channel.id),
                    "party_channel_expires_at": channel_expires_at.isoformat(),
                }).eq("id", recruitment_id).execute()
            )
        except Exception as e:
            print(f"[PARTY][ERROR] Failed to record party_channel_id for recruitment {recruitment_id}: "
                  f"{type(e).__name__}: {e}", flush=True)

        # 🛡️ 음성 채널은 부가 기능이다 - 생성이 실패해도(권한/채널 한도 등) 텍스트 채널과 파티
        # 성사 흐름 자체는 그대로 진행한다. 텍스트와 달리 시간 기반이 아니라 voice.py의 Join to
        # Create 수명주기(비면 유예 후 삭제)를 재사용한다 - 단, 네임스페이스는 분리(kyvo:party_voice:*).
        try:
            party_voice_channel = await guild.create_voice_channel(
                f"🔊 {channel_name}", overwrites=overwrites, reason="[KYVO PARTY]"
            )
        except Exception as e:
            print(f"[PARTY][WARN] Failed to create party voice channel for recruitment {recruitment_id}, "
                  f"continuing without one: {type(e).__name__}: {e}", flush=True)
            party_voice_channel = None

        if party_voice_channel is not None:
            try:
                await self._db_call(
                    lambda: self.bot.supabase.table("party_recruitments").update({
                        "party_voice_channel_id": str(party_voice_channel.id),
                    }).eq("id", recruitment_id).execute()
                )
            except Exception as e:
                print(f"[PARTY][ERROR] Failed to record party_voice_channel_id for recruitment "
                      f"{recruitment_id}: {type(e).__name__}: {e}", flush=True)

            voice_cog = self._get_voice_cog()
            if voice_cog is not None:
                redis_key = self._party_voice_redis_key(guild_id)
                await voice_cog.track_channel(redis_key, guild_id, party_voice_channel.id, self.tracked_party_voice_channels)
                # 아무도 안 들어오면 "비었다가 됨" 이벤트 자체가 영영 안 일어나 평생 안 지워지는
                # 갭을 막기 위해, 생성 직후 바로 한 번 유예 체크를 걸어둔다(그 사이 누가 들어오면
                # schedule_deletion이 알아서 취소한다).
                asyncio.create_task(voice_cog.schedule_deletion(
                    party_voice_channel, redis_key, self.tracked_party_voice_channels,
                    reason="[KYVO PARTY] empty voice channel cleanup",
                ))

        welcome = await self.get_msg(guild_id, "party_channel_welcome", mentions=" ".join(mentions))
        try:
            await party_channel.send(welcome)
        except Exception as e:
            print(f"[PARTY][ERROR] Failed to send welcome message in party channel {party_channel.id}: "
                  f"{type(e).__name__}: {e}", flush=True)

        finished_embed = await self._build_card_embed(row, current_count=len(participant_ids), finished=True, party_channel=party_channel)
        try:
            await card_message.edit(embed=finished_embed, view=None)
        except Exception as e:
            print(f"[PARTY][WARN] Failed to update recruitment card {recruitment_id} after channel creation: "
                  f"{type(e).__name__}: {e}", flush=True)

    # ══════════════════════════════════════════════════════════
    #  /party_close - 두 위치/상태를 모두 처리한다:
    #  ① 파티 채널 안에서, status=='full'  -> 기존 동작(채널 실제 삭제)
    #  ② 모집 카드가 있던 채널에서, status=='recruiting' -> 카드 취소(버튼 제거, "취소됨" 표시)
    #  어느 쪽에도 해당하지 않으면, 왜 안 되는지(위치가 틀렸는지/이미 종료됐는지) 구분해서 안내한다.
    # ══════════════════════════════════════════════════════════
    async def _find_recruitment(self, channel_id: str | None = None, party_channel_id: str | None = None,
                                 status: str | None = None) -> dict | None:
        def _query():
            q = self.bot.supabase.table("party_recruitments").select("*")
            if channel_id is not None:
                q = q.eq("channel_id", channel_id)
            if party_channel_id is not None:
                q = q.eq("party_channel_id", party_channel_id)
            if status is not None:
                q = q.eq("status", status)
            return q.order("id", desc=True).limit(1).execute()

        try:
            res = await self._db_call(_query)
            return res.data[0] if res.data else None
        except Exception as e:
            print(f"[PARTY][ERROR] Failed to look up recruitment (channel_id={channel_id}, "
                  f"party_channel_id={party_channel_id}, status={status}): {type(e).__name__}: {e}", flush=True)
            return None

    def _has_close_permission(self, row: dict, interaction: discord.Interaction) -> bool:
        is_leader = row["leader_id"] == str(interaction.user.id)
        is_admin = interaction.user.guild_permissions.administrator
        return is_leader or is_admin

    @app_commands.command(name="party_close", description="Cancel your recruitment, or close your party channel early.")
    async def party_close(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        channel_id = str(interaction.channel.id)

        full_row = await self._find_recruitment(party_channel_id=channel_id, status="full")
        if full_row:
            await self._close_full_party(interaction, full_row)
            return

        recruiting_row = await self._find_recruitment(channel_id=channel_id, status="recruiting")
        if recruiting_row:
            await self._cancel_recruiting_party(interaction, recruiting_row)
            return

        await self._send_close_not_found_message(interaction, channel_id)

    async def _send_close_not_found_message(self, interaction: discord.Interaction, channel_id: str) -> None:
        guild_id = interaction.guild_id

        # 더 친절한 안내를 위해 상태 무관하게 다시 조회한다 (왜 못 닫는지 구분).
        stale_full_row = await self._find_recruitment(party_channel_id=channel_id)
        stale_card_row = await self._find_recruitment(channel_id=channel_id)

        if stale_full_row:
            # party_channel_id는 매치되는데 status가 'full'이 아님 -> 이미 종료된 파티 채널
            msg = await self.get_msg(guild_id, "party_close_already_closed")
        elif stale_card_row and stale_card_row["status"] == "full":
            # 카드 채널인데 이미 인원이 다 차서 파티 채널로 넘어감 -> 위치 안내
            msg = await self.get_msg(guild_id, "party_close_use_party_channel")
        elif stale_card_row:
            # 카드 채널인데 이미 취소/만료됨
            msg = await self.get_msg(guild_id, "party_close_already_closed")
        else:
            msg = await self.get_msg(guild_id, "party_close_not_a_party_channel")
        await interaction.followup.send(msg, ephemeral=True)

    async def _close_full_party(self, interaction: discord.Interaction, row: dict) -> None:
        guild_id = interaction.guild_id

        if not self._has_close_permission(row, interaction):
            msg = await self.get_msg(guild_id, "party_close_no_permission")
            await interaction.followup.send(msg, ephemeral=True)
            return

        try:
            claim_res = await self._db_call(
                lambda: self.bot.supabase.table("party_recruitments")
                        .update({"status": "closed", "closed_at": datetime.now(timezone.utc).isoformat()})
                        .eq("id", row["id"]).eq("status", "full").execute()
            )
        except Exception as e:
            print(f"[PARTY][ERROR] Failed to close recruitment {row['id']}: {type(e).__name__}: {e}", flush=True)
            await interaction.followup.send("❌ An error occurred.", ephemeral=True)
            return

        if not claim_res.data:
            # 다른 요청(자동 정리 등)이 그 사이 먼저 닫았음
            msg = await self.get_msg(guild_id, "party_close_already_closed")
            await interaction.followup.send(msg, ephemeral=True)
            return

        msg = await self.get_msg(guild_id, "party_close_success")
        await interaction.followup.send(msg, ephemeral=True)

        # 텍스트/음성 각각 독립된 try/except - 하나가 실패해도 나머지 정리는 계속 진행된다.
        try:
            await interaction.channel.delete(reason=f"[KYVO PARTY] closed by {interaction.user}")
        except discord.NotFound:
            pass
        except Exception as e:
            print(f"[PARTY][ERROR] Failed to delete party channel {interaction.channel.id} after /party_close: "
                  f"{type(e).__name__}: {e}", flush=True)

        voice_channel_id = row.get("party_voice_channel_id")
        if voice_channel_id:
            voice_channel = interaction.guild.get_channel(int(voice_channel_id))
            if voice_channel is not None:
                try:
                    await voice_channel.delete(reason=f"[KYVO PARTY] closed by {interaction.user}")
                except discord.NotFound:
                    pass
                except Exception as e:
                    print(f"[PARTY][ERROR] Failed to delete party voice channel {voice_channel_id} after "
                          f"/party_close: {type(e).__name__}: {e}", flush=True)
            voice_cog = self._get_voice_cog()
            if voice_cog is not None:
                await voice_cog.untrack_channel(
                    self._party_voice_redis_key(guild_id), guild_id, int(voice_channel_id),
                    self.tracked_party_voice_channels,
                )

    async def _cancel_recruiting_party(self, interaction: discord.Interaction, row: dict) -> None:
        guild_id = interaction.guild_id

        if not self._has_close_permission(row, interaction):
            msg = await self.get_msg(guild_id, "party_close_no_permission")
            await interaction.followup.send(msg, ephemeral=True)
            return

        try:
            claim_res = await self._db_call(
                lambda: self.bot.supabase.table("party_recruitments")
                        .update({"status": "closed", "closed_at": datetime.now(timezone.utc).isoformat()})
                        .eq("id", row["id"]).eq("status", "recruiting").execute()
            )
        except Exception as e:
            print(f"[PARTY][ERROR] Failed to cancel recruiting party {row['id']}: {type(e).__name__}: {e}", flush=True)
            await interaction.followup.send("❌ An error occurred.", ephemeral=True)
            return

        if not claim_res.data:
            # 동시에 인원이 다 찼거나(-> full) 이미 만료/취소됐음 - 삭제할 채널은 없으니 그대로 종료.
            msg = await self.get_msg(guild_id, "party_close_already_closed")
            await interaction.followup.send(msg, ephemeral=True)
            return

        msg = await self.get_msg(guild_id, "party_close_cancel_success")
        await interaction.followup.send(msg, ephemeral=True)

        # 카드 갱신 - 버튼 제거, "모집자가 취소함" 표시. 삭제할 채널은 애초에 없다(아직 안 만들어짐).
        try:
            count_res = await self._db_call(
                lambda: self.bot.supabase.table("party_participants").select("user_id")
                        .eq("recruitment_id", row["id"]).execute()
            )
            current_count = len(count_res.data or [])
        except Exception as e:
            print(f"[PARTY][WARN] Failed to re-count participants for recruitment {row['id']}: {type(e).__name__}: {e}", flush=True)
            current_count = 1

        channel = self.bot.get_channel(int(row["channel_id"]))
        message_id = row.get("message_id")
        if channel is None or not message_id:
            return
        try:
            message = await channel.fetch_message(int(message_id))
            embed = await self._build_card_embed(row, current_count=current_count, cancelled=True)
            await message.edit(embed=embed, view=None)
        except Exception as e:
            print(f"[PARTY][WARN] Failed to update recruitment card {message_id} after cancellation: "
                  f"{type(e).__name__}: {e}", flush=True)

    # ══════════════════════════════════════════════════════════
    #  자동 정리 - 이 쿼리들이 정기 체크와 콜드 스타트 복구를 동시에 해결한다 (giveaway.py와
    #  동일한 이유: expires_at이 DB에 저장된 절대시각이라 재시작 여부와 무관하게 항상 정확하다).
    # ══════════════════════════════════════════════════════════
    @tasks.loop(seconds=PARTY_CHECK_INTERVAL_SECONDS)
    async def check_party_timers(self):
        await self._expire_recruitments()
        await self._cleanup_party_channels()

    @check_party_timers.before_loop
    async def before_check_party_timers(self):
        await self.bot.wait_until_ready()
        # gg_rsvp/party/giveaway/scrim 4개 루프의 30초 틱이 겹치지 않도록 어긋나게 시작한다.
        await asyncio.sleep(7)

    async def _expire_recruitments(self) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            res = await self._db_call(
                lambda: self.bot.supabase.table("party_recruitments").select("*")
                        .eq("status", "recruiting").lte("expires_at", now_iso).execute()
            )
            expired = res.data or []
        except Exception as e:
            print(f"[PARTY][ERROR] Failed to query expired recruitments: {type(e).__name__}: {e}", flush=True)
            return

        for row in expired:
            try:
                claim_res = await self._db_call(
                    lambda rid=row["id"]: self.bot.supabase.table("party_recruitments")
                            .update({"status": "expired", "closed_at": datetime.now(timezone.utc).isoformat()})
                            .eq("id", rid).eq("status", "recruiting").execute()
                )
            except Exception as e:
                print(f"[PARTY][ERROR] Failed to claim expiry for recruitment {row['id']}: {type(e).__name__}: {e}", flush=True)
                continue

            if not claim_res.data:
                continue  # 다른 인스턴스/이전 틱이 이미 처리함

            await self._mark_card_expired(row)

    async def _mark_card_expired(self, row: dict) -> None:
        channel = self.bot.get_channel(int(row["channel_id"]))
        message_id = row.get("message_id")
        if channel is None or not message_id:
            return
        try:
            message = await channel.fetch_message(int(message_id))
            try:
                count_res = await self._db_call(
                    lambda: self.bot.supabase.table("party_participants").select("user_id")
                            .eq("recruitment_id", row["id"]).execute()
                )
                current_count = len(count_res.data or [])
            except Exception:
                current_count = 1
            embed = await self._build_card_embed(row, current_count=current_count, expired=True)
            await message.edit(embed=embed, view=None)
        except Exception as e:
            print(f"[PARTY][WARN] Failed to mark recruitment card {message_id} as expired: {type(e).__name__}: {e}", flush=True)

    async def _cleanup_party_channels(self) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            res = await self._db_call(
                lambda: self.bot.supabase.table("party_recruitments").select("*")
                        .eq("status", "full").lte("party_channel_expires_at", now_iso).execute()
            )
            to_close = res.data or []
        except Exception as e:
            print(f"[PARTY][ERROR] Failed to query party channels due for cleanup: {type(e).__name__}: {e}", flush=True)
            return

        for row in to_close:
            try:
                claim_res = await self._db_call(
                    lambda rid=row["id"]: self.bot.supabase.table("party_recruitments")
                            .update({"status": "closed", "closed_at": datetime.now(timezone.utc).isoformat()})
                            .eq("id", rid).eq("status", "full").execute()
                )
            except Exception as e:
                print(f"[PARTY][ERROR] Failed to claim cleanup for recruitment {row['id']}: {type(e).__name__}: {e}", flush=True)
                continue

            if not claim_res.data:
                continue

            await self._delete_party_channel(row)

    async def _delete_party_channel(self, row: dict) -> None:
        channel_id = row.get("party_channel_id")
        if channel_id:
            channel = self.bot.get_channel(int(channel_id))
            if channel is None:
                print(f"[PARTY][WARN] Party channel {channel_id} not in cache, cannot auto-delete "
                      f"(recruitment={row['id']})", flush=True)
            else:
                try:
                    await channel.delete(reason="[KYVO PARTY] auto-cleanup after lifetime expired")
                except discord.NotFound:
                    pass
                except Exception as e:
                    print(f"[PARTY][ERROR] Failed to auto-delete party channel {channel_id}: "
                          f"{type(e).__name__}: {e}", flush=True)

        # 음성 채널은 점유 여부와 무관하게 강제 종료한다(텍스트와 동일한 하드컷) - 파티 수명이
        # 다했으면 이벤트 기반 유예 삭제가 어떤 이유로든 안 걸렸더라도 결국 여기서 정리된다.
        voice_channel_id = row.get("party_voice_channel_id")
        if voice_channel_id:
            guild_id = int(row["guild_id"])
            voice_channel = self.bot.get_channel(int(voice_channel_id))
            if voice_channel is None:
                print(f"[PARTY][WARN] Party voice channel {voice_channel_id} not in cache, cannot "
                      f"auto-delete (recruitment={row['id']})", flush=True)
            else:
                try:
                    await voice_channel.delete(
                        reason="[KYVO PARTY] auto-cleanup after lifetime expired (occupancy ignored)"
                    )
                except discord.NotFound:
                    pass
                except Exception as e:
                    print(f"[PARTY][ERROR] Failed to auto-delete party voice channel {voice_channel_id}: "
                          f"{type(e).__name__}: {e}", flush=True)
            voice_cog = self._get_voice_cog()
            if voice_cog is not None:
                await voice_cog.untrack_channel(
                    self._party_voice_redis_key(guild_id), guild_id, int(voice_channel_id),
                    self.tracked_party_voice_channels,
                )

    # ══════════════════════════════════════════════════════════
    #  /tier_role_set - 관리자 전용, 티어 <-> 역할 매핑 등록
    # ══════════════════════════════════════════════════════════
    @app_commands.command(name="tier_role_set", description="Map a rank tier to a role for party recruitment self-reporting.")
    @app_commands.describe(tier="The tier to map.", role="The role members get when they self-report this tier.")
    @app_commands.choices(tier=[app_commands.Choice(name=t, value=t) for t in TIER_CHOICES])
    @app_commands.default_permissions(administrator=True)
    async def tier_role_set(self, interaction: discord.Interaction, tier: app_commands.Choice[str], role: discord.Role):
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id

        if role.permissions.administrator:
            msg = await self.get_msg(guild_id, "cc_err_admin_role_blocked", role=role.name)
            await interaction.followup.send(msg, ephemeral=True)
            return

        bot_member = interaction.guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_roles:
            msg = await self.get_msg(guild_id, "rr_err_no_manage_roles_permission")
            await interaction.followup.send(msg, ephemeral=True)
            return

        if bot_member.top_role <= role:
            msg = await self.get_msg(guild_id, "rr_err_hierarchy", role=role.name)
            await interaction.followup.send(msg, ephemeral=True)
            return

        dangerous = _get_dangerous_permissions(role)
        if dangerous:
            confirmed = await self._confirm_dangerous_role(interaction, role, dangerous)
            if not confirmed:
                return  # 뷰 안에서 취소/타임아웃 메시지 이미 전송함

        # 🛡️ 재매핑(이미 이 티어에 다른 역할이 매핑돼 있던 경우) 감지 - upsert 전에 이전 값을
        # 미리 알아둬야, 저장 후 그 이전 역할을 실제로 갖고 있는 멤버들을 정리할 수 있다.
        try:
            existing_res = await self._db_call(
                lambda: self.bot.supabase.table("party_tier_roles").select("role_id")
                        .eq("guild_id", str(guild_id)).eq("tier", tier.value).execute()
            )
            old_role_id = existing_res.data[0]["role_id"] if existing_res.data else None
        except Exception as e:
            print(f"[PARTY][WARN] Failed to check existing tier role mapping before upsert: {type(e).__name__}: {e}", flush=True)
            old_role_id = None

        try:
            await self._db_call(
                lambda: self.bot.supabase.table("party_tier_roles").upsert({
                    "guild_id": str(guild_id), "tier": tier.value, "role_id": str(role.id),
                }, on_conflict="guild_id,tier").execute()
            )
        except Exception as e:
            print(f"[PARTY][ERROR] Failed to save tier role mapping: {type(e).__name__}: {e}", flush=True)
            await interaction.followup.send(f"❌ {type(e).__name__}: {e}", ephemeral=True)
            return

        msg = await self.get_msg(guild_id, "party_tier_role_saved", tier=tier.value, role=role.name)
        await interaction.followup.send(msg, ephemeral=True)

        if old_role_id and old_role_id != str(role.id):
            asyncio.create_task(self._cleanup_stale_tier_role(interaction.guild, int(old_role_id), tier.value))

    async def _confirm_dangerous_role(self, interaction: discord.Interaction, role: discord.Role, dangerous: list[str]) -> bool:
        guild_id = interaction.guild_id
        perms_text = ", ".join(f"`{p}`" for p in dangerous)
        warning_msg = await self.get_msg(guild_id, "cc_warning_dangerous_role", role=role.name, permissions=perms_text)
        confirm_label = await self.get_msg(guild_id, "cc_confirm_button")
        cancel_label = await self.get_msg(guild_id, "cc_cancel_button")

        view = RoleWarningConfirmView(interaction.user.id, confirm_label, cancel_label)
        warning_title = await self.get_msg(guild_id, "cc_warning_dangerous_title")
        embed = discord.Embed(title=warning_title, description=warning_msg, color=discord.Color.orange())
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        await view.wait()

        if view.confirmed is True:
            return True

        cancel_key = "cc_action_cancelled" if view.confirmed is False else "cc_confirm_timeout"
        cancel_msg = await self.get_msg(guild_id, cancel_key)
        await interaction.followup.send(cancel_msg, ephemeral=True)
        return False

    # ══════════════════════════════════════════════════════════
    #  /tier_set - 일반 유저, 배타적 티어 자기신고
    # ══════════════════════════════════════════════════════════
    @app_commands.command(name="tier_set", description="Self-report your rank tier.")
    @app_commands.describe(tier="Your current rank tier.")
    @app_commands.choices(tier=[app_commands.Choice(name=t, value=t) for t in TIER_CHOICES])
    async def tier_set(self, interaction: discord.Interaction, tier: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id

        try:
            res = await self._db_call(
                lambda: self.bot.supabase.table("party_tier_roles").select("*").eq("guild_id", str(guild_id)).execute()
            )
            all_rows = res.data or []
        except Exception as e:
            print(f"[PARTY][ERROR] Failed to fetch tier role mappings (guild={guild_id}): {type(e).__name__}: {e}", flush=True)
            await interaction.followup.send("❌ An error occurred.", ephemeral=True)
            return

        target_row = next((r for r in all_rows if r["tier"] == tier.value), None)
        if target_row is None:
            msg = await self.get_msg(guild_id, "party_tier_not_configured", tier=tier.value)
            await interaction.followup.send(msg, ephemeral=True)
            return

        new_role = interaction.guild.get_role(int(target_row["role_id"]))
        if new_role is None:
            print(f"[PARTY][ERROR] Tier role {target_row['role_id']} for tier '{tier.value}' no longer exists "
                  f"(guild={guild_id})", flush=True)
            msg = await self.get_msg(guild_id, "party_not_configured")
            await interaction.followup.send(msg, ephemeral=True)
            return

        # 🛡️ TOCTOU 재확인: 매핑을 만든 뒤 봇 권한/위계가 바뀌었을 수 있다.
        bot_member = interaction.guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_roles or bot_member.top_role <= new_role:
            print(f"[PARTY][ERROR] Cannot assign tier role '{new_role.name}' - permission/hierarchy issue "
                  f"(guild={guild_id})", flush=True)
            msg = await self.get_msg(guild_id, "party_tier_err_hierarchy")
            await interaction.followup.send(msg, ephemeral=True)
            return

        # 배타적 선택 - 이 길드의 다른 티어 역할 중 유저가 이미 갖고 있는 걸 전부 제거한다.
        other_tier_role_ids = {r["role_id"] for r in all_rows if r["tier"] != tier.value}
        roles_to_remove = [r for r in interaction.user.roles if str(r.id) in other_tier_role_ids]

        if roles_to_remove:
            try:
                await interaction.user.remove_roles(*roles_to_remove, reason="[KYVO TIER SET] replaced by new tier")
            except discord.Forbidden:
                print(f"[PARTY][ERROR] Forbidden while removing old tier roles from user={interaction.user.id} "
                      f"(guild={guild_id})", flush=True)
            except discord.HTTPException as e:
                print(f"[PARTY][ERROR] HTTPException while removing old tier roles: {type(e).__name__}: {e}", flush=True)

        if new_role not in interaction.user.roles:
            try:
                await interaction.user.add_roles(new_role, reason="[KYVO TIER SET]")
            except discord.Forbidden:
                print(f"[PARTY][ERROR] Forbidden while granting tier role '{new_role.name}' to "
                      f"user={interaction.user.id} (guild={guild_id})", flush=True)
                msg = await self.get_msg(guild_id, "party_tier_err_hierarchy")
                await interaction.followup.send(msg, ephemeral=True)
                return
            except discord.HTTPException as e:
                print(f"[PARTY][ERROR] HTTPException while granting tier role: {type(e).__name__}: {e}", flush=True)
                await interaction.followup.send(f"❌ {type(e).__name__}: {e}", ephemeral=True)
                return

        msg = await self.get_msg(guild_id, "party_tier_set_success", tier=tier.value)
        await interaction.followup.send(msg, ephemeral=True)


async def setup(bot):
    cog = KyvoParty(bot)
    await bot.add_cog(cog)
    # 🛡️ Persistent View 등록 - 재시작 후에도 이전에 보낸 모집 카드의 참여 버튼이 계속 작동한다.
    bot.add_view(PartyCardView(cog))
    print("[⚡ PARTY] Cog extension setup complete.", flush=True)
