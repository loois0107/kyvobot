import os
import time
import hmac
import hashlib
import json
import asyncio

import discord
from discord import app_commands
from discord.ext import tasks
from aiohttp import web
import aiohttp

from cogs.base import KyvoBaseCog

# 🛡️ [Fail-Fast] 셋 다 없으면 이 기능은 아무 것도 할 수 없다. 로드 시점에 즉시 예외를 던져서
# main.py의 기존 per-extension try/except([CRITICAL LAYER ERROR] 로그)에 걸리게 한다 - 이 코그만
# 로드 실패하고 다른 코그는 정상 작동한다 (tier_verify.py와 동일한 격리 철학).
TWITCH_CLIENT_ID = os.environ.get("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = os.environ.get("TWITCH_CLIENT_SECRET")
TWITCH_EVENTSUB_SECRET = os.environ.get("TWITCH_EVENTSUB_SECRET")
TWITCH_EVENTSUB_CALLBACK_URL = os.environ.get("TWITCH_EVENTSUB_CALLBACK_URL")

for _name, _val in (
    ("TWITCH_CLIENT_ID", TWITCH_CLIENT_ID),
    ("TWITCH_CLIENT_SECRET", TWITCH_CLIENT_SECRET),
    ("TWITCH_EVENTSUB_SECRET", TWITCH_EVENTSUB_SECRET),
    ("TWITCH_EVENTSUB_CALLBACK_URL", TWITCH_EVENTSUB_CALLBACK_URL),
):
    if not _val:
        raise RuntimeError(
            f"[TWITCH] {_name} environment variable is not set. "
            "Set it before starting the bot (Twitch Developer Console for CLIENT_ID/SECRET; "
            "TWITCH_EVENTSUB_SECRET is a secret you generate yourself; TWITCH_EVENTSUB_CALLBACK_URL "
            "is the public https URL that receives EventSub webhooks, e.g. "
            "https://kyvobot.onrender.com/webhooks/twitch)."
        )

# 🛡️ 대시보드 트위치 관리 페이지가 스트리머 삭제를 이 봇에게 위임할 때 쓰는 내부 전용 시크릿.
# party.py의 tier-role 정리 웹훅과 동일한 패턴 - 없어도 이 코그 자체는 정상 동작하고
# (슬래시 커맨드는 이 시크릿과 무관), 대시보드發 삭제 요청 라우트만 등록을 건너뛴다.
INTERNAL_API_SECRET = os.environ.get("INTERNAL_API_SECRET")

TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
TWITCH_HELIX_BASE = "https://api.twitch.tv/helix"
TWITCH_POLL_INTERVAL_MINUTES = 5
TWITCH_WEBHOOK_PATH = "/webhooks/twitch"
TWITCH_HTTP_TIMEOUT_SECONDS = 8.0

# 🛡️ 위험 권한 역할 확인 - custom_commands.py/reaction_roles.py/party.py와 동일한 목록/정신
# (각 코그가 자기 완결적이도록 복제).
DANGEROUS_ROLE_PERMISSIONS = (
    "manage_roles", "manage_guild", "manage_channels",
    "ban_members", "kick_members", "manage_webhooks", "manage_messages",
)


def _get_dangerous_permissions(role: discord.Role) -> list[str]:
    perms = role.permissions
    return [name for name in DANGEROUS_ROLE_PERMISSIONS if getattr(perms, name, False)]


class RoleWarningConfirmView(discord.ui.View):
    def __init__(self, author_id: int, confirm_label: str, cancel_label: str):
        super().__init__(timeout=60)
        self.author_id = author_id
        self.confirmed: bool | None = None

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


class KyvoTwitch(KyvoBaseCog):
    def __init__(self, bot):
        super().__init__(bot)
        self._app_token: str | None = None
        self._app_token_expires_at: float = 0.0

    async def cog_load(self):
        self.bot.web_app.router.add_post(TWITCH_WEBHOOK_PATH, self.handle_webhook)
        print(f"[⚡ TWITCH] Webhook route registered at {TWITCH_WEBHOOK_PATH}.", flush=True)

        if INTERNAL_API_SECRET:
            self.bot.web_app.router.add_post("/internal/twitch/remove", self.handle_remove_webhook)
            print("[⚡ TWITCH] Internal streamer-removal route registered at /internal/twitch/remove.", flush=True)
        else:
            print("[TWITCH][WARN] INTERNAL_API_SECRET not set - dashboard-triggered streamer removal "
                  "is disabled (the /twitch_channel_remove command still works).", flush=True)

    async def cog_unload(self):
        self.reconcile_streams.cancel()

    async def _db_call(self, fn):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.bot.db_executor, fn)

    # ══════════════════════════════════════════════════════════
    #  Helix API 헬퍼 - 앱 액세스 토큰 캐싱/자동 갱신
    # ══════════════════════════════════════════════════════════
    async def _get_app_token(self, session: aiohttp.ClientSession) -> str:
        if self._app_token and time.monotonic() < self._app_token_expires_at:
            return self._app_token

        async with session.post(
            TWITCH_TOKEN_URL,
            params={"client_id": TWITCH_CLIENT_ID, "client_secret": TWITCH_CLIENT_SECRET, "grant_type": "client_credentials"},
            timeout=aiohttp.ClientTimeout(total=TWITCH_HTTP_TIMEOUT_SECONDS),
        ) as resp:
            body = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"Failed to fetch Twitch app token: {resp.status} {body}")
            data = json.loads(body)

        self._app_token = data["access_token"]
        self._app_token_expires_at = time.monotonic() + data["expires_in"] - 60
        return self._app_token

    async def _helix_request(self, session: aiohttp.ClientSession, method: str, path: str,
                              params: dict | None = None, json_body: dict | None = None) -> dict:
        token = await self._get_app_token(session)
        headers = {"Client-Id": TWITCH_CLIENT_ID, "Authorization": f"Bearer {token}"}
        url = f"{TWITCH_HELIX_BASE}{path}"

        async with session.request(method, url, headers=headers, params=params, json=json_body,
                                    timeout=aiohttp.ClientTimeout(total=TWITCH_HTTP_TIMEOUT_SECONDS)) as resp:
            status, text = resp.status, await resp.text()

        if status == 401:
            # 토큰이 예상보다 일찍 무효화된 경우 - 강제로 새로 받아서 1회만 재시도.
            self._app_token = None
            token = await self._get_app_token(session)
            headers["Authorization"] = f"Bearer {token}"
            async with session.request(method, url, headers=headers, params=params, json=json_body,
                                        timeout=aiohttp.ClientTimeout(total=TWITCH_HTTP_TIMEOUT_SECONDS)) as resp2:
                status, text = resp2.status, await resp2.text()

        if status >= 400:
            raise RuntimeError(f"Helix {method} {path} failed: {status} {text}")
        return json.loads(text) if text else {}

    async def _resolve_broadcaster(self, session: aiohttp.ClientSession, login: str) -> dict | None:
        data = await self._helix_request(session, "GET", "/users", params={"login": login})
        users = data.get("data") or []
        return users[0] if users else None

    async def _create_subscription(self, session: aiohttp.ClientSession, sub_type: str, broadcaster_id: str) -> str:
        body = {
            "type": sub_type, "version": "1",
            "condition": {"broadcaster_user_id": broadcaster_id},
            "transport": {"method": "webhook", "callback": TWITCH_EVENTSUB_CALLBACK_URL, "secret": TWITCH_EVENTSUB_SECRET},
        }
        data = await self._helix_request(session, "POST", "/eventsub/subscriptions", json_body=body)
        return data["data"][0]["id"]

    async def _delete_subscription(self, session: aiohttp.ClientSession, sub_id: str) -> None:
        await self._helix_request(session, "DELETE", "/eventsub/subscriptions", params={"id": sub_id})

    async def _fetch_stream_info(self, broadcaster_id: str) -> dict | None:
        try:
            async with aiohttp.ClientSession() as session:
                data = await self._helix_request(session, "GET", "/streams", params={"user_id": broadcaster_id})
            streams = data.get("data") or []
            return streams[0] if streams else None
        except Exception as e:
            print(f"[TWITCH][WARN] Failed to fetch stream info for {broadcaster_id}: {type(e).__name__}: {e}", flush=True)
            return None

    # ══════════════════════════════════════════════════════════
    #  /twitch_channel_set - 관리자 전용
    # ══════════════════════════════════════════════════════════
    @app_commands.command(name="twitch_channel_set", description="Get notified (and optionally grant a role) when a Twitch channel goes live.")
    @app_commands.describe(
        streamer="Twitch login name (from twitch.tv/<this>)",
        channel="Channel to post the live announcement in",
        member="Discord member to grant the live role to (required if role is set)",
        role="Role to grant while they're live (optional)",
    )
    @app_commands.checks.has_permissions(administrator=True)
    async def twitch_channel_set(self, interaction: discord.Interaction, streamer: str, channel: discord.TextChannel,
                                  member: discord.Member | None = None, role: discord.Role | None = None):
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        streamer = streamer.strip().lstrip("@").lower()
        bot_member = interaction.guild.me

        if role is not None and member is None:
            msg = await self.get_msg(guild_id, "twitch_err_role_needs_member")
            await interaction.followup.send(msg, ephemeral=True)
            return

        perms = channel.permissions_for(bot_member)
        if not (perms.send_messages and perms.embed_links):
            msg = await self.get_msg(guild_id, "twitch_err_channel_permission", channel=channel.mention)
            await interaction.followup.send(msg, ephemeral=True)
            return

        if role is not None:
            if role.permissions.administrator:
                msg = await self.get_msg(guild_id, "cc_err_admin_role_blocked", role=role.name)
                await interaction.followup.send(msg, ephemeral=True)
                return
            if not bot_member.guild_permissions.manage_roles:
                msg = await self.get_msg(guild_id, "rr_err_no_manage_roles_permission")
                await interaction.followup.send(msg, ephemeral=True)
                return
            if bot_member.top_role <= role:
                msg = await self.get_msg(guild_id, "rr_err_hierarchy", role=role.name)
                await interaction.followup.send(msg, ephemeral=True)
                return
            dangerous = _get_dangerous_permissions(role)
            if dangerous:
                confirmed = await self._confirm_dangerous_role(interaction, role, dangerous)
                if not confirmed:
                    return

        async with aiohttp.ClientSession() as session:
            try:
                user = await self._resolve_broadcaster(session, streamer)
            except Exception as e:
                print(f"[TWITCH][ERROR] Failed to resolve broadcaster '{streamer}': {type(e).__name__}: {e}", flush=True)
                msg = await self.get_msg(guild_id, "twitch_err_subscription_failed")
                await interaction.followup.send(msg, ephemeral=True)
                return

            if user is None:
                msg = await self.get_msg(guild_id, "twitch_err_streamer_not_found", streamer=streamer)
                await interaction.followup.send(msg, ephemeral=True)
                return

            broadcaster_id = user["id"]

            try:
                existing = await self._db_call(
                    lambda: self.bot.supabase.table("twitch_streamers").select("*").eq("broadcaster_id", broadcaster_id).execute()
                )
                streamer_row = existing.data[0] if existing.data else None
            except Exception as e:
                print(f"[TWITCH][ERROR] Failed to look up streamer row for {broadcaster_id}: {type(e).__name__}: {e}", flush=True)
                msg = await self.get_msg(guild_id, "twitch_err_subscription_failed")
                await interaction.followup.send(msg, ephemeral=True)
                return

            if streamer_row is None:
                sub_online_id = None
                try:
                    sub_online_id = await self._create_subscription(session, "stream.online", broadcaster_id)
                    sub_offline_id = await self._create_subscription(session, "stream.offline", broadcaster_id)
                except Exception as e:
                    print(f"[TWITCH][ERROR] Failed to create EventSub subscriptions for {broadcaster_id}: {type(e).__name__}: {e}", flush=True)
                    if sub_online_id:
                        try:
                            await self._delete_subscription(session, sub_online_id)
                        except Exception as cleanup_e:
                            print(f"[TWITCH][ERROR] Rollback failed for subscription {sub_online_id}: {type(cleanup_e).__name__}: {cleanup_e}", flush=True)
                    msg = await self.get_msg(guild_id, "twitch_err_subscription_failed")
                    await interaction.followup.send(msg, ephemeral=True)
                    return

                try:
                    insert_res = await self._db_call(
                        lambda: self.bot.supabase.table("twitch_streamers").insert({
                            "broadcaster_id": broadcaster_id, "broadcaster_login": user["login"],
                            "subscription_id_online": sub_online_id, "subscription_id_offline": sub_offline_id,
                        }).execute()
                    )
                    streamer_row = insert_res.data[0]
                except Exception as e:
                    print(f"[TWITCH][ERROR] Failed to save streamer row for {broadcaster_id}: {type(e).__name__}: {e}", flush=True)
                    msg = await self.get_msg(guild_id, "twitch_err_subscription_failed")
                    await interaction.followup.send(msg, ephemeral=True)
                    return

        try:
            await self._db_call(
                lambda: self.bot.supabase.table("twitch_guild_configs").upsert({
                    "guild_id": str(guild_id), "broadcaster_id": broadcaster_id,
                    "announcement_channel_id": str(channel.id),
                    "member_id": str(member.id) if member else None,
                    "live_role_id": str(role.id) if role else None,
                    "created_by": str(interaction.user.id),
                }, on_conflict="guild_id,broadcaster_id").execute()
            )
        except Exception as e:
            print(f"[TWITCH][ERROR] Failed to save guild config (guild={guild_id}, broadcaster={broadcaster_id}): "
                  f"{type(e).__name__}: {e}", flush=True)
            msg = await self.get_msg(guild_id, "twitch_err_subscription_failed")
            await interaction.followup.send(msg, ephemeral=True)
            return

        if role is not None:
            msg = await self.get_msg(guild_id, "twitch_channel_set_success_with_role",
                                      streamer=streamer, channel=channel.mention, member=member.mention, role=role.name)
        else:
            msg = await self.get_msg(guild_id, "twitch_channel_set_success", streamer=streamer, channel=channel.mention)
        await interaction.followup.send(msg, ephemeral=True)

    async def _confirm_dangerous_role(self, interaction: discord.Interaction, role: discord.Role, dangerous: list[str]) -> bool:
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

    # ══════════════════════════════════════════════════════════
    #  /twitch_channel_remove - 관리자 전용
    # ══════════════════════════════════════════════════════════
    @app_commands.command(name="twitch_channel_remove", description="Stop notifications for a Twitch channel in this server.")
    @app_commands.describe(streamer="Twitch login name to remove")
    @app_commands.checks.has_permissions(administrator=True)
    async def twitch_channel_remove(self, interaction: discord.Interaction, streamer: str):
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        streamer = streamer.strip().lstrip("@").lower()

        try:
            srow = await self._db_call(
                lambda: self.bot.supabase.table("twitch_streamers").select("*").eq("broadcaster_login", streamer).execute()
            )
            streamer_row = srow.data[0] if srow.data else None
        except Exception as e:
            print(f"[TWITCH][ERROR] Failed to look up streamer '{streamer}': {type(e).__name__}: {e}", flush=True)
            await interaction.followup.send("❌ An error occurred.", ephemeral=True)
            return

        if streamer_row is None:
            msg = await self.get_msg(guild_id, "twitch_channel_not_registered", streamer=streamer)
            await interaction.followup.send(msg, ephemeral=True)
            return

        result = await self._remove_streamer_for_guild(guild_id, streamer_row)
        if not result["removed"]:
            msg = await self.get_msg(guild_id, "twitch_channel_not_registered", streamer=streamer)
            await interaction.followup.send(msg, ephemeral=True)
            return

        msg = await self.get_msg(guild_id, "twitch_channel_remove_success", streamer=streamer)
        await interaction.followup.send(msg, ephemeral=True)

    # ══════════════════════════════════════════════════════════
    #  스트리머 삭제 - /twitch_channel_remove 커맨드와 대시보드(내부 웹훅) 둘 다 이 함수 하나를
    #  공유한다. 작업량이 항상 작고 유계(행 삭제 몇 개 + 구독 취소 최대 2개)라 tier-role 정리와
    #  달리 백그라운드로 뺄 필요 없이 그 자리에서 끝내고 정확한 결과를 반환한다.
    # ══════════════════════════════════════════════════════════
    async def _remove_streamer_for_guild(self, guild_id: int, streamer_row: dict) -> dict:
        broadcaster_id = streamer_row["broadcaster_id"]

        try:
            del_res = await self._db_call(
                lambda: self.bot.supabase.table("twitch_guild_configs").delete()
                        .eq("guild_id", str(guild_id)).eq("broadcaster_id", broadcaster_id).execute()
            )
        except Exception as e:
            print(f"[TWITCH][ERROR] Failed to delete guild config (guild={guild_id}, broadcaster={broadcaster_id}): "
                  f"{type(e).__name__}: {e}", flush=True)
            return {"removed": False, "subscriptions_also_removed": False}

        if not del_res.data:
            return {"removed": False, "subscriptions_also_removed": False}

        # 이 스트리머를 참조하는 길드가 더 없으면 구독 자체를 정리 (안 쓰는 구독 방치 방지).
        subscriptions_also_removed = False
        try:
            remaining = await self._db_call(
                lambda: self.bot.supabase.table("twitch_guild_configs").select("guild_id").eq("broadcaster_id", broadcaster_id).execute()
            )
            if not remaining.data:
                async with aiohttp.ClientSession() as session:
                    for sub_id in (streamer_row.get("subscription_id_online"), streamer_row.get("subscription_id_offline")):
                        if sub_id:
                            try:
                                await self._delete_subscription(session, sub_id)
                            except Exception as e:
                                print(f"[TWITCH][WARN] Failed to delete subscription {sub_id}: {type(e).__name__}: {e}", flush=True)
                await self._db_call(
                    lambda: self.bot.supabase.table("twitch_streamers").delete().eq("broadcaster_id", broadcaster_id).execute()
                )
                subscriptions_also_removed = True
        except Exception as e:
            print(f"[TWITCH][ERROR] Failed to clean up unused streamer {broadcaster_id}: {type(e).__name__}: {e}", flush=True)

        return {"removed": True, "subscriptions_also_removed": subscriptions_also_removed}

    async def handle_remove_webhook(self, request: web.Request) -> web.Response:
        secret_header = request.headers.get("X-Internal-Secret", "")
        if not secret_header or not hmac.compare_digest(secret_header, INTERNAL_API_SECRET):
            print("[TWITCH][WARN] Rejected streamer-removal request with invalid/missing internal secret", flush=True)
            return web.Response(status=403)

        try:
            body = await request.json()
        except Exception:
            return web.Response(status=400)

        guild_id = body.get("guild_id")
        broadcaster_id = body.get("broadcaster_id")
        if not guild_id or not broadcaster_id:
            return web.Response(status=400, text="guild_id and broadcaster_id are required")

        try:
            srow = await self._db_call(
                lambda: self.bot.supabase.table("twitch_streamers").select("*").eq("broadcaster_id", broadcaster_id).execute()
            )
            streamer_row = srow.data[0] if srow.data else None
        except Exception as e:
            print(f"[TWITCH][ERROR] Failed to look up streamer {broadcaster_id} for removal: {type(e).__name__}: {e}", flush=True)
            return web.Response(status=500)

        if streamer_row is None:
            return web.Response(status=404, text="streamer not found")

        result = await self._remove_streamer_for_guild(int(guild_id), streamer_row)
        if not result["removed"]:
            return web.Response(status=404, text="not registered for this guild")

        return web.json_response(result)

    # ══════════════════════════════════════════════════════════
    #  전이(claim) 처리 - 원자적 조건부 UPDATE로 중복 웹훅/폴링 레이스를 흡수한다
    #  (giveaway/party에서 쓴 것과 동일한 패턴). 별도 메시지ID dedupe 불필요.
    # ══════════════════════════════════════════════════════════
    async def _claim_transition(self, broadcaster_id: str, going_live: bool, stream_id: str | None) -> dict | None:
        payload = {"is_live": going_live}
        if going_live:
            payload["current_stream_id"] = stream_id
        try:
            res = await self._db_call(
                lambda: self.bot.supabase.table("twitch_streamers").update(payload)
                        .eq("broadcaster_id", broadcaster_id).eq("is_live", not going_live).execute()
            )
        except Exception as e:
            print(f"[TWITCH][ERROR] Failed to claim {'online' if going_live else 'offline'} transition "
                  f"for {broadcaster_id}: {type(e).__name__}: {e}", flush=True)
            return None
        return res.data[0] if res.data else None

    async def _process_stream_online(self, broadcaster_id: str, stream_id: str | None) -> None:
        row = await self._claim_transition(broadcaster_id, going_live=True, stream_id=stream_id)
        if row is None:
            return
        info = await self._fetch_stream_info(broadcaster_id)
        await self._fanout_online(row, info)

    async def _process_stream_offline(self, broadcaster_id: str) -> None:
        row = await self._claim_transition(broadcaster_id, going_live=False, stream_id=None)
        if row is None:
            return
        await self._fanout_offline(row)

    async def _get_guild_configs(self, broadcaster_id: str) -> list[dict]:
        try:
            res = await self._db_call(
                lambda: self.bot.supabase.table("twitch_guild_configs").select("*").eq("broadcaster_id", broadcaster_id).execute()
            )
            return res.data or []
        except Exception as e:
            print(f"[TWITCH][ERROR] Failed to fetch guild configs for {broadcaster_id}: {type(e).__name__}: {e}", flush=True)
            return []

    async def _fanout_online(self, streamer_row: dict, info: dict | None) -> None:
        broadcaster_id = streamer_row["broadcaster_id"]
        login = streamer_row["broadcaster_login"]
        configs = await self._get_guild_configs(broadcaster_id)

        title = (info or {}).get("title") or ""
        game = (info or {}).get("game_name") or ""
        thumbnail = (info or {}).get("thumbnail_url") or ""
        if thumbnail:
            thumbnail = thumbnail.replace("{width}", "640").replace("{height}", "360")
        url = f"https://twitch.tv/{login}"
        game_suffix = f" ({game})" if game else ""

        for cfg in configs:
            guild_id = int(cfg["guild_id"])
            channel = self.bot.get_channel(int(cfg["announcement_channel_id"]))
            if channel is not None:
                try:
                    body = await self.get_msg(guild_id, "twitch_live_announcement_body",
                                               streamer=login, game_suffix=game_suffix, url=url)
                    embed = discord.Embed(title=title or login, url=url, description=body, color=discord.Color.purple())
                    if thumbnail:
                        embed.set_image(url=thumbnail)
                    await channel.send(embed=embed)
                except Exception as e:
                    print(f"[TWITCH][ERROR] Failed to send live announcement (guild={guild_id}, broadcaster={broadcaster_id}): "
                          f"{type(e).__name__}: {e}", flush=True)
            else:
                print(f"[TWITCH][WARN] Announcement channel {cfg['announcement_channel_id']} not in cache "
                      f"(guild={guild_id}, broadcaster={broadcaster_id})", flush=True)

            if cfg.get("live_role_id") and cfg.get("member_id"):
                await self._set_live_role(guild_id, int(cfg["member_id"]), int(cfg["live_role_id"]), grant=True)

    async def _fanout_offline(self, streamer_row: dict) -> None:
        broadcaster_id = streamer_row["broadcaster_id"]
        configs = await self._get_guild_configs(broadcaster_id)
        for cfg in configs:
            if cfg.get("live_role_id") and cfg.get("member_id"):
                await self._set_live_role(int(cfg["guild_id"]), int(cfg["member_id"]), int(cfg["live_role_id"]), grant=False)

    async def _set_live_role(self, guild_id: int, member_id: int, role_id: int, grant: bool) -> None:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            print(f"[TWITCH][WARN] Guild {guild_id} not in cache, cannot {'grant' if grant else 'revoke'} live role", flush=True)
            return
        member = guild.get_member(member_id)
        role = guild.get_role(role_id)
        if member is None or role is None:
            print(f"[TWITCH][WARN] Member {member_id} or role {role_id} not resolvable in guild {guild_id} "
                  f"(cannot {'grant' if grant else 'revoke'} live role)", flush=True)
            return

        # 🛡️ TOCTOU 재확인: 등록 시점 이후 봇 권한/위계가 바뀌었을 수 있다.
        bot_member = guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_roles or bot_member.top_role <= role:
            print(f"[TWITCH][ERROR] Cannot {'grant' if grant else 'revoke'} live role '{role.name}' - "
                  f"permission/hierarchy issue (guild={guild_id})", flush=True)
            return

        # 캐시된 member.roles를 믿고 "이미 상태가 맞으니 스킵"하지 않는다 - 게이트웨이 캐시가
        # stale하면 실제로는 반대 상태인데 호출 자체를 건너뛸 수 있다. add_roles/remove_roles는
        # Discord API 상 멱등적(이미 있는 역할을 또 줘도, 없는 역할을 또 지워도 에러 없음)이라
        # 매번 그냥 호출하는 게 더 안전하다.
        try:
            if grant:
                await member.add_roles(role, reason="[KYVO TWITCH] streamer went live")
            else:
                await member.remove_roles(role, reason="[KYVO TWITCH] streamer went offline")
        except discord.Forbidden:
            print(f"[TWITCH][ERROR] Forbidden while {'granting' if grant else 'revoking'} live role "
                  f"'{role.name}' to user={member_id} (guild={guild_id})", flush=True)
        except discord.HTTPException as e:
            print(f"[TWITCH][ERROR] HTTPException while {'granting' if grant else 'revoking'} live role: "
                  f"{type(e).__name__}: {e} (guild={guild_id})", flush=True)

    # ══════════════════════════════════════════════════════════
    #  Webhook 라우트 - 서명 검증 -> challenge 핸드셰이크 -> notification 처리
    # ══════════════════════════════════════════════════════════
    async def handle_webhook(self, request: web.Request) -> web.Response:
        raw_body = await request.read()
        message_id = request.headers.get("Twitch-Eventsub-Message-Id", "")
        timestamp = request.headers.get("Twitch-Eventsub-Message-Timestamp", "")
        signature = request.headers.get("Twitch-Eventsub-Message-Signature", "")

        expected = "sha256=" + hmac.new(
            TWITCH_EVENTSUB_SECRET.encode(), (message_id + timestamp).encode() + raw_body, hashlib.sha256
        ).hexdigest()
        if not signature or not hmac.compare_digest(expected, signature):
            print(f"[TWITCH][WARN] Webhook signature mismatch, rejecting (message_id={message_id})", flush=True)
            return web.Response(status=403)

        try:
            body = json.loads(raw_body)
        except Exception as e:
            print(f"[TWITCH][ERROR] Failed to parse webhook body: {type(e).__name__}: {e}", flush=True)
            return web.Response(status=400)

        msg_type = request.headers.get("Twitch-Eventsub-Message-Type", "")

        if msg_type == "webhook_callback_verification":
            challenge = body.get("challenge", "")
            return web.Response(status=200, text=challenge, content_type="text/plain")

        if msg_type == "revocation":
            sub = body.get("subscription", {})
            print(f"[TWITCH][CRITICAL] Subscription revoked: id={sub.get('id')} type={sub.get('type')} "
                  f"status={sub.get('status')}", flush=True)
            asyncio.create_task(self._handle_revocation(sub))
            return web.Response(status=200)

        if msg_type == "notification":
            sub = body.get("subscription", {})
            event = body.get("event", {})
            sub_type = sub.get("type")
            broadcaster_id = event.get("broadcaster_user_id")
            if sub_type == "stream.online" and broadcaster_id:
                asyncio.create_task(self._process_stream_online(broadcaster_id, event.get("id")))
            elif sub_type == "stream.offline" and broadcaster_id:
                asyncio.create_task(self._process_stream_offline(broadcaster_id))
            return web.Response(status=200)

        return web.Response(status=200)

    async def _handle_revocation(self, sub: dict) -> None:
        sub_id = sub.get("id")
        sub_type = sub.get("type")
        if not sub_id or sub_type not in ("stream.online", "stream.offline"):
            return

        col = "subscription_id_online" if sub_type == "stream.online" else "subscription_id_offline"
        try:
            res = await self._db_call(
                lambda: self.bot.supabase.table("twitch_streamers").select("*").eq(col, sub_id).execute()
            )
            row = res.data[0] if res.data else None
        except Exception as e:
            print(f"[TWITCH][ERROR] Failed to look up streamer for revoked subscription {sub_id}: {type(e).__name__}: {e}", flush=True)
            return

        if row is None:
            return

        try:
            async with aiohttp.ClientSession() as session:
                new_sub_id = await self._create_subscription(session, sub_type, row["broadcaster_id"])
            await self._db_call(
                lambda: self.bot.supabase.table("twitch_streamers").update({col: new_sub_id}).eq("broadcaster_id", row["broadcaster_id"]).execute()
            )
            print(f"[TWITCH] Re-created revoked {sub_type} subscription for {row['broadcaster_login']}", flush=True)
        except Exception as e:
            print(f"[TWITCH][ERROR] Failed to re-create revoked {sub_type} subscription for "
                  f"{row.get('broadcaster_login')}: {type(e).__name__}: {e}", flush=True)

    # ══════════════════════════════════════════════════════════
    #  5분 주기 폴링 안전망 - 무료 티어 슬립 중 놓친 웹훅을 자가치유, 구독 상태도 같이 확인.
    # ══════════════════════════════════════════════════════════
    @tasks.loop(minutes=TWITCH_POLL_INTERVAL_MINUTES)
    async def reconcile_streams(self):
        try:
            res = await self._db_call(lambda: self.bot.supabase.table("twitch_streamers").select("*").execute())
            streamers = res.data or []
        except Exception as e:
            print(f"[TWITCH][ERROR] Failed to fetch streamers for reconciliation: {type(e).__name__}: {e}", flush=True)
            return

        if streamers:
            await self._reconcile_live_status(streamers)
        await self._reconcile_subscription_health(streamers)

    @reconcile_streams.before_loop
    async def before_reconcile_streams(self):
        await self.bot.wait_until_ready()

    async def _reconcile_live_status(self, streamers: list[dict]) -> None:
        ids_by_broadcaster = {s["broadcaster_id"]: s for s in streamers}
        live_ids: set[str] = set()
        live_info_by_id: dict[str, dict] = {}

        try:
            async with aiohttp.ClientSession() as session:
                id_list = list(ids_by_broadcaster.keys())
                for i in range(0, len(id_list), 100):
                    batch = id_list[i:i + 100]
                    params = [("user_id", bid) for bid in batch]
                    data = await self._helix_request(session, "GET", "/streams", params=params)
                    for s in data.get("data") or []:
                        live_ids.add(s["user_id"])
                        live_info_by_id[s["user_id"]] = s
        except Exception as e:
            print(f"[TWITCH][ERROR] Reconciliation poll failed: {type(e).__name__}: {e}", flush=True)
            return

        now_iso = discord.utils.utcnow().isoformat()
        for broadcaster_id, row in ids_by_broadcaster.items():
            currently_live = broadcaster_id in live_ids
            if currently_live and not row["is_live"]:
                print(f"[TWITCH][WARN] Poll detected missed stream.online for {row['broadcaster_login']} - self-healing", flush=True)
                info = live_info_by_id.get(broadcaster_id)
                claimed = await self._claim_transition(broadcaster_id, going_live=True, stream_id=(info or {}).get("id"))
                if claimed:
                    await self._fanout_online(claimed, info)
            elif not currently_live and row["is_live"]:
                print(f"[TWITCH][WARN] Poll detected missed stream.offline for {row['broadcaster_login']} - self-healing", flush=True)
                claimed = await self._claim_transition(broadcaster_id, going_live=False, stream_id=None)
                if claimed:
                    await self._fanout_offline(claimed)

            try:
                await self._db_call(
                    lambda bid=broadcaster_id: self.bot.supabase.table("twitch_streamers")
                            .update({"last_checked_at": now_iso}).eq("broadcaster_id", bid).execute()
                )
            except Exception as e:
                print(f"[TWITCH][WARN] Failed to update last_checked_at for {broadcaster_id}: {type(e).__name__}: {e}", flush=True)

    async def _reconcile_subscription_health(self, streamers: list[dict]) -> None:
        try:
            async with aiohttp.ClientSession() as session:
                data = await self._helix_request(session, "GET", "/eventsub/subscriptions", params={"status": "enabled"})
                enabled_ids = {s["id"] for s in data.get("data") or []}

                for row in streamers:
                    for sub_type, col in (("stream.online", "subscription_id_online"), ("stream.offline", "subscription_id_offline")):
                        sub_id = row.get(col)
                        if sub_id and sub_id not in enabled_ids:
                            print(f"[TWITCH][WARN] Subscription {sub_id} ({sub_type}) for {row['broadcaster_login']} "
                                  f"is no longer enabled - re-creating", flush=True)
                            try:
                                new_sub_id = await self._create_subscription(session, sub_type, row["broadcaster_id"])
                                await self._db_call(
                                    lambda c=col, nid=new_sub_id, bid=row["broadcaster_id"]:
                                        self.bot.supabase.table("twitch_streamers").update({c: nid}).eq("broadcaster_id", bid).execute()
                                )
                            except Exception as e:
                                print(f"[TWITCH][ERROR] Failed to re-create subscription for {row['broadcaster_login']} "
                                      f"({sub_type}): {type(e).__name__}: {e}", flush=True)
        except Exception as e:
            print(f"[TWITCH][ERROR] Failed to check subscription health: {type(e).__name__}: {e}", flush=True)


async def setup(bot):
    cog = KyvoTwitch(bot)
    await bot.add_cog(cog)
    cog.reconcile_streams.start()
    print("[⚡ TWITCH] Cog extension setup complete.", flush=True)
