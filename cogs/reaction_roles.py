import discord
from discord import app_commands
from discord.ext import commands
from cogs.base import KyvoBaseCog
import asyncio
import os
import hmac
from aiohttp import web

INTERNAL_API_SECRET = os.environ.get("INTERNAL_API_SECRET")

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

    async def cog_load(self):
        if INTERNAL_API_SECRET:
            self.bot.web_app.router.add_post("/internal/reaction-roles/attach", self.handle_attach_webhook)
            print("[⚡ REACTION_ROLES] Internal attach route registered at /internal/reaction-roles/attach.", flush=True)
        else:
            print("[REACTION_ROLES][WARN] INTERNAL_API_SECRET not set - dashboard-triggered binding "
                  "creation is disabled (the /reaction_role_add command still works).", flush=True)

    async def _db_call(self, fn):
        """supabase-py는 동기 클라이언트라 이벤트 루프를 막지 않도록 executor로 감싼다."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, fn)

    # ══════════════════════════════════════════════════════════
    #  바인딩 생성 - /reaction_role_add와 대시보드(내부 웹훅) 둘 다 이 함수 하나를 공유한다.
    #  검증(메시지 존재/권한/위계) -> 이모지 부착(검증 겸) -> DB 저장 순서를 반드시 지킨다 -
    #  DB에 먼저 쓰고 나중에 이모지 부착에 실패하면 "매핑은 있는데 실제로는 못 누르는" 유령
    #  매핑이 생기기 때문에, 이모지 부착까지 성공해야만 DB에 쓴다.
    # ══════════════════════════════════════════════════════════
    async def _create_binding(self, guild: discord.Guild, channel_id: int, message_id: int,
                               emoji: str, role: discord.Role, created_by: int) -> dict:
        channel = guild.get_channel(channel_id)
        if channel is None:
            return {"status": "channel_not_found"}

        try:
            target_message = await channel.fetch_message(message_id)
        except (discord.NotFound, discord.HTTPException):
            return {"status": "message_not_found"}

        if role.permissions.administrator:
            return {"status": "admin_role_blocked"}

        bot_member = guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_roles:
            return {"status": "no_manage_roles_permission"}

        if bot_member.top_role <= role:
            return {"status": "hierarchy_blocked"}

        try:
            await target_message.add_reaction(emoji)
        except (discord.HTTPException, discord.NotFound):
            return {"status": "invalid_emoji"}

        try:
            await self._db_call(
                lambda: self.bot.supabase.table("reaction_roles").upsert({
                    "guild_id": str(guild.id),
                    "channel_id": str(channel_id),
                    "message_id": str(message_id),
                    "emoji": str(emoji),
                    "role_id": str(role.id),
                    "created_by": str(created_by),
                }, on_conflict="message_id,emoji").execute()
            )
        except Exception as e:
            print(f"[REACTION_ROLE][ERROR] Upsert failed: {type(e).__name__}: {e}", flush=True)
            return {"status": "db_error", "detail": f"{type(e).__name__}: {e}"}

        return {"status": "ok"}

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
        except ValueError:
            msg = await self.get_msg(guild_id, "rr_msg_not_found")
            await interaction.followup.send(msg, ephemeral=True)
            return

        # A/1: administrator 역할 차단 - _create_binding도 다시 검사하지만, 위험 권한 확인 뷰를
        # 띄우기 전에 여기서 먼저 걸러야 불필요한 확인 절차를 안 거친다.
        if role.permissions.administrator:
            msg = await self.get_msg(guild_id, "cc_err_admin_role_blocked", role=role.name)
            await interaction.followup.send(msg, ephemeral=True)
            return

        # A/2: 위험 권한 2차 확인 - 이것도 _create_binding 밖에서 처리한다(Discord View는
        # 인터랙션이 있어야만 가능해서, 웹훅 경로와 공유할 수 없다 - 대시보드는 자기 쪽 2단계
        # 확인 API로 동일한 역할을 한다).
        dangerous = _get_dangerous_permissions(role)
        if dangerous:
            confirmed = await self._confirm_dangerous_role(interaction, role, dangerous)
            if not confirmed:
                return  # 뷰 안에서 취소/타임아웃 메시지 이미 전송함

        result = await self._create_binding(interaction.guild, interaction.channel.id, msg_id, emoji, role, interaction.user.id)

        status_to_key = {
            "channel_not_found": "rr_msg_not_found",
            "message_not_found": "rr_msg_not_found",
            "admin_role_blocked": "cc_err_admin_role_blocked",
            "no_manage_roles_permission": "rr_err_no_manage_roles_permission",
            "hierarchy_blocked": "rr_err_hierarchy",
            "invalid_emoji": "rr_err_invalid_emoji",
        }
        if result["status"] != "ok":
            key = status_to_key.get(result["status"])
            if key:
                msg = await self.get_msg(guild_id, key, role=role.name)
            else:
                msg = f"❌ {result.get('detail', result['status'])}"
            await interaction.followup.send(msg, ephemeral=True)
            return

        msg = await self.get_msg(guild_id, "rr_success", message_id=message_id, emoji=emoji, role_name=role.name)
        await interaction.followup.send(msg, ephemeral=True)

    async def handle_attach_webhook(self, request: web.Request) -> web.Response:
        secret_header = request.headers.get("X-Internal-Secret", "")
        if not secret_header or not hmac.compare_digest(secret_header, INTERNAL_API_SECRET):
            print("[REACTION_ROLE][WARN] Rejected attach request with invalid/missing internal secret", flush=True)
            return web.Response(status=403)

        try:
            body = await request.json()
        except Exception:
            return web.Response(status=400)

        guild_id = body.get("guild_id")
        channel_id = body.get("channel_id")
        message_id = body.get("message_id")
        emoji = body.get("emoji")
        role_id = body.get("role_id")
        created_by = body.get("created_by")
        if not all([guild_id, channel_id, message_id, emoji, role_id, created_by]):
            return web.Response(status=400, text="guild_id, channel_id, message_id, emoji, role_id, created_by are all required")

        guild = self.bot.get_guild(int(guild_id))
        if guild is None:
            return web.Response(status=404, text="guild not in cache")

        role = guild.get_role(int(role_id))
        if role is None:
            return web.Response(status=404, text="role not found")

        result = await self._create_binding(guild, int(channel_id), int(message_id), str(emoji), role, int(created_by))

        status_to_http = {
            "channel_not_found": 404, "message_not_found": 404,
            "admin_role_blocked": 400, "no_manage_roles_permission": 400,
            "hierarchy_blocked": 400, "invalid_emoji": 400, "db_error": 500,
        }
        if result["status"] != "ok":
            return web.json_response(result, status=status_to_http.get(result["status"], 400))

        return web.json_response(result)

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
