import discord
from discord import app_commands
from discord.ext import commands, tasks
from cogs.base import KyvoBaseCog
import asyncio
import json
import os
import hmac
import random
from aiohttp import web
from datetime import datetime, timezone, timedelta

GIVEAWAY_MIN_DURATION_MINUTES = 5
GIVEAWAY_MAX_DURATION_MINUTES = 10080  # 7일
GIVEAWAY_CHECK_INTERVAL_SECONDS = 30

INTERNAL_API_SECRET = os.environ.get("INTERNAL_API_SECRET")

# 응모 버튼의 custom_id - 재시작 후에도 discord.py가 이 문자열만으로 콜백을 다시 찾아
# 연결할 수 있어야 하므로 고정 문자열이어야 한다 (giveaway_id는 여기 넣지 않는다 - 아래
# GiveawayEntryView docstring 참고, anonymous_reports의 관리자 큐 View와 동일한 이유).
GIVEAWAY_ENTRY_CUSTOM_ID = "kyvo_giveaway:enter"


def select_winners(entrant_ids: list[str], winner_count: int) -> list[str]:
    """비복원추출로 당첨자를 뽑는다. 응모자가 당첨자 수보다 적으면 전원 당첨."""
    actual_count = min(winner_count, len(entrant_ids))
    if actual_count <= 0:
        return []
    return random.sample(entrant_ids, actual_count)


class GiveawayEntryView(discord.ui.View):
    """추첨 공지 메시지에 붙는 영구(Persistent) View.

    anonymous_reports의 관리자 큐 View와 완전히 같은 이유로 timeout=None + 고정 custom_id로
    만든다 - 추첨이 언제 마감될지 알 수 없고 그 사이 봇이 재배포될 수 있다. 등록된 이 View
    인스턴스 하나를 "모든" 추첨 공지 메시지가 공유하므로, 어떤 추첨인지는 self에 저장하지
    않고 매 클릭마다 interaction.message.id(=giveaways.message_id)로 DB에서 다시 찾는다.
    """

    def __init__(self, cog: "KyvoGiveaway", enter_label: str = "Enter Giveaway"):
        super().__init__(timeout=None)
        self.cog = cog

        btn = discord.ui.Button(label=enter_label, style=discord.ButtonStyle.success,
                                 custom_id=GIVEAWAY_ENTRY_CUSTOM_ID, emoji="🎉")
        btn.callback = self._on_enter
        self.add_item(btn)

    async def _on_enter(self, interaction: discord.Interaction):
        await self.cog.handle_entry(interaction)


class KyvoGiveaway(KyvoBaseCog):
    def __init__(self, bot):
        super().__init__(bot)
        self.check_expired_giveaways.start()

    async def cog_load(self):
        if INTERNAL_API_SECRET:
            self.bot.web_app.router.add_post("/internal/giveaway/replace-winner", self.handle_replace_winner_webhook)
            print("[⚡ GIVEAWAY] Internal replace-winner route registered at /internal/giveaway/replace-winner.", flush=True)
        else:
            print("[GIVEAWAY][WARN] INTERNAL_API_SECRET not set - dashboard-triggered winner replacement is disabled "
                  "(/giveaway end still works).", flush=True)

    async def cog_unload(self):
        self.check_expired_giveaways.cancel()

    async def _db_call(self, fn):
        """supabase-py는 동기 클라이언트라 이벤트 루프를 막지 않도록 executor로 감싼다."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, fn)

    # ══════════════════════════════════════════════════════════
    #  /giveaway points / /giveaway role - 관리자 명령어 (서브커맨드로 분리)
    # ══════════════════════════════════════════════════════════
    giveaway_group = app_commands.Group(name="giveaway", description="Create and manage point-based giveaways.")

    @giveaway_group.command(name="points", description="Create a giveaway that pays out points to the winners.")
    @app_commands.describe(
        prize="Display name for the prize.",
        entry_cost="Points required to enter.",
        winner_count="Number of winners.",
        duration_minutes=f"Minutes until the giveaway closes ({GIVEAWAY_MIN_DURATION_MINUTES}-{GIVEAWAY_MAX_DURATION_MINUTES}).",
        prize_amount="Points each winner receives.",
        channel="Channel to post the giveaway in (defaults to the current channel).",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def giveaway_points(self, interaction: discord.Interaction, prize: str, entry_cost: int,
                               winner_count: int, duration_minutes: int, prize_amount: int,
                               channel: discord.TextChannel | None = None):
        await self._create_giveaway(
            interaction, prize, entry_cost, winner_count, duration_minutes,
            prize_type="points", prize_amount=prize_amount, prize_role=None, channel=channel
        )

    @giveaway_group.command(name="role", description="Create a giveaway that grants a role to the winners.")
    @app_commands.describe(
        prize="Display name for the prize.",
        entry_cost="Points required to enter.",
        winner_count="Number of winners.",
        duration_minutes=f"Minutes until the giveaway closes ({GIVEAWAY_MIN_DURATION_MINUTES}-{GIVEAWAY_MAX_DURATION_MINUTES}).",
        prize_role="Role each winner receives.",
        channel="Channel to post the giveaway in (defaults to the current channel).",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def giveaway_role(self, interaction: discord.Interaction, prize: str, entry_cost: int,
                             winner_count: int, duration_minutes: int, prize_role: discord.Role,
                             channel: discord.TextChannel | None = None):
        await self._create_giveaway(
            interaction, prize, entry_cost, winner_count, duration_minutes,
            prize_type="role", prize_amount=None, prize_role=prize_role, channel=channel
        )

    async def _active_giveaway_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        try:
            res = await self._db_call(
                lambda: self.bot.supabase.table("giveaways").select("id, prize, ends_at")
                        .eq("guild_id", str(interaction.guild_id)).eq("status", "active")
                        .order("ends_at").limit(25).execute()
            )
        except Exception as e:
            print(f"[GIVEAWAY][WARN] Autocomplete lookup failed: {type(e).__name__}: {e}", flush=True)
            return []

        current_lower = current.lower()
        choices = []
        for row in res.data or []:
            if current_lower and current_lower not in row["prize"].lower():
                continue
            ends_at = datetime.fromisoformat(row["ends_at"])
            label = f"{row['prize']} (ends {ends_at.strftime('%Y-%m-%d %H:%M UTC')})"[:100]
            choices.append(app_commands.Choice(name=label, value=str(row["id"])))
        return choices[:25]

    # ══════════════════════════════════════════════════════════
    #  /giveaway end - 조기 마감. check_expired_giveaways와 완전히 같은 원자적 클레임
    #  (_claim_and_conclude)을 공유한다 - 자동 타이머 틱과 이 명령어가 거의 동시에 발생해도
    #  status='active' 조건부 UPDATE 덕분에 정확히 하나만 실제로 마감 처리를 수행한다.
    # ══════════════════════════════════════════════════════════
    @giveaway_group.command(name="end", description="End an active giveaway immediately and draw winners now.")
    @app_commands.describe(giveaway="The active giveaway to end early.")
    @app_commands.autocomplete(giveaway=_active_giveaway_autocomplete)
    @app_commands.checks.has_permissions(administrator=True)
    async def giveaway_end(self, interaction: discord.Interaction, giveaway: str):
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id

        try:
            giveaway_id = int(giveaway)
        except ValueError:
            msg = await self.get_msg(guild_id, "giveaway_end_not_found")
            await interaction.followup.send(msg, ephemeral=True)
            return

        try:
            res = await self._db_call(
                lambda: self.bot.supabase.table("giveaways").select("*")
                        .eq("id", giveaway_id).eq("guild_id", str(guild_id)).execute()
            )
            giveaway_row = res.data[0] if res.data else None
        except Exception as e:
            print(f"[GIVEAWAY][ERROR] Failed to look up giveaway {giveaway_id} for early end: "
                  f"{type(e).__name__}: {e}", flush=True)
            msg = await self.get_msg(guild_id, "giveaway_end_error")
            await interaction.followup.send(msg, ephemeral=True)
            return

        if not giveaway_row:
            msg = await self.get_msg(guild_id, "giveaway_end_not_found")
            await interaction.followup.send(msg, ephemeral=True)
            return

        result = await self._claim_and_conclude(giveaway_row)

        if result == "claimed":
            msg = await self.get_msg(guild_id, "giveaway_end_success")
        elif result == "already_concluded":
            msg = await self.get_msg(guild_id, "giveaway_end_already_concluded")
        else:
            msg = await self.get_msg(guild_id, "giveaway_end_error")
        await interaction.followup.send(msg, ephemeral=True)

    async def _create_giveaway(self, interaction: discord.Interaction, prize: str, entry_cost: int,
                                winner_count: int, duration_minutes: int, prize_type: str,
                                prize_amount: int | None, prize_role: discord.Role | None,
                                channel: discord.TextChannel | None) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id

        invalid_metrics = (
            entry_cost <= 0 or winner_count <= 0
            or (prize_type == "points" and (prize_amount is None or prize_amount <= 0))
        )
        if invalid_metrics:
            msg = await self.get_msg(guild_id, "giveaway_metrics_error")
            await interaction.followup.send(msg, ephemeral=True)
            return

        if not (GIVEAWAY_MIN_DURATION_MINUTES <= duration_minutes <= GIVEAWAY_MAX_DURATION_MINUTES):
            msg = await self.get_msg(guild_id, "giveaway_err_duration_range",
                                      min=GIVEAWAY_MIN_DURATION_MINUTES, max=GIVEAWAY_MAX_DURATION_MINUTES)
            await interaction.followup.send(msg, ephemeral=True)
            return

        target_channel = channel or interaction.channel
        ends_at = datetime.now(timezone.utc) + timedelta(minutes=duration_minutes)

        # 1) DB에 먼저 기록한다 (message_id는 메시지를 보내야 알 수 있어 아직 비워둔다).
        try:
            insert_res = await self._db_call(
                lambda: self.bot.supabase.table("giveaways").insert({
                    "guild_id": str(guild_id),
                    "channel_id": str(target_channel.id),
                    "prize": prize,
                    "prize_type": prize_type,
                    "prize_amount": prize_amount,
                    "prize_role_id": str(prize_role.id) if prize_role else None,
                    "entry_cost": entry_cost,
                    "winner_count": winner_count,
                    "ends_at": ends_at.isoformat(),
                    "created_by": str(interaction.user.id),
                }).execute()
            )
            giveaway_row = insert_res.data[0] if insert_res.data else None
        except Exception as e:
            print(f"[GIVEAWAY][ERROR] Insert failed: {type(e).__name__}: {e}", flush=True)
            giveaway_row = None

        if not giveaway_row:
            msg = await self.get_msg(guild_id, "giveaway_err_save_failed")
            await interaction.followup.send(msg, ephemeral=True)
            return

        giveaway_id = giveaway_row["id"]

        # 2) 공지 임베드 + 응모 버튼 전송
        embed = await self._build_active_embed(giveaway_row)
        enter_label = await self.get_msg(guild_id, "giveaway_btn_label")
        view = GiveawayEntryView(self, enter_label)

        try:
            announce_message = await target_channel.send(embed=embed, view=view)
        except Exception as e:
            print(f"[GIVEAWAY][ERROR] Failed to post giveaway {giveaway_id} announcement: "
                  f"{type(e).__name__}: {e}", flush=True)
            msg = await self.get_msg(guild_id, "giveaway_err_save_failed")
            await interaction.followup.send(msg, ephemeral=True)
            return

        # 3) message_id 기록 - 이게 있어야 버튼 클릭 시 이 행을 다시 찾을 수 있다.
        try:
            await self._db_call(
                lambda: self.bot.supabase.table("giveaways")
                        .update({"message_id": str(announce_message.id)})
                        .eq("id", giveaway_id).execute()
            )
        except Exception as e:
            print(f"[GIVEAWAY][ERROR] Failed to record message_id for giveaway {giveaway_id}: "
                  f"{type(e).__name__}: {e}", flush=True)
            msg = await self.get_msg(guild_id, "giveaway_err_save_failed")
            await interaction.followup.send(msg, ephemeral=True)
            return

        ts = int(ends_at.timestamp())
        msg = await self.get_msg(guild_id, "giveaway_admin_success", timestamp=ts)
        await interaction.followup.send(msg, ephemeral=True)

    async def _build_active_embed(self, giveaway_row: dict) -> discord.Embed:
        guild_id = int(giveaway_row["guild_id"])
        ends_at = datetime.fromisoformat(giveaway_row["ends_at"])
        ts = int(ends_at.timestamp())

        title = await self.get_msg(guild_id, "giveaway_active_title", prize=giveaway_row["prize"])
        desc = await self.get_msg(guild_id, "giveaway_active_desc",
                                   winners=giveaway_row["winner_count"], time=f"<t:{ts}:R>")
        cost_label = await self.get_msg(guild_id, "giveaway_field_entry_cost")

        embed = discord.Embed(title=title, description=desc, color=discord.Color.gold())
        embed.add_field(name=cost_label, value=f"`{giveaway_row['entry_cost']:,}`", inline=True)
        return embed

    # ══════════════════════════════════════════════════════════
    #  응모 처리 - INSERT(유니크 제약으로 이중 응모 방지)가 먼저, 포인트 차감은 그다음.
    #  실패 시 롤백 대상이 "방금 만든 응모 기록 하나"뿐이도록 순서를 설계했다(반대 순서였다면
    #  "차감은 됐는데 응모 등록 실패"에서 포인트를 되돌려줘야 하는 훨씬 복잡한 롤백이 필요했다).
    # ══════════════════════════════════════════════════════════
    async def handle_entry(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        user_id = interaction.user.id
        message_id = str(interaction.message.id)

        try:
            res = await self._db_call(
                lambda: self.bot.supabase.table("giveaways").select("*")
                        .eq("message_id", message_id).execute()
            )
            giveaway_row = res.data[0] if res.data else None
        except Exception as e:
            print(f"[GIVEAWAY][ERROR] Failed to look up giveaway for message {message_id}: "
                  f"{type(e).__name__}: {e}", flush=True)
            await interaction.followup.send("❌ An error occurred while processing your entry.", ephemeral=True)
            return

        if not giveaway_row or giveaway_row["status"] != "active":
            msg = await self.get_msg(guild_id, "giveaway_expired")
            await interaction.followup.send(msg, ephemeral=True)
            return

        ends_at = datetime.fromisoformat(giveaway_row["ends_at"])
        if ends_at <= datetime.now(timezone.utc):
            msg = await self.get_msg(guild_id, "giveaway_expired")
            await interaction.followup.send(msg, ephemeral=True)
            return

        giveaway_id = giveaway_row["id"]
        entry_cost = giveaway_row["entry_cost"]

        # (사전 체크, UX 응답 속도용 - 진짜 방어선은 아래 INSERT의 UNIQUE 제약)
        try:
            existing = await self._db_call(
                lambda: self.bot.supabase.table("giveaway_entries").select("user_id")
                        .eq("giveaway_id", giveaway_id).eq("user_id", str(user_id)).execute()
            )
            if existing.data:
                msg = await self.get_msg(guild_id, "giveaway_err_already_entered")
                await interaction.followup.send(msg, ephemeral=True)
                return
        except Exception as e:
            print(f"[GIVEAWAY][WARN] Pre-check for existing entry failed (giveaway={giveaway_id}): "
                  f"{type(e).__name__}: {e}", flush=True)

        user_data = await self.bot.get_user_data(str(user_id), str(guild_id))
        current_points = user_data.get("points", 0)
        if current_points < entry_cost:
            msg = await self.get_msg(guild_id, "giveaway_err_insufficient_points",
                                      points=current_points, cost=entry_cost)
            await interaction.followup.send(msg, ephemeral=True)
            return

        # 응모 기록을 먼저 INSERT - UNIQUE 제약이 이중 응모(동시 클릭 레이스 포함)를 원자적으로 막는다.
        try:
            await self._db_call(
                lambda: self.bot.supabase.table("giveaway_entries").insert({
                    "giveaway_id": giveaway_id, "user_id": str(user_id),
                }).execute()
            )
        except Exception as e:
            err_str = str(e)
            if "duplicate key" in err_str or "23505" in err_str:
                print(f"[GIVEAWAY][INFO] Duplicate entry attempt blocked by unique constraint "
                      f"(user={user_id}, giveaway={giveaway_id})", flush=True)
                msg = await self.get_msg(guild_id, "giveaway_err_already_entered")
            else:
                print(f"[GIVEAWAY][ERROR] Entry insert failed for user={user_id} giveaway={giveaway_id}: "
                      f"{type(e).__name__}: {e}", flush=True)
                msg = await self.get_msg(guild_id, "giveaway_err_save_failed")
            await interaction.followup.send(msg, ephemeral=True)
            return

        # 포인트 차감 - 실패하면 방금 만든 응모 기록을 롤백한다.
        user_data["points"] = current_points - entry_cost
        save_ok = await self.bot.save_user_data(str(user_id), str(guild_id), user_data)
        if not save_ok:
            try:
                await self._db_call(
                    lambda: self.bot.supabase.table("giveaway_entries").delete()
                            .eq("giveaway_id", giveaway_id).eq("user_id", str(user_id)).execute()
                )
            except Exception as e:
                print(f"[GIVEAWAY][ERROR] Rollback of entry failed for user={user_id} giveaway={giveaway_id}: "
                      f"{type(e).__name__}: {e}", flush=True)
            msg = await self.get_msg(guild_id, "giveaway_err_save_failed")
            await interaction.followup.send(msg, ephemeral=True)
            return

        msg = await self.get_msg(guild_id, "giveaway_success")
        await interaction.followup.send(msg, ephemeral=True)

    # ══════════════════════════════════════════════════════════
    #  마감 처리 - 이 쿼리 한 줄이 정기 체크와 콜드 스타트 복구를 동시에 해결한다.
    #  ends_at은 DB에 저장된 절대시각이라, 봇이 꺼져있던 동안 지난 마감이든 방금 지난
    #  마감이든 같은 WHERE 조건이 똑같이 잡아낸다 - voice.py처럼 별도 복구 분기가 필요 없다.
    # ══════════════════════════════════════════════════════════
    @tasks.loop(seconds=GIVEAWAY_CHECK_INTERVAL_SECONDS)
    async def check_expired_giveaways(self):
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            res = await self._db_call(
                lambda: self.bot.supabase.table("giveaways").select("*")
                        .eq("status", "active").lte("ends_at", now_iso).execute()
            )
            expired = res.data or []
        except Exception as e:
            print(f"[GIVEAWAY][ERROR] Failed to query expired giveaways: {type(e).__name__}: {e}", flush=True)
            return

        for giveaway_row in expired:
            await self._claim_and_conclude(giveaway_row)

    @check_expired_giveaways.before_loop
    async def before_check_expired_giveaways(self):
        await self.bot.wait_until_ready()

    async def _claim_and_conclude(self, giveaway_row: dict) -> str:
        """반환값: 'claimed'(내가 선점해서 처리함) / 'already_concluded'(다른 경로가 선점) / 'error'.

        🛡️ 자동 타이머(check_expired_giveaways)와 수동 조기 마감(/giveaway end)이 이 메서드
        하나를 공유한다 - status='active' 조건이 걸린 UPDATE가 원자적 클레임이라, 거의
        동시에 들어와도 실제로 행이 갱신되는(claim_res.data가 채워지는) 쪽 단 하나만 진행한다.
        automod의 SET NX와 같은 정신, 매체만 Postgres.
        """
        try:
            claim_res = await self._db_call(
                lambda: self.bot.supabase.table("giveaways")
                        .update({"status": "concluded"})
                        .eq("id", giveaway_row["id"]).eq("status", "active").execute()
            )
        except Exception as e:
            print(f"[GIVEAWAY][ERROR] Failed to claim giveaway {giveaway_row['id']}: "
                  f"{type(e).__name__}: {e}", flush=True)
            return "error"

        if not claim_res.data:
            return "already_concluded"  # 다른 인스턴스/경로가 이미 선점함

        try:
            await self.conclude_giveaway(giveaway_row)
        except Exception as e:
            print(f"[GIVEAWAY][ERROR] Failed to conclude giveaway {giveaway_row['id']}: "
                  f"{type(e).__name__}: {e}", flush=True)
            return "error"

        return "claimed"

    async def conclude_giveaway(self, giveaway_row: dict) -> None:
        guild_id = int(giveaway_row["guild_id"])
        giveaway_id = giveaway_row["id"]

        try:
            entries_res = await self._db_call(
                lambda: self.bot.supabase.table("giveaway_entries").select("user_id")
                        .eq("giveaway_id", giveaway_id).execute()
            )
            entrant_ids = [row["user_id"] for row in (entries_res.data or [])]
        except Exception as e:
            print(f"[GIVEAWAY][ERROR] Failed to fetch entries for giveaway {giveaway_id}: "
                  f"{type(e).__name__}: {e}", flush=True)
            entrant_ids = []

        channel = self.bot.get_channel(int(giveaway_row["channel_id"]))

        if not entrant_ids:
            try:
                await self._db_call(
                    lambda: self.bot.supabase.table("giveaways").update({
                        "winners": [], "concluded_at": datetime.now(timezone.utc).isoformat()
                    }).eq("id", giveaway_id).execute()
                )
            except Exception as e:
                print(f"[GIVEAWAY][ERROR] Failed to record empty-winners state for giveaway {giveaway_id}: "
                      f"{type(e).__name__}: {e}", flush=True)

            if channel is not None:
                msg = await self.get_msg(guild_id, "giveaway_no_participants")
                try:
                    await channel.send(f"🎁 **{giveaway_row['prize']}**\n{msg}")
                except Exception as e:
                    print(f"[GIVEAWAY][ERROR] Failed to announce no-participants for giveaway {giveaway_id}: "
                          f"{type(e).__name__}: {e}", flush=True)
            await self._clear_announcement_view(giveaway_row)
            return

        winners = select_winners(entrant_ids, giveaway_row["winner_count"])

        try:
            await self._db_call(
                lambda: self.bot.supabase.table("giveaways").update({
                    "winners": winners, "concluded_at": datetime.now(timezone.utc).isoformat()
                }).eq("id", giveaway_id).execute()
            )
        except Exception as e:
            print(f"[GIVEAWAY][ERROR] Failed to record winners for giveaway {giveaway_id}: "
                  f"{type(e).__name__}: {e}", flush=True)

        if giveaway_row["prize_type"] == "points":
            await self._payout_points(guild_id, giveaway_id, winners, giveaway_row["prize_amount"])
        elif giveaway_row["prize_type"] == "role":
            await self._payout_role(guild_id, giveaway_id, winners, giveaway_row["prize_role_id"])

        if channel is not None:
            mentions = " ".join(f"<@{uid}>" for uid in winners)
            title = await self.get_msg(guild_id, "giveaway_concluded_title")
            desc = await self.get_msg(guild_id, "giveaway_concluded_desc", prize=giveaway_row["prize"], winners=mentions)
            congrats = await self.get_msg(guild_id, "giveaway_congratulations", mentions=mentions, prize=giveaway_row["prize"])
            payout_notice = await self.get_msg(guild_id, "giveaway_payout_notice")

            embed = discord.Embed(
                title=title, description=f"{desc}\n\n{congrats}\n\n{payout_notice}",
                color=discord.Color.green()
            )
            try:
                await channel.send(embed=embed)
            except Exception as e:
                print(f"[GIVEAWAY][ERROR] Failed to post conclusion announcement for giveaway {giveaway_id}: "
                      f"{type(e).__name__}: {e}", flush=True)

        await self._clear_announcement_view(giveaway_row)

    async def _clear_announcement_view(self, giveaway_row: dict) -> None:
        channel = self.bot.get_channel(int(giveaway_row["channel_id"]))
        message_id = giveaway_row.get("message_id")
        if channel is None or not message_id:
            return
        try:
            message = await channel.fetch_message(int(message_id))
            await message.edit(view=None)
        except Exception as e:
            print(f"[GIVEAWAY][WARN] Failed to clear buttons on giveaway announcement {message_id}: "
                  f"{type(e).__name__}: {e}", flush=True)

    # ══════════════════════════════════════════════════════════
    #  당첨 지급 - custom_commands.py의 역할 부여 로직과 동일한 권한/위계 체크 재사용.
    #  포인트 지급 실패는 당첨자별로 개별 로그만 남기고 나머지 당첨자 처리/발표는 그대로
    #  진행한다 - 한 명의 지급 실패가 전체 발표를 막는 것보다 낫다고 판단(사용자 승인됨).
    # ══════════════════════════════════════════════════════════
    async def _payout_points(self, guild_id: int, giveaway_id, winners: list[str], prize_amount: int) -> None:
        for user_id in winners:
            try:
                user_data = await self.bot.get_user_data(str(user_id), str(guild_id))
                user_data["points"] = user_data.get("points", 0) + prize_amount
                save_ok = await self.bot.save_user_data(str(user_id), str(guild_id), user_data)
                if not save_ok:
                    print(f"[GIVEAWAY][ERROR] Failed to pay out points to winner user={user_id} "
                          f"giveaway={giveaway_id} amount={prize_amount}", flush=True)
            except Exception as e:
                print(f"[GIVEAWAY][ERROR] Exception paying out points to winner user={user_id} "
                      f"giveaway={giveaway_id}: {type(e).__name__}: {e}", flush=True)

    async def _payout_role(self, guild_id: int, giveaway_id, winners: list[str], role_id: str) -> None:
        for user_id in winners:
            await self._payout_role_single(guild_id, giveaway_id, user_id, role_id)

    async def _payout_role_single(self, guild_id: int, giveaway_id, user_id: str, role_id: str) -> bool:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            print(f"[GIVEAWAY][ERROR] Guild {guild_id} not in cache, cannot pay out role for giveaway {giveaway_id}", flush=True)
            return False

        role = guild.get_role(int(role_id)) if role_id else None
        if role is None:
            print(f"[GIVEAWAY][ERROR] Prize role {role_id} no longer exists (guild={guild_id}, giveaway={giveaway_id})", flush=True)
            return False

        bot_member = guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_roles:
            print(f"[GIVEAWAY][ERROR] Bot lacks manage_roles permission (guild={guild_id}, giveaway={giveaway_id})", flush=True)
            return False

        if bot_member.top_role <= role:
            print(f"[GIVEAWAY][ERROR] Role hierarchy: bot's top role '{bot_member.top_role}' is not above "
                  f"prize role '{role.name}' (guild={guild_id}, giveaway={giveaway_id})", flush=True)
            return False

        member = guild.get_member(int(user_id))
        if member is None:
            print(f"[GIVEAWAY][ERROR] Winner {user_id} is not a cached guild member, cannot grant role "
                  f"(guild={guild_id}, giveaway={giveaway_id})", flush=True)
            return False

        try:
            await member.add_roles(role, reason=f"[KYVO GIVEAWAY] {giveaway_id}")
            return True
        except discord.Forbidden:
            print(f"[GIVEAWAY][ERROR] Forbidden while granting prize role to winner user={user_id} "
                  f"(guild={guild_id}, giveaway={giveaway_id})", flush=True)
            return False
        except discord.HTTPException as e:
            print(f"[GIVEAWAY][ERROR] HTTPException while granting prize role: {type(e).__name__}: {e} "
                  f"(guild={guild_id}, giveaway={giveaway_id})", flush=True)
            return False

    # ══════════════════════════════════════════════════════════
    #  재추첨 - 이미 발표된 당첨자 한 명을 다른 응모자로 교체한다(전체 재추첨이 아님).
    #  포인트 상품은 원 당첨자에게서 회수 + 새 당첨자에게 지급하지만, 역할 상품은 자동으로
    #  회수하지 않는다 - 그 유저가 다른 경로로 이미 정당하게 갖고 있던 역할일 수도 있어서,
    #  봇은 이걸 구분할 방법이 없다. role_note로 호출자(대시보드)에게 수동 처리가 필요함을 알린다.
    # ══════════════════════════════════════════════════════════
    async def _replace_winner(self, giveaway_row: dict, original_winner_id: str, admin_id, reason: str | None) -> dict:
        guild_id = int(giveaway_row["guild_id"])
        giveaway_id = giveaway_row["id"]

        if giveaway_row["status"] != "concluded":
            return {"status": "not_concluded"}

        winners = list(giveaway_row.get("winners") or [])
        if original_winner_id not in winners:
            return {"status": "not_a_winner"}

        try:
            entries_res = await self._db_call(
                lambda: self.bot.supabase.table("giveaway_entries").select("user_id")
                        .eq("giveaway_id", giveaway_id).execute()
            )
            entrant_ids = [row["user_id"] for row in (entries_res.data or [])]
        except Exception as e:
            print(f"[GIVEAWAY][ERROR] Failed to fetch entries for replacement (giveaway={giveaway_id}): "
                  f"{type(e).__name__}: {e}", flush=True)
            return {"status": "error"}

        remaining_pool = [uid for uid in entrant_ids if uid not in winners]
        if not remaining_pool:
            return {"status": "no_remaining_entrants"}

        new_winner_id = select_winners(remaining_pool, 1)[0]
        new_winners = [new_winner_id if uid == original_winner_id else uid for uid in winners]

        # 🛡️ 원자적 클레임: winners 배열에 여전히 original_winner_id가 들어있을 때만 갱신한다
        # (.contains 필터). 같은 재추첨 요청이 중복 제출되는 레이스를 막는다 - 앞선 기능들의
        # status='pending'/'active' 조건부 UPDATE와 동일한 정신, 조건만 배열 포함 여부로 바뀐 것.
        # winners는 jsonb 컬럼이라 .contains에 Postgres 배열 리터럴이 아닌 JSON 문자열을 넘겨야 한다
        # (supabase-py는 plain list를 주면 자동으로 '{a,b}' 리터럴로 직렬화하는데, 이는 jsonb가
        # 아닌 네이티브 배열 컬럼용이라 여기선 json.dumps로 직접 JSON 배열 문자열을 만들어 넘긴다).
        try:
            update_res = await self._db_call(
                lambda: self.bot.supabase.table("giveaways")
                        .update({"winners": new_winners})
                        .eq("id", giveaway_id).eq("status", "concluded")
                        .contains("winners", json.dumps([original_winner_id])).execute()
            )
        except Exception as e:
            print(f"[GIVEAWAY][ERROR] Failed to update winners for replacement (giveaway={giveaway_id}): "
                  f"{type(e).__name__}: {e}", flush=True)
            return {"status": "error"}

        if not update_res.data:
            return {"status": "already_replaced"}

        role_note = False
        payout_ok = None

        if giveaway_row["prize_type"] == "points":
            prize_amount = giveaway_row["prize_amount"]
            try:
                original_data = await self.bot.get_user_data(str(original_winner_id), str(guild_id))
                new_points = original_data.get("points", 0) - prize_amount
                if new_points < 0:
                    print(f"[GIVEAWAY][WARN] Reclaiming prize points from user={original_winner_id} "
                          f"(giveaway={giveaway_id}) drives their balance negative ({new_points}) - "
                          f"they likely already spent it.", flush=True)
                original_data["points"] = new_points
                await self.bot.save_user_data(str(original_winner_id), str(guild_id), original_data)
            except Exception as e:
                print(f"[GIVEAWAY][ERROR] Failed to reclaim points from original winner user={original_winner_id} "
                      f"(giveaway={giveaway_id}): {type(e).__name__}: {e}", flush=True)

            try:
                new_data = await self.bot.get_user_data(str(new_winner_id), str(guild_id))
                new_data["points"] = new_data.get("points", 0) + prize_amount
                payout_ok = await self.bot.save_user_data(str(new_winner_id), str(guild_id), new_data)
            except Exception as e:
                print(f"[GIVEAWAY][ERROR] Failed to pay out points to replacement winner user={new_winner_id} "
                      f"(giveaway={giveaway_id}): {type(e).__name__}: {e}", flush=True)
                payout_ok = False
        elif giveaway_row["prize_type"] == "role":
            role_note = True  # 원 당첨자 역할은 자동 회수하지 않음 - 호출자가 안내 문구를 보여줘야 함
            payout_ok = await self._payout_role_single(guild_id, giveaway_id, new_winner_id, giveaway_row["prize_role_id"])

        try:
            await self._db_call(
                lambda: self.bot.supabase.table("giveaway_replacements").insert({
                    "giveaway_id": giveaway_id, "original_winner_id": str(original_winner_id),
                    "new_winner_id": str(new_winner_id), "replaced_by": str(admin_id), "reason": reason,
                }).execute()
            )
        except Exception as e:
            print(f"[GIVEAWAY][ERROR] Failed to record replacement audit row (giveaway={giveaway_id}): "
                  f"{type(e).__name__}: {e}", flush=True)

        channel = self.bot.get_channel(int(giveaway_row["channel_id"]))
        if channel is not None:
            title = await self.get_msg(guild_id, "giveaway_replaced_announcement_title")
            desc = await self.get_msg(guild_id, "giveaway_replaced_announcement_desc",
                                       prize=giveaway_row["prize"], original=f"<@{original_winner_id}>",
                                       new_winner=f"<@{new_winner_id}>")
            embed = discord.Embed(title=title, description=desc, color=discord.Color.orange())
            try:
                await channel.send(embed=embed)
            except Exception as e:
                print(f"[GIVEAWAY][ERROR] Failed to post replacement announcement (giveaway={giveaway_id}): "
                      f"{type(e).__name__}: {e}", flush=True)

        return {
            "status": "ok", "giveaway_id": giveaway_id, "original_winner_id": str(original_winner_id),
            "new_winner_id": str(new_winner_id), "prize_type": giveaway_row["prize_type"],
            "role_note": role_note, "payout_ok": payout_ok,
        }

    async def handle_replace_winner_webhook(self, request: web.Request) -> web.Response:
        secret_header = request.headers.get("X-Internal-Secret", "")
        if not secret_header or not hmac.compare_digest(secret_header, INTERNAL_API_SECRET):
            print("[GIVEAWAY][WARN] Rejected replace-winner request with invalid/missing internal secret", flush=True)
            return web.Response(status=403)

        try:
            body = await request.json()
        except Exception:
            return web.Response(status=400)

        guild_id = body.get("guild_id")
        giveaway_id = body.get("giveaway_id")
        original_winner_id = body.get("original_winner_id")
        admin_id = body.get("admin_id")
        reason = body.get("reason")
        if not all([guild_id, giveaway_id, original_winner_id, admin_id]):
            return web.Response(status=400, text="guild_id, giveaway_id, original_winner_id, admin_id are all required")

        try:
            res = await self._db_call(
                lambda: self.bot.supabase.table("giveaways").select("*")
                        .eq("id", giveaway_id).eq("guild_id", str(guild_id)).execute()
            )
            giveaway_row = res.data[0] if res.data else None
        except Exception as e:
            print(f"[GIVEAWAY][ERROR] Failed to look up giveaway {giveaway_id} for replacement: "
                  f"{type(e).__name__}: {e}", flush=True)
            return web.json_response({"status": "error"}, status=500)

        if not giveaway_row:
            return web.json_response({"status": "giveaway_not_found"}, status=404)

        result = await self._replace_winner(giveaway_row, str(original_winner_id), admin_id, reason)

        if result["status"] != "ok":
            status_to_http = {
                "not_concluded": 400, "not_a_winner": 400, "no_remaining_entrants": 400,
                "already_replaced": 409, "error": 500,
            }
            return web.json_response(result, status=status_to_http.get(result["status"], 400))

        return web.json_response(result)


async def setup(bot):
    cog = KyvoGiveaway(bot)
    await bot.add_cog(cog)
    # 🛡️ Persistent View 등록 - 재시작 후에도 이전에 보낸 추첨 공지 메시지의 응모 버튼이
    # 계속 작동하려면, 프로세스가 새로 시작될 때마다 매번 다시 등록해야 한다.
    bot.add_view(GiveawayEntryView(cog))
    print("[⚡ GIVEAWAY] Cog extension setup complete.", flush=True)
