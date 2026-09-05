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

# 🛡️ 메인 캐스터 대사(실제 킬러/희생자 이름이 들어가는 한 줄)만 실시간 TTS로 합성한다 -
# 빌드업 1/2단계·Hype·Sub는 화면 상황과 무관한 정적 음성 풀(assets/highlight_voice/)이라
# 이 키가 없어도 동작하지만, 메인 캐스터 음성은 이 기능의 핵심이라 다른 필수 키들과 동일한
# fail-fast 원칙을 적용한다.
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY")
if not ELEVENLABS_API_KEY:
    raise RuntimeError(
        "[HIGHLIGHT] ELEVENLABS_API_KEY environment variable is not set. "
        "Set it before starting the bot (used for the real-time main-caster voice line)."
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
SFX_LEAD_MS = {"crowd_cheer_2.wav": 6000, "crowd_cheer_4.wav": 14800}

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

# 🛡️ [출력 용량 제어] 디스코드 업로드 한도(서버 부스트 레벨에 따라 다르지만 25MB가 기준선)를
# 넘기지 않도록, 실측 결과(오늘 실제 배포 코드 경로로 렌더한 파일이 15.78s에 6.74MB = 0.427MB/s)
# 기준 최악의 경우(MAX_CLIP_DURATION_SECONDS + 킬 후 멘트 꼬리 ~10s ≈ 55s)를 계산해보면
# 25MB 문턱에 위험할 만큼 가까워진다 - crf 고정값 대신 total_duration으로 목표 비트레이트를
# 역산해서 파일 크기 자체를 항상 목표 근처로 수렴시킨다(콘텐츠 복잡도/해상도와 무관하게).
DISCORD_UPLOAD_LIMIT_BYTES = 25 * 1024 * 1024
TARGET_OUTPUT_SIZE_MB = 23.0  # 25MB에서 안전마진
OUTPUT_AUDIO_BITRATE_KBPS = 128
MIN_OUTPUT_VIDEO_BITRATE_KBPS = 300  # 극단적으로 긴 렌더에서도 화면이 아예 뭉개지지 않게 하는 하한
# 🛡️ TARGET_OUTPUT_SIZE_MB/duration만 그대로 쓰면 짧은 클립에서 오히려 화질/용량이 쓸데없이
# 커진다(예: 16초짜리를 23MB에 딱 맞추면 ~11.8Mbps짜리 영상이 나옴 - 예전 crf=20이 자연스럽게
# 뽑던 ~3.4Mbps보다 훨씬 큼). "크기 예산이 허용하는 한도 안에서, 그래도 이 정도면 충분한
# 화질 상한"을 같이 둬서 짧은 클립은 정상적인 크기로, 긴 클립만 예산에 맞춰 낮아지게 한다.
MAX_OUTPUT_VIDEO_BITRATE_KBPS = 3500
# 두 번째 안전장치: 비트레이트 역산은 "얼마나 큰가"를 다루지만 "얼마나 무거운 콘텐츠인가"(고해상도
# 업로드)는 안 다룬다 - 같은 비트레이트라도 해상도가 크면 화질이 그만큼 더 나빠질 뿐 크기 자체는
# 여전히 목표에 맞게 나오긴 하지만, 화질 하한을 지키려면 애초에 픽셀 수 자체를 제한하는 게 낫다.
MAX_OUTPUT_WIDTH = 1920

# 🛡️ ffmpeg amix는 클리핑 방지를 위해 기본값(normalize=true)으로 입력 스트림들을 자동으로
# 나눠서 합친다 - 즉 지금까지 킬 효과음은 원본 게임 오디오와 함께 자동으로 절반 가까이
# 감쇠되고 있었다. normalize=0으로 그 자동 감쇠를 끄고, 대신 효과음 스트림에만 명시적으로
# SFX_MIX_GAIN_DB만큼 게인을 얹는다. normalize를 끄면 합산 시 0dBFS를 넘길 수 있어
# alimiter로 최종 출력을 안전하게 캡핑한다.
SFX_MIX_GAIN_DB = 6.0
# crowd_cheer_2.wav는 에셋 자체를 이미 정점이 0dBFS 근처까지 차도록 마스터링해뒀다(고조→도약 구조를
# 살리려고). 여기에 SFX_MIX_GAIN_DB를 그대로 더 얹으면 렌더링 단계의 alimiter가 다시 세게 눌러서
# 애써 만든 도약폭이 뭉개지는 걸 실측으로 확인함 - 그래서 이 파일만 추가 게인을 0으로 뺀다.
# crowd_cheer_4.wav(연속 배경+킬 시 dB 앵커 상승, 이번 라운드 신규 - assets/highlight_sfx/README.md
# 참고)도 이미 자체적으로 목표 레벨까지 차 있어 같은 이유로 추가 부스트를 뺀다.
SFX_MIX_GAIN_DB_OVERRIDE = {"crowd_cheer_2.wav": 0.0, "crowd_cheer_4.wav": 0.0}
# alimiter limit (선형 스케일, 1.0=0dBFS). 0.97(-0.3dB 근처)로 뒀더니 PCM 단계에선 안전했지만
# AAC로 인코딩한 뒤 다시 재보면 실측 피크가 +2.4dB까지 튀는 걸 확인함 - 트랜지언트(박수/함성)를
# 0dBFS 바로 아래까지 밀어붙이면 손실 압축 특유의 인터샘플 오버슈트가 나온다는 뜻. 인코딩 후에도
# 진짜로 0dBFS를 안 넘도록 사전에 -3.7dB 정도 여유를 더 준다.
SFX_LIMITER_CEILING = 0.65

# ══════════════════════════════════════════════════════════
#  3단계 빌드업 체인 (이상감지 -> 감정격상 -> 킬폭발) - 정적 음성 풀 + 실시간 메인 대사
# ══════════════════════════════════════════════════════════
# 🛡️ [비용 설계] 화면 상황과 무관한(사실 주장이 전혀 없는) 순수 감정 표현인 빌드업 1/2단계와
# Hype/Sub는 SFX_POOL과 똑같은 glob+random.choice 정적 풀로 미리 구워둔다 - 실제 킬러/희생자
# 이름이 들어가야 하는 메인 캐스터 대사 "한 줄"만 렌더당 ElevenLabs 실시간 호출 1회로 처리한다.
VOICE_DIR = os.path.join(REPO_ROOT, "assets", "highlight_voice")
BUILDUP1_POOL = sorted(glob.glob(os.path.join(VOICE_DIR, "buildup1_*.wav")))
BUILDUP2_POOL = sorted(glob.glob(os.path.join(VOICE_DIR, "buildup2_*.wav")))
HYPE_POOL = sorted(glob.glob(os.path.join(VOICE_DIR, "hype_*.wav")))
SUB_POOL = sorted(glob.glob(os.path.join(VOICE_DIR, "sub_*.wav")))

# 각 빌드업 파일의 실측 강조 지점(초) - 0.08s 윈도우 슬라이딩 RMS로 사전 측정
# (_build_highlight_voice_pool.py, 한 번 실행하고 버리는 오프라인 빌드 스크립트).
# 빌드업만 필요함: 킬 시점 기준 특정 목표 시각에 정렬돼야 하기 때문. Hype/Sub는 메인이 끝난
# 직후 순차 재생일 뿐이라 정렬 기준점이 필요 없다(파일 시작 = 배치 시작).
BUILDUP_PEAK_T = {
    "buildup1_a.wav": 0.48, "buildup1_b.wav": 0.28,
    "buildup2_a.wav": 0.80, "buildup2_b.wav": 0.20,
    "buildup2_c.wav": 0.76, "buildup2_d.wav": 0.20,
}
BUILDUP_TEXT = {
    "buildup1_a.wav": "어어?!", "buildup1_b.wav": "어?! 뭔가...?!",
    "buildup2_a.wav": "어어?! 분위기가?!", "buildup2_b.wav": "잠시만요! 잠시만요!",
    "buildup2_c.wav": "어어?! 조심해야죠!", "buildup2_d.wav": "기류가 심상치 않은데요?!",
}
HYPE_TEXT = {
    "hype_a.wav": "와아아아악!! 미쳤다!!", "hype_b.wav": "우와아!! 대박이다!!",
    "hype_c.wav": "미쳤어요 진짜!!",
}
SUB_TEXT = {
    "sub_a.wav": "아니, 이건 진짜 대담한 판단이에요!!", "sub_b.wav": "완전히 상황을 뒤집어버렸네요!!",
    "sub_c.wav": "이걸 해내네요, 진짜!!",
}

STAGE1_TARGET_OFFSET_SEC = 2.0    # 1단계(이상감지) 목표: 킬 - 2.0초
STAGE2_TARGET_OFFSET_SEC = 1.0    # 2단계(감정격상) 목표: 킬 - 1.0초
STAGE2_STAGE3_MIN_GAP_SEC = 0.4   # 2단계 끝 ~ 3단계(메인) 시작 최소 여유
POST_LINE_GAP_SEC = 0.15          # 메인->하이프, 하이프->서브 사이 간격
RENDER_TAIL_BUFFER_SEC = 0.8      # 서브 종료 후 여유

ELEVENLABS_MAIN_VOICE_ID = "tlUdVt24VftfDokp32eu"  # LCK_Main_caster
ELEVENLABS_MODEL_ID = "eleven_v3"


def plan_stages(kill_t: float, s1_off: float, s1_dur: float, s2_off: float, s2_dur: float,
                 s3_off: float, min_lead: float = 0.0,
                 min_gap_2_3: float = STAGE2_STAGE3_MIN_GAP_SEC) -> dict:
    """3단계 타이밍 계획(순수 함수, 테스트 가능) - 각 단계 후보의 실측 강조지점(peak_t)이
    킬 시점 기준 목표 시각(1단계 킬-2.0s / 2단계 킬-1.0s / 3단계 킬)에 오도록 배치한다.
    자리가 부족하면 앞단계부터 스킵하고, 남은 단계는 뒷단계에 딱 붙여 압축 배치한다
    (이전 세션 v10에서 검증된 것과 동일한 알고리즘)."""
    target1 = kill_t - STAGE1_TARGET_OFFSET_SEC
    target2 = kill_t - STAGE2_TARGET_OFFSET_SEC
    target3 = kill_t

    start3 = max(min_lead, target3 - s3_off)

    start1 = target1 - s1_off
    active1 = start1 >= min_lead
    end1 = (start1 + s1_dur) if active1 else min_lead
    if not active1:
        start1 = None

    start2 = max(target2 - s2_off, end1)
    latest_start2 = start3 - min_gap_2_3 - s2_dur
    if start2 > latest_start2:
        start2 = latest_start2
    if active1 and start2 < end1:
        shift = end1 - start2
        if start1 - shift >= min_lead:
            start1 -= shift
            end1 -= shift
        else:
            active1 = False
            start1 = None
            end1 = min_lead
            start2 = max(start2, min_lead)

    active2 = (start2 >= end1 - 1e-9 and start2 >= min_lead - 1e-9
               and start2 + s2_dur + min_gap_2_3 <= start3 + 1e-9)
    if not active2:
        start2 = None

    return {
        "start1": start1, "active1": active1,
        "start2": start2, "active2": active2,
        "start3": start3,
    }


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


# 🛡️ [비용 예측 가능성] 3단계 빌드업 체인(아래 plan_stages)+실시간 TTS는 킬 1건당 비용이
# 고정이라, 렌더당 비용을 예측 가능하게 만들려면 클립당 킬 개수 자체를 상한 걸어야 한다.
# 지금은 가장 단순하고 안전한 값인 1로 제한 - 클립에 킬이 여러 개(팀파이트/에이스)여도
# 시간상 가장 먼저 오는 킬 하나만 다룬다. 나머지가 조용히 버려지는 트레이드오프는 알려진
# 한계로 남겨둠(추후 필요하면 유저에게 "N개 중 1개만 다뤘습니다" 안내를 붙이는 걸 고려).
MAX_KILLS_PER_CLIP = 1


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
    return selected[:MAX_KILLS_PER_CLIP]


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
    def _probe_audio_duration(path: str) -> float:
        r = subprocess.run([FFMPEG_EXE, "-i", path], capture_output=True, text=True)
        m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr)
        if not m:
            raise ValueError(f"ffmpeg가 오디오 길이를 읽지 못함: {path}")
        h, mi, s = m.groups()
        return int(h) * 3600 + int(mi) * 60 + float(s)

    @staticmethod
    def _convert_to_wav(src_path: str, out_wav: str) -> None:
        subprocess.run([FFMPEG_EXE, "-y", "-i", src_path, "-c:a", "pcm_s16le", out_wav],
                        capture_output=True, check=True)

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

    def _render_video(self, video_path: str, video_duration: float, video_width: int,
                       schedule: dict, work_dir: str, out_mp4: str) -> str:
        """schedule = {"total_duration", "stage1"?, "stage2"?, "main", "hype", "sub"} - 각
        엔트리는 {"wav","text","start","duration"} (stage1/stage2는 자리가 없으면 None).
        타이밍 자체는 plan_stages()에서 이미 다 계산돼서 넘어오므로, 여기선 그 계획대로
        ffmpeg 인풋/필터그래프를 조립하기만 한다."""
        total_duration = schedule["total_duration"]
        cheer_path = self._make_sfx_pick()

        size_budget_total_kbps = (TARGET_OUTPUT_SIZE_MB * 8192) / total_duration
        quality_ceiling_total_kbps = MAX_OUTPUT_VIDEO_BITRATE_KBPS + OUTPUT_AUDIO_BITRATE_KBPS
        target_total_kbps = min(quality_ceiling_total_kbps, size_budget_total_kbps)
        target_video_kbps = max(MIN_OUTPUT_VIDEO_BITRATE_KBPS,
                                 target_total_kbps - OUTPUT_AUDIO_BITRATE_KBPS)

        run_dir = os.path.join(work_dir, f"txt_{uuid.uuid4().hex[:8]}")
        os.makedirs(run_dir, exist_ok=True)

        try:
            inputs = ["-i", video_path, "-i", cheer_path]
            voice_indices = {}
            for key in ("stage1", "stage2", "main", "hype", "sub"):
                entry = schedule.get(key)
                if entry is None:
                    continue
                inputs += ["-i", entry["wav"]]
                voice_indices[key] = len(inputs) // 2 - 1

            # ── 자막 ──
            draw_filters = []
            # 🛡️ 유저가 1440p/4K 등 고해상도 클립을 올리면(크기만 100MB 이내면 통과되므로
            # 충분히 가능) 목표 비트레이트가 픽셀 수 대비 너무 낮아져 화질이 심하게 뭉개진다 -
            # 스케일을 먼저 걸어 픽셀 수 자체를 낮춰둔다. -2로 짝수 높이 보장(libx264 요구사항).
            if video_width > MAX_OUTPUT_WIDTH:
                draw_filters.append(f"scale={MAX_OUTPUT_WIDTH}:-2")
            # 🛡️ 원본 클립보다 렌더 길이가 길어지면(빌드업+메인+하이프+서브 꼬리가 원본 영상
            # 길이를 넘어서는 게 일반적) 영상 쪽도 늘려야 오디오가 잘려나가지 않는다. 화면을
            # 정지시키는 대신 마지막 프레임을 그대로 붙잡아 늘리는 가장 단순한 방법(tpad) -
            # 이전 프로토타입의 펀치인 줌/비네트는 이번 라운드 범위 밖.
            extra_video_sec = max(0.0, total_duration - video_duration)
            if extra_video_sec > 0.01:
                draw_filters.append(f"tpad=stop_mode=clone:stop_duration={extra_video_sec:.3f}")

            for key in ("stage1", "stage2", "main", "hype", "sub"):
                entry = schedule.get(key)
                if entry is None:
                    continue
                txt_path = os.path.join(run_dir, f"line_{key}.txt")
                with open(txt_path, "w", encoding="utf-8") as f:
                    f.write(entry["text"])
                txt_escaped = txt_path.replace("\\", "/").replace(":", "\\:")
                start = entry["start"]
                end = start + entry["duration"]
                draw_filters.append(
                    f"drawtext=fontfile='{FONT_PATH}':"
                    f"textfile='{txt_escaped}':reload=0:"
                    "fontcolor=white:fontsize=32:borderw=3:bordercolor=black:"
                    "x=(w-text_w)/2:y=h-100:"
                    f"enable='between(t,{start},{end})'"
                )
            video_chain = "[0:v]" + ",".join(draw_filters) + "[vout]" if draw_filters else "[0:v]copy[vout]"

            # ── 오디오 ──
            # 🛡️ amix duration=first는 "첫 번째로 나열된 스트림"의 길이만 본다 - 게임 오디오를
            # 전체 렌더 길이만큼 apad로 먼저 늘려두지 않으면, 뒤에 붙는 빌드업/메인/하이프/서브가
            # 게임 오디오 원래 길이에서 통째로 잘려나간다(이번 세션 프로토타입에서 반복 확인된
            # 실수, 여기서도 그대로 적용).
            audio_parts = [f"[0:a]apad=whole_dur={total_duration}[game0];"]
            mix_labels = ["[game0]"]

            cheer_basename = os.path.basename(cheer_path)
            cheer_lead_ms = SFX_LEAD_MS.get(cheer_basename, 0)
            cheer_delay_ms = max(0, int(schedule["kill_t"] * 1000) - cheer_lead_ms)
            cheer_gain_db = SFX_MIX_GAIN_DB_OVERRIDE.get(cheer_basename, SFX_MIX_GAIN_DB)
            audio_parts.append(f"[1:a]adelay={cheer_delay_ms}|{cheer_delay_ms},volume={cheer_gain_db}dB[cheer0];")
            mix_labels.append("[cheer0]")

            for key, idx in voice_indices.items():
                entry = schedule[key]
                delay_ms = max(0, int(entry["start"] * 1000))
                audio_parts.append(f"[{idx}:a]adelay={delay_ms}|{delay_ms}[v_{key}];")
                mix_labels.append(f"[v_{key}]")

            n_mix = len(mix_labels)
            audio_parts.append(
                f"{''.join(mix_labels)}amix=inputs={n_mix}:duration=first:"
                f"dropout_transition=0:normalize=0[mixed];"
                f"[mixed]alimiter=limit={SFX_LIMITER_CEILING}:attack=5:release=50[aout]"
            )
            full_audio = "".join(audio_parts)

            filter_complex = f"{video_chain};{full_audio}"

            # 🛡️ crf 고정값 대신 total_duration에서 역산한 목표 비트레이트로 인코딩 -
            # 콘텐츠 복잡도/해상도와 무관하게 파일 크기가 항상 TARGET_OUTPUT_SIZE_MB 근처로
            # 수렴한다(디스코드 업로드 한도 대응). maxrate/bufsize로 순간적인 폭주만 눌러주고
            # 평균은 -b:v 그대로 나가게 하는 표준 단일 패스 VBV 제한 인코딩.
            cmd = [FFMPEG_EXE, "-y", *inputs,
                   "-filter_complex", filter_complex,
                   "-map", "[vout]", "-map", "[aout]",
                   "-c:v", "libx264", "-preset", "veryfast",
                   "-b:v", f"{int(target_video_kbps)}k",
                   "-maxrate", f"{int(target_video_kbps * 1.5)}k",
                   "-bufsize", f"{int(target_video_kbps * 2)}k",
                   "-c:a", "aac", "-b:a", f"{OUTPUT_AUDIO_BITRATE_KBPS}k",
                   "-t", str(total_duration),
                   out_mp4]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(f"ffmpeg 렌더링 실패:\n{result.stderr[-2000:]}")
        finally:
            shutil.rmtree(run_dir, ignore_errors=True)

        return cheer_basename

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

    async def _synthesize_main_voice(self, text: str, work_dir: str) -> str:
        """메인 캐스터 대사(실제 킬러/희생자 이름이 들어간 그 한 줄)를 ElevenLabs로 실시간
        합성 - 이 함수가 렌더당 유일한 ElevenLabs 실시간 호출이다(빌드업/Hype/Sub는 전부
        assets/highlight_voice/의 정적 풀에서 고름)."""
        tagged_text = f"[excited][shouts] {text}"
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_MAIN_VOICE_ID}",
                headers={"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"},
                json={"text": tagged_text, "model_id": ELEVENLABS_MODEL_ID},
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(f"ElevenLabs TTS 실패(status={resp.status}): {body[:500]}")
                content = await resp.read()
        mp3_path = os.path.join(work_dir, "main_voice_raw.mp3")
        with open(mp3_path, "wb") as f:
            f.write(content)
        wav_path = os.path.join(work_dir, "main_voice.wav")
        await self._to_executor(self._convert_to_wav, mp3_path, wav_path)
        return wav_path

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

        # MAX_KILLS_PER_CLIP=1이라 kills_with_names/lines_raw는 항상 정확히 1건.
        kill_t = kills_with_names[0]["clip_t_sec"]
        main_text = lines_raw[0]["text"]

        await progress_msg.edit(content=await self.get_msg(guild_id, "highlight_progress_rendering"))

        # ── 3단계 체인: 메인만 실시간 TTS(렌더당 ElevenLabs 호출 정확히 1회), 나머지는 정적 풀 ──
        try:
            main_wav = await self._synthesize_main_voice(main_text, work_dir)
        except Exception as e:
            print(f"[HIGHLIGHT][ERROR] ElevenLabs TTS failed (guild={guild_id}): {type(e).__name__}: {e}", flush=True)
            await progress_msg.edit(content=await self.get_msg(guild_id, "highlight_err_tts_failed"))
            return

        if not (BUILDUP1_POOL and BUILDUP2_POOL and HYPE_POOL and SUB_POOL):
            print(f"[HIGHLIGHT][CRITICAL] Static voice pool missing files (guild={guild_id}): "
                  f"stage1={len(BUILDUP1_POOL)} stage2={len(BUILDUP2_POOL)} "
                  f"hype={len(HYPE_POOL)} sub={len(SUB_POOL)}", flush=True)
            await progress_msg.edit(content=await self.get_msg(guild_id, "highlight_err_unexpected"))
            return

        stage1_file = random.choice(BUILDUP1_POOL)
        stage2_file = random.choice(BUILDUP2_POOL)
        hype_file = random.choice(HYPE_POOL)
        sub_file = random.choice(SUB_POOL)

        try:
            main_duration = await self._to_executor(self._probe_audio_duration, main_wav)
            stage1_duration = await self._to_executor(self._probe_audio_duration, stage1_file)
            stage2_duration = await self._to_executor(self._probe_audio_duration, stage2_file)
            hype_duration = await self._to_executor(self._probe_audio_duration, hype_file)
            sub_duration = await self._to_executor(self._probe_audio_duration, sub_file)
        except Exception as e:
            print(f"[HIGHLIGHT][ERROR] Failed to probe voice-line durations (guild={guild_id}): "
                  f"{type(e).__name__}: {e}", flush=True)
            await progress_msg.edit(content=await self.get_msg(guild_id, "highlight_err_render_failed"))
            return

        # plan_stages()는 순수 함수 - 각 단계 강조지점이 킬 기준 목표 시각에 오도록 배치하고,
        # 자리가 부족하면 앞단계 스킵/뒷단계 압축을 알아서 처리한다(이전 세션 v10에서 검증됨).
        plan = plan_stages(
            kill_t,
            s1_off=BUILDUP_PEAK_T[os.path.basename(stage1_file)], s1_dur=stage1_duration,
            s2_off=BUILDUP_PEAK_T[os.path.basename(stage2_file)], s2_dur=stage2_duration,
            s3_off=0.0,  # 메인은 실시간 합성이라 사전 실측 강조지점이 없음 - 파일 시작=킬 시점으로 단순화
        )
        main_start = plan["start3"]
        hype_start = main_start + main_duration + POST_LINE_GAP_SEC
        sub_start = hype_start + hype_duration + POST_LINE_GAP_SEC
        total_duration = max(duration, sub_start + sub_duration + RENDER_TAIL_BUFFER_SEC)

        schedule = {
            "kill_t": kill_t,
            "total_duration": total_duration,
            "main": {"wav": main_wav, "text": main_text, "start": main_start, "duration": main_duration},
            "hype": {"wav": hype_file, "text": HYPE_TEXT[os.path.basename(hype_file)],
                     "start": hype_start, "duration": hype_duration},
            "sub": {"wav": sub_file, "text": SUB_TEXT[os.path.basename(sub_file)],
                    "start": sub_start, "duration": sub_duration},
        }
        if plan["active1"]:
            schedule["stage1"] = {"wav": stage1_file, "text": BUILDUP_TEXT[os.path.basename(stage1_file)],
                                   "start": plan["start1"], "duration": stage1_duration}
        if plan["active2"]:
            schedule["stage2"] = {"wav": stage2_file, "text": BUILDUP_TEXT[os.path.basename(stage2_file)],
                                   "start": plan["start2"], "duration": stage2_duration}

        out_mp4 = os.path.join(work_dir, "highlight_final.mp4")
        try:
            await self._to_executor(self._render_video, video_path, duration, width, schedule, work_dir, out_mp4)
        except Exception as e:
            print(f"[HIGHLIGHT][ERROR] Render failed (guild={guild_id}): {type(e).__name__}: {e}", flush=True)
            await progress_msg.edit(content=await self.get_msg(guild_id, "highlight_err_render_failed"))
            return

        await self._send_result_or_report_failure(interaction, progress_msg, guild_id, out_mp4)

    async def _send_result_or_report_failure(self, interaction, progress_msg, guild_id, out_mp4) -> None:
        """렌더링된 파일을 보내되, 용량 초과나 그 외 업로드 실패를 조용히 묻지 않고 progress_msg를
        적절한 에러로 되돌린다. 독립 메서드로 뺀 이유: 이 분기 로직 자체를 파이프라인 전체를
        돌리지 않고도 단위 테스트할 수 있어야 하기 때문."""
        # 🛡️ 비트레이트 역산으로 크기를 목표 근처로 수렴시켰지만, 그래도 극단적인 경우(예상보다
        # 훨씬 복잡한 콘텐츠, 컨테이너/오디오 오버헤드 오차)를 대비해 실제 파일 크기를 보내기
        # 전에 먼저 확인한다 - 어차피 실패할 업로드를 시도해서 시간 버릴 필요 없이 바로 안내.
        out_size_bytes = os.path.getsize(out_mp4)
        if out_size_bytes > DISCORD_UPLOAD_LIMIT_BYTES:
            print(f"[HIGHLIGHT][WARN] Rendered output exceeds Discord upload limit "
                  f"({out_size_bytes / 1024 / 1024:.1f}MB, guild={guild_id})", flush=True)
            await progress_msg.edit(content=await self.get_msg(guild_id, "highlight_err_output_too_large"))
            return

        # 🛡️ [버그 수정] 이전에는 전송 성공 여부와 무관하게 먼저 "완성됐습니다"로 편집해버려서,
        # followup.send가 실패하면(용량 초과 등) 유저는 성공 메시지만 보고 실제 파일은 영영 못
        # 받는 상황이 조용히 묻혔다. 전송을 먼저 시도하고, 성공했을 때만 성공 메시지로 편집한다.
        try:
            await interaction.followup.send(
                content=await self.get_msg(guild_id, "highlight_success_caption"),
                file=discord.File(out_mp4, filename="highlight.mp4"),
                ephemeral=False,
            )
        except discord.HTTPException as e:
            print(f"[HIGHLIGHT][ERROR] Upload failed (status={e.status}, guild={guild_id}): {e}", flush=True)
            if e.status == 413:
                await progress_msg.edit(content=await self.get_msg(guild_id, "highlight_err_output_too_large"))
            else:
                await progress_msg.edit(content=await self.get_msg(guild_id, "highlight_err_upload_failed"))
            return
        except Exception as e:
            print(f"[HIGHLIGHT][ERROR] Unexpected upload failure (guild={guild_id}): {type(e).__name__}: {e}", flush=True)
            await progress_msg.edit(content=await self.get_msg(guild_id, "highlight_err_upload_failed"))
            return

        await progress_msg.edit(content=await self.get_msg(guild_id, "highlight_success_caption"))


async def setup(bot):
    if not HIGHLIGHT_FEATURE_ENABLED:
        print("[HIGHLIGHT] HIGHLIGHT_FEATURE_ENABLED is not set - skipping cog registration (command will not appear).", flush=True)
        return
    cog = KyvoHighlight(bot)
    await bot.add_cog(cog)
    print("[⚡ HIGHLIGHT] Cog extension setup complete.", flush=True)
