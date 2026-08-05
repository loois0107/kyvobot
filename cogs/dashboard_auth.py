import os
import time
import json
import secrets
from urllib.parse import urlencode

import aiohttp
import discord
import jwt
from aiohttp import web

from cogs.base import KyvoBaseCog

# 🛡️ [Fail-Fast] 대시보드 로그인 플로우는 이 다섯 개 없이는 아무 것도 할 수 없다. twitch.py/
# tier_verify.py와 동일한 격리 철학 - 이 코그만 로드 실패하고 다른 코그는 정상 작동한다.
DISCORD_CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET")
DISCORD_OAUTH_REDIRECT_URI = os.environ.get("DISCORD_OAUTH_REDIRECT_URI")
DASHBOARD_JWT_SECRET = os.environ.get("DASHBOARD_JWT_SECRET")
DASHBOARD_BASE_URL = os.environ.get("DASHBOARD_BASE_URL")

for _name, _val in (
    ("DISCORD_CLIENT_ID", DISCORD_CLIENT_ID),
    ("DISCORD_CLIENT_SECRET", DISCORD_CLIENT_SECRET),
    ("DISCORD_OAUTH_REDIRECT_URI", DISCORD_OAUTH_REDIRECT_URI),
    ("DASHBOARD_JWT_SECRET", DASHBOARD_JWT_SECRET),
    ("DASHBOARD_BASE_URL", DASHBOARD_BASE_URL),
):
    if not _val:
        raise RuntimeError(
            f"[DASHBOARD_AUTH] {_name} environment variable is not set. "
            "DISCORD_CLIENT_ID/DISCORD_CLIENT_SECRET come from the Discord Developer Portal "
            "(OAuth2 tab of the bot's application). DISCORD_OAUTH_REDIRECT_URI is the public https "
            "URL that receives the OAuth callback (e.g. https://kyvobot.onrender.com/auth/discord/callback) "
            "and must be registered verbatim in the Developer Portal's redirect list. "
            "DASHBOARD_JWT_SECRET is a secret you generate yourself (used to sign session tokens). "
            "DASHBOARD_BASE_URL is the deployed dashboard's origin."
        )

DISCORD_API_BASE = "https://discord.com/api/v10"
DISCORD_AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
DISCORD_TOKEN_URL = f"{DISCORD_API_BASE}/oauth2/token"
OAUTH_SCOPES = "identify guilds"
JWT_ALGORITHM = "HS256"
STATE_TTL_SECONDS = 300
JWT_TTL_SECONDS = 60 * 60 * 12  # 12시간 - 대시보드 세션 만료
HTTP_TIMEOUT_SECONDS = 8.0

# Discord permission bitfield - MANAGE_GUILD(0x20) 또는 ADMINISTRATOR(0x8)를 가진 길드만
# "관리 가능"으로 취급한다 (대시보드가 설정을 바꿀 수 있는 길드 목록을 필터링하는 기준).
MANAGE_GUILD_PERMISSION_BIT = 0x20
ADMINISTRATOR_PERMISSION_BIT = 0x8

# 대시보드가 채널 선택 드롭다운(로그 채널 등)에 노출할 만한 채널 종류만 필터링.
CHANNEL_TYPE_ALLOWLIST = (
    discord.ChannelType.text,
    discord.ChannelType.news,
    discord.ChannelType.voice,
    discord.ChannelType.stage_voice,
    discord.ChannelType.forum,
    discord.ChannelType.category,
)


def _has_manage_permission(permissions_str: str | None) -> bool:
    if not permissions_str:
        return False
    try:
        bits = int(permissions_str)
    except ValueError:
        return False
    return bool(bits & MANAGE_GUILD_PERMISSION_BIT) or bool(bits & ADMINISTRATOR_PERMISSION_BIT)


def verify_dashboard_token(token: str) -> dict | None:
    """다른 코그가 대시보드發 요청을 보호할 때 쓰는 공용 검증 헬퍼.
    유효하면 payload(dict)를, 만료/위조 등 어떤 이유로든 무효하면 None을 반환한다."""
    try:
        return jwt.decode(token, DASHBOARD_JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None


def _extract_bearer_token(request: web.Request) -> str | None:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    token = header[len("Bearer "):].strip()
    return token or None


# 🛡️ CORS - 대시보드(Vercel)는 이 봇(Render)과 다른 오리진이라, fetch()로 이 API를 호출하려면
# 브라우저가 매 요청(+Authorization 헤더가 있는 non-simple 요청은 사전 OPTIONS preflight까지)에
# 대해 Access-Control-Allow-* 헤더를 요구한다. 성공/실패 응답 전부에 일관되게 붙여야 하므로 - 하나라도
# 빠지면 브라우저가 실제 상태 코드/본문 대신 뭉뚱그린 "CORS 실패"로 프론트엔드에 전달한다.
def _cors_headers() -> dict:
    return {
        "Access-Control-Allow-Origin": DASHBOARD_BASE_URL,
        "Access-Control-Allow-Headers": "Authorization, Content-Type",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Vary": "Origin",
    }


def _json_response(data: dict, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, headers=_cors_headers())


class KyvoDashboardAuth(KyvoBaseCog):
    def __init__(self, bot):
        super().__init__(bot)

    async def cog_load(self):
        self.bot.web_app.router.add_get("/auth/discord/login", self.handle_login)
        self.bot.web_app.router.add_get("/auth/discord/callback", self.handle_callback)
        print("[⚡ DASHBOARD_AUTH] OAuth routes registered at /auth/discord/login and /auth/discord/callback.", flush=True)

        self.bot.web_app.router.add_get("/api/guilds", self.handle_get_guilds)
        self.bot.web_app.router.add_options("/api/guilds", self.handle_preflight)
        self.bot.web_app.router.add_get("/api/guilds/{guild_id}/channels", self.handle_get_guild_channels)
        self.bot.web_app.router.add_options("/api/guilds/{guild_id}/channels", self.handle_preflight)
        print("[⚡ DASHBOARD_AUTH] Dashboard API routes registered at /api/guilds and /api/guilds/{id}/channels.", flush=True)

    async def handle_preflight(self, request: web.Request) -> web.Response:
        return web.Response(status=204, headers=_cors_headers())

    async def _require_auth(self, request: web.Request) -> dict:
        """유효한 Bearer JWT면 payload를 반환하고, 아니면 CORS 헤더가 붙은 401을 던진다
        (헤더가 빠지면 브라우저가 실제 401 본문 대신 뭉뚱그린 CORS 에러로 프론트엔드에 전달한다)."""
        token = _extract_bearer_token(request)
        if not token:
            raise web.HTTPUnauthorized(
                text=json.dumps({"error": "missing_token"}), content_type="application/json", headers=_cors_headers()
            )
        payload = verify_dashboard_token(token)
        if payload is None:
            raise web.HTTPUnauthorized(
                text=json.dumps({"error": "invalid_or_expired_token"}), content_type="application/json", headers=_cors_headers()
            )
        return payload

    # ══════════════════════════════════════════════════════════
    #  GET /api/guilds - 로그인 시 JWT에 스냅샷된 "관리 가능한 길드" 목록을 그대로 반환.
    #  Discord를 재조회하지 않는다 - 최신 상태가 필요하면(길드 이탈/권한 변경 등) 재로그인으로 갱신.
    # ══════════════════════════════════════════════════════════
    async def handle_get_guilds(self, request: web.Request) -> web.Response:
        payload = await self._require_auth(request)
        return _json_response({"guilds": payload.get("guilds", [])})

    # ══════════════════════════════════════════════════════════
    #  GET /api/guilds/{guild_id}/channels - 해당 길드에 대한 관리 권한(JWT의 guilds 목록)을
    #  가진 유저에게만, 봇이 실제로 그 길드에 있을 때만 채널 목록을 반환.
    # ══════════════════════════════════════════════════════════
    async def handle_get_guild_channels(self, request: web.Request) -> web.Response:
        payload = await self._require_auth(request)
        guild_id_str = request.match_info["guild_id"]

        allowed_guild_ids = {g["id"] for g in payload.get("guilds", [])}
        if guild_id_str not in allowed_guild_ids:
            return _json_response({"error": "forbidden"}, status=403)

        try:
            guild_id = int(guild_id_str)
        except ValueError:
            return _json_response({"error": "invalid_guild_id"}, status=400)

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return _json_response({"error": "bot_not_in_guild"}, status=404)

        channels = [
            {
                "id": str(ch.id),
                "name": ch.name,
                "type": str(ch.type),
                "position": ch.position,
                "category_id": str(ch.category_id) if ch.category_id else None,
            }
            for ch in guild.channels
            if ch.type in CHANNEL_TYPE_ALLOWLIST
        ]
        channels.sort(key=lambda c: c["position"])
        return _json_response({"channels": channels})

    # ══════════════════════════════════════════════════════════
    #  /auth/discord/login - Discord 인증 화면으로 리다이렉트.
    #  CSRF state는 Redis에 단기 저장(콜백에서 원자적 delete로 1회성 소모).
    # ══════════════════════════════════════════════════════════
    async def handle_login(self, request: web.Request) -> web.Response:
        state = secrets.token_urlsafe(32)
        try:
            await self.redis.setex(f"dashboard_auth:state:{state}", STATE_TTL_SECONDS, "1")
        except Exception as e:
            print(f"[DASHBOARD_AUTH][ERROR] Failed to store OAuth state: {type(e).__name__}: {e}", flush=True)
            return web.Response(status=503, text="Auth temporarily unavailable.")

        params = {
            "client_id": DISCORD_CLIENT_ID,
            "redirect_uri": DISCORD_OAUTH_REDIRECT_URI,
            "response_type": "code",
            "scope": OAUTH_SCOPES,
            "state": state,
        }
        raise web.HTTPFound(f"{DISCORD_AUTHORIZE_URL}?{urlencode(params)}")

    # ══════════════════════════════════════════════════════════
    #  /auth/discord/callback - code<->token 교환, 유저+길드 조회, JWT 서명 후 대시보드로 리다이렉트.
    #  실패 시에도 500을 던지지 않고 항상 대시보드로 되돌려보낸다(?error=...) - 유저가 벌거벗은
    #  에러 페이지가 아니라 프론트엔드 로그인 화면에서 실패 메시지를 볼 수 있게.
    # ══════════════════════════════════════════════════════════
    async def handle_callback(self, request: web.Request) -> web.Response:
        error = request.query.get("error")
        if error:
            return web.HTTPFound(f"{DASHBOARD_BASE_URL}/login?error={error}")

        code = request.query.get("code")
        state = request.query.get("state")
        if not code or not state:
            return web.HTTPFound(f"{DASHBOARD_BASE_URL}/login?error=missing_code_or_state")

        state_key = f"dashboard_auth:state:{state}"
        try:
            # Redis DEL은 존재했던 키 개수를 반환한다 - "있었으면 지운다"가 원자적이라 재사용(replay) 불가.
            consumed = await self.redis.delete(state_key)
        except Exception as e:
            print(f"[DASHBOARD_AUTH][ERROR] Failed to verify OAuth state: {type(e).__name__}: {e}", flush=True)
            return web.HTTPFound(f"{DASHBOARD_BASE_URL}/login?error=state_check_failed")
        if not consumed:
            return web.HTTPFound(f"{DASHBOARD_BASE_URL}/login?error=invalid_or_expired_state")

        async with aiohttp.ClientSession() as session:
            try:
                token_data = await self._exchange_code(session, code)
                access_token = token_data["access_token"]
            except Exception as e:
                print(f"[DASHBOARD_AUTH][ERROR] Token exchange failed: {type(e).__name__}: {e}", flush=True)
                return web.HTTPFound(f"{DASHBOARD_BASE_URL}/login?error=token_exchange_failed")

            try:
                user = await self._discord_get(session, "/users/@me", access_token)
                guilds = await self._discord_get(session, "/users/@me/guilds", access_token)
            except Exception as e:
                print(f"[DASHBOARD_AUTH][ERROR] Failed to fetch user/guilds: {type(e).__name__}: {e}", flush=True)
                return web.HTTPFound(f"{DASHBOARD_BASE_URL}/login?error=profile_fetch_failed")

        bot_guild_ids = {str(g.id) for g in self.bot.guilds}
        manageable_guilds = [
            {
                "id": g["id"],
                "name": g["name"],
                "icon": g.get("icon"),
                "bot_present": g["id"] in bot_guild_ids,
            }
            for g in guilds
            if _has_manage_permission(g.get("permissions"))
        ]

        now = int(time.time())
        payload = {
            "sub": user["id"],
            "username": user.get("username"),
            "avatar": user.get("avatar"),
            "guilds": manageable_guilds,
            "iat": now,
            "exp": now + JWT_TTL_SECONDS,
        }
        session_token = jwt.encode(payload, DASHBOARD_JWT_SECRET, algorithm=JWT_ALGORITHM)

        return web.HTTPFound(f"{DASHBOARD_BASE_URL}/auth/callback?{urlencode({'token': session_token})}")

    async def _exchange_code(self, session: aiohttp.ClientSession, code: str) -> dict:
        data = {
            "client_id": DISCORD_CLIENT_ID,
            "client_secret": DISCORD_CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": DISCORD_OAUTH_REDIRECT_URI,
        }
        async with session.post(
            DISCORD_TOKEN_URL,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS),
        ) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"Discord token endpoint returned {resp.status}: {text}")
            return json.loads(text)

    async def _discord_get(self, session: aiohttp.ClientSession, path: str, access_token: str):
        async with session.get(
            f"{DISCORD_API_BASE}{path}",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS),
        ) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"Discord API {path} returned {resp.status}: {text}")
            return json.loads(text)


async def setup(bot):
    cog = KyvoDashboardAuth(bot)
    await bot.add_cog(cog)
    print("[⚡ DASHBOARD_AUTH] Cog extension setup complete.", flush=True)
