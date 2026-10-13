import discord
from discord import app_commands
from discord.ext import commands
import os
import asyncio
import datetime
import traceback
from concurrent.futures import ThreadPoolExecutor
from supabase import create_client, Client
from aiohttp import web

# 🛡️ [client_max_size - DOR 업로드 413 수정] cogs.highlight의 MAX_ATTACHMENT_BYTES(100MB)를
# 그대로 재사용한다 - 숫자를 또 하드코딩하면 한쪽만 바뀌는 괴리가 생길 수 있어서다. 이 시점에
# cogs.highlight를 import해도 안전한 이유: 이 모듈이 임포트 시점에 하는 RIOT_API_KEY/
# OPENAI_API_KEY 등 환경변수 체크는, 어차피 잠시 뒤 extensions 로딩 때 똑같이 import되면서
# 하게 될 체크를 몇 줄 앞당기는 것뿐이고(env var는 python 프로세스 시작 전에 이미 OS/배포
# 플랫폼이 주입해두므로 로딩 타이밍과 무관), cogs/dor.py도 이미 똑같은 패턴으로 cogs.highlight를
# 가져다 쓴다.
from cogs.highlight import MAX_ATTACHMENT_BYTES

print(">>> KYVO MAIN.PY VERSION 2026-07-17-CUSTOMCOMMANDS-FIX <<<", flush=True)

class KyvoBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  
        intents.members = True          
        
        super().__init__(command_prefix="!", intents=intents)
        
        supabase_url = os.getenv("SUPABASE_URL")
        supabase_key = os.getenv("SUPABASE_KEY")
        self.supabase: Client = create_client(supabase_url, supabase_key)

        # 🛡️ gg_rsvp/party/giveaway/scrim 등 10개 코그의 _db_call()이 공유하는 전용 스레드풀 -
        # 파이썬 프로세스 전체가 공유하는 기본 executor(run_in_executor(None, ...))는 다른 블로킹
        # 작업과도 경합하므로, DB 호출만 별도로 격리해서 병목/경합을 줄인다.
        self.db_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="kyvo-db")

    async def setup_hook(self):
        print(">>> SETUP_HOOK STARTED <<<", flush=True)
        self.tree.on_error = self.on_app_command_error

        # 🛡️ 웹앱 객체는 익스텐션 로드 "전"에 미리 만들어 self.web_app으로 노출한다 - 각 코그가
        # setup() 시점에 자기 라우트(예: 트위치 웹훅)를 등록할 수 있어야 하기 때문이다. 실제로
        # 포트를 열어 서빙을 시작하는 건 모든 코그가 라우트 등록을 마친 "후"로 미룬다.
        self.init_web_app()
        print(">>> web app initialized (routes can now be registered by cogs) <<<", flush=True)

        extensions = [
            'cogs.automod',
            'cogs.economy',
            'cogs.leveling',
            'cogs.ticket_ai',
            'cogs.custom_commands',
            'cogs.welcome',
            'cogs.voice',
            'cogs.anonymous_reports',
            'cogs.giveaway',
            'cogs.reaction_roles',
            'cogs.party',
            'cogs.gg_rsvp',
            'cogs.scrim',
            'cogs.tier_verify',
            'cogs.highlight',  # tier_verify 다음에 로드 - Riot 클라이언트/예외 클래스를 그쪽에서 재사용
            'cogs.twitch',
            'cogs.cs2_flex',
            'cogs.weekly_report',
            'cogs.onboarding',
            'cogs.help',
            'cogs.koreanbots',
            'cogs.bots_gg',
            'cogs.topgg',
            'cogs.inquiry',
            'cogs.presence',
        ]
        print(">>> Loading extensions... <<<", flush=True)

        for ext in extensions:
            try:
                await self.load_extension(ext)
                print(f"[SYSTEM LOADING] Successfully loaded slot module: {ext}", flush=True)
            except Exception as e:
                print(f"[CRITICAL LAYER ERROR] Failure launching extension node {ext}: {e}", flush=True)
                import traceback
                traceback.print_exc()

        self.loop.create_task(self.start_web_server())
        print(">>> web server start task created <<<", flush=True)

        try:
            from cogs.i18n_commands import KoreanCommandTranslator
            await self.tree.set_translator(KoreanCommandTranslator())
            print("[SYSTEM LOG] Korean command translator registered.", flush=True)
        except Exception as e:
            print(f"[SYSTEM ERROR] Failed to register Korean command translator: {e}", flush=True)

        # 🛡️ [Entry Point 커맨드 통합 동기화] Discord Activity의 Launch 버튼이 뜨려면
        # PRIMARY_ENTRY_POINT(type=4) 커맨드가 전역 커맨드 목록에 있어야 하는데, discord.py
        # 2.7.1(최신 릴리스)은 AppCommandType에 entry point 자체가 없어 CommandTree로는
        # 등록할 방법이 없다(공식 지원 없음 - 조사 완료). 그렇다고 tree.sync()와 별도로
        # entry point를 POST 업서트하면, Discord가 "entry point가 이미 존재하면 bulk overwrite
        # payload 안에 반드시 같이 포함돼야 한다"고 요구하기 때문에(안 넣으면 400 Bad Request,
        # error code 50240 - 실제로 겪은 에러) tree.sync()가 매번 실패한다. 그래서 tree.sync()가
        # 내부적으로 하는 일(로컬 커맨드 payload 생성 + bulk overwrite PUT)을 그대로 재현하면서
        # entry point dict 하나를 같은 payload에 끼워 넣어 단일 PUT으로 합쳐 보낸다.
        try:
            print("[SYSTEM LOG] Syncing application commands globally (entry point 포함)...", flush=True)

            # 🛡️ discord.py 내부 구현에 의존 - 버전 올릴 때 확인 필요
            # tree.sync()(app_commands/tree.py)가 쓰는 것과 동일한 절차: _get_all_commands()로
            # 로컬 커맨드를 얻고, translator가 등록돼 있으면(바로 위에서 KoreanCommandTranslator
            # 등록함) get_translated_payload()로, 없으면 to_dict()로 payload를 만든다. 이 네
            # 개(_get_all_commands/to_dict/get_translated_payload/tree.translator)는 전부
            # 언더스코어가 붙었거나 공식 문서화되지 않은 내부 API라, discord.py를 업그레이드할
            # 때 시그니처가 바뀌면 조용히 깨질 수 있다 - 그래서 아래 except AttributeError에서
            # 따로 크게 로그를 남긴다.
            local_commands = self.tree._get_all_commands(guild=None)
            translator = self.tree.translator
            if translator:
                payload = [await command.get_translated_payload(self.tree, translator) for command in local_commands]
            else:
                payload = [command.to_dict(self.tree) for command in local_commands]

            payload.append({
                "name": "launch",
                "description": "카이보와 함께 미니게임 실행하기",
                "type": 4,     # PRIMARY_ENTRY_POINT
                "handler": 2,  # DISCORD_LAUNCH_ACTIVITY
            })

            synced = await self.http.bulk_upsert_global_commands(self.application_id, payload=payload)
            print(
                f"[SYSTEM LOG] Successfully synced {len(synced)} commands globally to Discord "
                f"(entry point 'launch' 포함).",
                flush=True,
            )
        except AttributeError as e:
            print(
                f"[SYSTEM ERROR] discord.py 내부 API 변경으로 추정됨 (버전 확인 필요) - "
                f"커맨드 동기화 실패: {e}",
                flush=True,
            )
        except Exception as e:
            print(f"[SYSTEM ERROR] Failed global sync during startup: {e}", flush=True)

    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        # 🛡️ [순수 진단 로깅 - 기능 변경 없음] 에러 발생 순간의 게이트웨이 웹소켓 핑(latency)을
        # 같이 남겨서, 다음에 이 에러가 재현될 때 그 시점에 게이트웨이 자체가 느렸는지(재연결
        # 직후 등) 바로 확인할 수 있게 한다.
        print(f"[GATEWAY] latency={self.latency:.3f}s at on_app_command_error dispatch "
              f"(error_type={type(error).__name__})", flush=True)

        send_message = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message

        if isinstance(error, app_commands.CommandOnCooldown):
            try:
                await send_message(
                    f"⏳ **Command on Cooldown!**\n"
                    f"Please wait `{error.retry_after:.1f}` seconds before trying again.",
                    ephemeral=True
                )
            except (discord.NotFound, discord.HTTPException) as e:
                print(f"[SLASH EXCEPTION][WARN] Failed to send cooldown response (interaction likely expired): "
                      f"{type(e).__name__}: {e}", flush=True)
            return

        elif isinstance(error, app_commands.MissingPermissions):
            try:
                await send_message(
                    "❌ **Permission Denied!**\n"
                    "This command requires Administrator or Server Manager privileges.",
                    ephemeral=True
                )
            except (discord.NotFound, discord.HTTPException) as e:
                print(f"[SLASH EXCEPTION][WARN] Failed to send permission-denied response (interaction likely expired): "
                      f"{type(e).__name__}: {e}", flush=True)
            return

        else:
            print(f"[CRITICAL SLASH EXCEPTION] Intercepted runtime crash node: {error}", flush=True)
            # 🛡️ [순수 진단 로깅 - 기능 변경 없음] CommandInvokeError는 str(error)로는 "Command
            # '...' raised an exception: NotFound..."처럼 바깥쪽 요약 한 줄만 보이고, 실제로 명령어
            # 콜백 안에서 무슨 예외가 어디서 났는지(내부 스택 트레이스)는 안 보였다. error.original
            # (콜백 안에서 실제로 발생한 원본 예외)의 타입/메시지/전체 트레이스백을 그대로 찍는다.
            original = getattr(error, "original", None)
            if original is not None:
                print(f"[CRITICAL SLASH EXCEPTION][ORIGINAL] type={type(original).__name__}: {original}", flush=True)
                tb_lines = traceback.format_exception(type(original), original, original.__traceback__)
                print("[CRITICAL SLASH EXCEPTION][TRACEBACK]\n" + "".join(tb_lines), flush=True)
            try:
                await send_message(
                    "⚠️ **Internal Server Error!**\n"
                    "An unexpected error occurred while processing this command. Please contact the administrator.", 
                    ephemeral=True
                )
            except Exception:
                pass

    def init_web_app(self):
        # 🛡️ [client_max_size 명시 - 기본값 1MB 때문에 DOR 영상 업로드가 413으로 막혔었음]
        # 모든 /internal/* 라우트(dor.py 포함)가 이 Application 인스턴스 하나를 공유하므로,
        # 여기 한 곳만 고치면 전부 적용된다. MAX_ATTACHMENT_BYTES(100MB, cogs/highlight.py)와
        # 동일하게 맞춰서 /highlight가 받는 최대 영상 크기와 일관되게 유지한다.
        self.web_app = web.Application(client_max_size=MAX_ATTACHMENT_BYTES)
        self.web_app.router.add_get('/', lambda request: web.Response(text="KyvoBot AI Engine is Online and Running!"))

    async def start_web_server(self):
        runner = web.AppRunner(self.web_app)
        await runner.setup()

        port = int(os.environ.get("PORT", 8080))
        site = web.TCPSite(runner, '0.0.0.0', port)
        await site.start()
        print(f"[WEB INFRASTRUCTURE] Web server bound to port {port} ({len(self.web_app.router.routes())} routes).", flush=True)

    async def get_guild_settings(self, guild_id: str) -> dict:
        try:
            response = self.supabase.table("guild_settings").select("*").eq("guild_id", guild_id).execute()
            if response.data:
                return response.data[0].get("settings", {})
            else:
                default_settings = {
                    "antinuke_settings": {"enabled": False, "anti_spam_speed": 3, "whitelisted_roles": [], "log_channel_id": None},
                    "economy_settings": {"currency_name": "Points", "min_bet": 10, "shop_items": []},
                    "leveling_settings": {"xp_rate": 1.0, "blacklisted_channels": [], "role_rewards": {}},
                    "ticket_settings": {"faq_matrix": []}
                }
                self.supabase.table("guild_settings").insert({"guild_id": guild_id, "settings": default_settings}).execute()
                return default_settings
        except Exception as e:
            print(f"[DATABASE EXCEPTION] Failed tracking configuration matrix blocks for guild ID {guild_id}: {e}", flush=True)
            return {}

    # 🛡️ [데이터 안정성 수정 완료] 데이터 누실을 원천 차단하기 위해 update 대신 upsert 매커니즘 도입!
    async def bulk_update_guild_settings(self, guild_id: str, settings: dict):
        """행이 없어도 생성되도록 upsert로 저장한다 (update는 조용히 무시됨)."""
        try:
            self.supabase.table("guild_settings").upsert(
                {"guild_id": str(guild_id), "settings": settings},
                on_conflict="guild_id"
            ).execute()
        except Exception as e:
            print(f"[DATABASE EXCEPTION] Failed committing settings for {guild_id}: {e}", flush=True)

    async def _db_call(self, fn):
        """supabase-py는 동기 클라이언트라 이벤트 루프를 막지 않도록 executor로 감싼다 -
        cogs/*.py의 12개 cog가 이미 쓰는 것과 동일한 패턴(self.bot.db_executor 대신
        여기선 self가 이미 bot 인스턴스라 self.db_executor)."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.db_executor, fn)

    async def get_user_data(self, user_id: str, guild_id: str) -> dict:
        # 🛡️ [이벤트 루프 블로킹 수정] select/insert 둘 다 await 없이 동기 .execute()를 직접
        # 호출해서, 신규 유저(select 0건 -> insert까지)는 두 번 연속으로 이벤트 루프를 멈췄다.
        # economy 전체(20여 곳)와 leveling의 on_message(모든 메시지마다)가 이 함수를 거치므로
        # _db_call로 감싸 파급 범위 전체를 한 번에 해결한다.
        try:
            response = await self._db_call(
                lambda: self.supabase.table("users").select("*")
                        .eq("user_id", user_id).eq("guild_id", guild_id).execute()
            )
            if response.data:
                return response.data[0]
            else:
                default_profile = {"user_id": user_id, "guild_id": guild_id, "points": 0, "xp": 0, "level": 1}
                await self._db_call(
                    lambda: self.supabase.table("users").insert(default_profile).execute()
                )
                return default_profile
        except Exception as e:
            print(f"[DATABASE EXCEPTION] Flat profile matrix data acquisition fault on record ID {user_id}: {e}", flush=True)
            return {}

    async def save_user_data(self, user_id: str, guild_id: str, profile_data: dict) -> bool:
        try:
            update_payload = profile_data.copy()
            update_payload.pop("user_id", None)
            update_payload.pop("guild_id", None)

            await self._db_call(
                lambda: self.supabase.table("users").update(update_payload)
                        .eq("user_id", user_id).eq("guild_id", guild_id).execute()
            )
            return True
        except Exception as e:
            print(f"[DATABASE EXCEPTION] Critical write blockage handling flat record adjustments for user reference {user_id}: {e}", flush=True)
            return False

bot = KyvoBot()

@bot.event
async def on_ready():
    print("==========================================================================", flush=True)
    print(f"[APPLICATION CORE LIVE] Established secure connection tunnel as: {bot.user.name}", flush=True)
    print(f"[GATEWAY IDENTIFIER] Network ID: {bot.user.id}", flush=True)
    print("[SECURITY MATRIX] System modules running on optimized multi-thread clusters.", flush=True)
    print("==========================================================================", flush=True)

# 🛡️ [순수 진단 로깅 - 기능 변경 없음] 게이트웨이 재연결/resume/disconnect는 지금까지 전혀
# 로그가 안 남아서, 실제로 일어나고 있어도 우리가 못 보고 있었다 - on_ready만으론 재연결인지
# 최초 연결인지 구분이 안 되고(party.py/voice.py의 on_ready 주석 참고), on_resumed/on_disconnect/
# on_connect는 기존에 아무도 등록한 적이 없어(전체 코드베이스 검색 확인) @bot.event로 새로
# 등록해도 기존 리스너와 충돌하지 않는다.
@bot.event
async def on_connect():
    print(f"[GATEWAY] Connected at {datetime.datetime.now(datetime.timezone.utc).isoformat()}", flush=True)

@bot.event
async def on_resumed():
    print(f"[GATEWAY] Resumed at {datetime.datetime.now(datetime.timezone.utc).isoformat()}", flush=True)

@bot.event
async def on_disconnect():
    print(f"[GATEWAY] Disconnected at {datetime.datetime.now(datetime.timezone.utc).isoformat()}", flush=True)

@bot.command(name="sync")
@commands.is_owner()  # 🔒 서버별 권한이 아니라 봇 인프라(전역 커맨드 트리) 관리 명령이라 오너 전용으로 제한
async def sync_application_commands(ctx: commands.Context, scope: str = "local"):
    if scope == "global":
        await ctx.send("🌐 Deploying core command registry GLOBALLY to all servers... (Takes a few minutes)")
        try:
            synced = await bot.tree.sync()
            await ctx.send(f"✅ Global Success! Registered {len(synced)} slash command nodes globally.")
        except Exception as e:
            await ctx.send(f"❌ Global Sync failed: `{e}`")

    elif scope == "clear":
        await ctx.send("🗑️ Clearing guild-specific local command leftovers from this server...")
        try:
            bot.tree.clear_commands(guild=ctx.guild)
            await bot.tree.sync(guild=ctx.guild)
            await ctx.send("💥 Successfully wiped local command cache! Only clean global commands will remain.")
        except Exception as e:
            await ctx.send(f"❌ Clear failed: `{e}`")

    else:
        await ctx.send("🔄 Copying active core commands directly to this server instance...")
        try:
            bot.tree.copy_global_to(guild=ctx.guild)
            synced = await bot.tree.sync(guild=ctx.guild)
            await ctx.send(f"✅ Success! Deployed {len(synced)} command nodes directly to this server registry.")
            print(f"[SERVER SYNC] Successfully deployed {len(synced)} commands locally.", flush=True)
        except Exception as e:
            await ctx.send(f"❌ Sync failed: `{e}`")
            print(f"[SERVER SYNC ERROR] Critical crash: {e}", flush=True)

@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    # 접두사(!) 명령어 전용 에러 훅. 슬래시 명령어는 on_app_command_error가 따로 처리한다.
    if isinstance(error, commands.CommandNotFound):
        # custom_commands 코그가 같은 "!"/"/" 접두사를 커스텀 명령어 조회용으로도 쓰기 때문에,
        # 등록된 프리픽스 명령이 아닌 "!foo"는 여기서 조용히 무시해야 커스텀 명령어 UX가 안 깨진다.
        return

    if isinstance(error, commands.NotOwner):
        await ctx.send("🔒 **Access Denied.** `!sync` is a bot-owner-only infrastructure command, not a server permission.")
        return

    print(f"[PREFIX COMMAND ERROR] {ctx.command}: {error}", flush=True)
    try:
        await ctx.send(f"⚠️ **Command Error:** `{error}`")
    except Exception:
        pass

if __name__ == "__main__":
    bot_token = os.getenv("DISCORD_BOT_TOKEN") or os.getenv("BOT_TOKEN") or os.getenv("TOKEN")
    
    if not bot_token:
        print("[BOOT ABORT] Missing deployment parameter token configuration!", flush=True)
    else:
        bot.run(bot_token)