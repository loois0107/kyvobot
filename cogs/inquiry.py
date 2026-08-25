import discord
from discord import app_commands
from cogs.base import KyvoBaseCog
import asyncio
import os
import time
from datetime import datetime, timezone

SUPPORT_CHANNEL_ID = os.environ.get("SUPPORT_CHANNEL_ID")
SUPPORT_SERVER_INVITE_URL = os.environ.get("SUPPORT_SERVER_INVITE_URL")

INQUIRY_COOLDOWN_SECONDS = 300  # 유저 1명당 5분 - 개발자에게 스팸성 문의가 몰리는 것을 막기 위한 기본값

# 서포트 채널 문의 임베드의 [답변하기] 버튼 custom_id - 재시작 후에도 discord.py가 이 문자열만으로
# 콜백을 다시 찾아 연결할 수 있어야 하므로 고정 문자열이어야 한다 (inquiry_id는 여기 넣지 않는다 -
# 아래 InquirySupportView docstring 참고. anonymous_reports.py의 관리자 큐 버튼과 동일한 이유).
INQUIRY_REPLY_ID = "kyvo_inquiry:reply"

INQUIRY_EMBED_COLOR = 0x5865F2  # ticket_ai.py 설정 패널과 동일한 블러플 - "지원/서포트" 계열 임베드 관례


class InquiryModal(discord.ui.Modal):
    """/inquiry - 유저가 문의 내용을 입력하는 모달."""

    def __init__(self, cog: "KyvoInquiry", title: str, label: str):
        super().__init__(title=title[:45])  # 디스코드 모달 제목 45자 제한
        self.cog = cog
        self.content_input = discord.ui.TextInput(
            label=label[:45], style=discord.TextStyle.paragraph, max_length=4000, required=True
        )
        self.add_item(self.content_input)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.handle_inquiry_submission(interaction, self.content_input.value)


class InquiryReplyModal(discord.ui.Modal):
    """서포트 채널 [답변하기] 버튼 클릭 후 뜨는, 개발자가 답변을 입력하는 모달.

    inquiry_row는 버튼 클릭 시점(open_reply_modal)에 interaction.message.id로 미리 조회해
    생성자에 넘겨받는다 - 모달 제출 시점의 Interaction에는 원본 메시지 정보가 없기 때문에,
    필요한 문의 행 데이터를 모달 인스턴스에 직접 들려 보낸다.
    """

    def __init__(self, cog: "KyvoInquiry", inquiry_row: dict, title: str, label: str):
        super().__init__(title=title[:45])
        self.cog = cog
        self.inquiry_row = inquiry_row
        self.reply_input = discord.ui.TextInput(
            label=label[:45], style=discord.TextStyle.paragraph, max_length=4000, required=True
        )
        self.add_item(self.reply_input)

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.handle_inquiry_reply_submission(interaction, self.inquiry_row, self.reply_input.value)


class InquirySupportView(discord.ui.View):
    """서포트 채널의 문의 임베드에 붙는 영구(Persistent) View - [답변하기] 버튼 1개.

    anonymous_reports.py의 AnonymousReportAdminView와 완전히 동일한 이유로 timeout=None +
    고정 custom_id로 만든다(discord.py가 재시작 후에도 custom_id 문자열만으로 콜백을 다시
    연결할 수 있게 하기 위함 - setup()에서 bot.add_view()로 등록해야 함).

    🛡️ [중요] 등록된 이 View 인스턴스 하나를 "모든" 문의 메시지가 공유한다 - 새 문의마다 새
    View 객체가 생기는 게 아니다. 그래서 어떤 문의인지는 self에 저장하지 않고, 매 클릭마다
    interaction.message.id로 DB에서 다시 찾는다.
    """

    def __init__(self, cog: "KyvoInquiry"):
        super().__init__(timeout=None)
        self.cog = cog

        reply_btn = discord.ui.Button(label="답변하기", style=discord.ButtonStyle.primary,
                                       custom_id=INQUIRY_REPLY_ID)
        reply_btn.callback = self._on_reply
        self.add_item(reply_btn)

    async def _on_reply(self, interaction: discord.Interaction):
        await self.cog.open_reply_modal(interaction)


class KyvoInquiry(KyvoBaseCog):
    def __init__(self, bot):
        super().__init__(bot)
        # user_id -> 마지막 문의 제출 시각 (economy.py /daily의 수동 쿨다운 dict와 동일한 패턴).
        # 서포트 채널이 길드별이 아니라 봇 전체에서 단 하나이므로, 쿨다운도 길드가 아닌
        # user_id 하나만으로 전역 적용한다.
        self.cooldowns: dict[int, float] = {}

    async def cog_load(self):
        if not SUPPORT_CHANNEL_ID:
            print("[INQUIRY][WARN] SUPPORT_CHANNEL_ID not set - /inquiry will accept submissions but silently "
                  "fail to deliver them until this is configured.", flush=True)
        if not SUPPORT_SERVER_INVITE_URL:
            print("[INQUIRY][WARN] SUPPORT_SERVER_INVITE_URL not set - the inquiry confirmation message will "
                  "contain a broken support-server link until this is configured.", flush=True)

    async def _db_call(self, fn):
        """supabase-py는 동기 클라이언트라 이벤트 루프를 막지 않도록 executor로 감싼다."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.bot.db_executor, fn)

    def _resolve_support_channel(self) -> discord.abc.Messageable | None:
        if not SUPPORT_CHANNEL_ID:
            return None
        try:
            channel_id = int(SUPPORT_CHANNEL_ID)
        except ValueError:
            print(f"[INQUIRY][ERROR] SUPPORT_CHANNEL_ID is not a valid integer: {SUPPORT_CHANNEL_ID!r}", flush=True)
            return None
        return self.bot.get_channel(channel_id)

    # ══════════════════════════════════════════════════════════
    #  /inquiry - 1단계: 모달 오픈
    # ══════════════════════════════════════════════════════════
    @app_commands.command(name="inquiry", description="Send an inquiry directly to the KyvoBot developer.")
    async def inquiry(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        title = await self.get_msg(guild_id, "inquiry_modal_title")
        label = await self.get_msg(guild_id, "inquiry_modal_label")
        modal = InquiryModal(self, title, label)
        await interaction.response.send_modal(modal)

    # ══════════════════════════════════════════════════════════
    #  /inquiry - 2단계: 제출 처리
    # ══════════════════════════════════════════════════════════
    async def handle_inquiry_submission(self, interaction: discord.Interaction, content: str) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        user_id = interaction.user.id
        now = time.time()

        if user_id in self.cooldowns and now - self.cooldowns[user_id] < INQUIRY_COOLDOWN_SECONDS:
            remaining = INQUIRY_COOLDOWN_SECONDS - (now - self.cooldowns[user_id])
            minutes, seconds = int(remaining // 60), int(remaining % 60)
            msg = await self.get_msg(guild_id, "inquiry_err_cooldown", minutes=minutes, seconds=seconds)
            await interaction.followup.send(msg, ephemeral=True)
            return

        support_channel = self._resolve_support_channel()
        if support_channel is None:
            print(f"[INQUIRY][ERROR] Support channel unavailable (SUPPORT_CHANNEL_ID={SUPPORT_CHANNEL_ID!r}) - "
                  f"dropping inquiry from user={user_id} guild={guild_id}.", flush=True)
            msg = await self.get_msg(guild_id, "inquiry_send_failed")
            await interaction.followup.send(msg, ephemeral=True)
            return

        # 1) DB에 먼저 기록한다 (support_message_id는 메시지를 보내야 알 수 있어 아직 비워둔다).
        try:
            insert_res = await self._db_call(
                lambda: self.bot.supabase.table("inquiries").insert({
                    "guild_id": str(guild_id),
                    "user_id": str(user_id),
                    "content": content,
                    "status": "pending",
                }).execute()
            )
            inquiry_row = insert_res.data[0] if insert_res.data else None
        except Exception as e:
            print(f"[INQUIRY][ERROR] Insert failed: {type(e).__name__}: {e}", flush=True)
            inquiry_row = None

        if not inquiry_row:
            msg = await self.get_msg(guild_id, "inquiry_send_failed")
            await interaction.followup.send(msg, ephemeral=True)
            return

        inquiry_id = inquiry_row["id"]

        # 2) 서포트 채널에 전송 - 어느 서버/누가 보냈는지 포함(anonymous_reports와 달리 익명이 아니다).
        guild_name = interaction.guild.name if interaction.guild else str(guild_id)
        embed = discord.Embed(title="📨 새로운 문의", description=content, color=INQUIRY_EMBED_COLOR,
                               timestamp=datetime.now(timezone.utc))
        embed.add_field(name="서버", value=f"{guild_name}\n`{guild_id}`", inline=True)
        embed.add_field(name="문의자", value=f"{interaction.user.mention}\n{interaction.user} (`{user_id}`)", inline=True)
        embed.set_footer(text=f"문의 ID: {inquiry_id}")
        view = InquirySupportView(self)

        try:
            support_message = await support_channel.send(embed=embed, view=view)
        except Exception as e:
            print(f"[INQUIRY][ERROR] Failed to post inquiry {inquiry_id} to support channel: "
                  f"{type(e).__name__}: {e}", flush=True)
            # DB 행은 support_message_id 없이 pending으로 남는다 - anonymous_reports와 동일한 정책으로,
            # 심각한 문제 발생 시 봇 운영자가 DB에서 직접 확인 가능하다.
            msg = await self.get_msg(guild_id, "inquiry_send_failed")
            await interaction.followup.send(msg, ephemeral=True)
            return

        # 3) support_message_id 기록 - 이게 있어야 [답변하기] 클릭 시 이 행을 다시 찾을 수 있다.
        try:
            await self._db_call(
                lambda: self.bot.supabase.table("inquiries")
                        .update({"support_message_id": str(support_message.id)})
                        .eq("id", inquiry_id).execute()
            )
        except Exception as e:
            print(f"[INQUIRY][ERROR] Failed to record support_message_id for inquiry {inquiry_id}: "
                  f"{type(e).__name__}: {e}", flush=True)
            msg = await self.get_msg(guild_id, "inquiry_send_failed")
            await interaction.followup.send(msg, ephemeral=True)
            return

        # 저장이 실제로 성공했을 때만 쿨다운을 건다 - economy.py /daily와 동일한 이유
        # (실패했는데 묶어두면 문의도 못 보내고 재시도도 못 함).
        self.cooldowns[user_id] = now

        msg = await self.get_msg(guild_id, "inquiry_submitted", invite_url=SUPPORT_SERVER_INVITE_URL or "")
        confirm_embed = discord.Embed(description=msg, color=discord.Color.green())
        await interaction.followup.send(embed=confirm_embed, ephemeral=True)

    # ══════════════════════════════════════════════════════════
    #  [답변하기] 버튼 - 1단계: 답변 모달 오픈
    # ══════════════════════════════════════════════════════════
    async def open_reply_modal(self, interaction: discord.Interaction) -> None:
        message_id = str(interaction.message.id)
        try:
            res = await self._db_call(
                lambda: self.bot.supabase.table("inquiries")
                        .select("*")
                        .eq("support_message_id", message_id)
                        .execute()
            )
            inquiry_row = res.data[0] if res.data else None
        except Exception as e:
            print(f"[INQUIRY][ERROR] Failed to look up inquiry for message {message_id}: "
                  f"{type(e).__name__}: {e}", flush=True)
            await interaction.response.send_message("❌ 문의 정보를 불러오는 중 오류가 발생했습니다.", ephemeral=True)
            return

        if not inquiry_row or inquiry_row["status"] != "pending":
            await interaction.response.send_message("⚠️ 이미 답변 처리된 문의입니다.", ephemeral=True)
            return

        modal = InquiryReplyModal(self, inquiry_row, "문의 답변 작성", "답변 내용을 입력해주세요")
        await interaction.response.send_modal(modal)

    # ══════════════════════════════════════════════════════════
    #  [답변하기] 버튼 - 2단계: 답변 제출 처리 (DM 전송)
    #  - UPDATE에 WHERE status='pending' 조건을 걸어 먼저 원자적으로 "선점"한 뒤에만 DM을 보낸다
    #    (anonymous_reports._finalize_report와 동일한 이유) - 두 관리자가 거의 동시에 [답변하기]를
    #    눌러도 실제로 DM이 나가는 쪽은 하나뿐이다. DM 전송에 실패하면 선점을 되돌려 재시도 가능하게 한다.
    # ══════════════════════════════════════════════════════════
    async def handle_inquiry_reply_submission(self, interaction: discord.Interaction, inquiry_row: dict,
                                                reply_content: str) -> None:
        await interaction.response.defer(ephemeral=True)
        inquiry_id = inquiry_row["id"]
        guild_id = inquiry_row["guild_id"]
        reporter_user_id = inquiry_row["user_id"]
        answered_at = datetime.now(timezone.utc).isoformat()

        try:
            update_res = await self._db_call(
                lambda: self.bot.supabase.table("inquiries")
                        .update({"status": "answered", "answered_by": str(interaction.user.id),
                                 "answered_at": answered_at})
                        .eq("id", inquiry_id).eq("status", "pending").execute()
            )
        except Exception as e:
            print(f"[INQUIRY][ERROR] Failed to claim inquiry {inquiry_id} for reply: {type(e).__name__}: {e}", flush=True)
            await interaction.followup.send("❌ 답변 처리 중 오류가 발생했습니다.", ephemeral=True)
            return

        if not update_res.data:
            # 다른 관리자가 거의 동시에 먼저 답변을 선점했다.
            await interaction.followup.send("⚠️ 다른 관리자가 이미 이 문의에 답변했습니다.", ephemeral=True)
            return

        # DM 발송 - 실패 시(DM 차단 등) 선점을 되돌려 재시도 가능하게 하고, 서포트 채널에 알림을 남긴다.
        try:
            target_user = await self.bot.fetch_user(int(reporter_user_id))
            dm_title = await self.get_msg(int(guild_id), "inquiry_reply_dm_title")
            dm_footer = await self.get_msg(int(guild_id), "inquiry_reply_dm_footer")
            dm_embed = discord.Embed(title=dm_title, description=reply_content, color=discord.Color.blurple(),
                                      timestamp=datetime.now(timezone.utc))
            dm_embed.set_footer(text=dm_footer)
            await target_user.send(embed=dm_embed)
        except (discord.Forbidden, discord.HTTPException) as e:
            print(f"[INQUIRY][WARN] Failed to DM inquirer user_id={reporter_user_id} for inquiry {inquiry_id}: "
                  f"{type(e).__name__}: {e}", flush=True)
            try:
                await self._db_call(
                    lambda: self.bot.supabase.table("inquiries")
                            .update({"status": "pending", "answered_by": None, "answered_at": None})
                            .eq("id", inquiry_id).execute()
                )
            except Exception as rollback_err:
                print(f"[INQUIRY][ERROR] Failed to roll back claim on inquiry {inquiry_id}: "
                      f"{type(rollback_err).__name__}: {rollback_err}", flush=True)

            support_channel = self._resolve_support_channel()
            if support_channel is not None:
                try:
                    await support_channel.send(
                        f"⚠️ 문의 ID {inquiry_id}의 답변을 <@{reporter_user_id}>(`{reporter_user_id}`)님에게 "
                        f"DM으로 전달하지 못했습니다 (DM 차단 등). 문의는 다시 미답변 상태로 되돌렸습니다."
                    )
                except Exception as notice_err:
                    print(f"[INQUIRY][ERROR] Failed to post DM-failure notice for inquiry {inquiry_id}: "
                          f"{type(notice_err).__name__}: {notice_err}", flush=True)

            await interaction.followup.send(
                "❌ 답변 DM 전송에 실패했습니다 (유저가 DM을 차단했을 수 있습니다). 서포트 채널에 알림을 남겼고, "
                "문의는 다시 답변 대기 상태로 되돌렸습니다.", ephemeral=True)
            return

        # DM 성공 - 서포트 채널 원본 메시지에서 버튼을 제거하고 처리 결과를 표시한다.
        support_channel = self._resolve_support_channel()
        support_message_id = inquiry_row.get("support_message_id")
        if support_channel is not None and support_message_id:
            try:
                message = await support_channel.fetch_message(int(support_message_id))
                original_embed = message.embeds[0] if message.embeds else discord.Embed()
                updated_embed = original_embed.copy()
                updated_embed.add_field(name="상태", value=f"✅ 답변 완료 (by {interaction.user.mention})", inline=False)
                await message.edit(embed=updated_embed, view=None)
            except Exception as e:
                print(f"[INQUIRY][WARN] Failed to edit support message for inquiry {inquiry_id}: "
                      f"{type(e).__name__}: {e}", flush=True)

        await interaction.followup.send("✅ 답변을 전송했습니다.", ephemeral=True)


async def setup(bot):
    cog = KyvoInquiry(bot)
    await bot.add_cog(cog)
    # 🛡️ Persistent View 등록 - 재시작 후에도 이전에 보낸 문의 메시지의 [답변하기] 버튼이 계속
    # 작동하려면, 프로세스가 새로 시작될 때마다 매번 다시 등록해야 한다(등록은 프로세스 생애주기당
    # 1회, 메시지/문의마다가 아님 - custom_id 문자열만으로 라우팅되기 때문).
    bot.add_view(InquirySupportView(cog))
    print("[⚡ INQUIRY] Cog extension setup complete.", flush=True)
