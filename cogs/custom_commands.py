import discord
from discord import app_commands
from discord.ext import commands
from cogs.base import KyvoBaseCog
import traceback

class CustomCommands(KyvoBaseCog):
    def __init__(self, bot):
        super().__init__(bot)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        content = message.content.strip()
        if not (content.startswith("/") or content.startswith("!")):
            return
        trigger = content[1:].strip().lower()
        if not trigger:
            return

        try:
            settings = await self.get_guild_settings(message.guild.id)
            # 만약 행 전체가 반환되었다면 settings["settings"] 내부를 참조하도록 폴백 처리
            if "settings" in settings and isinstance(settings["settings"], dict):
                custom_commands = settings["settings"].get("custom_commands", {})
            else:
                custom_commands = settings.get("custom_commands", {})

            if trigger in custom_commands:
                await message.channel.send(custom_commands[trigger])
        except Exception as e:
            print(f"[CC_ON_MESSAGE][ERROR] {e}", flush=True)

    @app_commands.command(name="cc_add", description="Create or update a server-specific custom command response.")
    @app_commands.checks.has_permissions(administrator=True)
    async def cc_add(self, interaction: discord.Interaction, name: str, response: str):
        await interaction.response.defer(ephemeral=True)
        try:
            guild_id = str(interaction.guild_id)
            cmd_name = name.strip().lower().removeprefix("/").removeprefix("!")
            
            settings = await self.get_guild_settings(interaction.guild_id)
            print(f"[CC_ADD][DEBUG] Raw settings content: {settings}", flush=True)

            if "settings" in settings and isinstance(settings["settings"], dict):
                target_dict = settings["settings"]
                custom_commands = target_dict.get("custom_commands", {})
                custom_commands[cmd_name] = response
                await self.bot.bulk_update_guild_settings(guild_id, {"settings": target_dict})
            else:
                custom_commands = settings.get("custom_commands", {})
                custom_commands[cmd_name] = response
                await self.bot.bulk_update_guild_settings(guild_id, {"custom_commands": custom_commands})

            await self.invalidate_settings_cache(interaction.guild_id)
            await interaction.followup.send(f"✅ 커스텀 명령어 `/{cmd_name}` 등록이 완료되었습니다!", ephemeral=True)
        except Exception as e:
            traceback.print_exc()
            await interaction.followup.send(f"❌ cc_add 에러 발생: `{type(e).__name__}: {e}`", ephemeral=True)

    @app_commands.command(name="cc_delete", description="Permanently delete a custom command from the server configuration.")
    @app_commands.checks.has_permissions(administrator=True)
    async def cc_delete(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer(ephemeral=True)
        try:
            guild_id = str(interaction.guild_id)
            cmd_name = name.strip().lower().removeprefix("/").removeprefix("!")

            settings = await self.get_guild_settings(interaction.guild_id)
            
            if "settings" in settings and isinstance(settings["settings"], dict):
                target_dict = settings["settings"]
                custom_commands = target_dict.get("custom_commands", {})
                if cmd_name not in custom_commands:
                    await interaction.followup.send("❌ 해당 명령어가 없습니다.", ephemeral=True)
                    return
                custom_commands.pop(cmd_name)
                await self.bot.bulk_update_guild_settings(guild_id, {"settings": target_dict})
            else:
                custom_commands = settings.get("custom_commands", {})
                if cmd_name not in custom_commands:
                    await interaction.followup.send("❌ 해당 명령어가 없습니다.", ephemeral=True)
                    return
                custom_commands.pop(cmd_name)
                await self.bot.bulk_update_guild_settings(guild_id, {"custom_commands": custom_commands})

            await self.invalidate_settings_cache(interaction.guild_id)
            await interaction.followup.send(f"✅ 커스텀 명령어 `/{cmd_name}` 삭제 완료!", ephemeral=True)
        except Exception as e:
            traceback.print_exc()
            await interaction.followup.send(f"❌ cc_delete 에러 발생: `{type(e).__name__}: {e}`", ephemeral=True)

    @app_commands.command(name="cc_list", description="Display all active custom commands within this server node.")
    async def cc_list(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            # 🔍 [1] 부모 클래스에서 데이터 가져오기 시도
            settings = await self.get_guild_settings(interaction.guild_id)
            print(f"[CC_LIST][DEBUG] settings type={type(settings)}, value={settings}", flush=True)

            # 🔍 [2] 데이터 구조 판별 및 강제 매핑 구조 처리
            if "settings" in settings and isinstance(settings["settings"], dict):
                custom_commands = settings["settings"].get("custom_commands", {})
            else:
                custom_commands = settings.get("custom_commands", {})

            embed = discord.Embed(title="📜 서버 커스텀 명령어 목록", color=discord.Color.blue())

            if not custom_commands:
                embed.description = "현재 이 서버에 등록된 커스텀 명령어가 없습니다."
                await interaction.followup.send(embed=embed)
                return

            for cmd, resp in custom_commands.items():
                display_resp = resp if len(resp) <= 100 else resp[:97] + "..."
                embed.add_field(name=f"/{cmd}", value=f"┕ `{display_resp}`", inline=False)

            await interaction.followup.send(embed=embed)
            
        except Exception as e:
            # 🚨 어떤 예외가 나든 콘솔에 풀 트레이스백을 뿜고 디스코드 전송
            print("[CC_LIST][CRITICAL CRASH DETECTED]", flush=True)
            traceback.print_exc()
            await interaction.followup.send(f"⚠️ **자체 디버그 에러 핸들러 포착:** `{type(e).__name__}: {e}`")

async def setup(bot):
    await bot.add_cog(CustomCommands(bot))
    print("[⚡ CUSTOM_COMMANDS] Cog extension setup complete.", flush=True)