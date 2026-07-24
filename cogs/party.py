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

    return {
        "card_color": card_color,
        "card_description": card_description,
        "card_lifetime_minutes": card_lifetime_minutes,
        "channel_lifetime_hours": channel_lifetime_hours,
    }


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

    def __init__(self, cog: "KyvoParty", title: str, queue_label: str, lanes_label: str, count_label: str):
        super().__init__(title=title[:45])
        self.cog = cog
        self.queue_input = discord.ui.TextInput(label=queue_label[:45], max_length=50, required=True)
        self.lanes_input = discord.ui.TextInput(label=lanes_label[:45], max_length=100, required=False)
        self.count_input = discord.ui.TextInput(label=count_label[:45], max_length=3, required=True)
        self.add_item(self.queue_input)
        self.add_item(self.lanes_input)
        self.add_item(self.count_input)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.handle_recruitment_submit(interaction, self.queue_input.value, self.lanes_input.value, self.count_input.value)


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

    async def cog_load(self):
        if INTERNAL_API_SECRET:
            self.bot.web_app.router.add_post("/internal/tier-roles/cleanup", self.handle_tier_cleanup_webhook)
            print("[⚡ PARTY] Internal tier-role cleanup route registered at /internal/tier-roles/cleanup.", flush=True)
        else:
            print("[PARTY][WARN] INTERNAL_API_SECRET not set - dashboard-triggered stale tier-role "
                  "cleanup is disabled (the /tier_role_set command's own cleanup still works).", flush=True)

    async def cog_unload(self):
        self.check_party_timers.cancel()

    async def _db_call(self, fn):
        """supabase-py는 동기 클라이언트라 이벤트 루프를 막지 않도록 executor로 감싼다."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, fn)

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
    #  /party_recruit
    # ══════════════════════════════════════════════════════════
    @app_commands.command(name="party_recruit", description="Open a party recruitment post.")
    async def party_recruit(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        title = await self.get_msg(guild_id, "party_modal_title")
        queue_label = await self.get_msg(guild_id, "party_modal_queue_label")
        lanes_label = await self.get_msg(guild_id, "party_modal_lanes_label")
        count_label = await self.get_msg(guild_id, "party_modal_needed_count_label")
        modal = PartyRecruitmentModal(self, title, queue_label, lanes_label, count_label)
        await interaction.response.send_modal(modal)

    async def handle_recruitment_submit(self, interaction: discord.Interaction, queue_type: str, lanes: str, count_str: str) -> None:
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
            print(f"[PARTY][ERROR] Failed to post recruitment card {recruitment_id}: {type(e).__name__}: {e}", flush=True)
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
                                 cancelled: bool = False) -> discord.Embed:
        guild_id = int(row["guild_id"])
        title = await self.get_msg(guild_id, "party_card_title", queue_type=row["queue_type"])
        leader_label = await self.get_msg(guild_id, "party_field_leader")
        queue_label = await self.get_msg(guild_id, "party_field_queue_type")
        lanes_label = await self.get_msg(guild_id, "party_field_lanes")
        count_label = await self.get_msg(guild_id, "party_field_count")
        expires_label = await self.get_msg(guild_id, "party_field_expires")

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
            description = party_settings["card_description"] or None
            color = discord.Colour.from_str(party_settings["card_color"])

        embed = discord.Embed(title=title, description=description, color=color)
        embed.add_field(name=leader_label, value=f"<@{row['leader_id']}>", inline=True)
        embed.add_field(name=queue_label, value=row["queue_type"], inline=True)
        if row.get("lanes"):
            embed.add_field(name=lanes_label, value=row["lanes"], inline=True)
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
                print(f"[PARTY][WARN] Failed to update recruitment card {recruitment_id}: {type(e).__name__}: {e}", flush=True)

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

        try:
            await interaction.channel.delete(reason=f"[KYVO PARTY] closed by {interaction.user}")
        except discord.NotFound:
            pass
        except Exception as e:
            print(f"[PARTY][ERROR] Failed to delete party channel {interaction.channel.id} after /party_close: "
                  f"{type(e).__name__}: {e}", flush=True)

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
        if not channel_id:
            return
        channel = self.bot.get_channel(int(channel_id))
        if channel is None:
            print(f"[PARTY][WARN] Party channel {channel_id} not in cache, cannot auto-delete "
                  f"(recruitment={row['id']})", flush=True)
            return
        try:
            await channel.delete(reason="[KYVO PARTY] auto-cleanup after lifetime expired")
        except discord.NotFound:
            pass
        except Exception as e:
            print(f"[PARTY][ERROR] Failed to auto-delete party channel {channel_id}: {type(e).__name__}: {e}", flush=True)

    # ══════════════════════════════════════════════════════════
    #  /tier_role_set - 관리자 전용, 티어 <-> 역할 매핑 등록
    # ══════════════════════════════════════════════════════════
    @app_commands.command(name="tier_role_set", description="Map a rank tier to a role for party recruitment self-reporting.")
    @app_commands.describe(tier="The tier to map.", role="The role members get when they self-report this tier.")
    @app_commands.choices(tier=[app_commands.Choice(name=t, value=t) for t in TIER_CHOICES])
    @app_commands.checks.has_permissions(administrator=True)
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
        embed = discord.Embed(title="⚠️ Dangerous Permission Warning", description=warning_msg, color=discord.Color.orange())
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
