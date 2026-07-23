import discord
from discord import app_commands
from discord.ext import commands
from cogs.base import KyvoBaseCog
import asyncio
import time
from datetime import datetime, timezone

REPORT_DAILY_LIMIT = 3
REPORT_DAILY_WINDOW_SECONDS = 86400

# 관리자 큐 버튼의 custom_id - 재시작 후에도 discord.py가 이 문자열만으로 콜백을 다시 찾아
# 연결할 수 있어야 하므로 고정 문자열이어야 한다 (report_id는 여기 넣지 않는다 - 아래 View
# 클래스 docstring 참고).
ANON_REPORT_APPROVE_ID = "kyvo_anon_report:approve"
ANON_REPORT_REJECT_ID = "kyvo_anon_report:reject"
ANON_REPORT_BLOCK_ID = "kyvo_anon_report:block"


class ReportConsentView(discord.ui.View):
    """/anonymous_report의 1단계 - 동의 문구를 보여주고, 동의해야만 모달이 열린다."""

    def __init__(self, cog: "KyvoAnonymousReports", agree_label: str):
        super().__init__(timeout=120)
        self.cog = cog

        btn = discord.ui.Button(label=agree_label, style=discord.ButtonStyle.primary)
        btn.callback = self._on_agree
        self.add_item(btn)

    async def _on_agree(self, interaction: discord.Interaction):
        await self.cog.open_report_modal(interaction)


class ReportModal(discord.ui.Modal):
    """/anonymous_report의 2단계 - 실제 제보 내용을 입력받는 모달."""

    def __init__(self, cog: "KyvoAnonymousReports", title: str, label: str):
        super().__init__(title=title[:45])  # 디스코드 모달 제목 45자 제한
        self.cog = cog
        self.content_input = discord.ui.TextInput(
            label=label[:45], style=discord.TextStyle.paragraph, max_length=4000, required=True
        )
        self.add_item(self.content_input)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.handle_report_submission(interaction, self.content_input.value)


class AnonymousReportAdminView(discord.ui.View):
    """관리자 큐 메시지에 붙는 영구(Persistent) View.

    관리자가 언제 처리할지 알 수 없고 그 사이 봇이 재배포될 수 있으므로 timeout=None +
    고정 custom_id로 만든다(discord.py가 재시작 후에도 custom_id 문자열만으로 콜백을
    다시 연결할 수 있게 하기 위함 - setup()에서 bot.add_view()로 등록해야 함).

    🛡️ [중요] 등록된 이 View 인스턴스 하나를 "모든" 제보 메시지가 공유한다 - 새 제보마다
    새 View 객체가 생기는 게 아니다. 그래서 어떤 report인지는 self에 저장하지 않고, 매
    클릭마다 interaction.message.id(=admin_message_id)로 DB에서 다시 찾는다. 만약 report_id를
    self.report_id 같은 인스턴스 속성에 저장하면, 서로 다른 제보 메시지의 버튼 클릭끼리
    상태가 뒤섞이는 버그가 된다(공유 인스턴스이므로).
    """

    def __init__(self, cog: "KyvoAnonymousReports",
                 approve_label: str = "Approve", reject_label: str = "Reject",
                 block_label: str = "Block Reporter (Anonymous)"):
        super().__init__(timeout=None)
        self.cog = cog

        approve_btn = discord.ui.Button(label=approve_label, style=discord.ButtonStyle.success,
                                         custom_id=ANON_REPORT_APPROVE_ID)
        approve_btn.callback = self._on_approve
        self.add_item(approve_btn)

        reject_btn = discord.ui.Button(label=reject_label, style=discord.ButtonStyle.danger,
                                        custom_id=ANON_REPORT_REJECT_ID)
        reject_btn.callback = self._on_reject
        self.add_item(reject_btn)

        block_btn = discord.ui.Button(label=block_label, style=discord.ButtonStyle.secondary,
                                       custom_id=ANON_REPORT_BLOCK_ID)
        block_btn.callback = self._on_block
        self.add_item(block_btn)

    async def _on_approve(self, interaction: discord.Interaction):
        await self.cog.handle_admin_decision(interaction, "approve")

    async def _on_reject(self, interaction: discord.Interaction):
        await self.cog.handle_admin_decision(interaction, "reject")

    async def _on_block(self, interaction: discord.Interaction):
        await self.cog.handle_admin_decision(interaction, "block")


class KyvoAnonymousReports(KyvoBaseCog):
    def __init__(self, bot):
        super().__init__(bot)
        # Redis 장애 시 폴백용 (guild_id, user_id) -> (오늘 카운트, 윈도우 만료 시각)
        self.daily_limit_cache: dict[tuple[int, int], tuple[int, float]] = {}

    async def _db_call(self, fn):
        """supabase-py는 동기 클라이언트라 이벤트 루프를 막지 않도록 executor로 감싼다."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, fn)

    async def _get_report_settings(self, guild_id) -> dict:
        """anonymous_reports_settings를 Redis Cache-Aside(KyvoBaseCog.get_guild_settings)로 읽어온다."""
        row = await self.get_guild_settings(guild_id)
        nested_settings = row.get("settings") or {}
        return nested_settings.get("anonymous_reports_settings") or {}

    # ══════════════════════════════════════════════════════════
    #  일일 제출 한도 (SET NX로 카운터를 초기화 + INCR로 원자적 증가 - leveling의
    #  SET NX 클레임 정신을 불리언이 아닌 카운터로 확장한 형태. automod의 ZSET 슬라이딩
    #  윈도우는 짧은 시간 다건 감지용이라 "하루 3건" 같은 고정 긴 윈도우엔 맞지 않는다.)
    # ══════════════════════════════════════════════════════════
    async def _check_daily_limit(self, guild_id: int, user_id: int) -> bool:
        """True면 오늘 아직 한도 이내(제출 허용), False면 한도 초과."""
        key = f"report_daily:{guild_id}:{user_id}"
        try:
            await self.redis.set(key, 0, ex=REPORT_DAILY_WINDOW_SECONDS, nx=True)
            count = await self.redis.incr(key)
            return count <= REPORT_DAILY_LIMIT
        except Exception as e:
            print(f"[ANON_REPORT][WARN] Redis daily-limit check failed, falling back to local: "
                  f"{type(e).__name__}: {e}", flush=True)
            return self._check_daily_limit_local(guild_id, user_id)

    def _check_daily_limit_local(self, guild_id: int, user_id: int) -> bool:
        key = (guild_id, user_id)
        now = time.time()
        count, expiry = self.daily_limit_cache.get(key, (0, now + REPORT_DAILY_WINDOW_SECONDS))
        if now > expiry:
            count, expiry = 0, now + REPORT_DAILY_WINDOW_SECONDS
        count += 1
        self.daily_limit_cache[key] = (count, expiry)
        return count <= REPORT_DAILY_LIMIT

    # ══════════════════════════════════════════════════════════
    #  익명 차단 여부 확인
    # ══════════════════════════════════════════════════════════
    async def _is_banned(self, guild_id: int, user_id: int) -> bool:
        try:
            res = await self._db_call(
                lambda: self.bot.supabase.table("anonymous_report_bans")
                        .select("user_id")
                        .eq("guild_id", str(guild_id)).eq("user_id", str(user_id))
                        .execute()
            )
            return bool(res.data)
        except Exception as e:
            # 조회 실패 시 차단 안 된 것으로 취급(fail-open) - DB 장애로 정당한 제보까지
            # 전부 막히는 것보다, 드문 장애 창구에 차단된 유저의 제보가 한 번 새는 게 낫다고 판단.
            print(f"[ANON_REPORT][ERROR] Ban check failed, failing open: {type(e).__name__}: {e}", flush=True)
            return False

    # ══════════════════════════════════════════════════════════
    #  /anonymous_report - 1단계: 동의 화면
    # ══════════════════════════════════════════════════════════
    @app_commands.command(name="anonymous_report", description="Submit an anonymous report to server administrators.")
    async def anonymous_report(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        settings = await self._get_report_settings(guild_id)
        if not settings.get("admin_channel_id"):
            msg = await self.get_msg(guild_id, "anon_report_not_configured")
            await interaction.response.send_message(msg, ephemeral=True)
            return

        consent_msg = await self.get_msg(guild_id, "anon_report_consent")
        agree_label = await self.get_msg(guild_id, "anon_report_agree_button")
        view = ReportConsentView(self, agree_label)
        await interaction.response.send_message(consent_msg, view=view, ephemeral=True)

    async def open_report_modal(self, interaction: discord.Interaction) -> None:
        title = await self.get_msg(interaction.guild_id, "anon_report_modal_title")
        label = await self.get_msg(interaction.guild_id, "anon_report_modal_label")
        modal = ReportModal(self, title, label)
        await interaction.response.send_modal(modal)

    # ══════════════════════════════════════════════════════════
    #  /anonymous_report - 2단계: 제출 처리
    # ══════════════════════════════════════════════════════════
    async def handle_report_submission(self, interaction: discord.Interaction, content: str) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        user_id = interaction.user.id

        matched_word = await self.check_forbidden_words(guild_id, content)
        if matched_word:
            msg = await self.get_msg(guild_id, "anon_report_filtered")
            await interaction.followup.send(msg, ephemeral=True)
            return

        if await self._is_banned(guild_id, user_id):
            # 🛡️ 조용한 거절: 정상 제출과 100% 동일한 메시지. DB 기록도, 일일 한도 소비도 없음 -
            # 차단된 유저가 이 응답만으로 자신이 차단됐다는 걸 알아낼 방법이 없어야 한다.
            msg = await self.get_msg(guild_id, "anon_report_submitted")
            await interaction.followup.send(msg, ephemeral=True)
            return

        if not await self._check_daily_limit(guild_id, user_id):
            msg = await self.get_msg(guild_id, "anon_report_limit_reached")
            await interaction.followup.send(msg, ephemeral=True)
            return

        settings = await self._get_report_settings(guild_id)
        admin_channel_id = settings.get("admin_channel_id")
        admin_channel = interaction.guild.get_channel(int(admin_channel_id)) if admin_channel_id else None
        if admin_channel is None:
            msg = await self.get_msg(guild_id, "anon_report_not_configured")
            await interaction.followup.send(msg, ephemeral=True)
            return

        # 1) DB에 먼저 기록한다 (admin_message_id는 메시지를 보내야 알 수 있어 아직 비워둔다).
        try:
            insert_res = await self._db_call(
                lambda: self.bot.supabase.table("anonymous_reports").insert({
                    "guild_id": str(guild_id),
                    "user_id": str(user_id),
                    "content": content,
                    "status": "pending",
                }).execute()
            )
            report_row = insert_res.data[0] if insert_res.data else None
        except Exception as e:
            print(f"[ANON_REPORT][ERROR] Insert failed: {type(e).__name__}: {e}", flush=True)
            report_row = None

        if not report_row:
            msg = await self.get_msg(guild_id, "anon_report_save_failed")
            await interaction.followup.send(msg, ephemeral=True)
            return

        report_id = report_row["id"]

        # 2) 관리자 채널에 전송 - 내용만, 작성자 식별 정보는 절대 포함하지 않는다.
        title = await self.get_msg(guild_id, "anon_report_admin_title")
        footer = await self.get_msg(guild_id, "anon_report_admin_footer")
        approve_label = await self.get_msg(guild_id, "anon_report_btn_approve")
        reject_label = await self.get_msg(guild_id, "anon_report_btn_reject")
        block_label = await self.get_msg(guild_id, "anon_report_btn_block")

        embed = discord.Embed(title=title, description=content, color=discord.Color.dark_teal(),
                               timestamp=datetime.now(timezone.utc))
        embed.set_footer(text=footer)
        view = AnonymousReportAdminView(self, approve_label, reject_label, block_label)

        try:
            admin_message = await admin_channel.send(embed=embed, view=view)
        except Exception as e:
            print(f"[ANON_REPORT][ERROR] Failed to post report {report_id} to admin channel: "
                  f"{type(e).__name__}: {e}", flush=True)
            # DB 행은 admin_message_id 없이 pending으로 남는다 - 심각한 문제 발생 시
            # 봇 운영자가 DB에서 직접 확인 가능하다(정책상 허용된 경로).
            msg = await self.get_msg(guild_id, "anon_report_save_failed")
            await interaction.followup.send(msg, ephemeral=True)
            return

        # 3) admin_message_id 기록 - 이게 있어야 버튼 클릭 시 이 행을 다시 찾을 수 있다.
        try:
            await self._db_call(
                lambda: self.bot.supabase.table("anonymous_reports")
                        .update({"admin_message_id": str(admin_message.id)})
                        .eq("id", report_id).execute()
            )
        except Exception as e:
            print(f"[ANON_REPORT][ERROR] Failed to record admin_message_id for report {report_id}: "
                  f"{type(e).__name__}: {e}", flush=True)
            msg = await self.get_msg(guild_id, "anon_report_save_failed")
            await interaction.followup.send(msg, ephemeral=True)
            return

        msg = await self.get_msg(guild_id, "anon_report_submitted")
        await interaction.followup.send(msg, ephemeral=True)

    # ══════════════════════════════════════════════════════════
    #  관리자 큐 버튼 처리 (승인 / 거절 / 익명 차단)
    # ══════════════════════════════════════════════════════════
    async def handle_admin_decision(self, interaction: discord.Interaction, action: str) -> None:
        await interaction.response.defer()
        guild_id = interaction.guild_id
        admin_message_id = str(interaction.message.id)

        try:
            res = await self._db_call(
                lambda: self.bot.supabase.table("anonymous_reports")
                        .select("*")
                        .eq("admin_message_id", admin_message_id)
                        .execute()
            )
            report_row = res.data[0] if res.data else None
        except Exception as e:
            print(f"[ANON_REPORT][ERROR] Failed to look up report for message {admin_message_id}: "
                  f"{type(e).__name__}: {e}", flush=True)
            await interaction.followup.send("❌ An error occurred while processing this report.", ephemeral=True)
            return

        if not report_row or report_row["status"] != "pending":
            msg = await self.get_msg(guild_id, "anon_report_already_resolved")
            await interaction.followup.send(msg, ephemeral=True)
            return

        report_id = report_row["id"]
        reporter_user_id = report_row["user_id"]  # 🛡️ 시스템 내부 전용 - 임베드/메시지 어디에도 노출 금지
        admin_id = interaction.user.id
        new_status = "approved" if action == "approve" else "rejected"

        try:
            await self._db_call(
                lambda: self.bot.supabase.table("anonymous_reports")
                        .update({"status": new_status, "resolved_by": str(admin_id),
                                 "resolved_at": datetime.now(timezone.utc).isoformat()})
                        .eq("id", report_id).execute()
            )
        except Exception as e:
            print(f"[ANON_REPORT][ERROR] Failed to update report {report_id} status: {type(e).__name__}: {e}", flush=True)
            await interaction.followup.send("❌ An error occurred while processing this report.", ephemeral=True)
            return

        if action == "block":
            try:
                await self._db_call(
                    lambda: self.bot.supabase.table("anonymous_report_bans")
                            .upsert({"guild_id": str(guild_id), "user_id": str(reporter_user_id),
                                     "banned_by": str(admin_id)})
                            .execute()
                )
            except Exception as e:
                print(f"[ANON_REPORT][ERROR] Failed to record ban for report {report_id}: {type(e).__name__}: {e}", flush=True)

        if action == "approve":
            settings = await self._get_report_settings(guild_id)
            publish_channel_id = settings.get("publish_channel_id")
            publish_channel = interaction.guild.get_channel(int(publish_channel_id)) if publish_channel_id else None
            if publish_channel is not None:
                pub_title = await self.get_msg(guild_id, "anon_report_publish_title")
                pub_footer = await self.get_msg(guild_id, "anon_report_publish_footer")
                pub_embed = discord.Embed(title=pub_title, description=report_row["content"],
                                           color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
                pub_embed.set_footer(text=pub_footer)
                try:
                    await publish_channel.send(embed=pub_embed)
                except Exception as e:
                    print(f"[ANON_REPORT][ERROR] Failed to publish approved report {report_id}: "
                          f"{type(e).__name__}: {e}", flush=True)

        resolved_key = {
            "approve": "anon_report_resolved_approved",
            "reject": "anon_report_resolved_rejected",
            "block": "anon_report_resolved_blocked",
        }[action]
        resolved_text = await self.get_msg(guild_id, resolved_key)

        original_embed = interaction.message.embeds[0] if interaction.message.embeds else discord.Embed()
        updated_embed = original_embed.copy()
        updated_embed.add_field(name="Status", value=resolved_text, inline=False)

        # 🛡️ [Persistent View 주의사항] self(AnonymousReportAdminView)는 등록된 하나의 인스턴스를
        # 모든 제보 메시지가 공유한다. 여기서 self.disabled=True로 mutate한 뒤 view=self로 edit하면
        # 다른 모든 제보 메시지의 버튼까지 같이 영향을 받는 공유 상태 버그가 된다. 그래서 view=None으로
        # 교체해 이 메시지에서만 버튼을 완전히 제거한다(등록된 템플릿 자체는 안 건드림).
        await interaction.message.edit(embed=updated_embed, view=None)

    # ══════════════════════════════════════════════════════════
    #  /anonymous_unban - 관리자 전용, 익명 차단 해제
    # ══════════════════════════════════════════════════════════
    @app_commands.command(name="anonymous_unban", description="Remove a user's anonymous-report block.")
    @app_commands.checks.has_permissions(administrator=True)
    async def anonymous_unban(self, interaction: discord.Interaction, user: discord.User):
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id

        try:
            res = await self._db_call(
                lambda: self.bot.supabase.table("anonymous_report_bans")
                        .delete()
                        .eq("guild_id", str(guild_id)).eq("user_id", str(user.id))
                        .execute()
            )
        except Exception as e:
            print(f"[ANON_REPORT][ERROR] Unban failed: {type(e).__name__}: {e}", flush=True)
            await interaction.followup.send(f"❌ {type(e).__name__}: {e}", ephemeral=True)
            return

        if res.data:
            msg = await self.get_msg(guild_id, "anon_unban_success", user=user.mention)
        else:
            msg = await self.get_msg(guild_id, "anon_unban_not_found", user=user.mention)
        await interaction.followup.send(msg, ephemeral=True)


async def setup(bot):
    cog = KyvoAnonymousReports(bot)
    await bot.add_cog(cog)
    # 🛡️ Persistent View 등록 - 재시작 후에도 이전에 보낸 관리자 큐 메시지의 버튼이 계속
    # 작동하려면, 프로세스가 새로 시작될 때마다 매번 다시 등록해야 한다(등록은 프로세스
    # 생애주기당 1회, 메시지/제보마다가 아님 - custom_id 문자열만으로 라우팅되기 때문).
    bot.add_view(AnonymousReportAdminView(cog))
    print("[⚡ ANONYMOUS_REPORTS] Cog extension setup complete.", flush=True)
