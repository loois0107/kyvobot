import discord
from discord import app_commands
from discord.ext import commands
from cogs.base import KyvoBaseCog
import asyncio

# 🛡️ 부여했을 때 2차 확인을 받아야 하는 위험 권한 - custom_commands.py의 role_add와 동일한
# 목록/정신. 반응 제거(역할을 뺏는 것)는 위험하지 않으니 이 체크 대상이 아니다.
DANGEROUS_ROLE_PERMISSIONS = (
    "manage_roles", "manage_guild", "manage_channels",
    "ban_members", "kick_members", "manage_webhooks", "manage_messages",
)


def _get_dangerous_permissions(role: discord.Role) -> list[str]:
    perms = role.permissions
    return [name for name in DANGEROUS_ROLE_PERMISSIONS if getattr(perms, name, False)]


class RoleWarningConfirmView(discord.ui.View):
    """위험 권한 역할을 반응 역할로 등록하기 전 마지막 확인 - Confirm을 눌러야만 실제로 저장된다.
    custom_commands.py의 동명 View와 동일한 패턴(각 cog가 자기 완결적이도록 복제)."""

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


class ReactionRoles(KyvoBaseCog):
    def __init__(self, bot):
        super().__init__(bot)

    async def _db_call(self, fn):
        """supabase-py는 동기 클라이언트라 이벤트 루프를 막지 않도록 executor로 감싼다."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, fn)

    # ══════════════════════════════════════════════════════════
    #  /reaction_role_add - 관리자 명령어
    # ══════════════════════════════════════════════════════════
    @app_commands.command(name="reaction_role_add",
                           description="Bind an emoji reaction on a message to a role (toggle: react to gain, unreact to lose).")
    @app_commands.describe(
        message_id="ID of the message to react to (must be in this channel).",
        emoji="The emoji members will react with.",
        role="The role granted on react / removed on unreact.",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def reaction_role_add(self, interaction: discord.Interaction, message_id: str, emoji: str, role: discord.Role):
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id

        try:
            msg_id = int(message_id)
            target_message = await interaction.channel.fetch_message(msg_id)
        except (ValueError, discord.NotFound, discord.HTTPException):
            msg = await self.get_msg(guild_id, "rr_msg_not_found")
            await interaction.followup.send(msg, ephemeral=True)
            return

        # A/1: administrator 역할 차단
        if role.permissions.administrator:
            msg = await self.get_msg(guild_id, "cc_err_admin_role_blocked", role=role.name)
            await interaction.followup.send(msg, ephemeral=True)
            return

        # 🛡️ [신규] 생성 시점 사전 체크 - 실행 불가능한 매핑을 애초에 못 만들게 막는다.
        # on_raw_reaction_add는 응답할 interaction이 없어 실패해도 콘솔 로그만 남는 "조용한 실패"라,
        # 여기서 미리 걸러내는 게 유저 경험상 훨씬 중요하다.
        bot_member = interaction.guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_roles:
            msg = await self.get_msg(guild_id, "rr_err_no_manage_roles_permission")
            await interaction.followup.send(msg, ephemeral=True)
            return

        if bot_member.top_role <= role:
            msg = await self.get_msg(guild_id, "rr_err_hierarchy", role=role.name)
            await interaction.followup.send(msg, ephemeral=True)
            return

        # A/2: 위험 권한 2차 확인
        dangerous = _get_dangerous_permissions(role)
        if dangerous:
            confirmed = await self._confirm_dangerous_role(interaction, role, dangerous)
            if not confirmed:
                return  # 뷰 안에서 취소/타임아웃 메시지 이미 전송함

        # 이모지가 실제로 반응 가능한지 먼저 검증한다(반응 시도 자체가 검증) - DB 저장은 그 다음.
        try:
            await target_message.add_reaction(emoji)
        except (discord.HTTPException, discord.NotFound):
            msg = await self.get_msg(guild_id, "rr_err_invalid_emoji")
            await interaction.followup.send(msg, ephemeral=True)
            return

        try:
            await self._db_call(
                lambda: self.bot.supabase.table("reaction_roles").upsert({
                    "guild_id": str(guild_id),
                    "channel_id": str(interaction.channel.id),
                    "message_id": str(msg_id),
                    "emoji": str(emoji),
                    "role_id": str(role.id),
                    "created_by": str(interaction.user.id),
                }, on_conflict="message_id,emoji").execute()
            )
        except Exception as e:
            print(f"[REACTION_ROLE][ERROR] Upsert failed: {type(e).__name__}: {e}", flush=True)
            await interaction.followup.send(f"❌ {type(e).__name__}: {e}", ephemeral=True)
            return

        msg = await self.get_msg(guild_id, "rr_success", message_id=message_id, emoji=emoji, role_name=role.name)
        await interaction.followup.send(msg, ephemeral=True)

    async def _confirm_dangerous_role(self, interaction: discord.Interaction, role: discord.Role, dangerous: list[str]) -> bool:
        """위험 권한 역할일 때 Confirm/Cancel 뷰를 보여주고 응답을 기다린다."""
        guild_id = interaction.guild_id
        perms_text = ", ".join(f"`{p}`" for p in dangerous)
        warning_msg = await self.get_msg(guild_id, "cc_warning_dangerous_role", role=role.name, permissions=perms_text)
        confirm_label = await self.get_msg(guild_id, "cc_confirm_button")
        cancel_label = await self.get_msg(guild_id, "cc_cancel_button")

        view = RoleWarningConfirmView(interaction.user.id, confirm_label, cancel_label)
        embed = discord.Embed(title="⚠️ Dangerous Permission Warning", description=warning_msg, color=discord.Color.orange())
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        await view.wait()

        if view.confirmed is True:
            return True

        cancel_key = "cc_action_cancelled" if view.confirmed is False else "cc_confirm_timeout"
        cancel_msg = await self.get_msg(guild_id, cancel_key)
        await interaction.followup.send(cancel_msg, ephemeral=True)
        return False

    # ══════════════════════════════════════════════════════════
    #  실행 엔진 - raw reaction 이벤트는 순수 게이트웨이 이벤트라 응답할 interaction이 없다.
    #  실패는 콘솔 로그로만 남긴다(leveling.py의 레벨업 보상 실패 로그와 동일한 성격).
    #  별도 콜드 스타트 대응이 필요 없다 - 매핑이 DB에 있고 이벤트가 올 때마다 그 자리에서
    #  조회하는 구조라, 재시작해도 리스너만 다시 붙으면 그걸로 끝이다.
    # ══════════════════════════════════════════════════════════
    async def _lookup_mapping(self, message_id: int, emoji: str) -> dict | None:
        try:
            res = await self._db_call(
                lambda: self.bot.supabase.table("reaction_roles").select("role_id")
                        .eq("message_id", str(message_id)).eq("emoji", emoji).execute()
            )
            return res.data[0] if res.data else None
        except Exception as e:
            print(f"[REACTION_ROLE][ERROR] Lookup failed (message={message_id}, emoji={emoji}): "
                  f"{type(e).__name__}: {e}", flush=True)
            return None

    def _resolve_role_for_action(self, guild: discord.Guild, role_id: str) -> discord.Role | None:
        """역할 존재 + 봇 권한 + 위계를 확인하고, 실행 가능하면 Role을, 아니면 None을 반환하며
        구체적 사유를 콘솔에 남긴다."""
        role = guild.get_role(int(role_id))
        if role is None:
            print(f"[REACTION_ROLE][WARN] Mapped role {role_id} no longer exists (guild={guild.id})", flush=True)
            return None

        bot_member = guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_roles:
            print(f"[REACTION_ROLE][ERROR] Bot lacks manage_roles permission (guild={guild.id})", flush=True)
            return None

        if bot_member.top_role <= role:
            print(f"[REACTION_ROLE][ERROR] Role hierarchy: bot's top role '{bot_member.top_role}' is not above "
                  f"'{role.name}' (guild={guild.id})", flush=True)
            return None

        return role

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id is None:
            return
        if payload.member is None or payload.member.bot:
            return

        emoji_str = str(payload.emoji)
        mapping = await self._lookup_mapping(payload.message_id, emoji_str)
        if not mapping:
            return  # 우리가 관리하는 매핑이 아닌 반응 - 흔한 케이스라 로그 남기지 않음

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            print(f"[REACTION_ROLE][ERROR] Guild {payload.guild_id} not in cache, cannot grant role", flush=True)
            return

        role = self._resolve_role_for_action(guild, mapping["role_id"])
        if role is None:
            return

        # 🛡️ TOCTOU 재확인: 매핑을 만든 뒤 이 역할에 administrator 권한이 추가됐을 수 있다.
        if role.permissions.administrator:
            print(f"[REACTION_ROLE][WARN] Refusing to grant administrator role '{role.name}' via reaction "
                  f"(guild={guild.id}, message={payload.message_id})", flush=True)
            return

        try:
            await payload.member.add_roles(role, reason="[KYVO REACTION ROLE]")
        except discord.Forbidden:
            print(f"[REACTION_ROLE][ERROR] Forbidden while granting role '{role.name}' to user={payload.user_id} "
                  f"(guild={guild.id})", flush=True)
        except discord.HTTPException as e:
            print(f"[REACTION_ROLE][ERROR] HTTPException while granting role: {type(e).__name__}: {e} "
                  f"(guild={guild.id})", flush=True)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id is None:
            return

        emoji_str = str(payload.emoji)
        mapping = await self._lookup_mapping(payload.message_id, emoji_str)
        if not mapping:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            print(f"[REACTION_ROLE][ERROR] Guild {payload.guild_id} not in cache, cannot remove role", flush=True)
            return

        role = self._resolve_role_for_action(guild, mapping["role_id"])
        if role is None:
            return

        # reaction_remove 페이로드는 add와 달리 payload.member를 안 준다 - 직접 조회해야 한다.
        member = guild.get_member(payload.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(payload.user_id)
            except discord.NotFound:
                return
        if member.bot:
            return

        try:
            await member.remove_roles(role, reason="[KYVO REACTION ROLE]")
        except discord.Forbidden:
            print(f"[REACTION_ROLE][ERROR] Forbidden while removing role '{role.name}' from user={payload.user_id} "
                  f"(guild={guild.id})", flush=True)
        except discord.HTTPException as e:
            print(f"[REACTION_ROLE][ERROR] HTTPException while removing role: {type(e).__name__}: {e} "
                  f"(guild={guild.id})", flush=True)


async def setup(bot):
    await bot.add_cog(ReactionRoles(bot))
    print("[⚡ REACTION_ROLES] Cog extension setup complete.", flush=True)
