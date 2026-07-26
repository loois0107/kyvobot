import discord
from discord import app_commands
from discord.ext import commands, tasks
from cogs.base import KyvoBaseCog
import asyncio
import random
import itertools
from datetime import datetime, timezone, timedelta

SCRIM_TOTAL_NEEDED = 10                   # 로스터 상한(정원) - 이 이상은 못 들어옴
SCRIM_MIN_NEEDED = 4                      # 리더가 "경기 시작"을 누를 수 있는 최소 인원(2v2)
SCRIM_RECRUIT_DURATION_MINUTES = 30       # 모집 타임아웃 - gg_rsvp와 동일한 정신
SCRIM_AUTO_END_HOURS = 3                  # 리더가 /내전종료를 잊어도 결국 정리되는 최종 안전장치
SCRIM_CHECK_INTERVAL_SECONDS = 30

SCRIM_JOIN_ID = "kyvo_scrim:join"
SCRIM_LEAVE_ID = "kyvo_scrim:leave"
SCRIM_START_ID = "kyvo_scrim:start"

# party.py의 TIER_CHOICES/_tier_rank와 값이 반드시 일치해야 한다(같은 인증 데이터를 읽으므로) -
# 각 cog가 자기 완결적이도록 복제한다(이 세션 전반의 관례).
TIER_CHOICES = [
    "Iron", "Bronze", "Silver", "Gold", "Platinum",
    "Emerald", "Diamond", "Master", "Grandmaster", "Challenger",
]
TIER_NEUTRAL_FALLBACK_RANK = 4.0  # TIER_CHOICES 중앙값(Platinum) - 전원 미인증인 극단적 케이스에만 씀


def _tier_rank(tier: str | None) -> int | None:
    if not tier:
        return None
    try:
        return TIER_CHOICES.index(tier)
    except ValueError:
        return None


def compute_average_tier_rank(tier_ranks: list[int | None]) -> float | None:
    """인증된(None이 아닌) 참여자들의 평균 서수. 인증자가 0명이면 None."""
    verified = [r for r in tier_ranks if r is not None]
    if not verified:
        return None
    return sum(verified) / len(verified)


def resolve_effective_tier_ranks(participant_tiers: dict[str, str | None]) -> dict[str, float]:
    """미인증 참여자에게 '이번 매치에 실제로 모인 인증자들의 평균 서수'를 대입한다 - 고정된
    하드코딩 평균보다 그때그때 참여 풀에 맞게 적응한다. 전원 미인증이면(평균 자체를 낼 수 없음)
    TIER_CHOICES 중앙값으로 폴백한다."""
    ranks = {uid: _tier_rank(tier) for uid, tier in participant_tiers.items()}
    avg = compute_average_tier_rank(list(ranks.values()))
    fallback = avg if avg is not None else TIER_NEUTRAL_FALLBACK_RANK
    return {uid: (r if r is not None else fallback) for uid, r in ranks.items()}


def balance_teams(effective_ranks: dict[str, float]) -> tuple[list[str], list[str]]:
    """SCRIM_MIN_NEEDED~SCRIM_TOTAL_NEEDED 사이 아무 인원이나 받아, 완전탐색으로 두 팀
    (floor(N/2) : ceil(N/2))의 티어 합 차이가 최소인 분할을 찾는다. N이 홀수면 한쪽이 1명
    많아지는데 어차피 팀 이름(A/B)은 임의 라벨이라 공정성 문제는 없다. N<=10이라 최악의 경우도
    C(10,5)=252가지뿐이라 근사 없이 최적해를 그대로 구할 수 있다. 동점이면 무작위로 하나를
    골라 매번 같은 조합이 나오지 않게 한다."""
    user_ids = list(effective_ranks.keys())
    n = len(user_ids)
    if not (SCRIM_MIN_NEEDED <= n <= SCRIM_TOTAL_NEEDED):
        raise ValueError(f"balance_teams requires between {SCRIM_MIN_NEEDED} and {SCRIM_TOTAL_NEEDED} "
                          f"participants, got {n}")
    team_a_size = n // 2

    best_diff = None
    best_splits: list[tuple[list[str], list[str]]] = []
    for combo in itertools.combinations(user_ids, team_a_size):
        combo_set = set(combo)
        other = [u for u in user_ids if u not in combo_set]
        diff = abs(sum(effective_ranks[u] for u in combo) - sum(effective_ranks[u] for u in other))
        if best_diff is None or diff < best_diff:
            best_diff = diff
            best_splits = [(list(combo), other)]
        elif diff == best_diff:
            best_splits.append((list(combo), other))

    return random.choice(best_splits)


class ScrimRecruitView(discord.ui.View):
    """모집 중 카드에 붙는 영구 View - gg_rsvp.py의 GGRsvpView와 동일한 이유(재시작 후에도 버튼이
    계속 작동해야 함)로 timeout=None + 고정 custom_id. 어떤 내전인지는 매 클릭마다
    interaction.message.id로 DB에서 다시 찾는다.

    🛡️ 참여/취소/시작 버튼이 처음부터(리더 혼자일 때부터) 항상 다 같이 붙어있다 - 예전엔 정원
    10명이 차야 "경기 시작" 버튼으로 화면이 바뀌는 별도 View(ScrimReadyView)였지만, 리더가
    인원과 무관하게 아무 때나 시작할 수 있어야 한다는 요구사항으로 바뀌면서 상태 전환 자체가
    없어졌다 - "시작" 클릭 시 최소 인원 미달이면 handle_start가 안내만 하고 버튼/카드는 그대로
    둔다(뷰를 바꿀 필요가 없어짐)."""

    def __init__(self, cog: "KyvoScrim", join_label: str = "Join", leave_label: str = "Leave",
                 start_label: str = "Start Match"):
        super().__init__(timeout=None)
        self.cog = cog

        join_btn = discord.ui.Button(label=join_label, style=discord.ButtonStyle.success,
                                      custom_id=SCRIM_JOIN_ID, emoji="⚔️")
        join_btn.callback = self._on_join
        self.add_item(join_btn)

        leave_btn = discord.ui.Button(label=leave_label, style=discord.ButtonStyle.secondary,
                                       custom_id=SCRIM_LEAVE_ID, emoji="🚪")
        leave_btn.callback = self._on_leave
        self.add_item(leave_btn)

        start_btn = discord.ui.Button(label=start_label, style=discord.ButtonStyle.primary,
                                       custom_id=SCRIM_START_ID, emoji="🚀")
        start_btn.callback = self._on_start
        self.add_item(start_btn)

    async def _on_join(self, interaction: discord.Interaction):
        await self.cog.handle_join(interaction)

    async def _on_leave(self, interaction: discord.Interaction):
        await self.cog.handle_leave(interaction)

    async def _on_start(self, interaction: discord.Interaction):
        await self.cog.handle_start(interaction)


class KyvoScrim(KyvoBaseCog):
    def __init__(self, bot):
        super().__init__(bot)
        self.check_scrim_timers.start()

    async def cog_unload(self):
        self.check_scrim_timers.cancel()

    async def _db_call(self, fn):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, fn)

    # ══════════════════════════════════════════════════════════
    #  조회 헬퍼
    # ══════════════════════════════════════════════════════════
    async def _get_scrim_by_message(self, message_id: str) -> dict | None:
        try:
            res = await self._db_call(
                lambda: self.bot.supabase.table("scrims").select("*")
                        .eq("message_id", message_id).execute()
            )
            return res.data[0] if res.data else None
        except Exception as e:
            print(f"[SCRIM][ERROR] Failed to look up scrim for message {message_id}: {type(e).__name__}: {e}", flush=True)
            return None

    async def _get_participants(self, scrim_id) -> list[str]:
        """(joined_at, user_id) 순으로 정렬된 user_id 목록 - 정원 확정 로직(gg_rsvp와 동일)이 이
        순서에 의존한다."""
        try:
            res = await self._db_call(
                lambda: self.bot.supabase.table("scrim_participants").select("user_id, joined_at")
                        .eq("scrim_id", scrim_id).order("joined_at").order("user_id").execute()
            )
            return [row["user_id"] for row in (res.data or [])]
        except Exception as e:
            print(f"[SCRIM][ERROR] Failed to fetch participants for scrim {scrim_id}: {type(e).__name__}: {e}", flush=True)
            return []

    async def _get_participants_full(self, scrim_id) -> list[dict]:
        try:
            res = await self._db_call(
                lambda: self.bot.supabase.table("scrim_participants").select("*")
                        .eq("scrim_id", scrim_id).execute()
            )
            return res.data or []
        except Exception as e:
            print(f"[SCRIM][ERROR] Failed to fetch full participants for scrim {scrim_id}: {type(e).__name__}: {e}", flush=True)
            return []

    async def _get_verified_tiers(self, guild_id, user_ids: list[str]) -> dict[str, str | None]:
        try:
            res = await self._db_call(
                lambda: self.bot.supabase.table("riot_verifications").select("user_id, tier")
                        .eq("guild_id", str(guild_id)).in_("user_id", user_ids).execute()
            )
            found = {row["user_id"]: row["tier"] for row in (res.data or [])}
        except Exception as e:
            print(f"[SCRIM][ERROR] Failed to fetch verified tiers (guild={guild_id}): {type(e).__name__}: {e}", flush=True)
            found = {}
        return {uid: found.get(uid) for uid in user_ids}

    # ══════════════════════════════════════════════════════════
    #  /내전 - 모집 시작. gg_rsvp.py와 동일한 흐름(모달 없이 즉시 카드) + 10명 고정.
    # ══════════════════════════════════════════════════════════
    @app_commands.command(name="내전", description="Start recruiting a balanced 5v5 scrim.")
    async def scrim_start_recruit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        guild_id = interaction.guild_id
        leader_id = str(interaction.user.id)
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=SCRIM_RECRUIT_DURATION_MINUTES)

        try:
            insert_res = await self._db_call(
                lambda: self.bot.supabase.table("scrims").insert({
                    "guild_id": str(guild_id), "channel_id": str(interaction.channel.id),
                    "leader_id": leader_id, "expires_at": expires_at.isoformat(),
                }).execute()
            )
            row = insert_res.data[0] if insert_res.data else None
        except Exception as e:
            print(f"[SCRIM][ERROR] Insert failed (guild={guild_id}): {type(e).__name__}: {e}", flush=True)
            row = None

        if not row:
            msg = await self.get_msg(guild_id, "scrim_err_save_failed")
            await interaction.followup.send(msg, ephemeral=True)
            return

        scrim_id = row["id"]

        try:
            await self._db_call(
                lambda: self.bot.supabase.table("scrim_participants").insert({
                    "scrim_id": scrim_id, "user_id": leader_id,
                }).execute()
            )
        except Exception as e:
            print(f"[SCRIM][ERROR] Failed to auto-register leader for scrim {scrim_id}: {type(e).__name__}: {e}", flush=True)

        join_label = await self.get_msg(guild_id, "scrim_btn_join")
        leave_label = await self.get_msg(guild_id, "scrim_btn_leave")
        start_label = await self.get_msg(guild_id, "scrim_btn_start")
        view = ScrimRecruitView(self, join_label, leave_label, start_label)
        embed = await self._build_recruit_embed(row, [leader_id])

        try:
            sent = await interaction.followup.send(embed=embed, view=view)
        except Exception as e:
            print(f"[SCRIM][ERROR] Failed to post scrim card for scrim {scrim_id}: {type(e).__name__}: {e}", flush=True)
            return

        try:
            await self._db_call(
                lambda: self.bot.supabase.table("scrims")
                        .update({"message_id": str(sent.id)}).eq("id", scrim_id).execute()
            )
        except Exception as e:
            print(f"[SCRIM][ERROR] Failed to record message_id for scrim {scrim_id}: {type(e).__name__}: {e}", flush=True)

    # ══════════════════════════════════════════════════════════
    #  임베드 빌더
    # ══════════════════════════════════════════════════════════
    async def _build_recruit_embed(self, row: dict, participant_ids: list[str]) -> discord.Embed:
        guild_id = int(row["guild_id"])
        ts = int(datetime.fromisoformat(row["expires_at"]).timestamp())
        title = await self.get_msg(guild_id, "scrim_card_title")
        desc = await self.get_msg(guild_id, "scrim_card_recruit_desc",
                                   leader=f"<@{row['leader_id']}>", time=f"<t:{ts}:R>")
        embed = discord.Embed(title=title, description=desc, color=discord.Color.blurple())
        players_label = await self.get_msg(guild_id, "scrim_field_players")
        mentions = "\n".join(f"<@{uid}>" for uid in participant_ids) or "-"
        embed.add_field(name=f"{players_label} ({len(participant_ids)}/{SCRIM_TOTAL_NEEDED}, min {SCRIM_MIN_NEEDED} to start)",
                         value=mentions, inline=False)
        return embed

    async def _build_cancelled_embed(self, row: dict) -> discord.Embed:
        guild_id = int(row["guild_id"])
        title = await self.get_msg(guild_id, "scrim_card_title")
        desc = await self.get_msg(guild_id, "scrim_card_cancelled_desc")
        return discord.Embed(title=title, description=desc, color=discord.Color.greyple())

    async def _build_started_embed(self, row: dict, team_a: list[str], team_b: list[str],
                                    move_results: dict[str, str]) -> discord.Embed:
        guild_id = int(row["guild_id"])
        title = await self.get_msg(guild_id, "scrim_card_title")
        desc = await self.get_msg(guild_id, "scrim_card_started_desc")
        embed = discord.Embed(title=title, description=desc, color=discord.Color.green())

        team_a_label = await self.get_msg(guild_id, "scrim_field_team_a")
        team_b_label = await self.get_msg(guild_id, "scrim_field_team_b")
        embed.add_field(name=team_a_label, value="\n".join(f"<@{u}>" for u in team_a) or "-", inline=True)
        embed.add_field(name=team_b_label, value="\n".join(f"<@{u}>" for u in team_b) or "-", inline=True)

        failed = [uid for uid, result in move_results.items() if result != "moved"]
        if failed:
            reason_label = {
                "not_in_voice": await self.get_msg(guild_id, "scrim_move_reason_not_in_voice"),
                "hierarchy_blocked": await self.get_msg(guild_id, "scrim_move_reason_hierarchy"),
                "forbidden": await self.get_msg(guild_id, "scrim_move_reason_forbidden"),
                "http_error": await self.get_msg(guild_id, "scrim_move_reason_error"),
                "not_in_guild": await self.get_msg(guild_id, "scrim_move_reason_not_in_guild"),
            }
            lines = [f"<@{uid}> - {reason_label.get(move_results[uid], move_results[uid])}" for uid in failed]
            not_moved_label = await self.get_msg(guild_id, "scrim_field_not_moved")
            embed.add_field(name=not_moved_label, value="\n".join(lines), inline=False)

        return embed

    # ══════════════════════════════════════════════════════════
    #  참여/취소 - gg_rsvp.py의 handle_join/handle_leave와 동일한 정원 방어 패턴(카운터 컬럼
    #  없이 낙관적 INSERT + 정렬 후 truncate로 동시 클릭에도 정확히 10명 캡을 지킨다). 정원이
    #  차도 별도 상태 전환 없이 그냥 더 이상 못 들어오게만 막는다 - "경기 시작"은 리더가 인원과
    #  무관하게 언제든 누를 수 있어서(handle_start), 정원 도달이 더 이상 특별한 이벤트가 아니다.
    # ══════════════════════════════════════════════════════════
    async def handle_join(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        message_id = str(interaction.message.id)
        guild_id = interaction.guild_id
        user_id = str(interaction.user.id)

        row = await self._get_scrim_by_message(message_id)
        if not row or row["status"] != "recruiting":
            msg = await self.get_msg(guild_id, "scrim_err_not_recruiting")
            await interaction.followup.send(msg, ephemeral=True)
            return

        existing = await self._get_participants(row["id"])
        if user_id in existing:
            msg = await self.get_msg(guild_id, "scrim_err_already_joined")
            await interaction.followup.send(msg, ephemeral=True)
            return

        try:
            await self._db_call(
                lambda: self.bot.supabase.table("scrim_participants").insert({
                    "scrim_id": row["id"], "user_id": user_id,
                }).execute()
            )
        except Exception as e:
            err_str = str(e)
            if "duplicate key" in err_str or "23505" in err_str:
                msg = await self.get_msg(guild_id, "scrim_err_already_joined")
            else:
                print(f"[SCRIM][ERROR] Join insert failed for user={user_id} scrim={row['id']}: "
                      f"{type(e).__name__}: {e}", flush=True)
                msg = await self.get_msg(guild_id, "scrim_err_save_failed")
            await interaction.followup.send(msg, ephemeral=True)
            return

        all_participants = await self._get_participants(row["id"])
        confirmed = all_participants[:SCRIM_TOTAL_NEEDED]

        if user_id not in confirmed:
            try:
                await self._db_call(
                    lambda: self.bot.supabase.table("scrim_participants").delete()
                            .eq("scrim_id", row["id"]).eq("user_id", user_id).execute()
                )
            except Exception as e:
                print(f"[SCRIM][ERROR] Rollback of overflow join failed for user={user_id} scrim={row['id']}: "
                      f"{type(e).__name__}: {e}", flush=True)
            msg = await self.get_msg(guild_id, "scrim_err_full")
            await interaction.followup.send(msg, ephemeral=True)
            return

        msg = await self.get_msg(guild_id, "scrim_success_joined")
        await interaction.followup.send(msg, ephemeral=True)

        embed = await self._build_recruit_embed(row, confirmed)
        try:
            await interaction.message.edit(embed=embed)
        except Exception as e:
            print(f"[SCRIM][WARN] Failed to refresh scrim card {message_id} after join: "
                  f"{type(e).__name__}: {e}", flush=True)

    async def handle_leave(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        message_id = str(interaction.message.id)
        guild_id = interaction.guild_id
        user_id = str(interaction.user.id)

        row = await self._get_scrim_by_message(message_id)
        if not row or row["status"] != "recruiting":
            msg = await self.get_msg(guild_id, "scrim_err_not_recruiting")
            await interaction.followup.send(msg, ephemeral=True)
            return

        existing = await self._get_participants(row["id"])
        if user_id not in existing:
            msg = await self.get_msg(guild_id, "scrim_err_not_joined")
            await interaction.followup.send(msg, ephemeral=True)
            return

        try:
            await self._db_call(
                lambda: self.bot.supabase.table("scrim_participants").delete()
                        .eq("scrim_id", row["id"]).eq("user_id", user_id).execute()
            )
        except Exception as e:
            print(f"[SCRIM][ERROR] Leave failed for user={user_id} scrim={row['id']}: {type(e).__name__}: {e}", flush=True)
            msg = await self.get_msg(guild_id, "scrim_err_save_failed")
            await interaction.followup.send(msg, ephemeral=True)
            return

        msg = await self.get_msg(guild_id, "scrim_success_left")
        await interaction.followup.send(msg, ephemeral=True)

        remaining = await self._get_participants(row["id"])
        embed = await self._build_recruit_embed(row, remaining)
        try:
            await interaction.message.edit(embed=embed)
        except Exception as e:
            print(f"[SCRIM][WARN] Failed to refresh scrim card {message_id} after leave: "
                  f"{type(e).__name__}: {e}", flush=True)

    # ══════════════════════════════════════════════════════════
    #  경기 시작 - 리더만, 인원과 무관하게 아무 때나 누를 수 있다(최소 인원 미만이면 안내만 하고
    #  아무 것도 바꾸지 않는다 - 카드/버튼은 그대로 남아 계속 모집을 이어갈 수 있다).
    #  MOVE_MEMBERS/manage_channels 사전 체크(둘 다 없으면 명확히 실패, 수동 모드로 조용히
    #  대체하지 않는다) → 완전탐색 팀 배정 → 음성 채널 2개 생성 → 강제 이동.
    # ══════════════════════════════════════════════════════════
    async def handle_start(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        message_id = str(interaction.message.id)
        guild_id = interaction.guild_id
        user_id = str(interaction.user.id)
        guild = interaction.guild

        row = await self._get_scrim_by_message(message_id)
        if not row or row["status"] != "recruiting":
            msg = await self.get_msg(guild_id, "scrim_err_not_ready")
            await interaction.followup.send(msg, ephemeral=True)
            return

        if user_id != row["leader_id"]:
            msg = await self.get_msg(guild_id, "scrim_err_leader_only")
            await interaction.followup.send(msg, ephemeral=True)
            return

        current_count = len(await self._get_participants(row["id"]))
        if current_count < SCRIM_MIN_NEEDED:
            msg = await self.get_msg(guild_id, "scrim_err_min_not_reached",
                                      min=SCRIM_MIN_NEEDED, current=current_count)
            await interaction.followup.send(msg, ephemeral=True)
            return

        bot_member = guild.me
        if bot_member is None or not bot_member.guild_permissions.move_members:
            msg = await self.get_msg(guild_id, "scrim_err_no_move_members_permission")
            await interaction.followup.send(msg, ephemeral=True)
            return
        if not bot_member.guild_permissions.manage_channels:
            msg = await self.get_msg(guild_id, "scrim_err_no_manage_channels_permission")
            await interaction.followup.send(msg, ephemeral=True)
            return

        try:
            claim_res = await self._db_call(
                lambda: self.bot.supabase.table("scrims").update({
                    "status": "in_progress",
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "auto_end_at": (datetime.now(timezone.utc) + timedelta(hours=SCRIM_AUTO_END_HOURS)).isoformat(),
                }).eq("id", row["id"]).eq("status", "recruiting").execute()
            )
        except Exception as e:
            print(f"[SCRIM][ERROR] Failed to claim scrim {row['id']} for start: {type(e).__name__}: {e}", flush=True)
            msg = await self.get_msg(guild_id, "scrim_err_save_failed")
            await interaction.followup.send(msg, ephemeral=True)
            return

        if not claim_res.data:
            # 🛡️ 정원 마감 타임아웃(모집 취소)이나 다른 시작 클릭이 그 사이 먼저 이 행을
            # 선점했을 수 있다 - status='recruiting' 조건부 UPDATE라 정확히 하나만 통과한다.
            msg = await self.get_msg(guild_id, "scrim_err_already_started")
            await interaction.followup.send(msg, ephemeral=True)
            return

        participants = await self._get_participants(row["id"])
        tiers = await self._get_verified_tiers(guild_id, participants)
        effective_ranks = resolve_effective_tier_ranks(tiers)
        team_a, team_b = balance_teams(effective_ranks)

        try:
            await self._db_call(
                lambda: self.bot.supabase.table("scrim_participants").update({"team": "A"})
                        .eq("scrim_id", row["id"]).in_("user_id", team_a).execute()
            )
            await self._db_call(
                lambda: self.bot.supabase.table("scrim_participants").update({"team": "B"})
                        .eq("scrim_id", row["id"]).in_("user_id", team_b).execute()
            )
        except Exception as e:
            print(f"[SCRIM][ERROR] Failed to record team assignments for scrim {row['id']}: {type(e).__name__}: {e}", flush=True)

        channel_a, channel_b = await self._create_team_channels(guild, row, team_a, team_b)

        move_results: dict[str, str] = {}
        if channel_a is not None:
            move_results.update(await self._move_team(guild, bot_member, row["id"], team_a, channel_a))
        else:
            move_results.update({uid: "http_error" for uid in team_a})
        if channel_b is not None:
            move_results.update(await self._move_team(guild, bot_member, row["id"], team_b, channel_b))
        else:
            move_results.update({uid: "http_error" for uid in team_b})

        try:
            await self._db_call(
                lambda: self.bot.supabase.table("scrims").update({
                    "team_a_voice_channel_id": str(channel_a.id) if channel_a else None,
                    "team_b_voice_channel_id": str(channel_b.id) if channel_b else None,
                }).eq("id", row["id"]).execute()
            )
        except Exception as e:
            print(f"[SCRIM][ERROR] Failed to record team voice channel ids for scrim {row['id']}: {type(e).__name__}: {e}", flush=True)

        embed = await self._build_started_embed(row, team_a, team_b, move_results)
        try:
            await interaction.message.edit(embed=embed, view=None)
        except Exception as e:
            print(f"[SCRIM][ERROR] Failed to update started-state scrim card: {type(e).__name__}: {e}", flush=True)

    async def _create_team_channels(self, guild: discord.Guild, row: dict, team_a: list[str], team_b: list[str]):
        async def _create(team_ids: list[str], name: str):
            overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
            bot_member = guild.me
            if bot_member is not None:
                overwrites[bot_member] = discord.PermissionOverwrite(view_channel=True, connect=True, manage_channels=True)
            for uid in team_ids:
                member = guild.get_member(int(uid))
                if member is not None:
                    overwrites[member] = discord.PermissionOverwrite(view_channel=True, connect=True)
            try:
                return await guild.create_voice_channel(name, overwrites=overwrites, reason="[KYVO SCRIM]")
            except Exception as e:
                print(f"[SCRIM][ERROR] Failed to create team voice channel '{name}' (scrim={row['id']}): "
                      f"{type(e).__name__}: {e}", flush=True)
                return None

        channel_a = await _create(team_a, "⚔️ Team A")
        channel_b = await _create(team_b, "🛡️ Team B")
        return channel_a, channel_b

    async def _move_team(self, guild: discord.Guild, bot_member: discord.Member, scrim_id,
                          team_ids: list[str], target_channel: discord.VoiceChannel) -> dict[str, str]:
        """각 참여자를 이동시키기 직전에 원래 음성 채널을 DB에 기록한다(복귀용) - 이동 성공
        여부와 무관하게 항상 먼저 기록해서, 이후 어떤 이유로 실패해도 "원래 어디 있었는지"
        정보 자체는 유실되지 않게 한다."""
        results: dict[str, str] = {}
        for uid in team_ids:
            member = guild.get_member(int(uid))
            if member is None:
                results[uid] = "not_in_guild"
                continue

            original_channel_id = str(member.voice.channel.id) if member.voice and member.voice.channel else None
            try:
                await self._db_call(
                    lambda u=uid, oc=original_channel_id: self.bot.supabase.table("scrim_participants")
                            .update({"original_voice_channel_id": oc}).eq("scrim_id", scrim_id).eq("user_id", u).execute()
                )
            except Exception as e:
                print(f"[SCRIM][ERROR] Failed to record original voice channel for user={uid} scrim={scrim_id}: "
                      f"{type(e).__name__}: {e}", flush=True)

            if member.voice is None or member.voice.channel is None:
                # 🛡️ Discord API 자체의 근본 제약 - 음성에 아예 없는 유저는 봇이 "불러들일" 수
                # 없다(이미 음성에 연결된 유저만 다른 채널로 옮길 수 있음). 다른 서버 음성에 있는
                # 경우도 이 길드의 Member 객체에서는 구분 불가능해 동일하게 처리된다.
                results[uid] = "not_in_voice"
                continue

            if bot_member.top_role <= member.top_role:
                results[uid] = "hierarchy_blocked"
                print(f"[SCRIM][WARN] Cannot move user={uid} (scrim={scrim_id}): bot's top role is not "
                      f"above the member's top role.", flush=True)
                continue

            try:
                await member.move_to(target_channel, reason="[KYVO SCRIM] team assignment")
                results[uid] = "moved"
            except discord.Forbidden:
                results[uid] = "forbidden"
            except discord.HTTPException as e:
                print(f"[SCRIM][ERROR] HTTPException moving user={uid} (scrim={scrim_id}): {type(e).__name__}: {e}", flush=True)
                results[uid] = "http_error"

        return results

    # ══════════════════════════════════════════════════════════
    #  /내전종료 - 리더 또는 서버 관리 권한자만. 자동 종료(auto_end_at)와 이 함수 둘 다
    #  _end_scrim 하나를 공유한다(원자적 클레임으로 중복 처리 방지).
    # ══════════════════════════════════════════════════════════
    # 🛡️ app_commands.checks.has_permissions(manage_guild=True)를 데코레이터로 걸면 Discord
    # 클라이언트가 manage_guild 없는 유저에게 이 명령어 자체를 안 보여줄 수 있다 - "리더 또는
    # 관리자" 요구사항과 안 맞아서(리더는 관리 권한이 없을 수도 있음), 권한 판단은 전부 함수
    # 내부 로직으로 처리한다.
    @app_commands.command(name="내전종료", description="End the current scrim and return everyone to their original voice channel.")
    async def scrim_end(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        user_id = str(interaction.user.id)
        is_admin = interaction.user.guild_permissions.manage_guild

        row = await self._find_active_scrim_for_end(guild_id, user_id, is_admin)
        if row is None:
            msg = await self.get_msg(guild_id, "scrim_err_no_active_scrim")
            await interaction.followup.send(msg, ephemeral=True)
            return

        if user_id != row["leader_id"] and not is_admin:
            msg = await self.get_msg(guild_id, "scrim_err_end_not_allowed")
            await interaction.followup.send(msg, ephemeral=True)
            return

        summary = await self._end_scrim(row)
        if summary is None:
            msg = await self.get_msg(guild_id, "scrim_err_already_ended")
            await interaction.followup.send(msg, ephemeral=True)
            return

        msg = await self.get_msg(guild_id, "scrim_end_success",
                                  returned=summary["returned"], skipped=summary["skipped"])
        await interaction.followup.send(msg, ephemeral=True)

    async def _find_active_scrim_for_end(self, guild_id, user_id: str, is_admin: bool) -> dict | None:
        try:
            res = await self._db_call(
                lambda: self.bot.supabase.table("scrims").select("*")
                        .eq("guild_id", str(guild_id)).eq("status", "in_progress")
                        .eq("leader_id", user_id).order("started_at", desc=True).limit(1).execute()
            )
            if res.data:
                return res.data[0]
        except Exception as e:
            print(f"[SCRIM][ERROR] Failed to look up scrim led by {user_id} (guild={guild_id}): {type(e).__name__}: {e}", flush=True)

        if not is_admin:
            return None

        try:
            res = await self._db_call(
                lambda: self.bot.supabase.table("scrims").select("*")
                        .eq("guild_id", str(guild_id)).eq("status", "in_progress")
                        .order("started_at", desc=True).limit(1).execute()
            )
            return res.data[0] if res.data else None
        except Exception as e:
            print(f"[SCRIM][ERROR] Failed to look up any in-progress scrim (guild={guild_id}): {type(e).__name__}: {e}", flush=True)
            return None

    async def _end_scrim(self, row: dict) -> dict | None:
        """원자적 클레임: status='in_progress' 조건부 UPDATE - /내전종료 명령어와 auto_end_at
        타이머가 거의 동시에 발생해도 정확히 하나만 실제로 정리를 수행한다."""
        try:
            claim_res = await self._db_call(
                lambda: self.bot.supabase.table("scrims").update({
                    "status": "ended", "ended_at": datetime.now(timezone.utc).isoformat(),
                }).eq("id", row["id"]).eq("status", "in_progress").execute()
            )
        except Exception as e:
            print(f"[SCRIM][ERROR] Failed to claim scrim {row['id']} for end: {type(e).__name__}: {e}", flush=True)
            return None

        if not claim_res.data:
            return None

        guild = self.bot.get_guild(int(row["guild_id"]))
        participants = await self._get_participants_full(row["id"])

        returned = 0
        skipped = 0
        if guild is not None:
            for p in participants:
                member = guild.get_member(int(p["user_id"]))
                original_id = p.get("original_voice_channel_id")
                if member is None or not original_id:
                    skipped += 1
                    continue
                original_channel = guild.get_channel(int(original_id))
                if original_channel is None:
                    skipped += 1
                    continue
                if member.voice is None or member.voice.channel is None:
                    skipped += 1
                    continue
                try:
                    await member.move_to(original_channel, reason="[KYVO SCRIM] returning to original channel")
                    returned += 1
                except Exception as e:
                    print(f"[SCRIM][WARN] Failed to return user={p['user_id']} to original channel "
                          f"(scrim={row['id']}): {type(e).__name__}: {e}", flush=True)
                    skipped += 1

            for channel_id in (row.get("team_a_voice_channel_id"), row.get("team_b_voice_channel_id")):
                if not channel_id:
                    continue
                channel = guild.get_channel(int(channel_id))
                if channel is None:
                    continue
                try:
                    await channel.delete(reason="[KYVO SCRIM] match ended")
                except discord.NotFound:
                    pass
                except Exception as e:
                    print(f"[SCRIM][ERROR] Failed to delete team voice channel {channel_id} (scrim={row['id']}): "
                          f"{type(e).__name__}: {e}", flush=True)

        message_id = row.get("message_id")
        channel_id = row.get("channel_id")
        if guild is not None and message_id and channel_id:
            text_channel = guild.get_channel(int(channel_id))
            if text_channel is not None:
                try:
                    message = await text_channel.fetch_message(int(message_id))
                    guild_id = int(row["guild_id"])
                    ended_embed = discord.Embed(
                        title=await self.get_msg(guild_id, "scrim_card_title"),
                        description=await self.get_msg(guild_id, "scrim_card_ended_desc"),
                        color=discord.Color.greyple(),
                    )
                    await message.edit(embed=ended_embed, view=None)
                except discord.NotFound:
                    pass
                except Exception as e:
                    print(f"[SCRIM][WARN] Failed to update ended-state scrim card (scrim={row['id']}): "
                          f"{type(e).__name__}: {e}", flush=True)

        return {"returned": returned, "skipped": skipped}

    # ══════════════════════════════════════════════════════════
    #  자동 정리 - 모집 타임아웃(recruiting -> cancelled)과 자동 종료(in_progress -> ended)를
    #  같은 배치에서 함께 처리한다. expires_at/auto_end_at 둘 다 DB 절대시각이라 콜드 스타트에도
    #  안전하다(giveaway.py/gg_rsvp.py와 동일한 이유).
    # ══════════════════════════════════════════════════════════
    @tasks.loop(seconds=SCRIM_CHECK_INTERVAL_SECONDS)
    async def check_scrim_timers(self):
        now_iso = datetime.now(timezone.utc).isoformat()

        try:
            res = await self._db_call(
                lambda: self.bot.supabase.table("scrims").select("*")
                        .eq("status", "recruiting").lte("expires_at", now_iso).execute()
            )
            expired_recruiting = res.data or []
        except Exception as e:
            print(f"[SCRIM][ERROR] Failed to query expired recruiting scrims: {type(e).__name__}: {e}", flush=True)
            expired_recruiting = []

        for row in expired_recruiting:
            await self._cancel_recruiting(row)

        try:
            res = await self._db_call(
                lambda: self.bot.supabase.table("scrims").select("*")
                        .eq("status", "in_progress").not_.is_("auto_end_at", "null")
                        .lte("auto_end_at", now_iso).execute()
            )
            overdue = res.data or []
        except Exception as e:
            print(f"[SCRIM][ERROR] Failed to query overdue in-progress scrims: {type(e).__name__}: {e}", flush=True)
            overdue = []

        for row in overdue:
            print(f"[SCRIM][WARN] Auto-ending scrim {row['id']} - auto_end_at safety backstop reached "
                  f"(leader never ran /내전종료).", flush=True)
            await self._end_scrim(row)

    @check_scrim_timers.before_loop
    async def before_check_scrim_timers(self):
        await self.bot.wait_until_ready()

    async def _cancel_recruiting(self, row: dict) -> str:
        try:
            claim_res = await self._db_call(
                lambda: self.bot.supabase.table("scrims")
                        .update({"status": "cancelled"}).eq("id", row["id"]).eq("status", "recruiting").execute()
            )
        except Exception as e:
            print(f"[SCRIM][ERROR] Failed to claim scrim {row['id']} for cancellation: {type(e).__name__}: {e}", flush=True)
            return "error"

        if not claim_res.data:
            return "already_resolved"

        channel = self.bot.get_channel(int(row["channel_id"]))
        message_id = row.get("message_id")
        if channel is not None and message_id:
            try:
                message = await channel.fetch_message(int(message_id))
                embed = await self._build_cancelled_embed(row)
                await message.edit(embed=embed, view=None)
            except discord.NotFound:
                pass
            except Exception as e:
                print(f"[SCRIM][ERROR] Failed to update cancelled scrim card {message_id}: {type(e).__name__}: {e}", flush=True)

        return "claimed"


async def setup(bot):
    cog = KyvoScrim(bot)
    await bot.add_cog(cog)
    bot.add_view(ScrimRecruitView(cog))
    print("[⚡ SCRIM] Cog extension setup complete.", flush=True)
