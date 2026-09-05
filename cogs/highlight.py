"""/highlight - 게임 하이라이트 영상에 AI가 실제 매치 사실(Match-v5) 기반 해설 자막+효과음을
얹어 새 영상으로 만든다. 로컬 프로토타입에서 검증된 4개 조각(영상합성/Match-v5/시계OCR/톤생성)을
그대로 실서비스 코드로 옮긴 것.

🛡️ [Fail-Fast] tier_verify.py와 동일한 철학 - OPENAI_API_KEY 없이는 아무 것도 할 수 없으므로
로드 시점에 즉시 예외를 던져 main.py의 per-extension try/except에 걸리게 한다. RIOT_API_KEY는
cogs.tier_verify를 import하는 순간 그쪽에서 이미 검증되므로 여기서 중복 체크하지 않는다.
"""
import asyncio
import datetime
import glob
import os
import random
import re
import shutil
import subprocess
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor

import aiohttp
import discord
from discord import app_commands
import imageio_ffmpeg
from openai import AsyncOpenAI
from PIL import Image

from cogs.base import KyvoBaseCog
from cogs.tier_verify import (
    PLATFORM_TO_REGIONAL,
    RiotAPIError, RiotAuthError, RiotNotFoundError, RiotRateLimitedError, RiotServerError, RiotTimeoutError,
)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError(
        "[HIGHLIGHT] OPENAI_API_KEY environment variable is not set. "
        "Set it before starting the bot (used for clock OCR + commentary generation)."
    )

# 🛡️ Render 무료 플랜은 디스크가 없어 이 기능이 원천적으로 작동 불가능 - 유료 전환 전까지는
# 기본 비활성. env var가 없으면 setup()에서 add_cog() 자체를 건너뛰어 명령어가 아예 등록되지 않는다.
HIGHLIGHT_FEATURE_ENABLED = os.environ.get("HIGHLIGHT_FEATURE_ENABLED", "").strip().lower() in ("1", "true", "yes")

# 🛡️ db_executor(main.py:27)는 가벼운 Supabase 호출 전용 - ffmpeg 인코딩/OpenAI Vision 같은
# 무거운 블로킹 작업을 거기 섞으면 다른 10개 코그의 DB 응답이 전부 지연된다. 완전히 분리된
# 작은 풀 + 세마포어로 동시 처리량 자체를 인스턴스 사양에 맞게 제한한다.
HIGHLIGHT_MAX_WORKERS = int(os.environ.get("HIGHLIGHT_MAX_WORKERS", "2"))
HIGHLIGHT_MAX_CONCURRENT = int(os.environ.get("HIGHLIGHT_MAX_CONCURRENT", "1"))

FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONT_PATH_RAW = os.path.join(REPO_ROOT, "FontKR.otf")
FONT_PATH = FONT_PATH_RAW.replace("\\", "/").replace(":", "\\:")  # ffmpeg 필터 문법 콜론 이스케이프

SFX_DIR = os.path.join(REPO_ROOT, "assets", "highlight_sfx")
SFX_POOL = sorted(glob.glob(os.path.join(SFX_DIR, "crowd_cheer_*.wav")))

# 대부분의 효과음은 "킬 시점 = 파일 시작(즉시 폭발)"이라 리드타임이 0이다. crowd_cheer_2.wav만
# 예외 - 조용히 고조되다 마지막에 훅 터지는 구조라, "터짐이 완성된 시점"이 킬 시점에 오도록
# 앞에서부터 재생해야 한다. crowd_cheer_2.wav의 엔벨로프 설계는 0~1.5s 조용함 → 1.5~4.5s 완만한
# 고조 → 4.5~6.0s 큰 도약(훅 터짐) → 6.0s~ 정점 유지. 도약이 "끝나는" 6.0초 지점이 킬 시점에
# 오도록 리드타임=6.0초로 잡는다(도약이 "시작"하는 4.5초를 쓰면 킬 순간엔 아직 다 안 터진 상태가
# 됨 - 처음엔 4.5초로 했다가 실측으로 이 문제를 발견해서 6.0초로 수정함). 에셋을 다시 다듬으면
# 이 값도 같이 조정해야 한다.
SFX_LEAD_MS = {"crowd_cheer_2.wav": 6000}

# 화면 우측 상단 시계 영역 비율 크롭 박스 (프로토타입에서 1920x804 캡처 기준 보정).
# 다른 해상도/HUD 배치에서는 부정확할 수 있음 - 알려진 한계.
CLOCK_CROP_RATIO = (0.965, 0.0, 1.0, 0.028)

# 🛡️ [Sanity check] 크롭이 시계를 벗어나 골드/KDA 같은 다른 UI 숫자를 읽어도, 그 값들이
# 우연히 clip_t와 그럴듯하게 상관돼 보이면 최소자승 회귀 자체는 아무 에러 없이 성공해버려서
# 조용히 틀린 매핑을 쓰게 된다. 게임 시계는 항상 실시간 1배속(1초당 게임시간 1000ms)으로
# 흐른다는 유일하게 확실한 불변식을 슬로프에 강제해서, 이 범위를 벗어나면 "시계를 잘못
# 읽었다"고 간주하고 명확히 실패시킨다. ±15%는 짧은 클립(샘플 6개, 1초 단위 양자화 오차)에서도
# 정상 클록이 오탐되지 않을 만큼 넉넉하면서, 시계와 무관한 숫자(거의 항상 1000ms/s와 크게
# 다르거나 상관관계 자체가 약함)는 충분히 걸러낼 만큼 좁다.
EXPECTED_CLOCK_SLOPE_MS_PER_SEC = 1000.0
CLOCK_SLOPE_TOLERANCE_RATIO = 0.15

# 🛡️ [화면비 사전 검사] 처음엔 "16:9에 가까운지"로 걸렀는데, 이번 라운드 검증 중 실제로 이
# 세션 내내 검증에 써온 실제 캡처 파일 자체가 1920x804(비율≈2.39, DAR 160:67)라는 걸 발견함 -
# OBS/캡처 소프트웨어가 창 크기를 임의로 잘라 저장하는 게 흔해서, "16:9 근접"으로 걸렀다면
# 이미 정상 동작이 검증된 캡처까지 거절하는 회귀였을 것. 진짜 위험한 건 화면비가 "16:9와
# 다른 것"이 아니라 "가로/세로가 뒤집힌 것"(세로 폰 녹화) - PC 게임 캡처는 창 크기가 어떻게
# 잘리든 항상 가로가 세로보다 넓고, 게임 UI는 실제 뷰포트 코너에 붙어 그려지므로 화면비가
# 좀 달라도(4:3/21:9/이번처럼 임의로 자른 2.39:1) 우측 상단 크롭이 대체로 여전히 유효하다.
# 그래서 화면비 자체의 미세한 편차가 아니라 "가로가 세로보다 충분히 넓은가"만 앞단에서
# 명확히 거르고, 그 안에서의 세부 크롭 오차는 기존 slope sanity check(OCR 결과 자체 검증)에
# 맡긴다. MIN_LANDSCAPE_ASPECT_RATIO=1.2는 4:3(1.33)까지는 통과시키면서 정사각형/세로는
# 확실히 막을 만큼 낮게 잡음 - 오늘 재검증한 실제 캡처(2.39)는 물론 통과.
MIN_LANDSCAPE_ASPECT_RATIO = 1.2

# Match-v5 계열은 User-Agent 없으면 Cloudflare가 403으로 막는다는 게 프로토타입에서 확인된
# 핵심 교훈 (Riot 인증 문제 아님). account-v1/league-v4는 필요 없어서 tier_verify._riot_request의
# 기본 헤더엔 없었지만, match-v5 호출에는 반드시 추가해야 한다.
BROWSER_USER_AGENT_HEADER = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024  # 100MB
MAX_CLIP_DURATION_SECONDS = 45.0

# 🛡️ ffmpeg amix는 클리핑 방지를 위해 기본값(normalize=true)으로 입력 스트림들을 자동으로
# 나눠서 합친다 - 즉 지금까지 킬 효과음은 원본 게임 오디오와 함께 자동으로 절반 가까이
# 감쇠되고 있었다. normalize=0으로 그 자동 감쇠를 끄고, 대신 효과음 스트림에만 명시적으로
# SFX_MIX_GAIN_DB만큼 게인을 얹는다. normalize를 끄면 합산 시 0dBFS를 넘길 수 있어
# alimiter로 최종 출력을 안전하게 캡핑한다.
SFX_MIX_GAIN_DB = 6.0
# crowd_cheer_2.wav는 에셋 자체를 이미 정점이 0dBFS 근처까지 차도록 마스터링해뒀다(고조→도약 구조를
# 살리려고). 여기에 SFX_MIX_GAIN_DB를 그대로 더 얹으면 렌더링 단계의 alimiter가 다시 세게 눌러서
# 애써 만든 도약폭이 뭉개지는 걸 실측으로 확인함 - 그래서 이 파일만 추가 게인을 0으로 뺀다.
SFX_MIX_GAIN_DB_OVERRIDE = {"crowd_cheer_2.wav": 0.0}
# alimiter limit (선형 스케일, 1.0=0dBFS). 0.97(-0.3dB 근처)로 뒀더니 PCM 단계에선 안전했지만
# AAC로 인코딩한 뒤 다시 재보면 실측 피크가 +2.4dB까지 튀는 걸 확인함 - 트랜지언트(박수/함성)를
# 0dBFS 바로 아래까지 밀어붙이면 손실 압축 특유의 인터샘플 오버슈트가 나온다는 뜻. 인코딩 후에도
# 진짜로 0dBFS를 안 넘도록 사전에 -3.7dB 정도 여유를 더 준다.
SFX_LIMITER_CEILING = 0.65


def _mmss_to_ms(mmss: str) -> int:
    m, s = mmss.strip().split(":")
    return (int(m) * 60 + int(s)) * 1000


def _fit_linear_mapping(samples: list[dict]) -> tuple[float, float]:
    """clip_t_sec(x) -> game_ms(y) 최소자승 선형 회귀. game_ms = slope * clip_t_sec + intercept."""
    n = len(samples)
    if n < 2:
        raise ValueError("샘플이 2개 미만")
    xs = [s["clip_t_sec"] for s in samples]
    ys = [s["game_ms"] for s in samples]
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    den = sum((x - mean_x) ** 2 for x in xs)
    if den == 0:
        raise ValueError("모든 샘플의 clip_t가 동일함")
    slope = num / den
    intercept = mean_y - slope * mean_x
    deviation_ratio = abs(slope - EXPECTED_CLOCK_SLOPE_MS_PER_SEC) / EXPECTED_CLOCK_SLOPE_MS_PER_SEC
    if deviation_ratio > CLOCK_SLOPE_TOLERANCE_RATIO:
        raise ValueError(
            f"시계 기울기가 비정상적임(slope={slope:.1f}ms/s, 기대값={EXPECTED_CLOCK_SLOPE_MS_PER_SEC:.0f}ms/s "
            f"±{CLOCK_SLOPE_TOLERANCE_RATIO * 100:.0f}%, 편차={deviation_ratio * 100:.1f}%) - "
            "크롭이 시계를 벗어나 다른 UI 요소를 읽었을 가능성"
        )
    return slope, intercept


def _clip_t_to_game_ms(clip_t_sec: float, mapping: tuple[float, float]) -> float:
    slope, intercept = mapping
    return slope * clip_t_sec + intercept


def _game_ms_to_clip_t(game_ms: float, mapping: tuple[float, float]) -> float:
    slope, intercept = mapping
    return (game_ms - intercept) / slope


def _select_kills_in_clip(kills: list[dict], mapping: tuple[float, float],
                           clip_duration_sec: float, slack_sec: float = 1.5) -> list[dict]:
    start_ms = _clip_t_to_game_ms(-slack_sec, mapping)
    end_ms = _clip_t_to_game_ms(clip_duration_sec + slack_sec, mapping)
    selected = []
    for k in kills:
        if start_ms <= k["timestamp_ms"] <= end_ms:
            k = dict(k)
            k["clip_t_sec"] = _game_ms_to_clip_t(k["timestamp_ms"], mapping)
            selected.append(k)
    return selected


def _extract_champion_kills(timeline: dict) -> list[dict]:
    kills = []
    for frame in timeline["info"]["frames"]:
        for ev in frame["events"]:
            if ev.get("type") == "CHAMPION_KILL":
                kills.append({
                    "timestamp_ms": ev["timestamp"],
                    "killer_id": ev.get("killerId"),
                    "victim_id": ev.get("victimId"),
                    "assist_ids": ev.get("assistingParticipantIds", []),
                })
    kills.sort(key=lambda k: k["timestamp_ms"])
    return kills


def _participant_id_to_name(match_detail: dict) -> dict[int, dict]:
    mapping = {}
    for p in match_detail["info"]["participants"]:
        mapping[p["participantId"]] = {
            "champion": p["championName"],
            "name": p.get("riotIdGameName") or p.get("summonerName") or "Unknown",
        }
    return mapping


def _pick_match_for_clip(matches_detail: list[dict], clip_creation: datetime.datetime) -> dict | None:
    for md in matches_detail:
        info = md["info"]
        start = datetime.datetime.fromtimestamp(info["gameStartTimestamp"] / 1000, tz=datetime.timezone.utc)
        end = datetime.datetime.fromtimestamp(
            (info["gameStartTimestamp"] + info["gameDuration"] * 1000) / 1000, tz=datetime.timezone.utc
        )
        if start - datetime.timedelta(minutes=2) <= clip_creation <= end + datetime.timedelta(minutes=2):
            return md
    return None


SYSTEM_PROMPT = (
    "너는 LCK 결승전 하이라이트를 중계하는 초하이텐션 한국어 게임 캐스터다. "
    "아래 '확정된 사실 목록'에 있는 킬 이벤트 각각에 대해 짧고 격정적인 캐스터 멘트를 한 줄씩 만들어라.\n\n"
    "톤/연출 규칙:\n"
    "- 감탄사·의성어를 적극 사용해라 (예: '우와아아!!', '미쳤습니다!!', '이걸 잡아요?!', '오오오!!')\n"
    "- 문장은 짧고 임팩트 있게 끊어라. 한 줄에 절 하나, 길어도 두 절.\n"
    "- 킬을 낸 유저/챔피언 이름을 문장 맨 앞이나 강조되는 위치에 배치해서 부각시켜라 "
    "(예: '{killer}!! 지금 뭘 한 거예요!!')\n"
    "- 느낌표를 적극 사용하고 텐션을 끝까지 올려라. 밋밋한 사실 전달문('OO가 XX를 처치했습니다' 같은 문장)은 금지.\n\n"
    "사실관계 규칙 (절대 위반 금지):\n"
    "- 목록에 없는 내용(킬 원인, 사용 스킬, 위치, 상황 추측 등)은 절대 지어내지 마라. "
    "텐션은 말투에만 얹고, 누가 누구를 처치했는지의 사실관계는 목록 그대로 유지해라.\n"
    "- 목록에 있는 킬 이벤트는 하나도 빠짐없이 전부 다뤄야 한다. 목록에 event_index가 N개면 "
    "반드시 N개의 줄을 만들어라. 하나라도 건너뛰지 마라.\n\n"
    "반드시 아래 JSON 스키마로만 답해, 다른 텍스트는 절대 포함하지 마: "
    '{"lines": [{"event_index": int, "text": "자막 한 줄"}]}'
)


class KyvoHighlight(KyvoBaseCog):
    def __init__(self, bot):
        super().__init__(bot)
        self.ai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
        self.render_executor = ThreadPoolExecutor(max_workers=HIGHLIGHT_MAX_WORKERS, thread_name_prefix="kyvo-highlight")
        self.render_semaphore = asyncio.Semaphore(HIGHLIGHT_MAX_CONCURRENT)

    async def _db_call(self, fn):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.bot.db_executor, fn)

    def _to_executor(self, fn, *args):
        loop = asyncio.get_running_loop()
        return loop.run_in_executor(self.render_executor, fn, *args)

    def _tier_verify_cog(self):
        cog = self.bot.get_cog("KyvoTierVerify")
        if cog is None:
            print("[HIGHLIGHT][CRITICAL] KyvoTierVerify cog not loaded - cannot make Riot API calls.", flush=True)
        return cog

    # ══════════════════════════════════════════════════════════
    #  사전 조건 조회 (DB만, 비용 발생 전에 전부 확인)
    # ══════════════════════════════════════════════════════════
    async def _get_verified_puuid(self, guild_id: int, user_id: int) -> str | None:
        try:
            res = await self._db_call(
                lambda: self.bot.supabase.table("riot_verifications").select("puuid")
                        .eq("guild_id", str(guild_id)).eq("user_id", str(user_id)).execute()
            )
            rows = res.data or []
        except Exception as e:
            print(f"[HIGHLIGHT][ERROR] Failed to look up verified puuid (guild={guild_id}, user={user_id}): "
                  f"{type(e).__name__}: {e}", flush=True)
            return None
        return rows[0]["puuid"] if rows else None

    # ══════════════════════════════════════════════════════════
    #  ffmpeg/PIL/OpenCV 계열 블로킹 작업 (전부 render_executor로 격리)
    # ══════════════════════════════════════════════════════════
    @staticmethod
    def _probe_duration_and_creation(video_path: str) -> tuple[float, datetime.datetime, tuple[int, int]]:
        r = subprocess.run([FFMPEG_EXE, "-i", video_path], capture_output=True, text=True)
        stderr = r.stderr
        dur_m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", stderr)
        if not dur_m:
            raise ValueError("ffmpeg가 영상 길이를 읽지 못함 - 손상되었거나 지원하지 않는 형식")
        h, m, s = dur_m.groups()
        duration = int(h) * 3600 + int(m) * 60 + float(s)
        ct_m = re.search(r"creation_time\s*:\s*([\d\-T:.Z]+)", stderr)
        if ct_m:
            creation = datetime.datetime.fromisoformat(ct_m.group(1).replace("Z", "+00:00"))
        else:
            creation = datetime.datetime.now(datetime.timezone.utc)
        # 🛡️ 회전 메타데이터(휴대폰 rotate/displaymatrix 태그로 실제 표시 화면비가 저장된
        # 픽셀 치수와 달라지는 경우)는 감지하지 않음 - PC 화면 녹화(League 클립)라는 실제
        # 사용 범위에선 나타나지 않는 경우라 알려진 한계로 남겨둠.
        video_line_m = re.search(r"Stream #\d+:\d+.*Video:.*", stderr)
        if not video_line_m:
            raise ValueError("ffmpeg가 비디오 스트림 정보를 읽지 못함 - 손상되었거나 지원하지 않는 형식")
        res_m = re.search(r"(\d{2,5})x(\d{2,5})", video_line_m.group(0))
        if not res_m:
            raise ValueError("ffmpeg가 해상도를 읽지 못함")
        resolution = (int(res_m.group(1)), int(res_m.group(2)))
        return duration, creation, resolution

    @staticmethod
    def _extract_frame(video_path: str, t_sec: float, out_png: str) -> None:
        subprocess.run(
            [FFMPEG_EXE, "-y", "-ss", str(t_sec), "-i", video_path,
             "-frames:v", "1", "-update", "1", out_png],
            capture_output=True, check=True,
        )

    @staticmethod
    def _crop_clock(frame_png: str) -> Image.Image:
        im = Image.open(frame_png)
        w, h = im.size
        x0, y0, x1, y1 = CLOCK_CROP_RATIO
        box = (int(w * x0), int(h * y0), int(w * x1), int(h * y1))
        crop = im.crop(box)
        return crop.resize((crop.width * 4, crop.height * 4))

    @staticmethod
    def _make_sfx_pick() -> str:
        if not SFX_POOL:
            raise RuntimeError(f"관중 함성 효과음을 찾을 수 없음: {SFX_DIR}")
        return random.choice(SFX_POOL)

    def _render_video(self, video_path: str, lines: list[dict], kill_times_sec: list[float],
                       work_dir: str, out_mp4: str) -> list[str]:
        chosen_sfx = [self._make_sfx_pick() for _ in kill_times_sec]

        inputs = ["-i", video_path]
        for sfx_path in chosen_sfx:
            inputs += ["-i", sfx_path]

        # 자막 텍스트 파일을 요청마다 고유한 하위 디렉터리에 쓴다 - 동시 처리 개수를 세마포어로
        # 1개 이상 허용해도 서로의 line_{i}.txt를 덮어쓰는 경로 충돌이 안 생기게.
        run_dir = os.path.join(work_dir, f"txt_{uuid.uuid4().hex[:8]}")
        os.makedirs(run_dir, exist_ok=True)

        try:
            draw_filters = []
            for i, line in enumerate(lines):
                txt_path = os.path.join(run_dir, f"line_{i}.txt")
                with open(txt_path, "w", encoding="utf-8") as f:
                    f.write(line["text"])
                start = line["clip_t_sec"]
                end = start + line.get("duration", 3.0)
                txt_escaped = txt_path.replace("\\", "/").replace(":", "\\:")
                draw_filters.append(
                    f"drawtext=fontfile='{FONT_PATH}':"
                    f"textfile='{txt_escaped}':reload=0:"
                    "fontcolor=white:fontsize=32:borderw=3:bordercolor=black:"
                    "x=(w-text_w)/2:y=h-100:"
                    f"enable='between(t,{start},{end})'"
                )
            video_chain = "[0:v]" + ",".join(draw_filters) + "[vout]" if draw_filters else "[0:v]copy[vout]"

            audio_parts = ["[0:a]"]
            delay_filters = []
            for i, (t, sfx_path) in enumerate(zip(kill_times_sec, chosen_sfx)):
                basename = os.path.basename(sfx_path)
                lead_ms = SFX_LEAD_MS.get(basename, 0)
                # 리드타임이 클립 시작보다 앞서 당겨지면(킬이 클립 맨 앞부분에 있으면) 0으로 클램핑 -
                # 긴장감 도입부가 일부 잘린 채로 클립 시작점부터 재생될 뿐, 별도 처리가 필요 없다.
                delay_ms = max(0, int(t * 1000) - lead_ms)
                mix_gain_db = SFX_MIX_GAIN_DB_OVERRIDE.get(basename, SFX_MIX_GAIN_DB)
                delay_filters.append(
                    f"[{i+1}:a]adelay={delay_ms}|{delay_ms},volume={mix_gain_db}dB[sfx{i}]"
                )
                audio_parts.append(f"[sfx{i}]")
            n_audio = len(audio_parts)
            audio_chain = ";".join(delay_filters)
            # normalize=0: amix 기본값(자동 감쇠)을 꺼서 위 volume 부스트가 실제로 반영되게 한다.
            # 감쇠를 끈 대신 합산 결과가 0dBFS를 넘을 수 있어 alimiter로 최종 출력을 안전하게 캡핑.
            amix = (
                f"{''.join(audio_parts)}amix=inputs={n_audio}:duration=first:"
                f"dropout_transition=0:normalize=0[mixed];"
                f"[mixed]alimiter=limit={SFX_LIMITER_CEILING}:attack=5:release=50[aout]"
            )
            full_audio = f"{audio_chain};{amix}" if audio_chain else "[0:a]anull[aout]"

            filter_complex = f"{video_chain};{full_audio}"

            cmd = [FFMPEG_EXE, "-y", *inputs,
                   "-filter_complex", filter_complex,
                   "-map", "[vout]", "-map", "[aout]",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                   "-c:a", "aac",
                   out_mp4]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(f"ffmpeg 렌더링 실패:\n{result.stderr[-2000:]}")
        finally:
            shutil.rmtree(run_dir, ignore_errors=True)

        return [os.path.basename(p) for p in chosen_sfx]

    # ══════════════════════════════════════════════════════════
    #  OpenAI 호출 (AsyncOpenAI라 executor 불필요 - ticket_ai.py와 동일한 클라이언트 관례)
    # ══════════════════════════════════════════════════════════
    async def _read_clock(self, im: Image.Image) -> str:
        import base64, io
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        resp = await self.ai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": "이 이미지는 게임 화면의 시계 부분이다. MM:SS 형식으로만 답해."},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ],
            }],
            max_tokens=10, temperature=0,
        )
        return resp.choices[0].message.content.strip()

    async def _generate_commentary(self, kills_with_names: list[dict]) -> list[dict]:
        import json
        facts_lines = []
        for k in kills_with_names:
            assist_str = f", 어시스트: {', '.join(k['assists'])}" if k["assists"] else ""
            facts_lines.append(
                f"[{k['index']}] {k['timestamp_ms']}ms 시점 - "
                f"{k['killer']}가 {k['victim']}을(를) 처치{assist_str}"
            )
        facts_block = "\n".join(facts_lines)

        resp = await self.ai_client.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            temperature=0.8,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"확정된 사실 목록 (총 {len(kills_with_names)}건, 전부 다뤄야 함):\n{facts_block}"},
            ],
        )
        data = json.loads(resp.choices[0].message.content)
        lines = data["lines"]

        covered = {l["event_index"] for l in lines}
        for k in kills_with_names:
            if k["index"] not in covered:
                lines.append({"event_index": k["index"], "text": f"{k['killer']}가 {k['victim']}을(를) 처치했습니다!"})
        lines.sort(key=lambda l: l["event_index"])
        return lines

    # ══════════════════════════════════════════════════════════
    #  Riot API (rate limit/재시도는 tier_verify 코그의 공유 리미터+로직을 그대로 재사용)
    # ══════════════════════════════════════════════════════════
    async def _riot_get(self, tv_cog, session: aiohttp.ClientSession, url: str):
        return await tv_cog._riot_request(session, url, extra_headers=BROWSER_USER_AGENT_HEADER)

    # ══════════════════════════════════════════════════════════
    #  /highlight
    # ══════════════════════════════════════════════════════════
    @app_commands.command(name="highlight", description="Turn a gameplay clip into an AI-narrated highlight with real match facts (requires /tier_verify first).")
    @app_commands.describe(video="Your gameplay clip (mp4) with the in-game clock visible in the top-right corner.")
    @app_commands.checks.cooldown(1, 30.0, key=lambda i: i.user.id)
    async def highlight(self, interaction: discord.Interaction, video: discord.Attachment):
        guild_id = interaction.guild_id
        await interaction.response.defer(ephemeral=True)

        tv_cog = self._tier_verify_cog()
        if tv_cog is None:
            await interaction.followup.send(await self.get_msg(guild_id, "highlight_err_unexpected"), ephemeral=True)
            return

        # 1. 길드 지역 설정 확인 (tier_verify와 동일한 사전 조건, 메시지도 그대로 재사용)
        platform_region = await tv_cog._get_platform_region(guild_id)
        if not platform_region:
            await interaction.followup.send(await self.get_msg(guild_id, "tier_verify_err_region_not_set"), ephemeral=True)
            return
        regional_route = PLATFORM_TO_REGIONAL.get(platform_region)
        if regional_route is None:
            await interaction.followup.send(await self.get_msg(guild_id, "tier_verify_err_region_not_set"), ephemeral=True)
            return

        # 2. 티어 인증(puuid) 확인 - party.py의 min_tier 미인증 차단과 동일한 원칙: 비용 발생 전에 막는다
        puuid = await self._get_verified_puuid(guild_id, interaction.user.id)
        if puuid is None:
            await interaction.followup.send(await self.get_msg(guild_id, "highlight_err_not_verified"), ephemeral=True)
            return

        # 3. 첨부파일 형식/크기 확인 (다운로드 전에 메타데이터만으로 판단)
        if not (video.content_type or "").startswith("video/"):
            await interaction.followup.send(await self.get_msg(guild_id, "highlight_err_invalid_attachment"), ephemeral=True)
            return
        if video.size > MAX_ATTACHMENT_BYTES:
            await interaction.followup.send(await self.get_msg(guild_id, "highlight_err_invalid_attachment"), ephemeral=True)
            return

        progress_msg = await interaction.followup.send(
            await self.get_msg(guild_id, "highlight_progress_queued"), ephemeral=True, wait=True
        )

        work_dir = tempfile.mkdtemp(prefix="kyvo_highlight_")
        try:
            async with self.render_semaphore:
                await self._run_pipeline(interaction, guild_id, video, work_dir, progress_msg, tv_cog, regional_route, puuid)
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    async def _run_pipeline(self, interaction, guild_id, video, work_dir, progress_msg, tv_cog, regional_route, puuid):
        video_path = os.path.join(work_dir, "input.mp4")
        await video.save(video_path)

        try:
            duration, creation, (width, height) = await self._to_executor(self._probe_duration_and_creation, video_path)
        except Exception as e:
            print(f"[HIGHLIGHT][ERROR] Failed to probe attachment (guild={guild_id}): {type(e).__name__}: {e}", flush=True)
            await progress_msg.edit(content=await self.get_msg(guild_id, "highlight_err_invalid_attachment"))
            return

        if duration > MAX_CLIP_DURATION_SECONDS:
            await progress_msg.edit(content=await self.get_msg(guild_id, "highlight_err_clip_too_long", max=int(MAX_CLIP_DURATION_SECONDS)))
            return

        aspect_ratio = width / height
        if aspect_ratio < MIN_LANDSCAPE_ASPECT_RATIO:
            print(f"[HIGHLIGHT][INFO] Rejected non-landscape aspect ratio {width}x{height} "
                  f"(ratio={aspect_ratio:.3f}, min={MIN_LANDSCAPE_ASPECT_RATIO:.2f}, guild={guild_id})", flush=True)
            await progress_msg.edit(content=await self.get_msg(guild_id, "highlight_err_unsupported_aspect_ratio"))
            return

        await progress_msg.edit(content=await self.get_msg(guild_id, "highlight_progress_analyzing"))

        # 시계 표시가 정수 초 단위라 최대 ~1초의 양자화 오차가 있다 - 샘플을 촘촘히(최소 6개) 늘려
        # 최소자승 회귀의 slope 추정 오차를 줄인다.
        n_samples = min(12, max(6, round(duration / 1.5) + 1))
        sample_times = [min(round(duration * i / (n_samples - 1), 2), duration - 0.1) for i in range(n_samples)]

        try:
            clock_samples = []
            for t in sample_times:
                frame_png = os.path.join(work_dir, f"f_{t:.2f}.png")
                await self._to_executor(self._extract_frame, video_path, t, frame_png)
                crop = await self._to_executor(self._crop_clock, frame_png)
                mmss = await self._read_clock(crop)
                clock_samples.append({"clip_t_sec": t, "game_ms": _mmss_to_ms(mmss)})
            mapping = _fit_linear_mapping(clock_samples)
        except Exception as e:
            print(f"[HIGHLIGHT][ERROR] Clock OCR/mapping failed (guild={guild_id}): {type(e).__name__}: {e}", flush=True)
            await progress_msg.edit(content=await self.get_msg(guild_id, "highlight_err_clock_read_failed"))
            return

        # 매치 자동 판별 + 타임라인 조회
        try:
            async with aiohttp.ClientSession() as session:
                match_ids = await self._riot_get(
                    tv_cog, session,
                    f"https://{regional_route}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?start=0&count=5",
                )
                details = [
                    await self._riot_get(tv_cog, session, f"https://{regional_route}.api.riotgames.com/lol/match/v5/matches/{mid}")
                    for mid in match_ids
                ]
                chosen = _pick_match_for_clip(details, creation)
                if chosen is None:
                    await progress_msg.edit(content=await self.get_msg(guild_id, "highlight_err_match_not_found"))
                    return
                match_id = chosen["metadata"]["matchId"]
                timeline = await self._riot_get(
                    tv_cog, session, f"https://{regional_route}.api.riotgames.com/lol/match/v5/matches/{match_id}/timeline"
                )
        except RiotAuthError as e:
            print(f"[HIGHLIGHT][CRITICAL] Riot API auth failure (status={e.status}, guild={guild_id})", flush=True)
            await progress_msg.edit(content=await self.get_msg(guild_id, "highlight_err_riot_auth"))
            return
        except RiotRateLimitedError:
            await progress_msg.edit(content=await self.get_msg(guild_id, "highlight_err_riot_rate_limited"))
            return
        except RiotServerError as e:
            print(f"[HIGHLIGHT][WARN] Riot server error (status={e.status}, guild={guild_id})", flush=True)
            await progress_msg.edit(content=await self.get_msg(guild_id, "highlight_err_riot_server_error"))
            return
        except RiotTimeoutError:
            await progress_msg.edit(content=await self.get_msg(guild_id, "highlight_err_riot_timeout"))
            return
        except RiotNotFoundError:
            await progress_msg.edit(content=await self.get_msg(guild_id, "highlight_err_match_not_found"))
            return
        except RiotAPIError as e:
            print(f"[HIGHLIGHT][ERROR] Unexpected Riot API error (guild={guild_id}): {type(e).__name__}: {e}", flush=True)
            await progress_msg.edit(content=await self.get_msg(guild_id, "highlight_err_unexpected"))
            return

        kills = _extract_champion_kills(timeline)
        names = _participant_id_to_name(chosen)
        selected = _select_kills_in_clip(kills, mapping, duration)
        if not selected:
            await progress_msg.edit(content=await self.get_msg(guild_id, "highlight_err_no_kills"))
            return

        kills_with_names = []
        for i, k in enumerate(selected):
            killer = names.get(k["killer_id"], {}).get("name", "Unknown") if k["killer_id"] else "미니언/포탑"
            victim = names.get(k["victim_id"], {}).get("name", "Unknown")
            assists = [names.get(a, {}).get("name", "Unknown") for a in k["assist_ids"]]
            kills_with_names.append({
                "index": i, "timestamp_ms": k["timestamp_ms"],
                "killer": killer, "victim": victim, "assists": assists,
                "clip_t_sec": k["clip_t_sec"],
            })

        await progress_msg.edit(content=await self.get_msg(guild_id, "highlight_progress_scripting"))
        try:
            lines_raw = await self._generate_commentary(kills_with_names)
        except Exception as e:
            print(f"[HIGHLIGHT][ERROR] Commentary generation failed (guild={guild_id}): {type(e).__name__}: {e}", flush=True)
            await progress_msg.edit(content=await self.get_msg(guild_id, "highlight_err_ai_failed"))
            return

        by_index = {k["index"]: k for k in kills_with_names}
        lines = [{"clip_t_sec": by_index[l["event_index"]]["clip_t_sec"], "text": l["text"], "duration": 3.0} for l in lines_raw]
        kill_times = [k["clip_t_sec"] for k in kills_with_names]

        await progress_msg.edit(content=await self.get_msg(guild_id, "highlight_progress_rendering"))
        out_mp4 = os.path.join(work_dir, "highlight_final.mp4")
        try:
            await self._to_executor(self._render_video, video_path, lines, kill_times, work_dir, out_mp4)
        except Exception as e:
            print(f"[HIGHLIGHT][ERROR] Render failed (guild={guild_id}): {type(e).__name__}: {e}", flush=True)
            await progress_msg.edit(content=await self.get_msg(guild_id, "highlight_err_render_failed"))
            return

        await progress_msg.edit(content=await self.get_msg(guild_id, "highlight_success_caption"))
        await interaction.followup.send(
            content=await self.get_msg(guild_id, "highlight_success_caption"),
            file=discord.File(out_mp4, filename="highlight.mp4"),
            ephemeral=False,
        )


async def setup(bot):
    if not HIGHLIGHT_FEATURE_ENABLED:
        print("[HIGHLIGHT] HIGHLIGHT_FEATURE_ENABLED is not set - skipping cog registration (command will not appear).", flush=True)
        return
    cog = KyvoHighlight(bot)
    await bot.add_cog(cog)
    print("[⚡ HIGHLIGHT] Cog extension setup complete.", flush=True)
