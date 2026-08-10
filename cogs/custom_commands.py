import discord
from discord import app_commands
from discord.ext import commands
from cogs.base import KyvoBaseCog
import asyncio
import traceback

# 🛡️ role_add로 부여했을 때 2차 확인을 받아야 하는 위험 권한 - 이 중 하나라도 있으면 관리자가
# 의도한 게 맞는지 한 번 더 확인받는다(role_remove는 권한을 뺏는 것이므로 대상 아님).
DANGEROUS_ROLE_PERMISSIONS = (
    "manage_roles", "manage_guild", "manage_channels",
    "ban_members", "kick_members", "manage_webhooks", "manage_messages",
)


def _get_dangerous_permissions(role: discord.Role) -> list[str]:
    perms = role.permissions
    return [name for name in DANGEROUS_ROLE_PERMISSIONS if getattr(perms, name, False)]


class RoleWarningConfirmView(discord.ui.View):
    """위험 권한 역할을 role_add로 등록하기 전 마지막 확인 - Confirm을 눌러야만 실제로 저장된다."""

    def __init__(self, author_id: int, confirm_label: str, cancel_label: str):
        super().__init__(timeout=60)
        self.author_id = author_id
        self.confirmed: bool | None = None  # None = 타임아웃(무응답)

        confirm_btn = discord.ui.Button(label=confirm_label, style=discord.ButtonStyle.danger)
        confirm_btn.callback = self._on_confirm
        self.add_item(confirm_btn)

        cancel_btn = discord.ui.Button(label=cancel_label, style=discord.ButtonStyle.secondary)
        cancel_btn.callback = self._on_cancel
        self.add_item(cancel_btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ This confirmation is not for you.", ephemeral=True)
            return False
        return True

    def _disable_all(self):
        for item in self.children:
            item.disabled = True

    async def _on_confirm(self, interaction: discord.Interaction):
        self.confirmed = True
        self._disable_all()
        await interaction.response.edit_message(view=self)
        self.stop()

    async def _on_cancel(self, interaction: discord.Interaction):
        self.confirmed = False
        self._disable_all()
        await interaction.response.edit_message(view=self)
        self.stop()

    async def on_timeout(self):
        self._disable_all()


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

    def _normalize_command_entry(self, raw) -> dict:
        """레거시 문자열 형태({trigger: "응답텍스트"})와 신규 dict 형태({trigger: {type, ...}})를
        하나의 표준 형태로 통일한다. _extract_commands가 컨테이너(저장 위치) 차이를 흡수하듯,
        이건 값(leaf) 레벨의 스키마 차이를 흡수한다 - on_message는 이 정규화된 dict만 보고 분기하면 된다."""
        if isinstance(raw, str):
            return {"type": "text", "content": raw}
        if isinstance(raw, dict) and raw.get("type") in ("text", "role_add", "role_remove"):
            return raw
        # 알 수 없는 형태 방어 - 죽지 않고 안전하게 텍스트로 강등
        return {"type": "text", "content": str(raw)}

    async def _save_commands(self, guild_id: str, custom_commands: dict) -> None:
        # 🌐 최상위 custom_commands 컬럼에 직접 저장 (대시보드와 물리적 저장 위치 동기화)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: self.bot.supabase.table("guild_settings")
                    .update({"custom_commands": custom_commands})
                    .eq("guild_id", guild_id).execute()
        )
        await self.invalidate_settings_cache(guild_id)

    def _log_role_action(self, guild_id: int, user_id: int, action: str, reason: str) -> None:
        """automod.py의 배치 큐/DLQ 로깅 엔진을 그대로 재사용 - 누가/언제/무슨 역할을 받았는지 기록."""
        automod_cog = self.bot.get_cog("AutoMod")
        if automod_cog and hasattr(automod_cog, "enqueue_log"):
            automod_cog.enqueue_log(guild_id, user_id, action, reason)

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

            if trigger not in custom_commands:
                return

            entry = self._normalize_command_entry(custom_commands[trigger])

            if entry["type"] == "text":
                await message.channel.send(entry.get("content", ""))
                return

            await self._execute_role_action(message, trigger, entry)
        except Exception as e:
            print(f"[CC_ON_MESSAGE][ERROR] {e}", flush=True)

    async def _send_generic_unavailable(self, message: discord.Message) -> None:
        """실행 시점 실패는 절대 조용히 삼키지 않되, 위계/권한 같은 서버 관리 디테일은 일반 유저에게
        노출하지 않는다 - 구체적인 원인은 콘솔 로그에만 남긴다."""
        try:
            msg = await self.get_msg(message.guild.id, "cc_role_action_unavailable")
            await message.channel.send(msg)
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def _execute_role_action(self, message: discord.Message, trigger: str, entry: dict) -> None:
        guild = message.guild
        member = message.author  # 🛡️ self-target 확정: 실행한 본인에게만 적용, 멘션 파싱 없음
        action = entry["type"]
        role_id = entry.get("role_id")

        role = guild.get_role(int(role_id)) if role_id else None
        if role is None:
            print(f"[CC_ROLE_ACTION][WARN] Role {role_id} no longer exists (guild={guild.id}, trigger={trigger})", flush=True)
            await self._send_generic_unavailable(message)
            return

        # 1'-B: 관리자 권한 재확인 (TOCTOU 대응 - 생성 이후 이 역할에 admin 권한이 추가됐을 수 있음).
        # role_remove는 권한을 뺏는 것이므로 위험하지 않아 이 체크 대상이 아니다.
        if action == "role_add" and role.permissions.administrator:
            print(f"[CC_ROLE_ACTION][WARN] Refusing to grant administrator role '{role.name}' "
                  f"(guild={guild.id}, trigger={trigger})", flush=True)
            await self._send_generic_unavailable(message)
            return

        # 3-B: 봇 권한 + 역할 위계 확인 (add/remove 둘 다 동일하게 필요)
        bot_member = guild.me
        if not bot_member.guild_permissions.manage_roles:
            print(f"[CC_ROLE_ACTION][WARN] Bot lacks manage_roles permission "
                  f"(guild={guild.id}, trigger={trigger})", flush=True)
            await self._send_generic_unavailable(message)
            return

        if bot_member.top_role <= role:
            print(f"[CC_ROLE_ACTION][WARN] Role hierarchy: bot's top role '{bot_member.top_role}' "
                  f"is not above target role '{role.name}' (guild={guild.id}, trigger={trigger})", flush=True)
            await self._send_generic_unavailable(message)
            return

        try:
            if action == "role_add":
                await member.add_roles(role, reason=f"[KYVO CUSTOM COMMAND] /{trigger}")
            else:
                await member.remove_roles(role, reason=f"[KYVO CUSTOM COMMAND] /{trigger}")
        except discord.Forbidden:
            print(f"[CC_ROLE_ACTION][ERROR] Forbidden while executing '{action}' on role '{role.name}' "
                  f"(guild={guild.id}, trigger={trigger})", flush=True)
            await self._send_generic_unavailable(message)
            return
        except discord.HTTPException as e:
            print(f"[CC_ROLE_ACTION][ERROR] HTTPException while executing '{action}': {e} "
                  f"(guild={guild.id}, trigger={trigger})", flush=True)
            await self._send_generic_unavailable(message)
            return

        self._log_role_action(
            guild.id, member.id, action,
            f"Custom command '/{trigger}' -> role '{role.name}' ({role.id}), self-service"
        )

        success_key = "cc_role_add_success" if action == "role_add" else "cc_role_remove_success"
        try:
            msg = await self.get_msg(guild.id, success_key, role=role.name)
            await message.channel.send(msg)
        except (discord.Forbidden, discord.HTTPException):
            pass

    # ══════════════════════════════════════════════════════════
    #  슬래시 관리자 명령어 (추가) - text / role_add / role_remove 서브커맨드로 분리
    # ══════════════════════════════════════════════════════════
    cc_add_group = app_commands.Group(name="cc_add", description="Create or update a server-specific custom command.",
                                       default_permissions=discord.Permissions(administrator=True))

    async def _resolve_cmd_name(self, interaction: discord.Interaction, name: str) -> str | None:
        """이름 정규화 + 검증. 실패 시 에러 메시지를 보내고 None을 반환한다."""
        cmd_name = name.strip().lower().removeprefix("/").removeprefix("!")
        if not cmd_name:
            msg = await self.get_msg(interaction.guild_id, "cc_err_prefix")
            await interaction.followup.send(msg, ephemeral=True)
            return None
        return cmd_name

    @cc_add_group.command(name="text", description="Create a custom command that replies with text.")
    @app_commands.describe(name="The command's name, without the slash prefix.", response="The text this command replies with when triggered.")
    async def cc_add_text(self, interaction: discord.Interaction, name: str, response: str):
        await interaction.response.defer(ephemeral=True)
        try:
            guild_id = interaction.guild_id
            cmd_name = await self._resolve_cmd_name(interaction, name)
            if cmd_name is None:
                return

            settings = await self.get_guild_settings(guild_id)
            custom_commands = self._extract_commands(settings)
            custom_commands[cmd_name] = {"type": "text", "content": response}
            await self._save_commands(str(guild_id), custom_commands)

            msg = await self.get_msg(guild_id, "cc_success_add", name=cmd_name)
            await interaction.followup.send(msg, ephemeral=True)
        except Exception as e:
            traceback.print_exc()
            await interaction.followup.send(f"❌ cc_add 에러 발생: `{type(e).__name__}: {e}`", ephemeral=True)

    async def _confirm_dangerous_role(self, interaction: discord.Interaction, role: discord.Role, dangerous: list[str]) -> bool:
        """위험 권한 역할일 때 Confirm/Cancel 뷰를 보여주고 응답을 기다린다."""
        guild_id = interaction.guild_id
        perms_text = ", ".join(f"`{p}`" for p in dangerous)
        warning_msg = await self.get_msg(guild_id, "cc_warning_dangerous_role", role=role.name, permissions=perms_text)
        confirm_label = await self.get_msg(guild_id, "cc_confirm_button")
        cancel_label = await self.get_msg(guild_id, "cc_cancel_button")

        view = RoleWarningConfirmView(interaction.user.id, confirm_label, cancel_label)
        warning_title = await self.get_msg(guild_id, "cc_warning_dangerous_title")
        embed = discord.Embed(title=warning_title, description=warning_msg, color=discord.Color.orange())
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        await view.wait()

        if view.confirmed is True:
            return True

        cancel_key = "cc_action_cancelled" if view.confirmed is False else "cc_confirm_timeout"
        cancel_msg = await self.get_msg(guild_id, cancel_key)
        await interaction.followup.send(cancel_msg, ephemeral=True)
        return False

    async def _create_role_command(self, interaction: discord.Interaction, name: str, role: discord.Role, action: str):
        await interaction.response.defer(ephemeral=True)
        try:
            guild_id = interaction.guild_id
            cmd_name = await self._resolve_cmd_name(interaction, name)
            if cmd_name is None:
                return

            # A/1: 관리자 권한 역할 차단 (role_add에만 해당 - role_remove는 권한을 뺏는 것이므로 안전)
            if action == "role_add" and role.permissions.administrator:
                msg = await self.get_msg(guild_id, "cc_err_admin_role_blocked", role=role.name)
                await interaction.followup.send(msg, ephemeral=True)
                return

            # A/2: 위험 권한 보유 시 2차 확인 (role_add에만 해당)
            if action == "role_add":
                dangerous = _get_dangerous_permissions(role)
                if dangerous:
                    confirmed = await self._confirm_dangerous_role(interaction, role, dangerous)
                    if not confirmed:
                        return  # 뷰 안에서 취소/타임아웃 메시지 이미 전송함

            settings = await self.get_guild_settings(guild_id)
            custom_commands = self._extract_commands(settings)
            custom_commands[cmd_name] = {"type": action, "role_id": str(role.id)}
            await self._save_commands(str(guild_id), custom_commands)

            saved_key = "cc_role_add_saved" if action == "role_add" else "cc_role_remove_saved"
            msg = await self.get_msg(guild_id, saved_key, name=cmd_name, role=role.name)
            await interaction.followup.send(msg, ephemeral=True)
        except Exception as e:
            traceback.print_exc()
            await interaction.followup.send(f"❌ cc_add 에러 발생: `{type(e).__name__}: {e}`", ephemeral=True)

    @cc_add_group.command(name="role_add", description="Create a self-service custom command that grants a role to whoever triggers it.")
    @app_commands.describe(name="The command's name, without the slash prefix.", role="The role this command grants when triggered.")
    async def cc_add_role_add(self, interaction: discord.Interaction, name: str, role: discord.Role):
        await self._create_role_command(interaction, name, role, action="role_add")

    @cc_add_group.command(name="role_remove", description="Create a self-service custom command that removes a role from whoever triggers it.")
    @app_commands.describe(name="The command's name, without the slash prefix.", role="The role this command removes when triggered.")
    async def cc_add_role_remove(self, interaction: discord.Interaction, name: str, role: discord.Role):
        await self._create_role_command(interaction, name, role, action="role_remove")

    # ══════════════════════════════════════════════════════════
    #  슬래시 관리자 명령어 (삭제)
    # ══════════════════════════════════════════════════════════
    @app_commands.command(name="cc_delete", description="Permanently delete a custom command from the server configuration.")
    @app_commands.describe(name="The name of the custom command to delete.")
    @app_commands.default_permissions(administrator=True)
    async def cc_delete(self, interaction: discord.Interaction, name: str):
        await interaction.response.defer(ephemeral=True)
        try:
            guild_id = interaction.guild_id
            cmd_name = name.strip().lower().removeprefix("/").removeprefix("!")

            settings = await self.get_guild_settings(guild_id)
            custom_commands = self._extract_commands(settings)

            if cmd_name not in custom_commands:
                msg = await self.get_msg(guild_id, "cc_err_not_found")
                await interaction.followup.send(msg, ephemeral=True)
                return

            custom_commands.pop(cmd_name)
            await self._save_commands(str(guild_id), custom_commands)

            msg = await self.get_msg(guild_id, "cc_success_delete", name=cmd_name)
            await interaction.followup.send(msg, ephemeral=True)
        except Exception as e:
            traceback.print_exc()
            await interaction.followup.send(f"❌ cc_delete 에러 발생: `{type(e).__name__}: {e}`", ephemeral=True)

    # ══════════════════════════════════════════════════════════
    #  슬래시 관리자 명령어 (목록 조회)
    # ══════════════════════════════════════════════════════════
    @app_commands.command(name="cc_list", description="List all custom commands in this server.")
    async def cc_list(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            guild_id = interaction.guild_id
            settings = await self.get_guild_settings(guild_id)
            custom_commands = self._extract_commands(settings)

            title = await self.get_msg(guild_id, "cc_list_title")
            embed = discord.Embed(title=title, color=discord.Color.blue())

            if not custom_commands:
                embed.description = await self.get_msg(guild_id, "cc_list_empty")
                await interaction.followup.send(embed=embed)
                return

            for cmd, raw in custom_commands.items():
                entry = self._normalize_command_entry(raw)
                if entry["type"] == "text":
                    content = entry.get("content", "")
                    display = content if len(content) <= 100 else content[:97] + "..."
                    embed.add_field(name=f"/{cmd}", value=f"┕ `{display}`", inline=False)
                elif entry["type"] == "role_add":
                    embed.add_field(name=f"/{cmd}", value=f"┕ 🎭 Grants role <@&{entry.get('role_id')}>", inline=False)
                elif entry["type"] == "role_remove":
                    embed.add_field(name=f"/{cmd}", value=f"┕ 🚫 Removes role <@&{entry.get('role_id')}>", inline=False)

            await interaction.followup.send(embed=embed)
        except Exception as e:
            traceback.print_exc()
            await interaction.followup.send(f"⚠️ 에러 발생: `{type(e).__name__}: {e}`")

async def setup(bot):
    await bot.add_cog(CustomCommands(bot))
    print("[⚡ CUSTOM_COMMANDS] Cog extension setup complete.", flush=True)
