import discord
from discord import app_commands
from discord.ext import commands
import asyncio
import os
import time
import hmac
from aiohttp import web
from openai import AsyncOpenAI

INTERNAL_API_SECRET = os.environ.get("INTERNAL_API_SECRET")
TICKET_KNOWLEDGE_MAX_LENGTH = 4000  # OpenAI 토큰 한도 여유 + 과금/저장 폭주 방지 - party_game_presets 등과 동일한 관례

class OpenTicketView(discord.ui.View):
    """
    Persistent View class responsible for handling the initial ticket creation button matrix.
    Registered globally within setup invocation to survive bot container restarts.
    """
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="📩 Open Support Ticket", style=discord.ButtonStyle.primary, custom_id="kyvo_ticket_open")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await self.cog.create_ticket_channel(interaction)

class TicketSystemView(discord.ui.View):
    """
    Persistent View instance appended inside active ticket channel instances.
    Provides administrative utility operations such as secure archiving and channel purging.
    """
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="🔒 Close Ticket", style=discord.ButtonStyle.danger, custom_id="kyvo_ticket_close")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        channel = interaction.channel
        
        await interaction.followup.send("⚠️ **Archiving conversation transcript and deleting channel in 5 seconds...**")
        
        # 🛡️ CORE UPGRADE: Trigger the dynamic AI summary engine before the channel is destroyed
        await self.cog.archive_and_log_ticket(channel, interaction.user)
        
        await asyncio.sleep(5)
        try:
            await channel.delete(reason="Ticket session closed and securely archived by administrative request.")
        except discord.Forbidden:
            print(f"[SECURITY ERROR] Missing channel management permissions for: {channel.name}")

class KyvoTicketAI(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.ai_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        
        # Anti-Spam and Concurrency Lock Management Layers
        self.processing_channels = set()
        self.channel_cooldowns = {}

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
        description="Manage AI Vector Knowledge Base matrix and support structures",
        default_permissions=discord.Permissions(manage_guild=True)
    )

    @ticket_admin.command(name="add-knowledge", description="Inject a custom documentation block into Supabase Vector DB.")
    @app_commands.describe(content="The actual guidelines, server rules, or FAQ text chunk to teach the AI.")
    async def add_knowledge(self, interaction: discord.Interaction, content: str):
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id)

        result = await self._add_knowledge(guild_id, content)

        if result["status"] == "empty_content":
            await interaction.followup.send("❌ Cannot inject an empty knowledge block.", ephemeral=True)
        elif result["status"] == "content_too_long":
            await interaction.followup.send(f"❌ That's too long - keep it under {TICKET_KNOWLEDGE_MAX_LENGTH} characters.", ephemeral=True)
        elif result["status"] != "ok":
            await interaction.followup.send(f"❌ **Failed to inject knowledge node:** `{result.get('detail', result['status'])}`", ephemeral=True)
        else:
            await interaction.followup.send("✅ **Vector Knowledge Node Registered!** Securely pushed to pgvector storage.", ephemeral=True)

    @app_commands.command(name="ticket-setup", description="Deploy the persistent automated help desk support terminal.")
    @app_commands.default_permissions(manage_guild=True)
    async def ticket_setup(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id)

        settings = await self.get_ticket_settings(guild_id)
        
        embed_title = settings.get("setup_title") if settings and settings.get("setup_title") else "🎫 Support Portal & Advanced AI Concierge"
        embed_desc = settings.get("setup_desc") if settings and settings.get("setup_desc") else (
            "Click the button below to establish a private secure communication channel with staff.\n\n"
            "🤖 **Context-Aware RAG Engine Active:** State your inquiry freely. Our "
            "AI remembers the conversation history and queries server docs for an immediate resolution!"
        )

        embed = discord.Embed(
            title=embed_title,
            description=embed_desc,
            color=0x5865f2
        )

        view = OpenTicketView(self)
        await interaction.channel.send(embed=embed, view=view)
        await interaction.followup.send("✅ Support terminal panel operational.", ephemeral=True)

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
                await interaction.followup.send(
                    f"❌ **Access Denied:** You already have an active support session running at {existing_channel.mention}.",
                    ephemeral=True
                )
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
            welcome_title = settings.get("welcome_title") if settings and settings.get("welcome_title") else "🔒 Context-Aware AI Ticket Active"
            welcome_desc = settings.get("welcome_desc") if settings and settings.get("welcome_desc") else (
                f"Welcome, {user.mention}. Please state your question or issue description in detail.\n\n"
                "🤖 Our semantic RAG engine will instantly convert your message into vector fields, "
                "query our database index, and generate an answer based on server documentation."
            )

            welcome = discord.Embed(
                title=welcome_title,
                description=welcome_desc,
                color=0x2b2d31
            )
            
            view = TicketSystemView(self)
            await ticket_channel.send(embed=welcome, view=view)
            await interaction.followup.send(f"✅ Ticket environment established: {ticket_channel.mention}", ephemeral=True)

        except discord.Forbidden:
            print(f"[CRITICAL PERMISSION ERROR] Bot lacks 'Manage Channels' permission in guild: {interaction.guild_id}")
            await interaction.followup.send(
                "❌ **Creation Failed:** The bot lacks the required **'Manage Channels'** permission bitfield to spawn text channel nodes.",
                ephemeral=True
            )
        except Exception as e:
            print(f"[TICKET CREATION EXCEPTION] Pipeline failed: {e}")
            await interaction.followup.send(
                f"❌ **Internal Core Exception Encountered:** Failed to assemble ticket layer: `{e}`",
                ephemeral=True
            )

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
            log_channel = None
            guild_settings = await self.bot.get_guild_settings(guild_id)
            configured_log_id = guild_settings.get("antinuke_settings", {}).get("log_channel_id")

            # Priority 1: Check dashboard configuration settings row
            if configured_log_id:
                log_channel = guild.get_channel(int(configured_log_id))
            
            # Priority 2: Look for an explicit premium channel named 'ticket-logs'
            if not log_channel:
                log_channel = discord.utils.get(guild.text_channels, name="ticket-logs")
                
            # Priority 3: 🔒 SMART FALLBACK - Route to 'mod-logs' instead of general if ticket-logs is missing
            if not log_channel:
                log_channel = discord.utils.get(guild.text_channels, name="mod-logs")
                
            # Priority 4: Fallback to base system notification gateway channel
            if not log_channel:
                log_channel = guild.system_channel

            # 4. Deliver the completed summary report archive packet
            if log_channel:
                archive_embed = discord.Embed(
                    title=f"📋 Support Ticket Archive Log // {channel.name}",
                    color=0x34495e,
                    timestamp=discord.utils.utcnow()
                )
                archive_embed.add_field(name="Session Executor", value=f"Closed by {closed_by.mention} (`ID: {closed_by.id}`)", inline=False)
                archive_embed.add_field(name="AI Executive Summary Audit", value=ai_summary_report, inline=False)
                archive_embed.set_footer(text=f"Server ID Core Node: {guild_id}")
                
                await log_channel.send(embed=archive_embed)

        except Exception as e:
            print(f"[ARCHIVE LOG MATRIX FAULT] Critical pipeline blockage creating final report packet: {e}")

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
            try:
                query_response = await self.ai_client.embeddings.create(
                    model="text-embedding-3-small",
                    input=user_query
                )
                query_vector = query_response.data[0].embedding

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
            except Exception as db_err:
                print(f"[DATABASE SEARCH EXCEPTION] {db_err}")

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
                current_name = message.channel.name
                new_name = f"🚨-{current_name}"
                await message.channel.edit(name=new_name, reason="AI Smart escalation handover triggered.")
                
                escalation_embed = discord.Embed(
                    title="🚨 Human Assistance Requested",
                    description=(
                        f"Hello {message.author.mention}, I've paused my automated chat layer and "
                        f"flagged this support session for review. **Server Administration staff has been appended to this queue.**\n\n"
                        f"Please remain patient while an agent reviews the dialogue log above."
                    ),
                    color=0xe74c3c
                )
                await message.channel.send(embed=escalation_embed)
                return

            ai_reply = discord.Embed(
                title="🤖 Kyvo AI Intelligent Support Agent",
                description=ai_final_answer,
                color=0x9b59b6
            )
            ai_reply.set_footer(text="Kyvo Automation Layer • Multi-Turn Conversational RAG Architecture")
            await message.channel.send(embed=ai_reply)

        except Exception as e:
            print(f"[RAG ENGINE EXCEPTION] Pipeline failed: {e}")
            error_embed = discord.Embed(
                title="⚠️ AI Engine Fault Encountered",
                description=f"An exception occurred inside the RAG automation stack:\n`{e}`\n\n*Check OpenAI billing balance limits or parameter tokens.*",
                color=0xe67e22
            )
            await message.channel.send(embed=error_embed)
            
        finally:
            self.processing_channels.discard(channel_id)

async def setup(bot):
    cog = KyvoTicketAI(bot)
    bot.add_view(OpenTicketView(cog))
    bot.add_view(TicketSystemView(cog))
    await bot.add_cog(cog)
