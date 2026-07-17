import discord
from discord import app_commands
from discord.ext import commands

# ⚡ 공통 베이스 Cog 상속 (Redis 캐시 + get_guild_settings + 캐시 무효화 공짜 획득)
from cogs.base import KyvoBaseCog


class CustomCommands(KyvoBaseCog):
    """
    KyvoBaseCog를 상속해 고성능 Redis Cache-Aside 파이프라인을 재사용한다.
    대시보드 또는 슬래시 명령어로 등록된 커스텀 매크로를 실시간으로 실행하는 엔진.
    저장소는 guild_settings.custom_commands (JSONB 객체)로 봇/대시보드가 공유한다.
    """

    def __init__(self, bot):
        super().__init__(bot)  # bot, supabase, redis, get_msg 등 베이스가 초기화

    # ══════════════════════════════════════════════════════════
    #  커스텀 명령어 실행 엔진 (유저 채팅 감지 → 매크로 응답)
    # ══════════════════════════════════════════════════════════
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """유저 메시지가 등록된 커스텀 명령어와 일치하면 응답을 발사한다."""
        # 봇 메시지 / DM 은 무시
        if message.author.bot or not message.guild:
            return

        content = message.content.strip()
        if not content:
            return

        # ⚡ [오발동 방지] 접두사(/ 또는 !)로 시작할 때만 커스텀 명령어로 취급한다.
        #    이게 없으면 "hello"라고 인사만 해도 hello 매크로가 발동하는 참사가 난다.
        if not (content.startswith("/") or content.startswith("!")):
            return

        # 접두사 한 글자를 떼고 소문자화 → 대시보드 저장 양식(접두사 없는 소문자)과 매칭
        trigger = content[1:].strip().lower()
        if not trigger:
            return

        # 베이스에서 상속받은 초고속 Cache-Aside 설정 로더 (Redis 경유, DB 부하 없음)
        settings = await self.get_guild_settings(message.guild.id)
        custom_commands = settings.get("custom_commands", {})

        if trigger in custom_commands:
            response_text = custom_commands[trigger]
            try:
                await message.channel.send(response_text)
            except (discord.Forbidden, discord.HTTPException):
                print(f"[COMMANDS][WARN] Failed to send response in guild={message.guild.id}", flush=True)

    # ══════════════════════════════════════════════════════════
    #  슬래시 관리자 명령어 (추가 / 삭제 / 목록)
    #  저장 후 캐시 무효화로 대시보드·봇 실시간 동기화
    # ══════════════════════════════════════════════════════════
    @app_commands.command(name="cc_add", description="Create or update a server-specific custom command response.")
    @app_commands.checks.has_permissions(administrator=True)
    async def cc_add(self, interaction: discord.Interaction, name: str, response: str):
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id)

        # 대시보드 저장 양식과 동일하게 정규화 (접두사 제거 + 소문자)
        cmd_name = name.strip().lower().removeprefix("/").removeprefix("!")
        if not cmd_name:
            await interaction.followup.send("❌ Invalid command name.", ephemeral=True)
            return

        settings = await self.get_guild_settings(interaction.guild_id)
        custom_commands = settings.get("custom_commands", {})
        custom_commands[cmd_name] = response

        try:
            await self.bot.bulk_update_guild_settings(guild_id, {"custom_commands": custom_commands})
            # 봇에서 저장 시에도 캐시를 비워 대시보드와 즉시 동기화
            await self.invalidate_settings_cache(interaction.guild_id)

            msg = self.bot.locale_manager.get(str(interaction.locale), "cc_success_add", name=cmd_name)
            await interaction.followup.send(msg, ephemeral=True)
        except Exception as e:
            print(f"[COMMANDS][ERROR] cc_add failed: {type(e).__name__}: {e}", flush=True)
            await interaction.followup.send(f"❌ `{str(e)}`", ephemeral=True)

    @app_commands.command(name="cc_delete", description="Permanently delete a custom command from the server configuration.")
    @app_commands.checks.has_permissions(administrator=True)
    async def cc_delete(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer(ephemeral=True)
        guild_id = str(interaction.guild_id)

        cmd_name = name.strip().lower().removeprefix("/").removeprefix("!")

        settings = await self.get_guild_settings(interaction.guild_id)
        custom_commands = settings.get("custom_commands", {})

        if cmd_name not in custom_commands:
            msg = self.bot.locale_manager.get(str(interaction.locale), "cc_err_not_found")
            await interaction.followup.send(msg, ephemeral=True)
            return

        custom_commands.pop(cmd_name)

        try:
            await self.bot.bulk_update_guild_settings(guild_id, {"custom_commands": custom_commands})
            await self.invalidate_settings_cache(interaction.guild_id)

            msg = self.bot.locale_manager.get(str(interaction.locale), "cc_success_delete", name=cmd_name)
            await interaction.followup.send(msg, ephemeral=True)
        except Exception as e:
            print(f"[COMMANDS][ERROR] cc_delete failed: {type(e).__name__}: {e}", flush=True)
            await interaction.followup.send(f"❌ `{str(e)}`", ephemeral=True)

    @app_commands.command(name="cc_list", description="Display all active custom commands within this server node.")
    async def cc_list(self, interaction: discord.Interaction):
        await interaction.response.defer()

        settings = await self.get_guild_settings(interaction.guild_id)
        custom_commands = settings.get("custom_commands", {})

        title = self.bot.locale_manager.get(str(interaction.locale), "cc_list_title")
        embed = discord.Embed(title=title, color=discord.Color.blue())

        if not custom_commands:
            empty_msg = self.bot.locale_manager.get(str(interaction.locale), "cc_list_empty")
            embed.description = empty_msg
            await interaction.followup.send(embed=embed)
            return

        for cmd, resp in custom_commands.items():
            display_resp = resp if len(resp) <= 100 else resp[:97] + "..."
            embed.add_field(name=f"/{cmd}", value=f"┕ `{display_resp}`", inline=False)

        await interaction.followup.send(embed=embed)


async def setup(bot):
    await bot.add_cog(CustomCommands(bot))
    print("[⚡ CUSTOM_COMMANDS] Cog extension setup complete.", flush=True)