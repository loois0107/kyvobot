import discord
from discord import app_commands
from discord.ext import commands, tasks
from cogs.base import KyvoBaseCog
import asyncio
from datetime import datetime, timezone, timedelta

GG_MIN_NEEDED_COUNT = 2
GG_MAX_NEEDED_COUNT = 10  # party_recruit의 needed_count 범위와 통일
GG_DURATION_MINUTES = 30  # 고정 - 파라미터로 안 받는다("명령어 하나로 즉시"가 핵심이라 입력을 안 늘림)
GG_CHECK_INTERVAL_SECONDS = 30

# 응모/취소 버튼의 custom_id - giveaway.py/anonymous_reports.py의 영구 View와 동일한 이유로
# 고정 문자열이어야 한다 (message_id는 여기 넣지 않는다 - 아래 GGRsvpView docstring 참고).
GG_JOIN_ID = "kyvo_gg:join"
GG_LEAVE_ID = "kyvo_gg:leave"


class GGRsvpView(discord.ui.View):
    """RSVP 카드에 붙는 영구(Persistent) View.

    giveaway.py의 GiveawayEntryView와 완전히 같은 이유로 timeout=None + 고정 custom_id로 만든다.
    등록된 이 View 인스턴스 하나를 "모든" RSVP 카드가 공유하므로, 어떤 RSVP인지는 self에 저장하지
    않고 매 클릭마다 interaction.message.id(=gg_rsvps.message_id)로 DB에서 다시 찾는다.
    """

    def __init__(self, cog: "KyvoGGRsvp", join_label: str = "Join", leave_label: str = "Leave"):
        super().__init__(timeout=None)
        self.cog = cog

        join_btn = discord.ui.Button(label=join_label, style=discord.ButtonStyle.success,
                                      custom_id=GG_JOIN_ID, emoji="🙋")
        join_btn.callback = self._on_join
        self.add_item(join_btn)

        leave_btn = discord.ui.Button(label=leave_label, style=discord.ButtonStyle.secondary,
                                       custom_id=GG_LEAVE_ID, emoji="🚪")
        leave_btn.callback = self._on_leave
        self.add_item(leave_btn)

    async def _on_join(self, interaction: discord.Interaction):
        await self.cog.handle_join(interaction)

    async def _on_leave(self, interaction: discord.Interaction):
        await self.cog.handle_leave(interaction)


class KyvoGGRsvp(KyvoBaseCog):
    def __init__(self, bot):
        super().__init__(bot)
        self.check_expired_gg_rsvps.start()

    async def cog_unload(self):
        self.check_expired_gg_rsvps.cancel()

    async def _db_call(self, fn):
        """supabase-py는 동기 클라이언트라 이벤트 루프를 막지 않도록 executor로 감싼다."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.bot.db_executor, fn)

    # ══════════════════════════════════════════════════════════
    #  게임 프리셋 조회/자동완성 - party.py의 동명 헬퍼와 동일한 로직을 복제한다("각 cog가
    #  자기 완결적이도록 복제"하는 이 세션 전반의 관례 - reaction_roles.py 등도 동일).
    # ══════════════════════════════════════════════════════════
    async def _get_game_presets(self, guild_id: int) -> list[dict]:
        try:
            res = await self._db_call(
                lambda: self.bot.supabase.table("party_game_presets").select("*")
                        .eq("guild_id", str(guild_id)).execute()
            )
            return res.data or []
        except Exception as e:
            print(f"[GG_RSVP][ERROR] Failed to fetch game presets (guild={guild_id}): {type(e).__name__}: {e}", flush=True)
            return []

    async def _get_game_preset(self, guild_id: int, game_name: str) -> dict | None:
        try:
            res = await self._db_call(
                lambda: self.bot.supabase.table("party_game_presets").select("*")
                        .eq("guild_id", str(guild_id)).eq("game_name", game_name).limit(1).execute()
            )
            return res.data[0] if res.data else None
        except Exception as e:
            print(f"[GG_RSVP][ERROR] Failed to fetch game preset '{game_name}' (guild={guild_id}): "
                  f"{type(e).__name__}: {e}", flush=True)
            return None

    async def _game_preset_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        presets = await self._get_game_presets(interaction.guild_id)
        current_lower = current.lower()
        matches = [p["game_name"] for p in presets if current_lower in p["game_name"].lower()]
        return [app_commands.Choice(name=name, value=name) for name in matches[:25]]

    # ══════════════════════════════════════════════════════════
    #  /gg - 모달 없이 즉시 카드가 뜨는 가벼운 RSVP. party_recruit과 달리 채널 생성 등 무거운
    #  후속 동작이 전혀 없다 - 카드 하나, 참여/취소 버튼, 그게 끝이다.
    # ══════════════════════════════════════════════════════════
    @app_commands.command(name="gg", description="Instantly post a quick 'who wants to play?' RSVP card.")
    @app_commands.describe(
        game="Which game (pick from this server's saved presets)",
        needed_count=f"Total players needed, including you ({GG_MIN_NEEDED_COUNT}-{GG_MAX_NEEDED_COUNT})",
    )
    @app_commands.autocomplete(game=_game_preset_autocomplete)
    async def gg(self, interaction: discord.Interaction, game: str,
                 needed_count: app_commands.Range[int, GG_MIN_NEEDED_COUNT, GG_MAX_NEEDED_COUNT]):
        await interaction.response.defer()
        guild_id = interaction.guild_id
        leader_id = str(interaction.user.id)

        preset = await self._get_game_preset(guild_id, game)
        if preset is None:
            msg = await self.get_msg(guild_id, "gg_err_unknown_preset", game=game)
            await interaction.followup.send(msg, ephemeral=True)
            return

        expires_at = datetime.now(timezone.utc) + timedelta(minutes=GG_DURATION_MINUTES)

        # 1) DB에 먼저 기록한다 (message_id는 메시지를 보내야 알 수 있어 아직 비워둔다).
        try:
            insert_res = await self._db_call(
                lambda: self.bot.supabase.table("gg_rsvps").insert({
                    "guild_id": str(guild_id), "channel_id": str(interaction.channel.id),
                    "leader_id": leader_id, "game_name": game, "needed_count": needed_count,
                    "expires_at": expires_at.isoformat(),
                }).execute()
            )
            row = insert_res.data[0] if insert_res.data else None
        except Exception as e:
            print(f"[GG_RSVP][ERROR] Insert failed (guild={guild_id}): {type(e).__name__}: {e}", flush=True)
            row = None

        if not row:
            msg = await self.get_msg(guild_id, "gg_err_save_failed")
            await interaction.followup.send(msg, ephemeral=True)
            return

        rsvp_id = row["id"]

        # 🛡️ 모집자 본인은 party_recruit의 리더와 동일하게 handle_join을 거치지 않고 직접 등록한다 -
        # 본인이 만든 모임에 본인이 빠져 있는 게 더 이상하다.
        try:
            await self._db_call(
                lambda: self.bot.supabase.table("gg_rsvp_participants").insert({
                    "rsvp_id": rsvp_id, "user_id": leader_id,
                }).execute()
            )
        except Exception as e:
            print(f"[GG_RSVP][ERROR] Failed to auto-register leader for rsvp {rsvp_id}: "
                  f"{type(e).__name__}: {e}", flush=True)

        join_label = await self.get_msg(guild_id, "gg_btn_join")
        leave_label = await self.get_msg(guild_id, "gg_btn_leave")
        view = GGRsvpView(self, join_label, leave_label)
        embed = await self._build_embed(row, [leader_id])

        try:
            sent = await interaction.followup.send(embed=embed, view=view)
        except Exception as e:
            print(f"[GG_RSVP][ERROR] Failed to post RSVP card for rsvp {rsvp_id}: {type(e).__name__}: {e}", flush=True)
            return

        try:
            await self._db_call(
                lambda: self.bot.supabase.table("gg_rsvps")
                        .update({"message_id": str(sent.id)}).eq("id", rsvp_id).execute()
            )
        except Exception as e:
            print(f"[GG_RSVP][ERROR] Failed to record message_id for rsvp {rsvp_id}: {type(e).__name__}: {e}", flush=True)

    async def _build_embed(self, row: dict, participant_ids: list[str], closed: bool = False) -> discord.Embed:
        guild_id = int(row["guild_id"])
        game = row["game_name"]
        needed = row["needed_count"]
        current = len(participant_ids)
        ends_at = datetime.fromisoformat(row["expires_at"])
        ts = int(ends_at.timestamp())

        title = await self.get_msg(guild_id, "gg_card_title", game=game)
        leader_mention = f"<@{row['leader_id']}>"
        if closed:
            desc = await self.get_msg(guild_id, "gg_card_closed_desc", leader=leader_mention, game=game)
            color = discord.Color.greyple()
        else:
            desc = await self.get_msg(guild_id, "gg_card_active_desc", leader=leader_mention, game=game, time=f"<t:{ts}:R>")
            color = discord.Color.blurple()

        embed = discord.Embed(title=title, description=desc, color=color)
        players_label = await self.get_msg(guild_id, "gg_field_players")
        mentions = "\n".join(f"<@{uid}>" for uid in participant_ids) or "-"
        embed.add_field(name=f"{players_label} ({current}/{needed})", value=mentions, inline=False)
        return embed

    async def _get_rsvp_by_message(self, message_id: str) -> dict | None:
        try:
            res = await self._db_call(
                lambda: self.bot.supabase.table("gg_rsvps").select("*")
                        .eq("message_id", message_id).execute()
            )
            return res.data[0] if res.data else None
        except Exception as e:
            print(f"[GG_RSVP][ERROR] Failed to look up rsvp for message {message_id}: {type(e).__name__}: {e}", flush=True)
            return None

    async def _get_participants(self, rsvp_id) -> list[str]:
        try:
            res = await self._db_call(
                lambda: self.bot.supabase.table("gg_rsvp_participants").select("user_id, joined_at")
                        .eq("rsvp_id", rsvp_id).order("joined_at").order("user_id").execute()
            )
            return [row["user_id"] for row in (res.data or [])]
        except Exception as e:
            print(f"[GG_RSVP][ERROR] Failed to fetch participants for rsvp {rsvp_id}: {type(e).__name__}: {e}", flush=True)
            return []

    # ══════════════════════════════════════════════════════════
    #  참여 - INSERT(유니크 PK로 이중 참여 방지)를 먼저 하고, 그 다음 정원을 원자적으로 확정한다.
    #  🛡️ [동시 클릭 정원 방어] 카운터 컬럼이 없는 상태에서 "정원 N명"을 동시 클릭에도 정확히
    #  지키기 위해, 낙관적으로 먼저 넣고 - 전체를 (참여 시각, user_id) 순으로 정렬해 앞에서부터
    #  needed_count명만 "확정"으로 인정한다. 내가 그 안에 없으면 방금 넣은 행을 롤백하고 마감
    #  안내를 보낸다 - 여러 명이 동시에 마지막 자리를 다퉈도 절대 정원을 넘기지 않는다.
    # ══════════════════════════════════════════════════════════
    async def handle_join(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        message_id = str(interaction.message.id)
        guild_id = interaction.guild_id
        user_id = str(interaction.user.id)

        row = await self._get_rsvp_by_message(message_id)
        if not row or row["status"] != "active":
            msg = await self.get_msg(guild_id, "gg_err_already_closed")
            await interaction.followup.send(msg, ephemeral=True)
            return

        existing = await self._get_participants(row["id"])
        if user_id in existing:
            msg = await self.get_msg(guild_id, "gg_err_already_joined")
            await interaction.followup.send(msg, ephemeral=True)
            return

        try:
            await self._db_call(
                lambda: self.bot.supabase.table("gg_rsvp_participants").insert({
                    "rsvp_id": row["id"], "user_id": user_id,
                }).execute()
            )
        except Exception as e:
            err_str = str(e)
            if "duplicate key" in err_str or "23505" in err_str:
                msg = await self.get_msg(guild_id, "gg_err_already_joined")
            else:
                print(f"[GG_RSVP][ERROR] Join insert failed for user={user_id} rsvp={row['id']}: "
                      f"{type(e).__name__}: {e}", flush=True)
                msg = await self.get_msg(guild_id, "gg_err_save_failed")
            await interaction.followup.send(msg, ephemeral=True)
            return

        all_participants = await self._get_participants(row["id"])
        needed = row["needed_count"]
        confirmed = all_participants[:needed]

        if user_id not in confirmed:
            try:
                await self._db_call(
                    lambda: self.bot.supabase.table("gg_rsvp_participants").delete()
                            .eq("rsvp_id", row["id"]).eq("user_id", user_id).execute()
                )
            except Exception as e:
                print(f"[GG_RSVP][ERROR] Rollback of overflow join failed for user={user_id} rsvp={row['id']}: "
                      f"{type(e).__name__}: {e}", flush=True)
            msg = await self.get_msg(guild_id, "gg_err_full")
            await interaction.followup.send(msg, ephemeral=True)
            return

        msg = await self.get_msg(guild_id, "gg_success_joined")
        await interaction.followup.send(msg, ephemeral=True)

        if len(confirmed) >= needed:
            # interaction.message를 그대로 넘겨서 _claim_and_close가 채널을 다시 조회하지 않게 한다.
            await self._claim_and_close(row, confirmed, message=interaction.message)
        else:
            embed = await self._build_embed(row, confirmed)
            try:
                await interaction.message.edit(embed=embed)
            except Exception as e:
                print(f"[GG_RSVP][WARN] Failed to refresh rsvp card {message_id} after join: "
                      f"{type(e).__name__}: {e}", flush=True)

    async def handle_leave(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        message_id = str(interaction.message.id)
        guild_id = interaction.guild_id
        user_id = str(interaction.user.id)

        row = await self._get_rsvp_by_message(message_id)
        if not row or row["status"] != "active":
            msg = await self.get_msg(guild_id, "gg_err_already_closed")
            await interaction.followup.send(msg, ephemeral=True)
            return

        existing = await self._get_participants(row["id"])
        if user_id not in existing:
            msg = await self.get_msg(guild_id, "gg_err_not_joined")
            await interaction.followup.send(msg, ephemeral=True)
            return

        try:
            await self._db_call(
                lambda: self.bot.supabase.table("gg_rsvp_participants").delete()
                        .eq("rsvp_id", row["id"]).eq("user_id", user_id).execute()
            )
        except Exception as e:
            print(f"[GG_RSVP][ERROR] Leave failed for user={user_id} rsvp={row['id']}: {type(e).__name__}: {e}", flush=True)
            msg = await self.get_msg(guild_id, "gg_err_save_failed")
            await interaction.followup.send(msg, ephemeral=True)
            return

        msg = await self.get_msg(guild_id, "gg_success_left")
        await interaction.followup.send(msg, ephemeral=True)

        remaining = await self._get_participants(row["id"])
        embed = await self._build_embed(row, remaining)
        try:
            await interaction.message.edit(embed=embed)
        except Exception as e:
            print(f"[GG_RSVP][WARN] Failed to refresh rsvp card {message_id} after leave: "
                  f"{type(e).__name__}: {e}", flush=True)

    # ══════════════════════════════════════════════════════════
    #  마감 - 인원 마감(handle_join)과 시간 마감(타이머) 둘 다 이 원자적 클레임 하나를 공유한다.
    #  giveaway.py의 _claim_and_conclude와 동일한 정신 - status='active' 조건부 UPDATE라 두
    #  경로가 거의 동시에 마감을 시도해도 정확히 하나만 실제로 처리한다.
    # ══════════════════════════════════════════════════════════
    async def _claim_and_close(self, row: dict, participant_ids: list[str],
                                message: discord.Message | None = None) -> str:
        try:
            claim_res = await self._db_call(
                lambda: self.bot.supabase.table("gg_rsvps")
                        .update({"status": "closed", "closed_at": datetime.now(timezone.utc).isoformat()})
                        .eq("id", row["id"]).eq("status", "active").execute()
            )
        except Exception as e:
            print(f"[GG_RSVP][ERROR] Failed to claim rsvp {row['id']}: {type(e).__name__}: {e}", flush=True)
            return "error"

        if not claim_res.data:
            return "already_closed"

        embed = await self._build_embed(row, participant_ids, closed=True)

        # 🛡️ handle_join 경로는 이미 interaction.message를 갖고 있으니 그걸 그대로 재사용한다
        # (channel.fetch_message 왕복을 한 번 아낌). 타이머 경로만 message가 없어서 새로 가져온다.
        if message is None:
            channel = self.bot.get_channel(int(row["channel_id"]))
            message_id = row.get("message_id")
            if channel is None or not message_id:
                return "claimed"
            try:
                message = await channel.fetch_message(int(message_id))
            except discord.NotFound:
                return "claimed"
            except Exception as e:
                print(f"[GG_RSVP][ERROR] Failed to fetch closed rsvp card {message_id}: {type(e).__name__}: {e}", flush=True)
                return "claimed"

        try:
            await message.edit(embed=embed, view=None)
        except discord.NotFound:
            pass
        except Exception as e:
            print(f"[GG_RSVP][ERROR] Failed to update closed rsvp card: {type(e).__name__}: {e}", flush=True)

        return "claimed"

    @tasks.loop(seconds=GG_CHECK_INTERVAL_SECONDS)
    async def check_expired_gg_rsvps(self):
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            res = await self._db_call(
                lambda: self.bot.supabase.table("gg_rsvps").select("*")
                        .eq("status", "active").lte("expires_at", now_iso).execute()
            )
            expired = res.data or []
        except Exception as e:
            print(f"[GG_RSVP][ERROR] Failed to query expired rsvps: {type(e).__name__}: {e}", flush=True)
            return

        for row in expired:
            participant_ids = await self._get_participants(row["id"])
            await self._claim_and_close(row, participant_ids)

    @check_expired_gg_rsvps.before_loop
    async def before_check_expired_gg_rsvps(self):
        await self.bot.wait_until_ready()
        # 🛡️ gg_rsvp/party/giveaway/scrim 4개 루프가 전부 30초 주기라 동시에 틱을 맞으면 DB
        # 호출이 한 순간에 몰린다 - 최초 1회만 지연을 주면 이후에도 서로 어긋난 채로 유지된다.
        await asyncio.sleep(0)


async def setup(bot):
    cog = KyvoGGRsvp(bot)
    await bot.add_cog(cog)
    # 🛡️ Persistent View 등록 - 재시작 후에도 이전에 보낸 RSVP 카드의 버튼이 계속 작동하려면,
    # 프로세스가 새로 시작될 때마다 매번 다시 등록해야 한다(등록은 프로세스 생애주기당 1회).
    bot.add_view(GGRsvpView(cog))
    print("[⚡ GG_RSVP] Cog extension setup complete.", flush=True)
