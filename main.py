import discord
from discord import app_commands  
from discord.ext import commands
import os
import asyncio
from supabase import create_client, Client
from aiohttp import web

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

    async def setup_hook(self):
        print(">>> SETUP_HOOK STARTED <<<", flush=True)
        self.tree.on_error = self.on_app_command_error

        self.loop.create_task(self.keep_alive_server())
        print(">>> keep_alive task created <<<", flush=True)

        extensions = [
            'cogs.automod',
            'cogs.economy',
            'cogs.leveling',
            'cogs.ticket_ai',
            'cogs.custom_commands',
            'cogs.welcome',
            'cogs.voice',
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

        try:
            print("[SYSTEM LOG] Syncing application commands globally...", flush=True)
            synced = await self.tree.sync()
            print(f"[SYSTEM LOG] Successfully synced {len(synced)} commands globally to Discord.", flush=True)
        except Exception as e:
            print(f"[SYSTEM ERROR] Failed global sync during startup: {e}", flush=True)

    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        send_message = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message

        if isinstance(error, app_commands.CommandOnCooldown):
            await send_message(
                f"⏳ **Command on Cooldown!**\n"
                f"Please wait `{error.retry_after:.1f}` seconds before trying again.", 
                ephemeral=True
            )
            return

        elif isinstance(error, app_commands.MissingPermissions):
            await send_message(
                "❌ **Permission Denied!**\n"
                "This command requires Administrator or Server Manager privileges.", 
                ephemeral=True
            )
            return

        else:
            print(f"[CRITICAL SLASH EXCEPTION] Intercepted runtime crash node: {error}", flush=True)
            try:
                await send_message(
                    "⚠️ **Internal Server Error!**\n"
                    "An unexpected error occurred while processing this command. Please contact the administrator.", 
                    ephemeral=True
                )
            except Exception:
                pass

    async def keep_alive_server(self):
        app = web.Application()
        app.router.add_get('/', lambda request: web.Response(text="KyvoBot AI Engine is Online and Running!"))
        runner = web.AppRunner(app)
        await runner.setup()
        
        port = int(os.environ.get("PORT", 8080))
        site = web.TCPSite(runner, '0.0.0.0', port)
        await site.start()
        print(f"[WEB INFRASTRUCTURE] Dummy health-check server bound to port {port}.", flush=True)

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

    async def get_user_data(self, user_id: str, guild_id: str) -> dict:
        try:
            response = self.supabase.table("users").select("*") \
                .eq("user_id", user_id).eq("guild_id", guild_id).execute()
            if response.data:
                return response.data[0]
            else:
                default_profile = {"user_id": user_id, "guild_id": guild_id, "points": 0, "xp": 0, "level": 1}
                self.supabase.table("users").insert(default_profile).execute()
                return default_profile
        except Exception as e:
            print(f"[DATABASE EXCEPTION] Flat profile matrix data acquisition fault on record ID {user_id}: {e}", flush=True)
            return {}

    async def save_user_data(self, user_id: str, guild_id: str, profile_data: dict) -> bool:
        try:
            update_payload = profile_data.copy()
            update_payload.pop("user_id", None)
            update_payload.pop("guild_id", None)

            self.supabase.table("users").update(update_payload) \
                .eq("user_id", user_id).eq("guild_id", guild_id).execute()
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