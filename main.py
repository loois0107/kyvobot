import discord
from discord import app_commands  
from discord.ext import commands
import os
import asyncio
from supabase import create_client, Client
from aiohttp import web

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
        # 🔒 [GLOBAL DEFENSE BLOCK] Bind the global slash command error handler
        self.tree.on_error = self.on_app_command_error

        # [RENDER INFRASTRUCTURE HACK] Start Dummy Web Server
        self.loop.create_task(self.keep_alive_server())

        extensions = [
            'cogs.automod',
            'cogs.economy',
            'cogs.leveling',
            'cogs.ticket_ai',
            'cogs.custom_commands',
        ]
        
        for ext in extensions:
            try:
                await self.load_extension(ext)
                print(f"[SYSTEM LOADING] Successfully loaded slot module: {ext}")
            except Exception as e:
                print(f"[CRITICAL LAYER ERROR] Failure launching extension node {ext}: {e}")

        # Automatically deploy slash commands globally across all servers upon initialization
        try:
            print("[SYSTEM LOG] Syncing application commands globally...")
            synced = await self.tree.sync() # Empty arguments trigger a true global sync hierarchy
            print(f"[SYSTEM LOG] Successfully synced {len(synced)} commands globally to Discord.")
        except Exception as e:
            print(f"[SYSTEM ERROR] Failed global sync during startup: {e}")

    async def on_app_command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        """🛡️ Centralized Interceptor Matrix: Catches all global slash command failures smoothly."""
        
        # Guard: If the bot already deferred or responded, use followup to prevent crash cascades
        send_message = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message

        # 1. Handle Command Rate Limits (Cooldowns)
        if isinstance(error, app_commands.CommandOnCooldown):
            await send_message(
                f"⏳ **Command on Cooldown!**\n"
                f"Please wait `{error.retry_after:.1f}` seconds before trying again.", 
                ephemeral=True
            )
            return

        # 2. Handle Unauthorized Access (Missing Permissions)
        elif isinstance(error, app_commands.MissingPermissions):
            await send_message(
                "❌ **Permission Denied!**\n"
                "This command requires Administrator or Server Manager privileges.", 
                ephemeral=True
            )
            return

        # 3. Fallback for unexpected infrastructure crashes
        else:
            print(f"[CRITICAL SLASH EXCEPTION] Intercepted runtime crash node: {error}")
            try:
                await send_message(
                    "⚠️ **Internal Server Error!**\n"
                    "An unexpected error occurred while processing this command. Please contact the administrator.", 
                    ephemeral=True
                )
            except Exception:
                pass

    async def keep_alive_server(self):
        """Deploys a dummy HTTP server to satisfy Render.com Web Service port binding requirements."""
        app = web.Application()
        app.router.add_get('/', lambda request: web.Response(text="KyvoBot AI Engine is Online and Running!"))
        runner = web.AppRunner(app)
        await runner.setup()
        
        # Capture dynamic port bound by Render infrastructure (Fallback to 8080)
        port = int(os.environ.get("PORT", 8080))
        site = web.TCPSite(runner, '0.0.0.0', port)
        await site.start()
        print(f"[WEB INFRASTRUCTURE] Dummy health-check server bound to port {port}.")

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
            print(f"[DATABASE EXCEPTION] Failed tracking configuration matrix blocks for guild ID {guild_id}: {e}")
            return {}

    async def bulk_update_guild_settings(self, guild_id: str, settings: dict):
        try:
            self.supabase.table("guild_settings").update({"settings": settings}).eq("guild_id", guild_id).execute()
        except Exception as e:
            print(f"[DATABASE EXCEPTION] Failed committing execution changes onto tracking block {guild_id}: {e}")

    async def get_user_data(self, user_id: str) -> dict:
        """Fetches flat user profile data row matching your explicit schema columns."""
        try:
            response = self.supabase.table("users").select("*").eq("user_id", user_id).execute()
            if response.data:
                return response.data[0]
            else:
                default_profile = {"user_id": user_id, "points": 0, "xp": 0, "level": 1}
                self.supabase.table("users").insert(default_profile).execute()
                return default_profile
        except Exception as e:
            print(f"[DATABASE EXCEPTION] Flat profile matrix data acquisition fault on record ID {user_id}: {e}")
            return {}

    async def save_user_data(self, user_id: str, profile_data: dict):
        """Commits transaction updates directly back into flat database columns."""
        try:
            update_payload = profile_data.copy()
            update_payload.pop("user_id", None)  # Protect primary key from mutation
            
            self.supabase.table("users").update(update_payload).eq("user_id", user_id).execute()
        except Exception as e:
            print(f"[DATABASE EXCEPTION] Critical write blockage handling flat record adjustments for user reference {user_id}: {e}")

bot = KyvoBot()

@bot.event
async def on_ready():
    print("==========================================================================")
    print(f"[APPLICATION CORE LIVE] Established secure connection tunnel as: {bot.user.name}")
    print(f"[GATEWAY IDENTIFIER] Network ID: {bot.user.id}")
    print("[SECURITY MATRIX] System modules running on optimized multi-thread clusters.")
    print("==========================================================================")

@bot.command(name="sync")
async def sync_application_commands(ctx: commands.Context, scope: str = "local"):
    """
    !sync -> Sync application commands locally to this specific server context
    !sync global -> Force push and update application commands globally across all servers
    !sync clear -> Purge guild-specific local command registration entries
    """
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
            print(f"[SERVER SYNC] Successfully deployed {len(synced)} commands locally.")
        except Exception as e:
            await ctx.send(f"❌ Sync failed: `{e}`")
            print(f"[SERVER SYNC ERROR] Critical crash: {e}")

if __name__ == "__main__":
    bot_token = os.getenv("DISCORD_BOT_TOKEN") or os.getenv("BOT_TOKEN") or os.getenv("TOKEN")
    
    if not bot_token:
        print("[BOOT ABORT] Missing deployment parameter token configuration!")
    else:
        bot.run(bot_token)
