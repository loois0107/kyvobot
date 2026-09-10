"""/highlight - 게임 하이라이트 영상에 AI가 실제 매치 사실(Match-v5) 기반 해설을 음성으로만
(화면 텍스트 없이) 얹어 새 영상으로 만든다. 로컬 프로토타입에서 검증된 4개 조각(영상합성/
Match-v5/시계OCR/톤생성)을 그대로 실서비스 코드로 옮긴 것.

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

# 🛡️ imageio-ffmpeg가 배포하는 Linux 바이너리는 drawtext 필터가 빠진 최소 빌드다(Dockerfile에서
# 실측 확인). 현재 렌더링은 화면에 아무 것도 그리지 않아 drawtext를 안 쓰지만, 이후 화면 오버레이
# 기능이 다시 생기면 필요해지므로 Dockerfile이 apt로 설치한 drawtext 포함 풀빌드 ffmpeg를 계속
# 우선 사용한다. 시스템에 ffmpeg가 없는 환경(예: 로컬 개발 PC)을 위해서만 imageio-ffmpeg를 폴백으로 남긴다.
FFMPEG_EXE = shutil.which("ffmpeg") or imageio_ffmpeg.get_ffmpeg_exe()
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SFX_DIR = os.path.join(REPO_ROOT, "assets", "highlight_sfx")
# 🛡️ [배경음 고정] assets/highlight_sfx/에는 crowd_cheer_1~4.wav 4개가 있는데, 1/2/3.wav는
# "즉시 폭발형" 짧은 스팅어(2.7~13.3초)로 설계됐고, 오직 crowd_cheer_4.wav만 클립 전체에 지속되는
# 배경음 전용으로 설계됐다(README 참고). 예전엔 이 넷을 glob+random.choice로 무작위 골랐는데,
# 75% 확률로 스팅어가 뽑혀 "초반 몇 초만 나오고 나머지는 조용해지는" 문제가 실제 배포 영상에서
# 확인됨 - 배경음 용도로는 이제 4번만 고정으로 쓴다. 1/2/3.wav는 지우지 않고 디스크에 남겨둔다
# (다른 용도로 재사용 가능하니 파일만 보존, 이 코드에서 더는 선택하지 않을 뿐).
BACKGROUND_SFX_PATH = os.path.join(SFX_DIR, "crowd_cheer_4.wav")

# 대부분의 효과음은 "킬 시점 = 파일 시작(즉시 폭발)"이라 리드타임이 0이다. crowd_cheer_2.wav만
# 예외 - 조용히 고조되다 마지막에 훅 터지는 구조라, "터짐이 완성된 시점"이 킬 시점에 오도록
# 앞에서부터 재생해야 한다. crowd_cheer_2.wav의 엔벨로프 설계는 0~1.5s 조용함 → 1.5~4.5s 완만한
# 고조 → 4.5~6.0s 큰 도약(훅 터짐) → 6.0s~ 정점 유지. 도약이 "끝나는" 6.0초 지점이 킬 시점에
# 오도록 리드타임=6.0초로 잡는다(도약이 "시작"하는 4.5초를 쓰면 킬 순간엔 아직 다 안 터진 상태가
# 됨 - 처음엔 4.5초로 했다가 실측으로 이 문제를 발견해서 6.0초로 수정함). 에셋을 다시 다듬으면
# 이 값도 같이 조정해야 한다.
# 🛡️ [crowd_cheer_4.wav 재보정] 원래 14800(=램프가 "끝나고" 고점 유지 구간이 "시작"되는 지점)으로
# 잡았었는데, 실제 배포 영상에서 "함성 정점이 킬보다 한참 늦게 터진다"는 문제가 확인됨 - numpy로
# PCM을 직접 디코딩해 50ms 슬라이딩 윈도우 RMS를 스캔해보니, 파일에서 실제로 가장 크게 들리는
# 순간은 14.8s가 아니라 15.3s 부근(그 주변 15.3~16.8s대에 -0.5dB 이내로 여러 근접 정점이 몰려
# 있음 - 순간적인 샘플 단위 최댓값은 18.8s에도 하나 있었지만 그건 인지적으로 두드러지지 않는
# 짧은 트랜지언트라 기준으로 쓰지 않음)이라 15300으로 재보정한다. 게다가 킬이 15.3초 이전에
# 나오는 클립(짧은 클립 다수)에서는 여전히 아래 리드타임 클램프(max(0, ...))에 걸려 정점이 밀리는
# 게 known limitation으로 남아있음 - 이번엔 그 클램프 자체는 안 건드림.
SFX_LEAD_MS = {"crowd_cheer_2.wav": 6000, "crowd_cheer_4.wav": 15300}

# 화면 우측 상단 시계 영역 비율 크롭 박스 (프로토타입에서 1920x804 캡처 기준 보정).
# 다른 해상도/HUD 배치에서는 부정확할 수 있음 - 알려진 한계.
CLOCK_CROP_RATIO_NORMAL = (0.965, 0.0, 1.0, 0.028)
# 🛡️ 리플레이 뷰어(내보내기든, 재생 화면을 그냥 녹화한 것이든) 화면은 시계가 우측 상단이
# 아니라 스코어보드 KDA 배너 바로 아래 중앙에 있다 - 실제 실패 클립 2개(해상도 1728x720,
# 1920x804로 서로 다름)에서 실측 확인된 값. 두 해상도 다 이 비율로 정확히 잡혀서 비율
# 기반 접근이 유효해 보이지만, 표본이 2개뿐이라 확정은 아님 - 알려진 한계로 남겨둠.
CLOCK_CROP_RATIO_REPLAY = (0.47, 0.06, 0.53, 0.09)

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

# 🛡️ [매치 판별 2단계] 1차(creation_time ±2분 정밀 매칭)는 그대로 두고, 그게 실패했을 때만
# (주로 리플레이 뷰어 녹화본 - creation_time이 "본 시각"이라 실제 매치 시각과 몇 시간씩
# 어긋날 수 있음) 최근 매치 폭을 넓혀 재조회한다. 1차 그대로일 때 비용이 하나도 안 늘게
# 하려고 2차에서만 더 넓게 본다 - 실측(이 계정 최근 20경기)해보니 클립의 게임시각이 이른
# 구간(1~5분)이면 duration 조건만으로는 20개 중 20개가 다 살아남아서, 후보 폭 자체를
# 넓혀야 creation_time 근접도 비교가 의미 있어진다.
MATCH_LOOKUP_COUNT_NORMAL = 5
MATCH_LOOKUP_COUNT_FALLBACK = 20
# 🛡️ [2차 안전장치] "가장 가까운 후보"를 고르는 것과 "그 후보가 실제로 말이 되는 정도로
# 가까운가"는 별개 문제 - 실측으로 확인된 정상 리플레이 사용 범위(3시간)와 완전히 잘못된
# 경우(원본 클립이 8일+ 지나 최근 기록에서 아예 밀려난 경우)를 기준으로 24시간을 임계값으로
# 잡았다. 정상 케이스(3h)엔 8배, 실패 케이스(8일+)엔 실제 간격의 1/7 수준이라 양쪽 다
# 넉넉한 여유가 있다. 이걸 넘으면 "그럴듯한 오답"보다 명확한 실패가 낫다고 판단해 None 처리.
MATCH_GAME_TIME_MAX_STALENESS_SEC = 24 * 60 * 60

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
#  2단계 반응 체인 (전면 재설계) - 1단계: 킬 순간에 Main+Hype+Sub 3목소리가 동시에
#  "감탄사+닉네임" 샤우팅 / 2단계: Hype(사실 서술)->Sub(의문형 감탄)->Main(짧은 감탄)이
#  살짝 겹치며 순서대로 이어짐.
# ══════════════════════════════════════════════════════════
# 🛡️ [비용 설계] 실제 킬러 닉네임이 필요한 곳은 정확히 두 군데뿐 - (1) 1단계 샤우팅 중
# "감탄사+닉네임"을 외치는 목소리, (2) 2단계에서 사실을 서술하는 목소리. 나머지 네 자리
# (1단계의 나머지 두 목소리 + 2단계의 나머지 두 목소리)는 닉네임이 필요 없는 순수 감정
# 표현이라 정적 풀로 미리 구워둔다 - 렌더당 ElevenLabs 실시간 호출은 정확히 2회로 고정
# (이전 라운드까지는 1회였는데, "1단계도 실제 닉네임이 들어가야 한다"는 이번 요구사항 자체가
# 두 번째 실시간 호출을 요구함 - 문자 수 자체는 짧은 외침이라 부담이 크지 않음, 실측 후 아래
# 검증 결과에 남김).
VOICE_DIR = os.path.join(REPO_ROOT, "assets", "highlight_voice")

# ── 1단계(킬 순간, 3인 동시 샤우팅) ──
# Main만 실시간 TTS로 "감탄사+닉네임"을 외친다(MAIN_SHOUT_TEMPLATE). Hype/Sub는 닉네임을
# 넣을 수 없는 정적 풀이라 순수 감탄사만 - Hype는 기존 hype_*.wav(예전 "메인 종료 후 순차
# 재생" 역할)를 재사용한다. Sub는 새로 녹음(sub_shout_*.wav) - 기존 sub_*.wav(analyst 멘트)는
# 이 역할에 안 맞음.
# 🛡️ [버그 수정] hype_a.wav/hype_b.wav가 각각 "미쳤다!!"/"대박이다!!"로 반말체 녹음돼 있어서
# HYPE_SHOUT_POOL에서 random.choice로 뽑힐 때마다(2/3 확률) 존댓말 정책을 어긴 채 실제
# 배포됐던 게 실측으로 확인됨 - hype_c.wav만 존댓말이라 안 걸리고 넘어갔었다. 두 파일 다
# 같은 감탄사 프리픽스("와아아아악!!"/"우와아!!")는 유지하고 종결어미만 존댓말로 다시 녹음.
HYPE_SHOUT_POOL = sorted(glob.glob(os.path.join(VOICE_DIR, "hype_*.wav")))
SUB_SHOUT_POOL = sorted(glob.glob(os.path.join(VOICE_DIR, "sub_shout_*.wav")))
HYPE_SHOUT_TEXT = {
    "hype_a.wav": "와아아아악!! 미쳤어요!!", "hype_b.wav": "우와아!! 대박이에요!!",
    "hype_c.wav": "미쳤어요 진짜!!",
}
SUB_SHOUT_TEXT = {
    "sub_shout_a.wav": "우와아아아!!", "sub_shout_b.wav": "허어어!!",
}
MAIN_SHOUT_TEMPLATE = "우와아아아악!! {killer}~~!!"

# ── 2단계(리액션 체인, Hype -> Sub -> Main 순서로 살짝 겹치며 진행) ──
# Hype만 실시간 TTS로 사실을 서술한다(_generate_commentary, 예전엔 Main의 역할이었음 -
# _i_or_ga/_eul_or_reul 조사 검증도 이번에 여기로 같이 옮김). Sub(의문형 감탄)/Main(짧은
# 감탄)은 상황과 무관한 순수 감정 표현이라 정적 풀 - 둘 다 새로 녹음(sub_question_*.wav,
# main_react_*.wav).
SUB_QUESTION_POOL = sorted(glob.glob(os.path.join(VOICE_DIR, "sub_question_*.wav")))
MAIN_REACT_POOL = sorted(glob.glob(os.path.join(VOICE_DIR, "main_react_*.wav")))
SUB_QUESTION_TEXT = {
    "sub_question_a.wav": "진짜 돌았는데요??!!", "sub_question_b.wav": "이게 실화예요??!!",
}
MAIN_REACT_TEXT = {
    "main_react_a.wav": "와....", "main_react_b.wav": "허.....",
}

STAGE1_STAGE2_GAP_SEC = 0.15
# 🛡️ [겹침 비율] "완전 동시(뭉개짐)도 완전 순차(지루함)도 아니게" - 앞 목소리가 70~80%
# 지점에 왔을 때 다음 목소리가 시작되도록. 실측(각 문장 1~3초대) 기준 75%는 앞 목소리의
# 마지막 음절이 살짝 겹치면서도 새 목소리가 끼어드는 느낌이 나되, 문장 전체가 뭉개지진
# 않는 지점이라 중간값으로 골랐다 - 정확한 "자연스러움"은 결국 사람이 들어봐야 하는
# 판단이라, 이 상수 하나로 나중에 쉽게 튜닝할 수 있게 남겨둔다.
STAGE2_OVERLAP_RATIO = 0.75
RENDER_TAIL_BUFFER_SEC = 0.8      # 마지막으로 끝나는 목소리 종료 후 여유

ELEVENLABS_VOICE_IDS = {
    "main": "tlUdVt24VftfDokp32eu",  # LCK_Main_caster
    "hype": "IyAj6lA2EjUlXLg33b1o",  # LCK_Hype_Reaction
    "sub": "K4OVml3awIZZxKC33zQV",   # Lck_Sub_Analyst
}
ELEVENLABS_MODEL_ID = "eleven_v3"


def plan_stage2_chain(stage1_end: float, hype_fact_dur: float, sub_question_dur: float,
                       gap: float = STAGE1_STAGE2_GAP_SEC,
                       overlap_ratio: float = STAGE2_OVERLAP_RATIO) -> dict:
    """2단계(Hype 사실서술 -> Sub 의문형 -> Main 짧은 감탄) 타이밍 계획(순수 함수, 테스트
    가능). 1단계가 끝난 stage1_end에 gap을 두고 Hype가 시작하고, 그 뒤로는 각 목소리가
    앞 목소리 재생시간의 overlap_ratio 지점에 다음 목소리가 시작되도록(= 완전히 겹치지도,
    완전히 순차적이지도 않게) 배치한다."""
    hype_start = stage1_end + gap
    sub_start = hype_start + hype_fact_dur * overlap_ratio
    main_start = sub_start + sub_question_dur * overlap_ratio
    return {"hype_start": hype_start, "sub_start": sub_start, "main_start": main_start}


def _mmss_to_ms(mmss: str) -> int:
    m, s = mmss.strip().split(":")
    return (int(m) * 60 + int(s)) * 1000


def _eul_or_reul(word: str) -> str:
    """한글 마지막 글자에 받침이 있으면 '을', 없으면 '를' - 한글 완성형 유니코드 범위(가~힣)에서
    (codepoint - '가') % 28 == 0이면 종성 없음(를), 아니면 종성 있음(을). 한글이 아닌 이름(라틴
    닉네임 등)은 받침 판단이 무의미하므로 '를'로 기본 처리."""
    if not word or not ("가" <= word[-1] <= "힣"):
        return "를"
    return "를" if (ord(word[-1]) - ord("가")) % 28 == 0 else "을"


def _i_or_ga(word: str) -> str:
    """주격 조사 - 받침 있으면 '이', 없으면 '가' (판단 로직은 _eul_or_reul과 동일한 원리).
    실제 배포 영상에서 "장인정신가!!!"처럼 받침 있는 이름에 '가'가 잘못 붙는 문제가 확인됨 -
    facts_lines/커밋멘터리 폴백 줄이 조사를 하드코딩("가")해서 GPT가 그 문구를 그대로
    베껴 쓰다 오류가 전파된 것으로 보임(GPT는 '사실관계를 목록 그대로 유지하라'는 지시를
    충실히 따름)."""
    if not word or not ("가" <= word[-1] <= "힣"):
        return "가"
    return "가" if (ord(word[-1]) - ord("가")) % 28 == 0 else "이"


def _commentary_names_killer(text: str, killer: str) -> bool:
    """GPT가 생성한 main_text에 실제 킬러 이름이 들어있는지 확인 - 온도 0.8로 자유 생성되는
    텍스트라 프롬프트 지시(킬러 이름을 강조하라)를 안 따르고 희생자만 부각시키는 경우가 실제
    배포 영상에서 발견됨. 코드가 이를 검증하는 지점이 전혀 없었던 게 근본 원인이라 여기서 막는다."""
    return bool(killer) and killer in text


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


# 🛡️ [비용 예측 가능성] 1단계+2단계 반응 체인(위 plan_stage2_chain)+실시간 TTS 2회는 킬 1건당
# 비용이 고정이라, 렌더당 비용을 예측 가능하게 만들려면 클립당 킬 개수 자체를 상한 걸어야 한다.
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


def _has_bot_participant(match_detail: dict) -> bool:
    """🛡️ AI 상대 대전 후보를 걸러내려고 gameType/gameMode/queueId를 먼저 확인해봤는데,
    실제로 발견된 문제 사례(KR_8364744586)는 gameType=MATCHED_GAME, gameMode=SWIFTPLAY로
    "봇으로 채워진 정식 스위프트플레이"라 그 기준으로는 안 걸러졌다(Riot이 이걸 진짜
    매치메이드로 분류함). 대신 참가자 데이터에서 확인되는 훨씬 확실한 공식 신호를 쓴다 -
    AI로 채워진 슬롯은 participant.puuid가 리터럴 문자열 "BOT"(길이 3)이다(실제 이 매치로
    실측 확인). 사람 플레이어의 puuid는 항상 78자 고유 문자열이라 오탐 위험이 없다."""
    return any(p.get("puuid") == "BOT" for p in match_detail["info"]["participants"])


def _pick_match_by_game_time_range(matches_detail: list[dict], clip_creation: datetime.datetime,
                                    game_ms_end: float) -> dict | None:
    """_pick_match_for_clip(1차)이 실패했을 때만 쓰는 2차 판별 - 주로 리플레이 뷰어를 녹화한
    클립처럼 creation_time(파일을 "본" 시각)을 못 믿는 경우를 위한 것. 클립이 게임 내 시계
    기준 game_ms_end 시점까지 진행된 걸 보여주므로, 그보다 짧게 끝난 매치는 확실히 아니다 -
    이걸로 후보를 추리고, 남은 후보 중 실제 게임 "종료" 시각이 clip_creation에 가장 가까운
    걸 고른다(리플레이는 항상 게임이 끝난 "후"에나 볼 수 있으므로 종료 시각이 자연스러운
    기준점). creation_time은 더 이상 정확한 창이 아니라 "그럴듯한 순서"를 매기는 느슨한
    참고용일 뿐이라, 이 결과가 항상 정답이라는 보장은 없다(알려진 한계 - 특히 리플레이
    시청 전에 다른 게임을 더 했다면 그 게임이 더 가까워서 잘못 뽑힐 수 있음).
    AI 상대 대전(봇으로 채워진 매치 포함)은 애초에 후보에서 제외한다(_has_bot_participant).

    🛡️ [안전장치] 그 "가장 가까운" 후보조차 MATCH_GAME_TIME_MAX_STALENESS_SEC보다 더 멀리
    떨어져 있으면, 확신할 수 없는 추측을 내놓는 대신 None을 반환해 명확한 실패로 처리한다
    (원본 클립이 너무 오래돼서 최근 매치 목록에 애초에 정답이 없는 경우를 위한 방어).
    """
    candidates = [md for md in matches_detail
                  if md["info"]["gameDuration"] * 1000 >= game_ms_end and not _has_bot_participant(md)]
    if not candidates:
        return None

    def end_of(md):
        info = md["info"]
        return datetime.datetime.fromtimestamp(
            (info["gameStartTimestamp"] + info["gameDuration"] * 1000) / 1000, tz=datetime.timezone.utc
        )

    best = min(candidates, key=lambda md: abs((end_of(md) - clip_creation).total_seconds()))
    gap_sec = abs((end_of(best) - clip_creation).total_seconds())
    if gap_sec > MATCH_GAME_TIME_MAX_STALENESS_SEC:
        return None
    return best


# 🛡️ [역할 이동] 예전엔 이 문장이 "감탄사+이름 외침"으로 시작해서 Main 혼자 전부(외침+사실
# 전달)를 담당했다. 이번 재설계로 "감탄사+닉네임 외침"은 1단계(Main+Hype+Sub 동시 샤우팅,
# MAIN_SHOUT_TEMPLATE)가 전담하게 됐고, 이 문장(2단계 Hype 담당)은 순수하게 "누가 누구를
# 처치했는지"를 서술하는 역할만 남았다 - 그래서 "감탄사+이름으로 시작하라"는 구조 규칙은
# 빼고, 사실 서술에만 집중하도록 되돌렸다.
SYSTEM_PROMPT = (
    "너는 LCK 결승전 하이라이트를 중계하는 초하이텐션 한국어 게임 캐스터다. "
    "아래 '확정된 사실 목록'에 있는 킬 이벤트 각각에 대해, 이미 함성과 샤우팅이 한 번 터진 뒤 "
    "곧바로 이어지는 '사실 서술' 캐스터 멘트를 한 줄씩 만들어라(닉네임을 외치는 건 이미 다른 "
    "목소리가 끝냈으니, 여기서는 누가 누구를 처치했는지를 명확하게 짚어주는 역할이다).\n\n"
    "말투 규칙 (절대 위반 금지):\n"
    "- 항상 존댓말(합니다/습니다/해요/이에요체)만 써라. 반말체 감탄사(예: '대박이다', '미쳤다', '쩐다')는 "
    "절대 쓰지 말고 반드시 존댓말로 바꿔라(예: '대박이에요', '미쳤습니다').\n"
    "- 의문형 감탄을 적극 활용해라 (예: '미쳤는데요?!', '이걸 잡아요?!', '지금 뭘 한 거예요?!').\n"
    "- 문장을 끝맺을 땐 ~습니다체를 유지해라 (예: '완전히 뒤집어버렸습니다!!').\n\n"
    "구조 규칙:\n"
    "- 누가 누구를 처치했는지 사실을 존댓말로 명확하게 전달하는 문장 하나로 만들어라 "
    "(예: '{killer}가 {victim}를 완전히 끝내버렸습니다!!').\n"
    "- 문장은 짧고 임팩트 있게 끊어라. 한 줄에 절 하나, 길어도 두 절.\n"
    "- 느낌표를 적극 사용하고 텐션을 끝까지 올려라. 감탄사 없는 밋밋한 사실 전달문('OO가 XX를 처치했습니다' "
    "같은 문장)은 금지.\n\n"
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
    def _crop_clock(frame_png: str, ratio: tuple[float, float, float, float] = CLOCK_CROP_RATIO_NORMAL) -> Image.Image:
        im = Image.open(frame_png)
        w, h = im.size
        x0, y0, x1, y1 = ratio
        box = (int(w * x0), int(h * y0), int(w * x1), int(h * y1))
        crop = im.crop(box)
        return crop.resize((crop.width * 4, crop.height * 4))

    @staticmethod
    def _make_sfx_pick() -> str:
        if not os.path.exists(BACKGROUND_SFX_PATH):
            raise RuntimeError(f"배경음 효과음을 찾을 수 없음: {BACKGROUND_SFX_PATH}")
        return BACKGROUND_SFX_PATH

    def _render_video(self, video_path: str, video_duration: float, video_width: int,
                       schedule: dict, work_dir: str, out_mp4: str) -> str:
        """schedule = {"total_duration", "kill_t", <voice_key>...} - <voice_key>는
        main_shout/hype_shout/sub_shout(1단계)/hype_fact/sub_question/main_react(2단계) 중
        실제로 쓰인 것만 있고, 각 엔트리는 {"wav","text","start","duration"}. 타이밍 자체는
        호출부에서 이미 다 계산돼서 넘어오므로, 여기선 그 계획대로 ffmpeg 인풋/필터그래프를
        조립하기만 한다."""
        total_duration = schedule["total_duration"]
        cheer_path = self._make_sfx_pick()

        size_budget_total_kbps = (TARGET_OUTPUT_SIZE_MB * 8192) / total_duration
        quality_ceiling_total_kbps = MAX_OUTPUT_VIDEO_BITRATE_KBPS + OUTPUT_AUDIO_BITRATE_KBPS
        target_total_kbps = min(quality_ceiling_total_kbps, size_budget_total_kbps)
        target_video_kbps = max(MIN_OUTPUT_VIDEO_BITRATE_KBPS,
                                 target_total_kbps - OUTPUT_AUDIO_BITRATE_KBPS)

        # 🛡️ [환호 클램프 버그 수정] 예전엔 "정점이 킬보다 늦게 온다" 문제를 SFX_LEAD_MS 값만
        # 재보정해서 고치려 했는데, kill_t*1000 < lead_ms인 클립(흔함 - 킬이 15.3s 이전에 나오는
        # 짧은 클립)에서는 delay=max(0, ...)가 0으로 클램프돼서 파일이 그냥 t=0부터 재생되고,
        # 정점은 여전히 늦게(때로는 훨씬 늦게) 터졌다 - 재보정 자체는 맞았지만 클램프가 그걸
        # 무력화하는 별개의 버그였음. delay를 0으로 뭉개는 대신, 그 경우엔 cheer 입력 자체를
        # "-ss"로 (lead_ms - kill_ms)만큼 앞부분을 건너뛰고 시작한다 - 그러면 파일 안에서
        # "정점"에 해당하는 지점이 항상 real-time kill_t에 오게 된다(도입부 조성이 짧아지거나
        # 아예 없어질 뿐, 정점 자체는 절대 늦어지지 않음).
        cheer_basename = os.path.basename(cheer_path)
        cheer_lead_ms = SFX_LEAD_MS.get(cheer_basename, 0)
        kill_ms = schedule["kill_t"] * 1000
        if kill_ms >= cheer_lead_ms:
            cheer_delay_ms = int(kill_ms - cheer_lead_ms)
            cheer_skip_sec = 0.0
        else:
            cheer_delay_ms = 0
            cheer_skip_sec = (cheer_lead_ms - kill_ms) / 1000.0

        inputs = ["-i", video_path]
        if cheer_skip_sec > 0:
            inputs += ["-ss", f"{cheer_skip_sec:.3f}", "-i", cheer_path]
        else:
            inputs += ["-i", cheer_path]
        # 🛡️ [버그 수정] len(inputs)//2로 인덱스를 역산하던 방식은 모든 입력이 정확히
        # ["-i", path] 2칸짜리라는 가정에 의존했다 - cheer 입력에 "-ss"가 붙으면(4칸) 그 뒤
        # 모든 목소리의 인덱스가 통째로 틀어져서 "Invalid file index" 에러가 났다(실측으로
        # 발견). ffmpeg 입력 인덱스를 별도 카운터로 직접 추적해서 CLI 인자 개수와 무관하게
        # 정확한 인덱스를 매긴다.
        next_input_idx = 2  # 0=video, 1=cheer
        voice_indices = {}
        for key in ("main_shout", "hype_shout", "sub_shout", "hype_fact", "sub_question", "main_react"):
            entry = schedule.get(key)
            if entry is None:
                continue
            inputs += ["-i", entry["wav"]]
            voice_indices[key] = next_input_idx
            next_input_idx += 1

        # ── 화면 처리 (해설은 음성 전용 - 화면에 텍스트를 그리지 않는다) ──
        video_filters = []
        # 🛡️ 유저가 1440p/4K 등 고해상도 클립을 올리면(크기만 100MB 이내면 통과되므로
        # 충분히 가능) 목표 비트레이트가 픽셀 수 대비 너무 낮아져 화질이 심하게 뭉개진다 -
        # 스케일을 먼저 걸어 픽셀 수 자체를 낮춰둔다. -2로 짝수 높이 보장(libx264 요구사항).
        if video_width > MAX_OUTPUT_WIDTH:
            video_filters.append(f"scale={MAX_OUTPUT_WIDTH}:-2")
        # 🛡️ 원본 클립보다 렌더 길이가 길어지면(빌드업+메인+하이프+서브 꼬리가 원본 영상
        # 길이를 넘어서는 게 일반적) 영상 쪽도 늘려야 오디오가 잘려나가지 않는다. 화면을
        # 정지시키는 대신 마지막 프레임을 그대로 붙잡아 늘리는 가장 단순한 방법(tpad) -
        # 이전 프로토타입의 펀치인 줌/비네트는 이번 라운드 범위 밖.
        extra_video_sec = max(0.0, total_duration - video_duration)
        if extra_video_sec > 0.01:
            video_filters.append(f"tpad=stop_mode=clone:stop_duration={extra_video_sec:.3f}")
        video_chain = "[0:v]" + ",".join(video_filters) + "[vout]" if video_filters else "[0:v]copy[vout]"

        # ── 오디오 (화면 처리와 무관하게 그대로 유지) ──
        # 🛡️ amix duration=first는 "첫 번째로 나열된 스트림"의 길이만 본다 - 게임 오디오를
        # 전체 렌더 길이만큼 apad로 먼저 늘려두지 않으면, 뒤에 붙는 빌드업/메인/하이프/서브가
        # 게임 오디오 원래 길이에서 통째로 잘려나간다(이번 세션 프로토타입에서 반복 확인된
        # 실수, 여기서도 그대로 적용).
        audio_parts = [f"[0:a]apad=whole_dur={total_duration}[game0];"]
        mix_labels = ["[game0]"]

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

    async def _synthesize_voice_line(self, text: str, voice_key: str, work_dir: str, out_basename: str) -> str:
        """실제 킬러/희생자 이름이 들어가는 대사를 ElevenLabs로 실시간 합성 - 렌더당 정확히
        2회 호출된다(1단계 Main의 "감탄사+닉네임" 외침, 2단계 Hype의 사실 서술). voice_key는
        ELEVENLABS_VOICE_IDS의 키("main"/"hype"/"sub") 중 하나. 나머지 네 자리(1단계
        Hype/Sub, 2단계 Sub/Main)는 닉네임이 필요 없는 순수 감정 표현이라 정적 풀에서 고른다."""
        tagged_text = f"[excited][shouts] {text}"
        voice_id = ELEVENLABS_VOICE_IDS[voice_key]
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                headers={"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"},
                json={"text": tagged_text, "model_id": ELEVENLABS_MODEL_ID},
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise RuntimeError(f"ElevenLabs TTS 실패(status={resp.status}): {body[:500]}")
                content = await resp.read()
        mp3_path = os.path.join(work_dir, f"{out_basename}_raw.mp3")
        with open(mp3_path, "wb") as f:
            f.write(content)
        wav_path = os.path.join(work_dir, f"{out_basename}.wav")
        await self._to_executor(self._convert_to_wav, mp3_path, wav_path)
        return wav_path

    async def _generate_commentary(self, kills_with_names: list[dict]) -> list[dict]:
        import json
        facts_lines = []
        for k in kills_with_names:
            assist_str = f", 어시스트: {', '.join(k['assists'])}" if k["assists"] else ""
            facts_lines.append(
                f"[{k['index']}] {k['timestamp_ms']}ms 시점 - "
                f"{k['killer']}{_i_or_ga(k['killer'])} {k['victim']}{_eul_or_reul(k['victim'])} 처치{assist_str}"
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
                lines.append({"event_index": k["index"], "text": (
                    f"{k['killer']}{_i_or_ga(k['killer'])} {k['victim']}{_eul_or_reul(k['victim'])} 처치했습니다!"
                )})
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
    @app_commands.command(name="highlight", description="Turn a gameplay clip into an AI-narrated highlight with real match facts (run /tier_verify first).")
    @app_commands.describe(video="Your gameplay clip (mp4) with the clock visible top-right. Long names may slow pacing.")
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

        # 🛡️ [리플레이 뷰어 지원] 프레임 추출(ffmpeg) 자체는 크롭 좌표와 무관하니 한 번만 하고,
        # 크롭+OCR만 좌표 세트별로 재시도한다 - 일반 플레이 클립은 1차(우측 상단)에서 바로
        # 성공해서 추가 호출이 전혀 없고, 리플레이 뷰어 화면(재생바가 하단에 보이는 녹화본)만
        # 2차(중앙, CLOCK_CROP_RATIO_REPLAY)로 넘어가면서 호출이 늘어난다.
        frame_pngs = []
        for t in sample_times:
            frame_png = os.path.join(work_dir, f"f_{t:.2f}.png")
            await self._to_executor(self._extract_frame, video_path, t, frame_png)
            frame_pngs.append(frame_png)

        async def try_crop_ratio(ratio):
            clock_samples = []
            for t, frame_png in zip(sample_times, frame_pngs):
                crop = await self._to_executor(self._crop_clock, frame_png, ratio)
                mmss = await self._read_clock(crop)
                clock_samples.append({"clip_t_sec": t, "game_ms": _mmss_to_ms(mmss)})
            return _fit_linear_mapping(clock_samples)

        try:
            try:
                mapping = await try_crop_ratio(CLOCK_CROP_RATIO_NORMAL)
            except Exception as e:
                print(f"[HIGHLIGHT][INFO] Normal clock crop failed ({type(e).__name__}: {e}) - "
                      f"retrying with replay-viewer crop (guild={guild_id})", flush=True)
                mapping = await try_crop_ratio(CLOCK_CROP_RATIO_REPLAY)
        except Exception as e:
            print(f"[HIGHLIGHT][ERROR] Clock OCR/mapping failed (guild={guild_id}): {type(e).__name__}: {e}", flush=True)
            await progress_msg.edit(content=await self.get_msg(guild_id, "highlight_err_clock_read_failed"))
            return

        # 매치 자동 판별 + 타임라인 조회
        # 🛡️ [진단성] highlight_err_match_not_found는 아래 두 군데에서 나올 수 있다 -
        # (a) _pick_match_for_clip이 5개 후보 중 시간대가 맞는 걸 못 찾음
        # (b) Riot API 자체가 세 호출(매치목록/매치상세/타임라인) 중 하나에서 404를 반환
        # 이 둘을 로그만 보고 구별할 방법이 없었다(RiotNotFoundError 분기가 유일하게 print가
        # 없었음) - riot_call_stage/riot_call_url을 각 호출 직전에 갱신해두고, 404가 나면
        # 그 시점의 값을 그대로 로그에 남겨서 어느 호출이 실패했는지 바로 알 수 있게 한다.
        riot_call_stage = "match_ids"
        riot_call_url = (
            f"https://{regional_route}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids"
            f"?start=0&count={MATCH_LOOKUP_COUNT_NORMAL}"
        )
        try:
            async with aiohttp.ClientSession() as session:
                match_ids = await self._riot_get(tv_cog, session, riot_call_url)

                details = []
                for mid in match_ids:
                    riot_call_stage = "match_detail"
                    riot_call_url = f"https://{regional_route}.api.riotgames.com/lol/match/v5/matches/{mid}"
                    details.append(await self._riot_get(tv_cog, session, riot_call_url))

                chosen = _pick_match_for_clip(details, creation)
                if chosen is None:
                    # 🛡️ [진단성] 지난 라운드에 RiotNotFoundError 분기만 로그를 붙이고 이 분기(진짜
                    # _pick_match_for_clip이 못 찾은 경우)는 빼먹었었다 - _pick_match_for_clip
                    # 자체는 순수 함수로 남겨두고(테스트 용이성), 호출부에서 5개 후보 전부의
                    # 시간창과 클립 creation_time을 비교해 "왜" 안 맞았는지(너무 이르다/늦다,
                    # 얼마나) 남긴다.
                    diag_lines = [f"clip_creation={creation.isoformat()}"]
                    for d in details:
                        info = d["info"]
                        start = datetime.datetime.fromtimestamp(info["gameStartTimestamp"] / 1000, tz=datetime.timezone.utc)
                        end = datetime.datetime.fromtimestamp(
                            (info["gameStartTimestamp"] + info["gameDuration"] * 1000) / 1000, tz=datetime.timezone.utc
                        )
                        window_start = start - datetime.timedelta(minutes=2)
                        window_end = end + datetime.timedelta(minutes=2)
                        if creation < window_start:
                            reason = f"too early by {(window_start - creation).total_seconds():.0f}s"
                        elif creation > window_end:
                            reason = f"too late by {(creation - window_end).total_seconds():.0f}s"
                        else:
                            reason = "within window (unexpected - should have matched)"
                        diag_lines.append(
                            f"  {d['metadata']['matchId']}: window=[{window_start.isoformat()}, "
                            f"{window_end.isoformat()}] - {reason}"
                        )
                    print(f"[HIGHLIGHT][WARN] No candidate match window contains clip creation_time "
                          f"(guild={guild_id}):\n" + "\n".join(diag_lines), flush=True)

                    # 🛡️ [2차: creation_time을 못 믿는 경우 - 주로 리플레이 뷰어 녹화본] 1차가
                    # 실패했을 때만, 후보 폭을 넓혀(count=20) 다시 조회하고 "클립이 보여주는
                    # 게임시각까지 실제로 진행됐는가"로 후보를 추린 뒤 creation_time이 가장
                    # 가까운 걸 고른다. 1차가 성공하는 일반 클립은 이 블록 자체를 안 타서
                    # 조회량이 늘지 않는다.
                    game_ms_end = _clip_t_to_game_ms(duration, mapping)
                    riot_call_stage = "match_ids_fallback"
                    riot_call_url = (
                        f"https://{regional_route}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids"
                        f"?start=0&count={MATCH_LOOKUP_COUNT_FALLBACK}"
                    )
                    fallback_ids = await self._riot_get(tv_cog, session, riot_call_url)
                    fallback_details = []
                    for mid in fallback_ids:
                        riot_call_stage = "match_detail_fallback"
                        riot_call_url = f"https://{regional_route}.api.riotgames.com/lol/match/v5/matches/{mid}"
                        fallback_details.append(await self._riot_get(tv_cog, session, riot_call_url))

                    chosen = _pick_match_by_game_time_range(fallback_details, creation, game_ms_end)
                    if chosen is None:
                        print(f"[HIGHLIGHT][WARN] 2차(게임시각+creation_time 근접) 판별도 실패 - "
                              f"game_ms_end={game_ms_end:.0f}ms 이상 진행된 후보가 {len(fallback_details)}개 "
                              f"중 없음 (guild={guild_id})", flush=True)
                        await progress_msg.edit(content=await self.get_msg(guild_id, "highlight_err_match_not_found"))
                        return
                    print(f"[HIGHLIGHT][INFO] 2차 판별로 매치 선택됨: {chosen['metadata']['matchId']} "
                          f"(game_ms_end={game_ms_end:.0f}ms, guild={guild_id})", flush=True)
                match_id = chosen["metadata"]["matchId"]
                riot_call_stage = "timeline"
                riot_call_url = f"https://{regional_route}.api.riotgames.com/lol/match/v5/matches/{match_id}/timeline"
                timeline = await self._riot_get(tv_cog, session, riot_call_url)
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
            print(f"[HIGHLIGHT][WARN] Riot API 404 not found (stage={riot_call_stage}, url={riot_call_url}, "
                  f"guild={guild_id})", flush=True)
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
        killer_name = kills_with_names[0]["killer"]
        victim_name = kills_with_names[0]["victim"]
        hype_fact_text = lines_raw[0]["text"]

        # 🛡️ [킬러 이름 검증 - 2단계 Hype로 이동] GPT는 온도 0.8로 자유 생성돼서 "킬러 이름을
        # 강조하라"는 프롬프트 지시를 안 따르고 희생자만 부각시킨 문장을 내놓는 경우가 실제로
        # 확인됨 - 코드가 이걸 검증하는 지점이 아예 없었던 게 실질적 원인. 이 검증은 예전엔
        # Main(사실 서술까지 겸함)에 있었는데, 사실 서술 역할 자체가 2단계 Hype로 옮겨졌으므로
        # 검증도 그대로 따라온다. LLM을 재호출하면 비용/시간이 또 드니, 검증 실패 시 즉시 안전한
        # 고정 템플릿으로 대체한다(재시도 없음).
        if not _commentary_names_killer(hype_fact_text, killer_name):
            print(f"[HIGHLIGHT][WARN] Commentary text missing killer name (guild={guild_id}) - "
                  f"falling back to template. killer={killer_name!r} text={hype_fact_text!r}", flush=True)
            hype_fact_text = f"{killer_name}{_i_or_ga(killer_name)} {victim_name}{_eul_or_reul(victim_name)} 처치했습니다!!"

        await progress_msg.edit(content=await self.get_msg(guild_id, "highlight_progress_rendering"))

        # ── 1단계(킬 순간 3인 동시 샤우팅)의 Main + 2단계(리액션)의 Hype만 실시간 TTS
        # (렌더당 ElevenLabs 호출 정확히 2회) - 나머지 네 자리는 정적 풀에서 고른다.
        main_shout_text = MAIN_SHOUT_TEMPLATE.format(killer=killer_name)
        try:
            main_shout_wav = await self._synthesize_voice_line(main_shout_text, "main", work_dir, "main_shout")
            hype_fact_wav = await self._synthesize_voice_line(hype_fact_text, "hype", work_dir, "hype_fact")
        except Exception as e:
            print(f"[HIGHLIGHT][ERROR] ElevenLabs TTS failed (guild={guild_id}): {type(e).__name__}: {e}", flush=True)
            await progress_msg.edit(content=await self.get_msg(guild_id, "highlight_err_tts_failed"))
            return

        if not (HYPE_SHOUT_POOL and SUB_SHOUT_POOL and SUB_QUESTION_POOL and MAIN_REACT_POOL):
            print(f"[HIGHLIGHT][CRITICAL] Static voice pool missing files (guild={guild_id}): "
                  f"hype_shout={len(HYPE_SHOUT_POOL)} sub_shout={len(SUB_SHOUT_POOL)} "
                  f"sub_question={len(SUB_QUESTION_POOL)} main_react={len(MAIN_REACT_POOL)}", flush=True)
            await progress_msg.edit(content=await self.get_msg(guild_id, "highlight_err_unexpected"))
            return

        hype_shout_file = random.choice(HYPE_SHOUT_POOL)
        sub_shout_file = random.choice(SUB_SHOUT_POOL)
        sub_question_file = random.choice(SUB_QUESTION_POOL)
        main_react_file = random.choice(MAIN_REACT_POOL)

        try:
            main_shout_duration = await self._to_executor(self._probe_audio_duration, main_shout_wav)
            hype_shout_duration = await self._to_executor(self._probe_audio_duration, hype_shout_file)
            sub_shout_duration = await self._to_executor(self._probe_audio_duration, sub_shout_file)
            hype_fact_duration = await self._to_executor(self._probe_audio_duration, hype_fact_wav)
            sub_question_duration = await self._to_executor(self._probe_audio_duration, sub_question_file)
            main_react_duration = await self._to_executor(self._probe_audio_duration, main_react_file)
        except Exception as e:
            print(f"[HIGHLIGHT][ERROR] Failed to probe voice-line durations (guild={guild_id}): "
                  f"{type(e).__name__}: {e}", flush=True)
            await progress_msg.edit(content=await self.get_msg(guild_id, "highlight_err_render_failed"))
            return

        # 1단계: Main+Hype+Sub 셋 다 kill_t에 정확히 동시 시작. 셋 중 가장 길게 끝나는 목소리
        # 뒤로 2단계가 이어진다.
        stage1_end = kill_t + max(main_shout_duration, hype_shout_duration, sub_shout_duration)

        # plan_stage2_chain()은 순수 함수 - Hype(사실서술)->Sub(의문형)->Main(짧은 감탄)이
        # STAGE2_OVERLAP_RATIO만큼 겹치며 순서대로 이어지게 배치한다.
        chain = plan_stage2_chain(stage1_end, hype_fact_duration, sub_question_duration)
        hype_start = chain["hype_start"]
        sub_start = chain["sub_start"]
        main_react_start = chain["main_start"]

        end_times = [
            kill_t + main_shout_duration, kill_t + hype_shout_duration, kill_t + sub_shout_duration,
            hype_start + hype_fact_duration, sub_start + sub_question_duration,
            main_react_start + main_react_duration,
        ]
        total_duration = max(duration, max(end_times) + RENDER_TAIL_BUFFER_SEC)

        schedule = {
            "kill_t": kill_t,
            "total_duration": total_duration,
            "main_shout": {"wav": main_shout_wav, "text": main_shout_text, "start": kill_t, "duration": main_shout_duration},
            "hype_shout": {"wav": hype_shout_file, "text": HYPE_SHOUT_TEXT[os.path.basename(hype_shout_file)],
                           "start": kill_t, "duration": hype_shout_duration},
            "sub_shout": {"wav": sub_shout_file, "text": SUB_SHOUT_TEXT[os.path.basename(sub_shout_file)],
                          "start": kill_t, "duration": sub_shout_duration},
            "hype_fact": {"wav": hype_fact_wav, "text": hype_fact_text, "start": hype_start, "duration": hype_fact_duration},
            "sub_question": {"wav": sub_question_file, "text": SUB_QUESTION_TEXT[os.path.basename(sub_question_file)],
                              "start": sub_start, "duration": sub_question_duration},
            "main_react": {"wav": main_react_file, "text": MAIN_REACT_TEXT[os.path.basename(main_react_file)],
                           "start": main_react_start, "duration": main_react_duration},
        }

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
