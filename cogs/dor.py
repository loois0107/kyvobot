"""
DOR(서드파티 로컬 녹화 프로그램) 연동 - "유저 PC의 DOR이 서버에 클립을 올려두면, 유저가
디스코드에서 /dor_list로 보고 골라서 기존 /highlight 파이프라인을 OCR 없이 바로 돌린다"는
구조. cogs/highlight.py의 _run_pipeline()에 추가된 dor_precomputed 분기(해당 커밋 참고)와
짝을 이룬다 - OCR/Riot 매치탐색만 건너뛰고 그 이후(킬 추출~렌더링)는 완전히 동일한 코드를
그대로 탄다.

🛡️ [가정한 테이블 스키마 - 실제 Supabase 콘솔 스키마와 다르면 이 파일의 컬럼명만 고치면 됨]
dor_tokens: id(uuid pk) / guild_id(text) / user_id(text) / token_hash(text) /
            created_at(timestamptz) / expires_at(timestamptz) / revoked_at(timestamptz, null 허용)
dor_candidates: id(uuid pk) / guild_id(text) / user_id(text) / platform_region(text) /
            raw_match_id(bigint) / riot_uuid(text, 참고용만-아래 설명) / champion(text) /
            tag(text) / queue_id(int) / queue_type(text) / win(bool) /
            kills/deaths/assists(int, 매치 최종 KDA - 표시용만) /
            killer_name/victim_name(text) / assisters(jsonb) /
            event_time_sec/rec_offset_sec/clip_duration_sec(double precision) /
            storage_path(text) / original_filename(text) /
            status(text: 'pending'|'processing'|'consumed'|'unprocessable') /
            created_at(timestamptz)

🛡️ [DOR의 riotUuid를 신원 식별에 안 쓰는 이유] 샘플 값("8404e8e4-889a-5105-a717-
6bde79dde2ec")이 표준 UUID 형식인데, 이번 세션 내내 실측된 진짜 Riot PUUID는 78자
안팎의 opaque base64류 문자열(예: "If3ootBX4rpk...")이라 형태가 다르다 - DOR 자체
내부 식별자로 보이고 Riot PUUID가 아니다. 그래서 puuid는 DOR이 준 값을 쓰지 않고,
토큰이 이미 보증하는 (guild_id,user_id) 신원으로 riot_verifications를 다시 조회해서
가져온다 - 토큰 발급 시점에 이미 그 길드에서 인증됐는지 확인했으므로 이중 안전장치이기도
하다. riot_uuid 컬럼 자체는 디버깅 참고용으로만 저장해둔다.

🛡️ [ClipEvents JSON 실제 구조 - 실물 파일 직접 열어서 확인함, 가정 아님] 실제
DOR 세션 폴더(예: E:/DOR/2026-10-09 16_15/) 안의 ClipEvents.json 파일을 직접 열어
확인했다 - 지난번 가정과 다르게
`basicGameData`는 각 클립 엔트리 "안"이 아니라 파일 최상위에 "딱 한 번"만 있고, 그 세션
(매치) 전체 클립들이 공유한다. 최상위 키는 "<클립 파일명>.mp4"들 + "basicGameData"
하나다:
    {"<파일명1>.mp4": {"clipData": {"clipEndTime":..., "eventType":"Assist",
                                     "recOffsetSec":...},
                        "eventData": [{"EventName":"ChampionKill", "EventTime":...,
                                        "KillerName":..., "VictimName":..., "Assisters":[...]}]},
     "<파일명2>.mp4": {...},
     ...
     "basicGameData": {"matchId":8412367111, "riotUuid":"...", "champion":"Lucian",
                        "tag":"DoubleKill", "queueId":400, "queueType":"NORMAL",
                        "win":true, "kills":19, "deaths":5, "assists":14, "clipDuration":30,
                        "partyClip": {...}}}
(eventType이 "Full"/"End"인 클립은 eventData가 빈 배열일 수 있음 - 실제 파일에서도
확인됨, 이런 건 업로드 자체는 받되 status='unprocessable'로 저장한다.)

이 구조 때문에 DOR 로컬 업로더가 "지금 막 생긴 클립 하나"를 올릴 때 파일 전체가 아니라
그 클립의 {clipData, eventData} 조각 + 세션 공용 basicGameData를 따로 추출해서 보내는
쪽이 서버 구현을 단순하게 만든다고 판단해, 멀티파트 필드를 "clip_events"(그 클립의
{clipData, eventData}만)와 "basic_game_data"(세션 공용 객체 그대로)로 나눠 받는다 -
아래 handle_upload_webhook 참고. (업로더 쪽에서 ClipEvents.json을 열어 해당 파일명
키와 "basicGameData" 키 두 개를 그대로 떼어 보내면 된다.)
"""
import asyncio
import hashlib
import json
import os
import secrets
import shutil
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

import aiohttp
import discord
from aiohttp import web
from discord import app_commands
from discord.ext import tasks

from cogs.base import KyvoBaseCog
import cogs.highlight as highlight_mod
import cogs.tier_verify as tier_verify_mod

INTERNAL_API_SECRET = os.environ.get("INTERNAL_API_SECRET")  # 참고로만 import - DOR 업로드
# 자체는 이 공유 시크릿을 안 쓴다(유저 PC에 배포되는 프로그램에 공유 시크릿을 심으면 그대로
# 노출되는 문제 때문에 이번에 따로 설계함). 다른 /internal/* 라우트들과 달리 아래
# handle_upload_webhook은 INTERNAL_API_SECRET 유무와 무관하게 항상 등록된다.

DOR_STORAGE_BUCKET = "dor-clips"
# 🛡️ [토큰 만료 기간 - 가정값] "폐기(revoked_at)"는 사용자 요청에 명시돼 있었지만 "만료"
# 기간 자체는 명시된 적이 없어서, 장기 사용 기기 토큰에 흔한 기본값(90일)을 가정했다 -
# 실제 운영 정책에 맞게 바꿔도 이 상수 하나만 고치면 된다.
DOR_TOKEN_EXPIRY_DAYS = 90
DOR_CANDIDATE_EXPIRY_DAYS = 3
DOR_CLEANUP_INTERVAL_SECONDS = 3600  # 1시간마다
DOR_LIST_MAX_OPTIONS = 25  # Discord Select 옵션 상한(API 레벨 제약)


def _generate_dor_token() -> str:
    return secrets.token_urlsafe(32)


def _hash_dor_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class _DorVideoAdapter:
    """_run_pipeline()이 기대하는 discord.Attachment의 ".save(path)" 인터페이스만 흉내
    낸다 - DOR 경로는 Storage에서 이미 바이트를 받아와 있어서 진짜 디스코드 첨부파일이
    아니다. _run_pipeline 본문은 video.save(video_path) 한 줄만 호출하므로 이것만
    있으면 충분하다(이번 세션 내내 실제 파이프라인 테스트에 썼던 FakeAttachment와 동일한
    발상)."""

    def __init__(self, data: bytes):
        self._data = data

    async def save(self, dest_path: str) -> None:
        with open(dest_path, "wb") as f:
            f.write(self._data)


class DorCandidateSelectView(discord.ui.View):
    """cogs/party.py의 PositionSelectView와 완전히 동일한 패턴 - 그 순간의 pending 후보
    목록만 담아 매번 새로 만드는 일회성 Select다. 절대 재사용하면 안 된다(재사용하면
    먼저 만든 인스턴스의 후보 목록이 나중 호출에도 남는다)."""

    def __init__(self, cog: "KyvoDor", author_id: int, candidates: list[dict]):
        super().__init__(timeout=120)
        self.cog = cog
        self.author_id = author_id

        options = []
        for c in candidates:
            created = (c.get("created_at") or "")[:16].replace("T", " ")
            tag = c.get("tag") or "Kill"
            champ = c.get("champion") or "?"
            label = f"{champ} · {tag} · {created}"[:100]
            options.append(discord.SelectOption(label=label, value=c["id"]))

        select = discord.ui.Select(placeholder="Choose a clip to turn into a highlight...", options=options)
        select.callback = self._on_select
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ This selection is not for you.", ephemeral=True)
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction):
        select: discord.ui.Select = self.children[0]
        candidate_id = select.values[0]
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="⏳ Processing your selection...", view=self)
        self.stop()
        await self.cog._process_selected_candidate(interaction, candidate_id)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


class KyvoDor(KyvoBaseCog):
    def __init__(self, bot):
        super().__init__(bot)
        self.cleanup_expired_candidates.start()

    async def cog_load(self):
        # 🛡️ [공유 시크릿 아님] 다른 7개 코그의 /internal/* 라우트와 달리 INTERNAL_API_SECRET
        # 유무와 무관하게 항상 등록한다 - 인증은 라우트 안에서 유저별 토큰으로 한다.
        self.bot.web_app.router.add_post("/internal/dor/upload", self.handle_upload_webhook)
        print("[⚡ DOR] Internal upload route registered at /internal/dor/upload.", flush=True)

    async def _db_call(self, fn):
        """다른 12개 코그와 동일한 관례 - KyvoBaseCog가 아니라 각 코그가 자기 복사본을 든다."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.bot.db_executor, fn)

    def _highlight_cog(self):
        return self.bot.get_cog("KyvoHighlight")

    def _tier_verify_cog_instance(self):
        return self.bot.get_cog("KyvoTierVerify")

    # ══════════════════════════════════════════════════════════
    #  Supabase Storage (동기 클라이언트라 executor로 격리 - _to_executor와 동일 관례)
    # ══════════════════════════════════════════════════════════
    def _to_executor(self, fn, *args):
        loop = asyncio.get_running_loop()
        return loop.run_in_executor(None, fn, *args)

    def _storage_upload(self, path: str, data: bytes) -> None:
        self.bot.supabase.storage.from_(DOR_STORAGE_BUCKET).upload(
            path, data, {"content-type": "video/mp4"}
        )

    def _storage_download(self, path: str) -> bytes:
        return self.bot.supabase.storage.from_(DOR_STORAGE_BUCKET).download(path)

    def _storage_remove(self, paths: list[str]) -> None:
        self.bot.supabase.storage.from_(DOR_STORAGE_BUCKET).remove(paths)

    # ══════════════════════════════════════════════════════════
    #  /dor_token
    # ══════════════════════════════════════════════════════════
    @app_commands.command(name="dor_token", description="Generate a personal upload token for the DOR desktop companion app.")
    async def dor_token(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        user_id = interaction.user.id

        try:
            res = await self._db_call(
                lambda: self.bot.supabase.table("riot_verifications").select("puuid")
                        .eq("guild_id", str(guild_id)).eq("user_id", str(user_id)).execute()
            )
            verified = bool(res.data)
        except Exception as e:
            print(f"[DOR][ERROR] Failed to check verification (guild={guild_id}, user={user_id}): "
                  f"{type(e).__name__}: {e}", flush=True)
            await interaction.followup.send("❌ Something went wrong. Please try again.", ephemeral=True)
            return
        if not verified:
            await interaction.followup.send(
                "❌ You need to verify your League account first - run `/tier_verify`.", ephemeral=True
            )
            return

        now = datetime.now(timezone.utc)
        # 🛡️ [기존 활성 토큰 폐기] 유저가 이미 토큰을 갖고 있으면 revoked_at을 찍어 즉시
        # 무효화하고 새로 하나만 발급 - 한 유저당 "활성 토큰은 최대 1개"를 보장한다.
        try:
            await self._db_call(
                lambda: self.bot.supabase.table("dor_tokens")
                        .update({"revoked_at": now.isoformat()})
                        .eq("guild_id", str(guild_id)).eq("user_id", str(user_id))
                        .is_("revoked_at", "null")
                        .execute()
            )
        except Exception as e:
            print(f"[DOR][ERROR] Failed to revoke old token (guild={guild_id}, user={user_id}): "
                  f"{type(e).__name__}: {e}", flush=True)
            await interaction.followup.send("❌ Something went wrong. Please try again.", ephemeral=True)
            return

        raw_token = _generate_dor_token()
        token_hash = _hash_dor_token(raw_token)
        expires_at = now + timedelta(days=DOR_TOKEN_EXPIRY_DAYS)
        try:
            await self._db_call(
                lambda: self.bot.supabase.table("dor_tokens").insert({
                    "guild_id": str(guild_id), "user_id": str(user_id),
                    "token_hash": token_hash, "created_at": now.isoformat(),
                    "expires_at": expires_at.isoformat(), "revoked_at": None,
                }).execute()
            )
        except Exception as e:
            print(f"[DOR][ERROR] Failed to store new token (guild={guild_id}, user={user_id}): "
                  f"{type(e).__name__}: {e}", flush=True)
            await interaction.followup.send("❌ Something went wrong. Please try again.", ephemeral=True)
            return

        # 🛡️ [원문은 여기서 딱 한 번만] 해시만 DB에 남고, 평문 토큰은 이 응답 이후로는
        # 서버 어디에도 안 남는다 - ephemeral이라 유저 본인한테만 보이고, 재발급하면
        # 이 메시지를 다시 봐도 이미 지난 토큰이라 의미 없다.
        await interaction.followup.send(
            "🔑 **Your DOR upload token (shown only once - copy it now):**\n"
            f"```\n{raw_token}\n```\n"
            "Paste this into the DOR companion app's settings. Running `/dor_token` again will "
            "immediately invalidate this token and issue a new one.",
            ephemeral=True,
        )

    # ══════════════════════════════════════════════════════════
    #  /internal/dor/upload - DOR 로컬 프로그램이 호출
    # ══════════════════════════════════════════════════════════
    async def handle_upload_webhook(self, request: web.Request) -> web.Response:
        auth_header = request.headers.get("Authorization", "")
        raw_token = auth_header[7:] if auth_header.lower().startswith("bearer ") else auth_header
        if not raw_token:
            return web.Response(status=401, text="missing token")
        token_hash = _hash_dor_token(raw_token)

        try:
            res = await self._db_call(
                lambda: self.bot.supabase.table("dor_tokens").select("*")
                        .eq("token_hash", token_hash).execute()
            )
            rows = res.data or []
        except Exception as e:
            print(f"[DOR][ERROR] Token lookup failed: {type(e).__name__}: {e}", flush=True)
            return web.Response(status=500)
        if not rows:
            print("[DOR][WARN] Rejected upload with unknown token", flush=True)
            return web.Response(status=401, text="invalid token")

        token_row = rows[0]
        now = datetime.now(timezone.utc)
        if token_row.get("revoked_at"):
            print(f"[DOR][WARN] Rejected upload with revoked token (guild={token_row['guild_id']}, "
                  f"user={token_row['user_id']})", flush=True)
            return web.Response(status=401, text="token revoked")
        expires_at_str = token_row.get("expires_at")
        if expires_at_str and datetime.fromisoformat(expires_at_str) < now:
            print(f"[DOR][WARN] Rejected upload with expired token (guild={token_row['guild_id']}, "
                  f"user={token_row['user_id']})", flush=True)
            return web.Response(status=401, text="token expired")

        guild_id = token_row["guild_id"]
        user_id = token_row["user_id"]

        tv_cog = self._tier_verify_cog_instance()
        platform_region = await tv_cog._get_platform_region(int(guild_id)) if tv_cog else None
        if not platform_region:
            return web.Response(status=422, text="guild has no platform_region set")

        try:
            reader = await request.multipart()
        except Exception as e:
            return web.Response(status=400, text=f"invalid multipart body: {e}")

        # 🛡️ [세 필드로 분리 - 실제 ClipEvents.json 구조 확인 후 수정] basicGameData는
        # 파일 최상위에 "세션 전체 공용"으로 딱 한 번만 있고(실측 확인됨), 각 클립 엔트리
        # 안에는 없다 - 그래서 "이 클립 하나의 {clipData,eventData}"와 "세션 공용
        # basicGameData"를 업로더가 미리 떼어서 각각 보내게 한다(서버가 전체 파일+파일명을
        # 받아서 다시 골라내는 것보다 단순함).
        video_bytes = None
        video_filename = None
        clip_events = None  # {"clipData": {...}, "eventData": [...]} - 이 클립 하나분
        basic = None  # basicGameData 그대로 - 세션(매치) 공용
        async for part in reader:
            if part.name == "video":
                video_filename = part.filename or f"{uuid.uuid4()}.mp4"
                video_bytes = await part.read(decode=False)
            elif part.name == "clip_events":
                raw = await part.read(decode=False)
                try:
                    clip_events = json.loads(raw.decode("utf-8"))
                except Exception:
                    return web.Response(status=400, text="clip_events is not valid JSON")
            elif part.name == "basic_game_data":
                raw = await part.read(decode=False)
                try:
                    basic = json.loads(raw.decode("utf-8"))
                except Exception:
                    return web.Response(status=400, text="basic_game_data is not valid JSON")
        if video_bytes is None or clip_events is None or basic is None:
            return web.Response(
                status=400, text="missing 'video', 'clip_events', or 'basic_game_data' multipart field")

        clip_data = clip_events.get("clipData") or {}
        event_list = clip_events.get("eventData") or []
        event = event_list[0] if event_list else None
        status = "pending" if event else "unprocessable"
        if event is None:
            print(f"[DOR][INFO] Candidate has no eventData (eventType={clip_data.get('eventType')}) - "
                  f"storing as unprocessable (guild={guild_id}, user={user_id})", flush=True)

        storage_path = f"{guild_id}/{user_id}/{uuid.uuid4()}.mp4"
        try:
            await self._to_executor(self._storage_upload, storage_path, video_bytes)
        except Exception as e:
            print(f"[DOR][ERROR] Storage upload failed (guild={guild_id}, user={user_id}): "
                  f"{type(e).__name__}: {e}", flush=True)
            return web.Response(status=500, text="storage upload failed")

        row = {
            "guild_id": guild_id, "user_id": user_id,
            "platform_region": platform_region,
            "raw_match_id": basic.get("matchId"),
            "riot_uuid": basic.get("riotUuid"),
            "champion": basic.get("champion"),
            "tag": basic.get("tag"),
            "queue_id": basic.get("queueId"),
            "queue_type": basic.get("queueType"),
            "win": basic.get("win"),
            "kills": basic.get("kills"), "deaths": basic.get("deaths"), "assists": basic.get("assists"),
            "killer_name": event.get("KillerName") if event else None,
            "victim_name": event.get("VictimName") if event else None,
            "assisters": event.get("Assisters") if event else None,
            "event_time_sec": event.get("EventTime") if event else None,
            "rec_offset_sec": clip_data.get("recOffsetSec"),
            "clip_duration_sec": basic.get("clipDuration"),
            "storage_path": storage_path,
            "original_filename": video_filename,
            "status": status,
            "created_at": now.isoformat(),
        }
        try:
            insert_res = await self._db_call(lambda: self.bot.supabase.table("dor_candidates").insert(row).execute())
        except Exception as e:
            print(f"[DOR][ERROR] Candidate insert failed (guild={guild_id}, user={user_id}): "
                  f"{type(e).__name__}: {e}", flush=True)
            # DB 기록이 실패했으면 방금 올린 Storage 파일도 고아가 되므로 같이 치운다.
            try:
                await self._to_executor(self._storage_remove, [storage_path])
            except Exception:
                pass
            return web.Response(status=500, text="db insert failed")

        candidate_id = insert_res.data[0]["id"] if insert_res.data else None
        print(f"[DOR][INFO] Candidate stored: id={candidate_id} status={status} "
              f"(guild={guild_id}, user={user_id})", flush=True)
        return web.Response(status=200, text=json.dumps({"status": "ok", "candidate_id": candidate_id}))

    # ══════════════════════════════════════════════════════════
    #  /dor_list
    # ══════════════════════════════════════════════════════════
    @app_commands.command(name="dor_list", description="List your pending DOR clips and turn one into a highlight.")
    async def dor_list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        user_id = interaction.user.id

        try:
            res = await self._db_call(
                lambda: self.bot.supabase.table("dor_candidates").select("*")
                        .eq("guild_id", str(guild_id)).eq("user_id", str(user_id))
                        .eq("status", "pending").order("created_at", desc=True)
                        .limit(DOR_LIST_MAX_OPTIONS).execute()
            )
            candidates = res.data or []
        except Exception as e:
            print(f"[DOR][ERROR] Failed to list candidates (guild={guild_id}, user={user_id}): "
                  f"{type(e).__name__}: {e}", flush=True)
            await interaction.followup.send("❌ Something went wrong. Please try again.", ephemeral=True)
            return

        if not candidates:
            await interaction.followup.send("You have no pending DOR clips right now.", ephemeral=True)
            return

        view = DorCandidateSelectView(self, interaction.user.id, candidates)
        await interaction.followup.send("Select a clip to turn into a highlight:", view=view, ephemeral=True)

    # ══════════════════════════════════════════════════════════
    #  선택 콜백 - 일일 한도 체크부터 _run_pipeline 합류까지
    # ══════════════════════════════════════════════════════════
    async def _process_selected_candidate(self, interaction: discord.Interaction, candidate_id: str):
        guild_id = interaction.guild_id
        user_id = interaction.user.id
        highlight_cog = self._highlight_cog()
        tv_cog = self._tier_verify_cog_instance()
        if highlight_cog is None or tv_cog is None:
            await interaction.followup.send("❌ Something went wrong. Please try again.", ephemeral=True)
            return

        # 1) 일일 한도 - /highlight와 동일한 지점(처리 시작 직전), 동일한 Redis 키 네임스페이스
        #    공유(같은 하루 한도를 같이 쓴다 - DOR로 만들었다고 별도 한도가 생기지 않음).
        guild_daily_key = f"highlight_daily:guild:{guild_id}"
        user_daily_key = f"highlight_daily:user:{guild_id}:{user_id}"
        if not await highlight_cog._check_daily_limit(guild_daily_key, highlight_mod.HIGHLIGHT_DAILY_LIMIT_GUILD):
            await interaction.followup.send(
                f"❌ This server's daily highlight limit ({highlight_mod.HIGHLIGHT_DAILY_LIMIT_GUILD}) has been reached.",
                ephemeral=True,
            )
            return
        if not await highlight_cog._check_daily_limit(user_daily_key, highlight_mod.HIGHLIGHT_DAILY_LIMIT_USER):
            await interaction.followup.send(
                f"❌ Your daily highlight limit ({highlight_mod.HIGHLIGHT_DAILY_LIMIT_USER}) has been reached.",
                ephemeral=True,
            )
            return

        # 2) candidate 원자적 선점(pending -> processing) - giveaway.py의 claim 패턴과 동일한
        #    정신(status='pending' 조건이 걸린 UPDATE라 동시에 두 번 눌러도 하나만 진행됨).
        try:
            claim_res = await self._db_call(
                lambda: self.bot.supabase.table("dor_candidates")
                        .update({"status": "processing"})
                        .eq("id", candidate_id).eq("status", "pending").execute()
            )
        except Exception as e:
            print(f"[DOR][ERROR] Failed to claim candidate {candidate_id}: {type(e).__name__}: {e}", flush=True)
            await interaction.followup.send("❌ Something went wrong. Please try again.", ephemeral=True)
            return
        if not claim_res.data:
            await interaction.followup.send(
                "❌ This clip was already processed (or picked by someone else) - run `/dor_list` again.",
                ephemeral=True,
            )
            return
        candidate = claim_res.data[0]

        async def fail(reason_msg: str):
            await self._mark_unprocessable(candidate_id)
            await interaction.followup.send(reason_msg, ephemeral=True)

        guild_id_str = candidate["guild_id"]
        user_id_str = candidate["user_id"]
        platform_region = candidate["platform_region"]
        regional_route = tier_verify_mod.PLATFORM_TO_REGIONAL.get(platform_region)
        if not regional_route:
            await fail("❌ Could not resolve this clip's region.")
            return

        # 3) puuid는 DOR의 riotUuid가 아니라 (guild_id,user_id) 신원으로 riot_verifications를
        #    다시 조회해서 가져온다 - 파일 상단 설명 참고.
        try:
            puuid_res = await self._db_call(
                lambda: self.bot.supabase.table("riot_verifications").select("puuid")
                        .eq("guild_id", guild_id_str).eq("user_id", user_id_str).execute()
            )
            puuid_rows = puuid_res.data or []
        except Exception as e:
            print(f"[DOR][ERROR] puuid lookup failed (candidate={candidate_id}): {type(e).__name__}: {e}", flush=True)
            await interaction.followup.send("❌ Something went wrong. Please try again.", ephemeral=True)
            return
        if not puuid_rows:
            await fail("❌ Your Riot verification could not be found - run `/tier_verify` again.")
            return
        puuid = puuid_rows[0]["puuid"]

        # 4) 영상 다운로드
        try:
            video_bytes = await self._to_executor(self._storage_download, candidate["storage_path"])
        except Exception as e:
            print(f"[DOR][ERROR] Storage download failed (candidate={candidate_id}): {type(e).__name__}: {e}", flush=True)
            await interaction.followup.send("❌ Could not retrieve this clip's video file.", ephemeral=True)
            return

        # 5) Riot API 직접 조회 - OCR 없이 matchId를 바로 조합
        match_id = f"{platform_region.upper()}_{candidate['raw_match_id']}"
        try:
            async with aiohttp.ClientSession() as session:
                chosen = await highlight_cog._riot_get(
                    tv_cog, session, f"https://{regional_route}.api.riotgames.com/lol/match/v5/matches/{match_id}")
                timeline = await highlight_cog._riot_get(
                    tv_cog, session,
                    f"https://{regional_route}.api.riotgames.com/lol/match/v5/matches/{match_id}/timeline")
        except Exception as e:
            print(f"[DOR][WARN] Riot API fetch failed for {match_id} (candidate={candidate_id}): "
                  f"{type(e).__name__}: {e}", flush=True)
            await fail("❌ Could not fetch this match from Riot's servers.")
            return

        # 6) 킬 이벤트 매칭 - KillerName/VictimName(챔피언 이름)으로 후보를 좁히고, 같은
        #    조합이 여러 번 있으면 recOffsetSec 기반 근사 game_ms에 가장 가까운 걸 고른다.
        #    🛡️ [근사 game_ms 판단 근거] DOR의 EventTime/recOffsetSec는 둘 다 "녹화 세션
        #    경과 초"로 보인다(실측: EventTime-recOffsetSec가 clipDuration과 거의 일치) -
        #    DOR이 매치(게임) 시작과 거의 동시에 녹화를 시작한다고 가정하면, EventTime(초)을
        #    그대로 game_ms 근사치로 쓸 수 있다(EventTime*1000). 동명이인 킬(같은 챔피언
        #    조합이 그 매치에서 두 번 이상 난 경우)이 있을 때 이 근사치로 가장 가까운 실제
        #    타임라인 이벤트를 고르는 동률 해소 용도로만 쓰고, mapping 자체의 절편은 아래
        #    실제 타임라인 timestamp_ms로 다시 정확하게 계산한다 - 이 근사치가 틀려도
        #    "여러 후보 중 어느 걸 고르느냐"에만 영향이 있을 뿐 최종 mapping 정밀도에는
        #    영향이 없다.
        kills = highlight_mod._extract_champion_kills(timeline)
        names = highlight_mod._participant_id_to_name(chosen)
        champ_to_pid = {info["champion"]: pid for pid, info in names.items()}
        killer_pid = champ_to_pid.get(candidate["killer_name"])
        victim_pid = champ_to_pid.get(candidate["victim_name"])
        if killer_pid is None or victim_pid is None:
            await fail("❌ Could not match this clip's champions to the match data.")
            return

        matching_kills = [k for k in kills if k["killer_id"] == killer_pid and k["victim_id"] == victim_pid]
        if not matching_kills:
            await fail("❌ Could not find a matching kill in this match's timeline.")
            return

        event_time_sec = candidate["event_time_sec"]
        rec_offset_sec = candidate["rec_offset_sec"]
        approx_game_ms = event_time_sec * 1000.0
        best_kill = min(matching_kills, key=lambda k: abs(k["timestamp_ms"] - approx_game_ms))
        game_ms_of_kill = best_kill["timestamp_ms"]

        # 7) mapping 구성 - _fit_linear_mapping()이 기대하는 포맷은 game_ms = slope*clip_t+intercept
        #    (slope 단위: ms game-time / sec clip-time, 기대값 1000.0 = 실시간 재생). DOR
        #    원본 녹화본은 리플레이 뷰어처럼 배속이 바뀔 일이 없으므로(실제 게임 화면을
        #    그대로 긁는 캡처 도구), 기존 OCR 여러 샘플 최소자승 회귀 대신 이 상수를 그대로
        #    쓰고, 점 하나(clip_t_of_kill, game_ms_of_kill)로 절편만 역산한다.
        clip_t_of_kill = event_time_sec - rec_offset_sec
        slope = highlight_mod.EXPECTED_CLOCK_SLOPE_MS_PER_SEC
        intercept = game_ms_of_kill - slope * clip_t_of_kill
        mapping = (slope, intercept)

        # 8) 기존 _run_pipeline()의 dor_precomputed 분기로 합류
        work_dir = tempfile.mkdtemp(prefix="kyvo_dor_")
        try:
            adapter = _DorVideoAdapter(video_bytes)
            progress_msg = await interaction.followup.send("⏳ Building your highlight...", ephemeral=True, wait=True)
            async with highlight_cog.render_semaphore:
                await highlight_cog._run_pipeline(
                    interaction, guild_id, adapter, work_dir, progress_msg, tv_cog, regional_route, puuid,
                    style_override=None,
                    dor_precomputed={"mapping": mapping, "chosen": chosen, "timeline": timeline},
                )
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

        try:
            await self._db_call(
                lambda: self.bot.supabase.table("dor_candidates").update({"status": "consumed"})
                        .eq("id", candidate_id).execute()
            )
        except Exception as e:
            print(f"[DOR][ERROR] Failed to mark candidate consumed (candidate={candidate_id}): "
                  f"{type(e).__name__}: {e}", flush=True)

    async def _mark_unprocessable(self, candidate_id: str):
        try:
            await self._db_call(
                lambda: self.bot.supabase.table("dor_candidates").update({"status": "unprocessable"})
                        .eq("id", candidate_id).execute()
            )
        except Exception as e:
            print(f"[DOR][ERROR] Failed to mark candidate {candidate_id} unprocessable: "
                  f"{type(e).__name__}: {e}", flush=True)

    # ══════════════════════════════════════════════════════════
    #  3일 지난 pending/unprocessable 후보 정리 - giveaway.py의 @tasks.loop 패턴 재사용
    # ══════════════════════════════════════════════════════════
    @tasks.loop(seconds=DOR_CLEANUP_INTERVAL_SECONDS)
    async def cleanup_expired_candidates(self):
        cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=DOR_CANDIDATE_EXPIRY_DAYS)).isoformat()
        try:
            res = await self._db_call(
                lambda: self.bot.supabase.table("dor_candidates").select("id, storage_path")
                        .in_("status", ["pending", "unprocessable"])
                        .lt("created_at", cutoff_iso).execute()
            )
            rows = res.data or []
        except Exception as e:
            print(f"[DOR][ERROR] Cleanup query failed: {type(e).__name__}: {e}", flush=True)
            return
        if not rows:
            return

        storage_paths = [r["storage_path"] for r in rows if r.get("storage_path")]
        if storage_paths:
            try:
                await self._to_executor(self._storage_remove, storage_paths)
            except Exception as e:
                print(f"[DOR][ERROR] Cleanup storage remove failed: {type(e).__name__}: {e}", flush=True)
                # Storage 삭제가 실패해도 DB 행 정리는 계속 진행한다(고아 파일보단 고아 행이
                # 덜 위험하다고 판단 - 파일은 다음 사이클에 다시 안 지워질 수 있지만 재시도
                # 로직은 이후 라운드 과제로 남긴다).

        ids = [r["id"] for r in rows]
        try:
            await self._db_call(lambda: self.bot.supabase.table("dor_candidates").delete().in_("id", ids).execute())
        except Exception as e:
            print(f"[DOR][ERROR] Cleanup delete failed: {type(e).__name__}: {e}", flush=True)
            return
        print(f"[DOR][INFO] Cleaned up {len(ids)} expired candidate(s) ({len(storage_paths)} storage file(s)).", flush=True)

    @cleanup_expired_candidates.before_loop
    async def before_cleanup_expired_candidates(self):
        await self.bot.wait_until_ready()
        await asyncio.sleep(30)


async def setup(bot):
    await bot.add_cog(KyvoDor(bot))
