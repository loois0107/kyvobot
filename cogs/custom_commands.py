import discord
from discord import app_commands
from discord.ext import commands
from cogs.base import KyvoBaseCog
import asyncio
import traceback

class CustomCommands(KyvoBaseCog):
    def __init__(self, bot):
        super().__init__(bot)

    def _extract_commands(self, settings: dict) -> dict:
        """데이터베이스 반환 구조 어디에 custom_commands가 있든 최상위 우선으로 안전하게 꺼냅니다."""
        # 1) 최상위 컬럼 우선 (웹 대시보드 저장 위치)
        top = settings.get("custom_commands")
        if isinstance(top, dict) and top:
            return top
            
        # 2) 폴백: settings JSON 안쪽 (봇 레거시 저장 위치)
        inner = settings.get("settings")
        if isinstance(inner, dict):
            nested = inner.get("custom_commands")
            if isinstance(nested, dict):
                return nested
                
        # 3) 둘 다 없거나 빈 데이터라면 빈 딕셔너리 안전 반환
        return top if isinstance(top, dict) else {}

    # ══════════════════════════════════════════════════════════
    #  커스텀 명령어 실행 엔진 (유저 채팅 감지)
    # ══════════════════════════════════════════════════════════
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
            custom_commands = self._extract_commands(settings)

            if trigger in custom_commands:
                await message.channel.send(custom_commands[trigger])
        except Exception as e:
            print(f"[CC_ON_MESSAGE][ERROR] {e}", flush=True)

    # ══════════════════════════════════════════════════════════
    #  슬래시 관리자 명령어 (추가)
    # ══════════════════════════════════════════════════════════
    @app_commands.command(name="cc_add", description="Create or update a server-specific custom command response.")
    @app_commands.checks.has_permissions(administrator=True)
    async def cc_add(self, interaction: discord.Interaction, name: str, response: str):
        await interaction.response.defer(ephemeral=True)
        try:
            guild_id = str(interaction.guild_id)
            cmd_name = name.strip().lower().removeprefix("/").removeprefix("!")
            if not cmd_name:
                await interaction.followup.send("❌ 올바르지 않은 명령어 이름입니다.", ephemeral=True)
                return
            
            settings = await self.get_guild_settings(interaction.guild_id)
            custom_commands = self._extract_commands(settings)
            custom_commands[cmd_name] = response

            # 🌐 최상위 custom_commands 컬럼에 직접 저장 (대시보드와 물리적 저장 위치 동기화)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                lambda: self.bot.supabase.table("guild_settings")
                        .update({"custom_commands": custom_commands})
                        .eq("guild_id", guild_id).execute()
            )

            await self.invalidate_settings_cache(interaction.guild_id)
            await interaction.followup.send(f"✅ 커스텀 명령어 `/{cmd_name}` 등록 및 수정이 완료되었습니다!", ephemeral=True)
        except Exception as e:
            traceback.print_exc()
            await interaction.followup.send(f"❌ cc_add 에러 발생: `{type(e).__name__}: {e}`", ephemeral=True)

    # ══════════════════════════════════════════════════════════
    #  슬래시 관리자 명령어 (삭제)
    # ══════════════════════════════════════════════════════════
    @app_commands.command(name="cc_delete", description="Permanently delete a custom command from the server configuration.")
    @app_commands.checks.has_permissions(administrator=True)
    async def cc_delete(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer(ephemeral=True)
        try:
            guild_id = str(interaction.guild_id)
            cmd_name = name.strip().lower().removeprefix("/").removeprefix("!")

            settings = await self.get_guild_settings(interaction.guild_id)
            custom_commands = self._extract_commands(settings)

            if cmd_name not in custom_commands:
                await interaction.followup.send("❌ 해당 커스텀 명령어를 찾을 수 없습니다.", ephemeral=True)
                return

            custom_commands.pop(cmd_name)

            # 🌐 최상위 custom_commands 컬럼에서 직접 컴팩트 삭제 반영
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                lambda: self.bot.supabase.table("guild_settings")
                        .update({"custom_commands": custom_commands})
                        .eq("guild_id", guild_id).execute()
            )

            await self.invalidate_settings_cache(interaction.guild_id)
            await interaction.followup.send(f"✅ 커스텀 명령어 `/{cmd_name}` 삭제가 완료되었습니다!", ephemeral=True)
        except Exception as e:
            traceback.print_exc()
            await interaction.followup.send(f"❌ cc_delete 에러 발생: `{type(e).__name__}: {e}`", ephemeral=True)

    # ══════════════════════════════════════════════════════════
    #  슬래시 관리자 명령어 (목록 조회)
    # ══════════════════════════════════════════════════════════
    @app_commands.command(name="cc_list", description="Display all active custom commands within this server node.")
    async def cc_list(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            settings = await self.get_guild_settings(interaction.guild_id)
            custom_commands = self._extract_commands(settings)

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
            traceback.print_exc()
            await interaction.followup.send(f"⚠️ 에러 발생: `{type(e).__name__}: {e}`")

async def setup(bot):
    await bot.add_cog(CustomCommands(bot))
    print("[⚡ CUSTOM_COMMANDS] Cog extension setup complete.", flush=True)