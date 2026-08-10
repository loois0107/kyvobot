import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import os
import time
import hmac
from datetime import datetime, timezone
from aiohttp import web
from openai import AsyncOpenAI
from cogs.base import KyvoBaseCog

INTERNAL_API_SECRET = os.environ.get("INTERNAL_API_SECRET")
TICKET_KNOWLEDGE_MAX_LENGTH = 4000  # OpenAI 토큰 한도 여유 + 과금/저장 폭주 방지 - party_game_presets 등과 동일한 관례

# 지식 검색(임베딩 생성/Supabase 매칭) 반복 실패 감지 - anonymous_reports의 일일 한도 카운터와 동일한
# SET NX + INCR 윈도우 패턴.
TICKET_AI_FAILURE_WINDOW_SECONDS = 600     # 10분
TICKET_AI_FAILURE_THRESHOLD = 5            # 윈도우 안 5회
TICKET_AI_ALERT_COOLDOWN_SECONDS = 3600    # 재알림까지 1시간

# 하루 AI 답변 한도 - "첫 요청 시점 기준 24시간 롤링"(anonymous_reports의 REPORT_DAILY_WINDOW_SECONDS와
# 동일한 정신, 타임존 문제 회피). guild_ticket_settings.daily_guild_limit/daily_user_limit이 없으면
# 이 기본값으로 폴백한다.
TICKET_AI_DAILY_WINDOW_SECONDS = 86400
TICKET_AI_DEFAULT_DAILY_GUILD_LIMIT = 400
TICKET_AI_DEFAULT_DAILY_USER_LIMIT = 20

# AI 답변 피드백 버튼의 custom_id - anonymous_reports/giveaway의 영구 View와 동일한 이유로
# 고정 문자열이어야 한다 (message_id는 여기 넣지 않는다 - 아래 TicketFeedbackView docstring 참고).
TICKET_FEEDBACK_UP_ID = "kyvo_ticket_feedback:up"
TICKET_FEEDBACK_DOWN_ID = "kyvo_ticket_feedback:down"

class OpenTicketView(discord.ui.View):
    """
    Persistent View class responsible for handling the initial ticket creation button matrix.
    Registered globally within setup invocation to survive bot container restarts.

    🛡️ [다국어] btn_label은 기본값(영어)이 있지만, 실제로 티켓 패널을 보낼 때(ticket_setup)는
    항상 self.open_ticket.label에 get_msg로 조회한 값을 덮어써서 보낸다 - 여기 기본값은
    setup() 안에서 bot 재시작 시 인터랙션 라우팅용으로만 등록되는 인스턴스(실제로 화면에
    새로 렌더링되지 않음, custom_id 매칭만 필요)에 쓰인다.
    """
    def __init__(self, cog, btn_label: str = "📩 Open Support Ticket"):
        super().__init__(timeout=None)
        self.cog = cog
        self.open_ticket.label = btn_label

    @discord.ui.button(label="📩 Open Support Ticket", style=discord.ButtonStyle.primary, custom_id="kyvo_ticket_open")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await self.cog.create_ticket_channel(interaction)

class TicketSystemView(discord.ui.View):
    """
    Persistent View instance appended inside active ticket channel instances.
    Provides administrative utility operations such as secure archiving and channel purging.
    """
    def __init__(self, cog, btn_label: str = "🔒 Close Ticket"):
        super().__init__(timeout=None)
        self.cog = cog
        self.close_ticket.label = btn_label

    @discord.ui.button(label="🔒 Close Ticket", style=discord.ButtonStyle.danger, custom_id="kyvo_ticket_close")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        channel = interaction.channel
        guild_id = channel.guild.id

        archiving_msg = await self.cog.get_msg(guild_id, "tai_close_archiving_msg")
        await interaction.followup.send(archiving_msg)

        # 🛡️ CORE UPGRADE: Trigger the dynamic AI summary engine before the channel is destroyed
        await self.cog.archive_and_log_ticket(channel, interaction.user)

        await asyncio.sleep(5)
        try:
            await channel.delete(reason="Ticket session closed and securely archived by administrative request.")
        except discord.Forbidden:
            print(f"[SECURITY ERROR] Missing channel management permissions for: {channel.name}")

class TicketFeedbackView(discord.ui.View):
    """AI 답변 메시지에 붙는 영구(Persistent) View.

    anonymous_reports의 관리자 큐 View와 완전히 같은 이유로 timeout=None + 고정 custom_id로
    만든다. 등록된 이 View 인스턴스 하나를 "모든" AI 답변 메시지가 공유하므로, 어떤 메시지인지는
    self에 저장하지 않고 매 클릭마다 interaction.message.id로 DB에서 다시 찾는다.

    🛡️ [다국어] up_label/down_label 기본값(영어)은 setup()의 전역 재등록 인스턴스용 - 실제 AI
    답변마다 새로 만들어지는 인스턴스(on_message)는 항상 get_msg로 조회한 값을 넘긴다.
    """

    def __init__(self, cog: "KyvoTicketAI", up_label: str = "Helpful", down_label: str = "Not Helpful"):
        super().__init__(timeout=None)
        self.cog = cog

        up_btn = discord.ui.Button(label=up_label, style=discord.ButtonStyle.success,
                                    custom_id=TICKET_FEEDBACK_UP_ID, emoji="👍")
        up_btn.callback = self._on_up
        self.add_item(up_btn)

        down_btn = discord.ui.Button(label=down_label, style=discord.ButtonStyle.danger,
                                      custom_id=TICKET_FEEDBACK_DOWN_ID, emoji="👎")
        down_btn.callback = self._on_down
        self.add_item(down_btn)

    async def _on_up(self, interaction: discord.Interaction):
        await self.cog.handle_feedback_click(interaction, True)

    async def _on_down(self, interaction: discord.Interaction):
        await self.cog.handle_feedback_click(interaction, False)


class KyvoTicketAI(KyvoBaseCog):
    def __init__(self, bot):
        # 🛡️ [다국어 전환] 다른 모든 Cog와 동일하게 KyvoBaseCog를 상속해서 self.get_msg /
        # self.get_guild_settings(Redis Cache-Aside)를 그대로 물려받는다 - 이 Cog는 원래
        # commands.Cog만 상속해서 get_msg가 아예 없었다(오늘 발견한 버그의 근본 원인).
        super().__init__(bot)
        self.ai_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

        # Anti-Spam and Concurrency Lock Management Layers
        self.processing_channels = set()
        self.channel_cooldowns = {}

        # 하루 AI 답변 한도 - Redis 장애 시 폴백용 (key -> (오늘 카운트, 윈도우 만료 시각)).
        # anonymous_reports의 daily_limit_cache와 동일한 정신 - 장애 시에도 진짜 작동하는 방어.
        self.daily_limit_cache: dict[str, tuple[int, float]] = {}

    async def cog_load(self):
        if INTERNAL_API_SECRET:
            self.bot.web_app.router.add_post("/internal/ticket-knowledge/add", self.handle_add_knowledge_webhook)
            print("[⚡ TICKET_AI] Internal add-knowledge route registered at /internal/ticket-knowledge/add.", flush=True)
        else:
            print("[TICKET_AI][WARN] INTERNAL_API_SECRET not set - dashboard-triggered knowledge base "
                  "writes are disabled (the /ticket-admin add-knowledge command still works).", flush=True)

    # ══════════════════════════════════════════════════════════
    #  지식베이스 추가 - /ticket-admin add-knowledge와 대시보드(내부 웹훅) 둘 다 이 함수 하나를
    #  공유한다. 임베딩 생성(OpenAI 호출)은 항상 여기, 봇 쪽에서만 한다 - 대시보드는 목록 조회/
    #  삭제만 직접 처리(Supabase 직결)하고, 실제 벡터 생성이 필요한 쓰기 작업은 전부 위임한다.
    # ══════════════════════════════════════════════════════════
    async def _add_knowledge(self, guild_id: str, content: str) -> dict:
        cleaned = (content or "").strip()
        if not cleaned:
            return {"status": "empty_content"}
        if len(cleaned) > TICKET_KNOWLEDGE_MAX_LENGTH:
            return {"status": "content_too_long", "max_length": TICKET_KNOWLEDGE_MAX_LENGTH}

        try:
            response = await self.ai_client.embeddings.create(
                model="text-embedding-3-small",
                input=cleaned
            )
            embedding_vector = response.data[0].embedding
        except Exception as e:
            print(f"[TICKET_AI][ERROR] Embedding generation failed (guild={guild_id}): {type(e).__name__}: {e}", flush=True)
            return {"status": "embedding_failed", "detail": f"{type(e).__name__}: {e}"}

        try:
            payload = {"guild_id": str(guild_id), "content": cleaned, "embedding": embedding_vector}
            insert_res = await asyncio.to_thread(self.bot.supabase.table("guild_knowledge").insert(payload).execute)
            row = insert_res.data[0] if insert_res.data else None
        except Exception as e:
            print(f"[TICKET_AI][ERROR] Insert failed (guild={guild_id}): {type(e).__name__}: {e}", flush=True)
            return {"status": "db_error", "detail": f"{type(e).__name__}: {e}"}

        return {"status": "ok", "id": row["id"] if row else None, "content": cleaned}

    async def handle_add_knowledge_webhook(self, request: web.Request) -> web.Response:
        secret_header = request.headers.get("X-Internal-Secret", "")
        if not secret_header or not hmac.compare_digest(secret_header, INTERNAL_API_SECRET):
            print("[TICKET_AI][WARN] Rejected add-knowledge request with invalid/missing internal secret", flush=True)
            return web.Response(status=403)

        try:
            body = await request.json()
        except Exception:
            return web.Response(status=400)

        guild_id = body.get("guild_id")
        content = body.get("content")
        if not guild_id or content is None:
            return web.Response(status=400, text="guild_id and content are both required")

        result = await self._add_knowledge(str(guild_id), str(content))

        status_to_http = {
            "empty_content": 400, "content_too_long": 400,
            "embedding_failed": 502, "db_error": 500,
        }
        if result["status"] != "ok":
            return web.json_response(result, status=status_to_http.get(result["status"], 400))

        return web.json_response(result)

    async def get_ticket_settings(self, guild_id: str):
        """Fetches server-specific custom ticket metadata configurations from Supabase."""
        try:
            response = await asyncio.to_thread(
                self.bot.supabase.table("guild_ticket_settings").select("*").eq("guild_id", guild_id).execute
            )
            if response.data:
                return response.data[0]
        except Exception as e:
            print(f"[DB SETTINGS ERROR] Failed to fetch ticket settings for {guild_id}: {e}")
        return None

    ticket_admin = app_commands.Group(
        name="ticket-admin",
        description="Manage the AI support ticket system's settings and reference info.",
        default_permissions=discord.Permissions(manage_guild=True)
    )

    @ticket_admin.command(name="add-knowledge", description="Teach the AI new information for ticket support.")
    @app_commands.describe(content="The actual guidelines, server rules, or FAQ text to teach the AI.")
    async def add_knowledge(self, interaction: discord.Interaction, content: str):
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id)

        result = await self._add_knowledge(guild_id, content)

        if result["status"] == "empty_content":
            msg = await self.get_msg(guild_id, "tai_admin_err_empty_content")
            await interaction.followup.send(msg, ephemeral=True)
        elif result["status"] == "content_too_long":
            msg = await self.get_msg(guild_id, "tai_admin_err_too_long", max=TICKET_KNOWLEDGE_MAX_LENGTH)
            await interaction.followup.send(msg, ephemeral=True)
        elif result["status"] != "ok":
            msg = await self.get_msg(guild_id, "tai_admin_err_failed", detail=result.get('detail', result['status']))
            await interaction.followup.send(msg, ephemeral=True)
        else:
            msg = await self.get_msg(guild_id, "tai_admin_success")
            await interaction.followup.send(msg, ephemeral=True)

    @app_commands.command(name="ticket-setup", description="Set up the AI-powered support ticket panel.")
    @app_commands.default_permissions(manage_guild=True)
    async def ticket_setup(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id)

        settings = await self.get_ticket_settings(guild_id)

        default_title = await self.get_msg(guild_id, "tai_setup_default_title")
        default_desc = await self.get_msg(guild_id, "tai_setup_default_desc")
        embed_title = settings.get("setup_title") if settings and settings.get("setup_title") else default_title
        embed_desc = settings.get("setup_desc") if settings and settings.get("setup_desc") else default_desc

        embed = discord.Embed(
            title=embed_title,
            description=embed_desc,
            color=0x5865f2
        )

        btn_label = await self.get_msg(guild_id, "tai_setup_btn_label")
        view = OpenTicketView(self, btn_label=btn_label)
        await interaction.channel.send(embed=embed, view=view)
        success_msg = await self.get_msg(guild_id, "tai_setup_success")
        await interaction.followup.send(success_msg, ephemeral=True)

    async def create_ticket_channel(self, interaction: discord.Interaction):
        """Spawns an isolated private support text channel encrypted with custom permission overwrites."""
        try:
            guild = interaction.guild
            user = interaction.user
            guild_id = str(guild.id)

            # Sanitize and compile strict channel name strings
            clean_name = "".join(c for c in user.name.lower() if c.isalnum() or c in "-_")
            if not clean_name:
                clean_name = str(user.id)
            target_channel_name = f"ticket-{clean_name}"

            # Anti-Spam Guard Check
            existing_channel = discord.utils.get(guild.text_channels, name=target_channel_name)
            if existing_channel:
                msg = await self.get_msg(guild_id, "tai_err_already_active", channel=existing_channel.mention)
                await interaction.followup.send(msg, ephemeral=True)
                return

            bot_member = guild.me
            if not bot_member:
                try:
                    bot_member = await guild.fetch_member(self.bot.user.id)
                except Exception:
                    bot_member = guild.me

            overwrites = {
                guild.default_role: discord.PermissionOverwrite(read_messages=False),
                user: discord.PermissionOverwrite(read_messages=True, send_messages=True, embed_links=True, attach_files=True),
            }
            if bot_member:
                overwrites[bot_member] = discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True)

            target_category = interaction.channel.category if hasattr(interaction.channel, 'category') else None

            ticket_channel = await guild.create_text_channel(
                name=target_channel_name,
                category=target_category,
                overwrites=overwrites,
                reason=f"Kyvo Ticket session init for {user.name}"
            )

            settings = await self.get_ticket_settings(guild_id)
            default_welcome_title = await self.get_msg(guild_id, "tai_welcome_default_title")
            default_welcome_desc = await self.get_msg(guild_id, "tai_welcome_default_desc", mention=user.mention)
            welcome_title = settings.get("welcome_title") if settings and settings.get("welcome_title") else default_welcome_title
            welcome_desc = settings.get("welcome_desc") if settings and settings.get("welcome_desc") else default_welcome_desc

            welcome = discord.Embed(
                title=welcome_title,
                description=welcome_desc,
                color=0x2b2d31
            )

            close_btn_label = await self.get_msg(guild_id, "tai_close_btn_label")
            view = TicketSystemView(self, btn_label=close_btn_label)
            await ticket_channel.send(embed=welcome, view=view)
            create_success_msg = await self.get_msg(guild_id, "tai_create_success", channel=ticket_channel.mention)
            await interaction.followup.send(create_success_msg, ephemeral=True)

        except discord.Forbidden:
            print(f"[CRITICAL PERMISSION ERROR] Bot lacks 'Manage Channels' permission in guild: {interaction.guild_id}")
            msg = await self.get_msg(interaction.guild_id, "tai_err_no_manage_channels")
            await interaction.followup.send(msg, ephemeral=True)
        except Exception as e:
            print(f"[TICKET CREATION EXCEPTION] Pipeline failed: {e}")
            msg = await self.get_msg(interaction.guild_id, "tai_err_internal_exception", error=e)
            await interaction.followup.send(msg, ephemeral=True)

    async def _resolve_admin_log_channel(self, guild: discord.Guild, guild_id: str) -> discord.TextChannel | None:
        """관리자에게 뭔가 알려야 할 때(티켓 아카이브, 지식 검색 반복 실패 등) 쓸 채널을 찾는다 -
        archive_and_log_ticket이 원래 쓰던 로직을 추출한 것. 두 군데 이상에서 재사용하므로 공유
        함수로 뺐다(이 세션 전체의 관례 - 두 번째 쓰임이 생기면 추출)."""
        guild_settings = await self.bot.get_guild_settings(guild_id)
        configured_log_id = guild_settings.get("antinuke_settings", {}).get("log_channel_id")

        if configured_log_id:
            log_channel = guild.get_channel(int(configured_log_id))
            if log_channel:
                return log_channel

        log_channel = discord.utils.get(guild.text_channels, name="ticket-logs")
        if log_channel:
            return log_channel

        log_channel = discord.utils.get(guild.text_channels, name="mod-logs")
        if log_channel:
            return log_channel

        return guild.system_channel

    async def _record_knowledge_search_failure(self, guild: discord.Guild, guild_id: str, stage: str, detail: str) -> None:
        """지식 검색(임베딩 생성/Supabase 매칭) 실패를 Redis 윈도우 카운터에 기록하고, 짧은 시간
        안에 반복되면 관리자 로그 채널에 1회 알린다. 유저에게는 여전히 아무것도 노출하지 않는다 -
        이건 순수 관리자용 신호다. 이 감지 자체가 실패해도(Redis 장애 등) 원래 티켓 응답 흐름을
        절대 막으면 안 되므로 전체가 fail-open이다."""
        fail_key = f"ticket_ai:knowledge_search_fail:{guild_id}"
        cooldown_key = f"ticket_ai:knowledge_search_alert_cooldown:{guild_id}"

        try:
            await self.bot.redis.set(fail_key, 0, ex=TICKET_AI_FAILURE_WINDOW_SECONDS, nx=True)
            count = await self.bot.redis.incr(fail_key)
        except Exception as e:
            print(f"[TICKET_AI][WARN] Failure-counter tracking itself failed, skipping alert check: "
                  f"{type(e).__name__}: {e}", flush=True)
            return

        if count < TICKET_AI_FAILURE_THRESHOLD:
            return

        try:
            cooldown_acquired = await self.bot.redis.set(cooldown_key, "1", ex=TICKET_AI_ALERT_COOLDOWN_SECONDS, nx=True)
        except Exception as e:
            print(f"[TICKET_AI][WARN] Alert cooldown check failed, skipping alert to avoid spam risk: "
                  f"{type(e).__name__}: {e}", flush=True)
            return

        if not cooldown_acquired:
            return  # 이미 최근에(쿨다운 안에) 알림을 보냈다

        log_channel = await self._resolve_admin_log_channel(guild, guild_id)
        if log_channel is None:
            print(f"[TICKET_AI][WARN] Knowledge search failing repeatedly (guild={guild_id}) but no admin "
                  f"log channel could be resolved to alert.", flush=True)
            return

        alert_title = await self.get_msg(guild_id, "tai_knowledge_alert_title")
        alert_desc = await self.get_msg(
            guild_id, "tai_knowledge_alert_desc",
            count=count, minutes=TICKET_AI_FAILURE_WINDOW_SECONDS // 60, stage=stage, detail=detail[:500],
        )
        alert_embed = discord.Embed(title=alert_title, description=alert_desc, color=0xe67e22)
        try:
            await log_channel.send(embed=alert_embed)
        except Exception as e:
            print(f"[TICKET_AI][ERROR] Failed to send knowledge-search-failure alert (guild={guild_id}): "
                  f"{type(e).__name__}: {e}", flush=True)

    # ══════════════════════════════════════════════════════════
    #  하루 AI 답변 한도 (서버 전체 + 유저 개인) - anonymous_reports의 일일 한도 카운터와 동일한
    #  SET NX + INCR 패턴. Redis 장애 시 인메모리 카운터로 진짜 방어를 유지한다(그냥 스킵 아님) -
    #  이 기능의 목적이 도배/비용 방어라, 장애 시 전부 허용으로 새면 방어가 필요한 순간 무력화된다.
    # ══════════════════════════════════════════════════════════
    async def _get_daily_limits(self, guild_id: str) -> tuple[int, int]:
        settings = await self.get_ticket_settings(guild_id)
        guild_limit = (settings or {}).get("daily_guild_limit") or TICKET_AI_DEFAULT_DAILY_GUILD_LIMIT
        user_limit = (settings or {}).get("daily_user_limit") or TICKET_AI_DEFAULT_DAILY_USER_LIMIT
        return guild_limit, user_limit

    async def _check_daily_limit(self, key: str, limit: int) -> bool:
        """True면 아직 한도 이내(허용), False면 한도 초과."""
        try:
            await self.bot.redis.set(key, 0, ex=TICKET_AI_DAILY_WINDOW_SECONDS, nx=True)
            count = await self.bot.redis.incr(key)
            return count <= limit
        except Exception as e:
            print(f"[TICKET_AI][WARN] Redis daily-limit check failed for '{key}', falling back to local: "
                  f"{type(e).__name__}: {e}", flush=True)
            return self._check_daily_limit_local(key, limit)

    def _check_daily_limit_local(self, key: str, limit: int) -> bool:
        now = time.time()
        count, expiry = self.daily_limit_cache.get(key, (0, now + TICKET_AI_DAILY_WINDOW_SECONDS))
        if now > expiry:
            count, expiry = 0, now + TICKET_AI_DAILY_WINDOW_SECONDS
        count += 1
        self.daily_limit_cache[key] = (count, expiry)
        return count <= limit

    # ══════════════════════════════════════════════════════════
    #  스태프 에스컬레이션 - AI가 스스로 판단해서(TRIGGER_STAFF_ALERT) 트리거하는 경우와, 하루
    #  한도 초과로 트리거하는 경우가 이 함수 하나를 공유한다. 한도 초과 시엔 OpenAI를 아예
    #  호출하지 않고 바로 이걸 실행하므로 그 티켓에서는 추가 비용이 들지 않는다.
    # ══════════════════════════════════════════════════════════
    async def _escalate_to_staff(self, channel: discord.TextChannel, mention: str, reason_key: str) -> None:
        guild_id = channel.guild.id
        reason = await self.get_msg(guild_id, reason_key)

        current_name = channel.name
        new_name = f"🚨-{current_name}"
        await channel.edit(name=new_name, reason=f"AI Smart escalation handover triggered ({reason}).")

        title = await self.get_msg(guild_id, "tai_escalate_title")
        desc = await self.get_msg(guild_id, "tai_escalate_desc", mention=mention)
        footer = await self.get_msg(guild_id, "tai_escalate_footer", reason=reason)
        escalation_embed = discord.Embed(title=title, description=desc, color=0xe74c3c)
        escalation_embed.set_footer(text=footer)
        await channel.send(embed=escalation_embed)

    async def archive_and_log_ticket(self, channel: discord.TextChannel, closed_by: discord.User):
        """🤖 TRANSCRIPT SUMMARY PILELINE: Extracts message history and generates an executive AI report."""
        try:
            guild = channel.guild
            guild_id = str(guild.id)

            # 1. Fetch recent text context strings inside the target channel node
            transcript_history = []
            async for msg in channel.history(limit=150, oldest_first=True):
                author_role = "BOT" if msg.author.bot else "USER"
                content = msg.content
                if msg.author.bot and msg.embeds:
                    content = f"[Embed Card] Title: {msg.embeds[0].title} | Desc: {msg.embeds[0].description}"
                
                if content:
                    transcript_history.append(f"[{msg.author.name} ({author_role})]: {content}")

            if not transcript_history:
                return

            full_transcript_text = "\n".join(transcript_history)

            # 2. Invoke GPT-4o-mini auditing node to distill the raw logs into a concise summary layout
            summary_system_prompt = (
                "You are an elite administrative support auditor inside a premium automation infrastructure.\n"
                "Analyze the provided raw support ticket conversation log and synthesize a clear, objective executive summary.\n"
                "Format requirements:\n"
                "- Write exactly 3 concise bullet points.\n"
                "- Point 1: Core issue/inquiry stated by the user.\n"
                "- Point 2: Actions taken by the AI assistant or human staff.\n"
                "- Point 3: Final resolution state or why the ticket was closed.\n"
                "Output pure clean English text. Do not embed code blocks or meta notes."
            )

            chat_response = await self.ai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": summary_system_prompt},
                    {"role": "user", "content": f"Dialogue Log Matrix:\n{full_transcript_text}"}
                ],
                max_tokens=250,
                temperature=0.3
            )
            ai_summary_report = chat_response.choices[0].message.content.strip()

            # 3. 📡 SMART ROUTING SECURITY: Strict sequence routing to prevent public general leaks
            log_channel = await self._resolve_admin_log_channel(guild, guild_id)

            # 4. Deliver the completed summary report archive packet
            if log_channel:
                archive_title = await self.get_msg(guild_id, "tai_archive_title", channel=channel.name)
                executor_field = await self.get_msg(guild_id, "tai_archive_field_executor")
                executor_value = await self.get_msg(guild_id, "tai_archive_field_executor_value", mention=closed_by.mention, id=closed_by.id)
                summary_field = await self.get_msg(guild_id, "tai_archive_field_summary")
                archive_footer = await self.get_msg(guild_id, "tai_archive_footer", guild_id=guild_id)

                archive_embed = discord.Embed(
                    title=archive_title,
                    color=0x34495e,
                    timestamp=discord.utils.utcnow()
                )
                archive_embed.add_field(name=executor_field, value=executor_value, inline=False)
                archive_embed.add_field(name=summary_field, value=ai_summary_report, inline=False)
                archive_embed.set_footer(text=archive_footer)

                await log_channel.send(embed=archive_embed)

        except Exception as e:
            print(f"[ARCHIVE LOG MATRIX FAULT] Critical pipeline blockage creating final report packet: {e}")

    # ══════════════════════════════════════════════════════════
    #  AI 답변 피드백 (👍/👎)
    # ══════════════════════════════════════════════════════════
    def _get_ticket_opener_id(self, channel: discord.TextChannel) -> str | None:
        """티켓 채널에는 소유권을 기록하는 별도 테이블이 없다 - create_ticket_channel이 부여한
        채널 오버라이트(봇/@everyone 제외한 유일한 멤버 = 개설자)에서 역산한다. 오버라이트가
        예상과 다르게 변형돼 있으면(수동 편집 등) 안전하게 None을 반환한다(투표는 fail-closed로 거부)."""
        bot_id = self.bot.user.id if self.bot.user else None
        candidates = [
            target for target in channel.overwrites
            if isinstance(target, discord.Member) and target.id != bot_id
        ]
        if len(candidates) == 1:
            return str(candidates[0].id)
        return None

    async def handle_feedback_click(self, interaction: discord.Interaction, rating: bool) -> None:
        # 🛡️ anonymous_reports/giveaway와 동일한 패턴 - defer 후 interaction.message.edit()를 쓴다
        # (interaction.response.edit_message()가 아니라). 실제 라이브 버튼 클릭과, 진짜 메시지를
        # 감싼 합성 interaction으로 하는 테스트 양쪽에서 동일한 코드 경로가 동작해야 하기 때문.
        await interaction.response.defer(ephemeral=True)
        message_id = str(interaction.message.id)

        guild_id = interaction.guild_id

        try:
            res = await asyncio.to_thread(
                self.bot.supabase.table("ticket_ai_feedback").select("*").eq("message_id", message_id).execute
            )
            row = res.data[0] if res.data else None
        except Exception as e:
            print(f"[TICKET_AI][ERROR] Failed to look up feedback row for message {message_id}: "
                  f"{type(e).__name__}: {e}", flush=True)
            msg = await self.get_msg(guild_id, "tai_feedback_err_generic")
            await interaction.followup.send(msg, ephemeral=True)
            return

        if not row:
            msg = await self.get_msg(guild_id, "tai_feedback_err_stale")
            await interaction.followup.send(msg, ephemeral=True)
            return

        if not row.get("opener_id") or row["opener_id"] != str(interaction.user.id):
            msg = await self.get_msg(guild_id, "tai_feedback_err_not_opener")
            await interaction.followup.send(msg, ephemeral=True)
            return

        # 🛡️ [중복 방지] 버튼 제거(voted 후 view=None)가 1차 방어선이지만, 편집이 반영되기 전에
        # 두 번째 클릭이 들어오는 레이스도 있을 수 있다 - rating이 이미 채워져 있으면 조용히
        # 덮어쓰지 않고 명시적으로 거부한다.
        if row.get("rating") is not None:
            msg = await self.get_msg(guild_id, "tai_feedback_err_already_rated")
            await interaction.followup.send(msg, ephemeral=True)
            return

        try:
            await asyncio.to_thread(
                self.bot.supabase.table("ticket_ai_feedback").update({
                    "rating": rating, "voted_at": datetime.now(timezone.utc).isoformat(),
                }).eq("message_id", message_id).execute
            )
        except Exception as e:
            print(f"[TICKET_AI][ERROR] Failed to record feedback for message {message_id}: "
                  f"{type(e).__name__}: {e}", flush=True)
            msg = await self.get_msg(guild_id, "tai_feedback_err_generic")
            await interaction.followup.send(msg, ephemeral=True)
            return

        field_name = await self.get_msg(guild_id, "tai_feedback_field_name")
        field_value = await self.get_msg(guild_id, "tai_feedback_value_up" if rating else "tai_feedback_value_down")
        original_embed = interaction.message.embeds[0] if interaction.message.embeds else discord.Embed()
        updated_embed = original_embed.copy()
        updated_embed.add_field(name=field_name, value=field_value, inline=False)
        await interaction.message.edit(embed=updated_embed, view=None)
        success_msg = await self.get_msg(guild_id, "tai_feedback_success")
        await interaction.followup.send(success_msg, ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        if not message.channel.name.startswith("ticket-") or message.channel.name.startswith("🚨-"):
            return

        guild_id = str(message.guild.id)
        channel_id = message.channel.id
        user_query = message.content

        if len(user_query.strip()) < 2:
            return

        current_time = time.time()
        last_message_time = self.channel_cooldowns.get(channel_id, 0)
        if current_time - last_message_time < 1.5:
            return  
        self.channel_cooldowns[channel_id] = current_time

        if channel_id in self.processing_channels:
            return  

        self.processing_channels.add(channel_id)

        try:
            # 🛡️ 하루 한도 체크는 OpenAI 호출 전에 한다 - 한도 초과면 답변 대신 바로 에스컬레이션
            # 하므로 그 요청에서는 임베딩/채팅 완성 호출이 아예 발생하지 않는다(비용 절감 겸함).
            # 서버 전체 한도를 먼저 보고(더 넓은 게이트), 그다음 유저 개인 한도를 본다.
            guild_limit, user_limit = await self._get_daily_limits(guild_id)
            guild_key = f"ticket_ai_daily:guild:{guild_id}"
            user_key = f"ticket_ai_daily:user:{guild_id}:{message.author.id}"

            if not await self._check_daily_limit(guild_key, guild_limit):
                await self._escalate_to_staff(
                    message.channel, message.author.mention,
                    "tai_escalate_reason_guild_limit"
                )
                return

            if not await self._check_daily_limit(user_key, user_limit):
                await self._escalate_to_staff(
                    message.channel, message.author.mention,
                    "tai_escalate_reason_user_limit"
                )
                return

            async with message.channel.typing():
                memory_history = []
                async for hist_msg in message.channel.history(limit=6, oldest_first=False):
                    if hist_msg.id == message.id:
                        continue
                    
                    role = "assistant" if hist_msg.author.bot else "user"
                    content = hist_msg.content
                    
                    if hist_msg.author.bot and hist_msg.embeds:
                        content = hist_msg.embeds[0].description or ""
                    
                    if content and not content.startswith("⚠️") and not content.startswith("🔒"):
                        memory_history.append({"role": role, "content": content})
                
                memory_history.reverse()

            retrieved_context = "No explicit server documentation matching this specific query was found in the database."
            matched_nodes = []  # 두 단계 모두 실패해도 아래에서 안전하게 참조 가능하도록 기본값 확보

            # 🛡️ 임베딩 생성과 Supabase 매칭을 별도 try/except로 분리한다 - 예전엔 하나로 뭉쳐서
            # 잡았기 때문에 로그만 보고는 OpenAI가 문제였는지 Supabase가 문제였는지 알 수 없었다.
            query_vector = None
            try:
                query_response = await self.ai_client.embeddings.create(
                    model="text-embedding-3-small",
                    input=user_query
                )
                query_vector = query_response.data[0].embedding
            except Exception as e:
                print(f"[TICKET_AI][ERROR] Embedding generation failed during knowledge search "
                      f"(guild={guild_id}): {type(e).__name__}: {e}", flush=True)
                await self._record_knowledge_search_failure(message.guild, guild_id, "embedding", f"{type(e).__name__}: {e}")

            if query_vector is not None:
                try:
                    rpc_params = {
                        "query_embedding": query_vector,
                        "match_threshold": 0.28,
                        "match_count": 1,
                        "p_guild_id": guild_id
                    }
                    db_response = await asyncio.to_thread(self.bot.supabase.rpc("match_knowledge", rpc_params).execute)
                    matched_nodes = db_response.data

                    if matched_nodes:
                        retrieved_context = matched_nodes[0]["content"]
                except Exception as e:
                    print(f"[TICKET_AI][ERROR] Knowledge match RPC failed (guild={guild_id}): "
                          f"{type(e).__name__}: {e}", flush=True)
                    await self._record_knowledge_search_failure(message.guild, guild_id, "supabase_rpc", f"{type(e).__name__}: {e}")

            settings = await self.get_ticket_settings(guild_id)

            if settings and settings.get("system_prompt"):
                base_prompt = settings.get("system_prompt")
                if "{context}" in base_prompt:
                    system_prompt = base_prompt.replace("{context}", retrieved_context)
                else:
                    system_prompt = f"{base_prompt}\n\n[Server Documentation Context]\n{retrieved_context}"
            else:
                system_prompt = (
                    "You are the premium Kyvo AI Smart Support Assistant for this Discord server.\n"
                    "Your mission is to answer the user's question accurately by referencing the Server Documentation Context provided below.\n"
                    "You must evaluate the short-term chat history to maintain conversation flow (pronouns, continuous topics).\n\n"
                    f"Server Documentation Context:\n{retrieved_context}\n\n"
                    "CRITICAL ROUTING INSTRUCTIONS:\n"
                    "If the user explicitly asks for human staff, manager, administrator, or support agents, OR if they ask a specific server question that completely fails to match any relevant server documentation context, you MUST output exactly 'TRIGGER_STAFF_ALERT' as your final response string.\n"
                    "DO NOT output 'TRIGGER_STAFF_ALERT' for casual greetings (e.g., 'hello', 'hi', 'hey', or foreign equivalents like '안녕'), polite gestures, or basic small talk. For greetings, simply respond warmly, acknowledge the user, and ask how you can assist them based on server guidelines."
                )

            # 🛡️ [다국어] 이 서버(guild_settings.language)가 한국어면 AI에게 항상 한국어로 답하라고
            # 못박는다 - 안 그러면 유저가 영어로 물었을 때 AI가 영어로 답해서, 같은 서버 안에서도
            # 응답 언어가 뒤섞이는 원인이 된다(오늘 발견한 "메시지가 섞여 나온다" 버그의 일부).
            guild_settings = await self.get_guild_settings(guild_id)
            reply_lang = guild_settings.get("language", "en")
            language_instruction = (
                "\n\nIMPORTANT: Always respond in Korean (한국어), regardless of what language the user's message is written in."
                if reply_lang == "ko"
                else "\n\nIMPORTANT: Always respond in English, regardless of what language the user's message is written in."
            )
            system_prompt += language_instruction

            openai_messages = [{"role": "system", "content": system_prompt}]
            openai_messages.extend(memory_history)
            openai_messages.append({"role": "user", "content": user_query})

            chat_response = await self.ai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=openai_messages,
                max_tokens=450,
                temperature=0.4
            )

            ai_final_answer = chat_response.choices[0].message.content.strip()

            if "TRIGGER_STAFF_ALERT" in ai_final_answer:
                await self._escalate_to_staff(
                    message.channel, message.author.mention,
                    "tai_escalate_reason_ai_triggered"
                )
                return

            reply_title = await self.get_msg(guild_id, "tai_reply_title")
            reply_footer = await self.get_msg(guild_id, "tai_reply_footer")
            ai_reply = discord.Embed(
                title=reply_title,
                description=ai_final_answer,
                color=0x9b59b6
            )
            ai_reply.set_footer(text=reply_footer)
            up_label = await self.get_msg(guild_id, "tai_feedback_btn_up")
            down_label = await self.get_msg(guild_id, "tai_feedback_btn_down")
            feedback_view = TicketFeedbackView(self, up_label=up_label, down_label=down_label)
            sent_reply = await message.channel.send(embed=ai_reply, view=feedback_view)

            # 🛡️ 이 행이 있어야 버튼 클릭 시 message_id로 다시 찾을 수 있다(익명 제보/추첨과 동일한
            # "먼저 메시지를 보내고, 그 ID로 추적 행을 만든다" 순서). knowledge_id는 매칭된 지식
            # 항목이 없으면 NULL - 이 경우도 피드백 자체는 남지만 특정 항목 통계엔 안 잡힌다.
            knowledge_id = matched_nodes[0]["id"] if matched_nodes else None
            opener_id = self._get_ticket_opener_id(message.channel)
            try:
                await asyncio.to_thread(
                    self.bot.supabase.table("ticket_ai_feedback").insert({
                        "message_id": str(sent_reply.id), "guild_id": guild_id, "channel_id": str(channel_id),
                        "opener_id": opener_id, "knowledge_id": knowledge_id,
                    }).execute
                )
            except Exception as e:
                print(f"[TICKET_AI][ERROR] Failed to record feedback tracking row for message {sent_reply.id}: "
                      f"{type(e).__name__}: {e}", flush=True)

        except Exception as e:
            print(f"[RAG ENGINE EXCEPTION] Pipeline failed: {e}")
            error_title = await self.get_msg(guild_id, "tai_error_title")
            error_desc = await self.get_msg(guild_id, "tai_error_desc", error=e)
            error_embed = discord.Embed(
                title=error_title,
                description=error_desc,
                color=0xe67e22
            )
            await message.channel.send(embed=error_embed)
            
        finally:
            self.processing_channels.discard(channel_id)

async def setup(bot):
    cog = KyvoTicketAI(bot)
    bot.add_view(OpenTicketView(cog))
    bot.add_view(TicketSystemView(cog))
    bot.add_view(TicketFeedbackView(cog))
    await bot.add_cog(cog)
