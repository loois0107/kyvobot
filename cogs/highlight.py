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

# 🛡️ 실제 킬러 닉네임이 들어가는 두 줄(1단계 Hype 닉네임 샤우팅, 3단계 Main 사실 전달)만
# 실시간 TTS로 합성한다 - 0단계(3인 동시 폭발)와 2단계 Sub 추임새는 화면 상황과 무관한 정적
# 음성 풀(assets/highlight_voice/)이라 이 키가 없어도 동작하지만, 실시간 두 줄이 이 기능의
# 핵심이라 다른 필수 키들과 동일한 fail-fast 원칙을 적용한다.
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

# 🛡️ [오버레이 UI 사전 조사용] LCK 스타일 오버레이(파형 애니메이션 등)를 구현하기 전에,
# 배포 서버(Render)의 ffmpeg가 필요한 필터(showwaves/showvolume/overlay/zoompan/geq)를
# 실제로 갖췄는지 미리 확인해두기 위한 로그. drawtext 때(로컬에선 되는데 Render 서버
# 바이너리엔 없어서 실배포 후에야 발견된 사고 - 위 FFMPEG_EXE 주석 참고)와 같은 사고를
# 구현 전에 미리 잡으려는 목적. showwaves/showvolume/overlay/zoompan/geq는 drawtext(별도
# libfreetype 필요)와 달리 libavfilter 코어 내장 필터라 특별 빌드 옵션이 필요 없지만,
# 배포 서버에서 직접 확인하기 전까진 추론일 뿐이라 이 로그로 실측한다.
_OVERLAY_UI_CHECK_FILTERS = ["showwaves", "showvolume", "overlay", "zoompan", "geq"]


def _log_ffmpeg_filter_support() -> None:
    try:
        result = subprocess.run([FFMPEG_EXE, "-filters"], capture_output=True, text=True, timeout=10)
        output = result.stdout + result.stderr
        available = set(re.findall(r"^\s*[T.][S.][C.]\s+(\S+)", output, re.MULTILINE))
        status = {name: (name in available) for name in _OVERLAY_UI_CHECK_FILTERS}
        all_present = all(status.values())
        detail = ", ".join(f"{name}={'OK' if ok else 'MISSING'}" for name, ok in status.items())
        print(f"[HIGHLIGHT][FFMPEG_FILTERS] ffmpeg={FFMPEG_EXE} all_present={all_present} - {detail}", flush=True)
    except Exception as e:
        print(f"[HIGHLIGHT][FFMPEG_FILTERS][ERROR] Failed to check ffmpeg filter support: "
              f"{type(e).__name__}: {e}", flush=True)

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
# 🛡️ [2차 안전장치 - 임계값 재검토, 24h -> 6h] "가장 가까운 후보"를 고르는 것과 "그 후보가
# 실제로 말이 되는 정도로 가까운가"는 별개 문제. 원래 24시간이었는데, 이 값의 근거("정상
# 리플레이 사용 범위 3시간"으로 8배 여유)는 실측이 아니라 가정이었다 - 실제 배포 사고에서
# 이 로직이 14시간 이상 떨어진 후보를 "가장 가까운 매치"로 골라 완전히 다른 매치의 킬러/
# 희생자/타이밍이 그대로 서술되는 문제가 실측 로그로 확인됐다(24h 임계값이 이 명백한 오답을
# 못 걸러냄).
#
# 처음엔 사용자가 예시로 든 1~2시간을 그대로 썼는데(2h로 구현), 이번 세션 내내 검증에 써온
# 실제 클립("장인정신" 킬 클립, League of Legends (TM) Client 2026-09-09 04-46-30.mp4)으로
# 회귀 테스트를 돌려보니 그 클립의 실제 정답 매치(KR_8374071995)조차 게임 종료~클립
# creation_time 간격이 **+3.15시간**으로 실측되어, 2시간 임계값이 이 정상 케이스까지
# 걸러버리는 걸 실측으로 확인했다(회귀 발견 - "no match found"로 실패). 즉 2시간은 이
# 계정의 실제 정상 사용 패턴보다도 타이트했다.
#
# 그래서 6시간으로 다시 잡았다: 확인된 정상 케이스(3.15h)의 약 2배 여유를 두면서, 사고
# 케이스(14h+)와는 2배 이상 차이 나게 확실히 갈라놓는 값이다. 이것도 여전히 "실측 데이터
# 1건 + 사고 데이터 1건"으로 정한 값이라 완전히 확정은 아니다 - 정상 사용자가 실제로 6시간
# 넘게 걸리는 패턴이 흔하다는 게 나중에 드러나면 다시 조정이 필요할 수 있다(알려진 한계).
# 이 임계값을 넘으면 "그럴듯한 오답"보다 명확한 실패가 낫다고 판단해 None 처리하는 기존
# 철학은 그대로 유지.
MATCH_GAME_TIME_MAX_STALENESS_SEC = 6 * 60 * 60

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
# 🛡️ [alimiter level=false - 진짜 원인 발견] 해설 게인을 올리면서 0~9dB를 스윕했더니 최종
# 피크가 게인에 비례하지 않고 특정 값(3.0/5.5/6.0/8.0/9.0dB)에서만 콕 집어 0dBFS를 살짝
# 넘는 불안정한 패턴이 나왔다 - 원인을 파고보니 ffmpeg의 alimiter는 `level`(자동 레벨 보정)
# 옵션이 **기본값 true**라, 리미터가 게인을 깎은 만큼 출력을 다시 끌어올려서 정작 "천장"
# 자체를 제멋대로 무력화하고 있었다(입력 게인이 달라질 때마다 보정량도 달라지니 결과가
# 비선형적으로 튄 것). `level=false`로 명시적으로 꺼서 진짜 하드 리미터로 만들었더니 게인을
# 0~10dB 전부 스윕해도 피크가 -3.2~-3.8dB 범위에 안정적으로 고정됨을 확인(SFX_LIMITER_CEILING
# =0.65의 이론치 -3.74dB와 거의 정확히 일치) - 더 이상 게인 값에 따라 클리핑 여부가 복불복이
# 아니다. 이 alimiter 필터 자체는 이 세션 훨씬 이전(로컬 프로토타입 단계)에 한 번 배운 교훈
# 이었는데 실제 프로덕션 코드로 옮겨질 때 빠졌던 것으로 보인다.
VOICE_MIX_GAIN_DB = 6.0

# ══════════════════════════════════════════════════════════
#  0~3단계 킬 리액션 시퀀스 (전면 재설계 - 오늘 저녁 로컬 프로토타입 v1~v9에서 검증) -
#  전체 순서: 상황 멘트(0단계 전) -> "어어??"(0단계 전) -> 0단계(킬 순간 3인 동시 폭발) ->
#  1단계(Hype 닉네임) -> 2단계(Sub 의문형) -> 3단계(Main 사실 전달)
#  각 단계 시작은 "이전 단계 최장 음성 길이 × STAGE_OVERLAP_RATIO" - 고정 초가 아니다(0~3단계
#  한정, 킬 이전 두 리드인 단계는 아래 별도 gap 규칙).
# ══════════════════════════════════════════════════════════
# 🛡️ [비용 설계] 실제 킬러 닉네임이 필요한 곳은 정확히 두 군데 - (1) 1단계 Hype의 닉네임
# 샤우팅, (2) 3단계 Main의 사실 전달. 나머지 자리(상황 멘트+어어??+0단계 세 목소리+2단계
# Sub)는 닉네임이 필요 없는 순수 감정 표현이라 정적 풀로 미리 구워둔다 - 렌더당 ElevenLabs
# 실시간 호출은 정확히 2회로 고정(문자 수 자체는 짧은 외침/한 문장이라 부담이 크지 않음).
VOICE_DIR = os.path.join(REPO_ROOT, "assets", "highlight_voice")

# ── 킬 이전 리드인 1/2: 상황 멘트 -> "어어??" -> (0단계로 이어짐) ──
# 🛡️ [환각 위험 차단] "소리지르기 전에 상황 멘트"라는 요청 자체에 예시로 "탑쪽은 신경전이
# 벌어지는 중이네요" 같은 특정 라인(탑) 지목 문구가 포함돼 있었는데, 이건 실제로 위험하다 -
# 킬이 탑에서 안 났으면 명백한 오지어낸 사실이 된다(SYSTEM_PROMPT가 지키는 "목록에 없는
# 내용은 절대 지어내지 마라" 원칙과 정면으로 어긋남). 그래서 이 문구는 채택하지 않고, 위치/
# 챔피언/상황을 전혀 특정하지 않는 순수 분위기 감탄("구도 좋은데요?" 계열)만 골랐다 -
# 어떤 클립에 붙어도 항상 사실일 수 있는 문장들이라 환각 위험이 없다(예전 BUILDUP_TEXT와
# 동일한 원칙).
PRE_BUILDUP_POOL = sorted(glob.glob(os.path.join(VOICE_DIR, "pre_buildup_*.wav")))
PRE_BUILDUP_TEXT = {
    "pre_buildup_a.wav": "구도 좋은데요?",
    "pre_buildup_b.wav": "분위기가 심상치 않은데요?",
    "pre_buildup_c.wav": "긴장감이 느껴지는데요?",
}
# 🛡️ ["어어??" 신규] 상황 멘트와 0단계 폭발 사이에 짧게 끼워 넣는 "이상 감지" 반응 - 옛날
# buildup1_*.wav("어어?!" 계열, Main 목소리) 정적 풀이 이 구조 재설계 전에 만들어져 있던 걸
# 그대로 재사용한다(새 TTS 없음). 파일이 이미 짧아서(0.8~1.5초) "짧게"라는 요구사항도 그대로
# 충족.
# 🛡️ [풀 재확장 - "어어?!" 계열 안에서만] 예전엔 "어?! 뭔가...?!" 계열(구 buildup1_b.wav)
# 대신 "어어?!" 계열만 쓰라는 요청으로 buildup1_a.wav 하나로 좁혔었다 - 그 구 buildup1_b.wav
# 파일은 이름 충돌을 피해 legacy_unused_buildup1_b.wav로 옮겨두고(내용은 그대로 보관,
# "buildup1_"로 시작하지 않아서 아래 와일드카드에 다시 안 잡힘) buildup1_b.wav/
# buildup1_c.wav 자리에 "어어?!" 계열(모음 개수만 변주) 신규 녹음을 채웠다. 이제 다시
# buildup1_*.wav로 넓혀서 3개(a/b/c) 전부 잡는다.
EOEO_POOL = sorted(glob.glob(os.path.join(VOICE_DIR, "buildup1_*.wav")))
EOEO_TEXT = {
    "buildup1_a.wav": "어어?!",
    "buildup1_b.wav": "어" * 3 + "?!",  # 0.88s, 1회 시도로 무음/깊은 딥 없음 통과
    "buildup1_c.wav": "어" * 4 + "?!",  # 0.96s, 1회 시도로 무음/깊은 딥 없음 통과
}
# 🛡️ [앵커링 기준 = 클립 시작(t=0), kill_t 역산 아님] 처음엔 "0단계(킬) 직전에 끝나도록"
# kill_t에서 거꾸로 역산했는데, 실제로 들어보니 "영상 시작하자마자" 나와야 한다는 요구와
# 다른 결과가 나왔다(kill_t가 클립 중간쯤이면 리드인도 자동으로 중간쯤에 옴 - 클립 길이
# 자체는 계산식에 아예 안 들어가서 "초반"을 보장 못 함, 실측으로 확인된 버그 아닌 설계
# 오해). 이번엔 클립 시작(t=0) 기준으로 앞에서부터 배치하고, kill_t와 안 겹치는지만
# 안전장치로 검사한다 - 자리가 없으면(비정상적으로 짧은 클립/이른 킬) 예전 빌드업1/2단계와
# 같은 원칙으로 스킵한다(억지로 겹치게 밀어넣지 않음).
PRE_BUILDUP_START_OFFSET_SEC = 0.4  # 상황 멘트: 클립 시작 후 이만큼 뒤에 시작(0.3~0.5 범위)
PRE_BUILDUP_GAP_SEC = 0.2  # 상황 멘트 종료 ~ "어어??" 시작 사이 간격
EOEO_GAP_SEC = 0.2         # "어어??" 종료 ~ 0단계(킬 시점) 시작 사이 최소 안전 여백(충돌 검사용)

# ── 0단계(킬 순간, 3인 동시 폭발 - 닉네임 없는 순수 감탄사) ──
# 셋 다 정적 풀. Hype/Sub는 기존 1단계(구조 변경 전) 풀을 그대로 재사용 - 역할만 바뀌었을 뿐
# 파일/텍스트는 그대로. Main은 이 역할의 정적 풀이 없어서 새로 녹음(main_explode_*.wav).
# 🛡️ [버그 수정, 이미 반영됨] hype_a.wav/hype_b.wav가 각각 "미쳤다!!"/"대박이다!!"로 반말체
# 녹음돼 있어서 HYPE_EXPLODE_POOL에서 random.choice로 뽑힐 때마다(2/3 확률) 존댓말 정책을
# 어긴 채 실제 배포됐던 게 실측으로 확인됨 - hype_c.wav만 존댓말이라 안 걸리고 넘어갔었다.
# 두 파일 다 같은 감탄사 프리픽스("와아아아악!!"/"우와아!!")는 유지하고 종결어미만 존댓말로
# 다시 녹음.
# 🛡️ [체감 비중 강화, 3차 - 재녹음 방식 자체를 교체] 1~2차("완전!!"/"진짜!!" 등 짧은 문장을
# 이어붙이는 방식)는 문장 경계마다 TTS가 자연스러운 숨쉬기 무음을 넣어서, silencedetect로
# 실측해보니 파일마다 서로 안 맞는 타이밍에 무음 구간이 1~3곳씩 있었다 - 세 목소리가 계속
# 겹쳐서 울리는 게 아니라 "끊기는 지점마다 한둘만 들리는" 문제의 실제 원인이었음(에코박스
# 확인). 그래서 이번엔 문장을 이어붙이는 대신 감탄사 자체의 모음을 길게 늘이는 방식으로
# 바꿨다(닉네임과 달리 의미 있는 고유명사가 아니라 순수 감탄사라 늘여 발음해도 안전).
# 🛡️ [재검증 결과 - 정밀 판정 기준 도입] silencedetect(noise=-35dB:d=0.15, "0.15초 이상"만
# 잡는 기준)만으로는 부족하다는 게 실측으로 확인됨 - 모음 15개 안팎으로 처음 보정했던 버전도
# 이 기준은 통과했지만, 더 민감한 기준(noise=-30dB, 프레임 20ms RMS, peak 대비 -30dB 이상
# 깊은 딥을 "진짜 딥"으로 판정)으로 재측정하니 0.15초보다 짧지만 -40~-90dB까지 떨어지는 딥이
# 파일당 여러 곳(3~6곳) 있었고 파형에서도 소리가 여러 뭉치로 쪼개져 보였다. ElevenLabs
# Creator 티어로 업그레이드(문자 한도 121,000) 후 모음 개수를 6~10개로 더 줄이고, 파일당
# 여러 번 생성해서 "0.15초 이상 무음 0곳 + 정밀 기준 깊은 딥 0곳"을 만족하는 테이크를 자동
# 채택하는 방식(최대 12~14회 재시도)으로 6개 파일 전부 재녹음했다 - 최종적으로 6개 모두 두
# 기준 다 통과(완전한 무음 없는 연속음). sub_shout_b는 "허"+"어" 계열 텍스트로 28회를
# 시도해도 매번 1곳이 남아서, 모음 자체를 "히"+"이" 계열로 바꾸니 7회 만에 해결됐다 - 특정
# 음소(어) 자체가 이 목소리에서 유독 끊기기 쉬웠던 것으로 보인다.
MAIN_EXPLODE_POOL = sorted(glob.glob(os.path.join(VOICE_DIR, "main_explode_*.wav")))
HYPE_EXPLODE_POOL = sorted(glob.glob(os.path.join(VOICE_DIR, "hype_*.wav")))
SUB_EXPLODE_POOL = sorted(glob.glob(os.path.join(VOICE_DIR, "sub_shout_*.wav")))
MAIN_EXPLODE_TEXT = {
    "main_explode_a.wav": "우와" + "아" * 10 + "악!!",  # 1.52s, 무음/깊은 딥 없음(정밀 기준 통과)
    "main_explode_b.wav": "우와" + "아" * 8 + "악!!",  # 1.44s, 1회 시도로 무음/깊은 딥 없음 통과
    "main_explode_c.wav": "우와" + "아" * 12 + "악!!",  # 1.52s, 1회 시도로 무음/깊은 딥 없음 통과
}
HYPE_EXPLODE_TEXT = {
    "hype_a.wav": "와" + "아" * 10 + "악!!",  # 2.08s, 무음/깊은 딥 없음(정밀 기준 통과)
    "hype_b.wav": "우와" + "아" * 8 + "!!",  # 1.76s, 무음/깊은 딥 없음(정밀 기준 통과)
    "hype_c.wav": "으" + "아" * 6 + "악!",  # 1.36s, 무음/깊은 딥 없음(정밀 기준 통과)
    "hype_d.wav": "와" + "아" * 8 + "!!",  # 1.68s, 1회 시도로 무음/깊은 딥 없음 통과
    "hype_e.wav": "우와" + "아" * 10 + "악!!",  # 2.00s, 1회 시도로 무음/깊은 딥 없음 통과
    "hype_f.wav": "으" + "아" * 8 + "악!!",  # 1.76s, 1회 시도로 무음/깊은 딥 없음 통과
}
SUB_EXPLODE_TEXT = {
    "sub_shout_a.wav": "우와" + "아" * 8 + "!!",  # 1.60s, 무음/깊은 딥 없음(정밀 기준 통과)
    "sub_shout_b.wav": "히" + "이" * 8 + "!!",  # 1.44s, 무음/깊은 딥 없음(정밀 기준 통과)
    # 🛡️ [문제 음소 회피] "허"+"어" 계열이 유독 끊기기 쉬웠던 이력(README 참고) - 새 후보는
    # 이미 검증된 "히"+"이"/"우와"+"아" 두 계열 안에서만 개수를 바꿨다.
    "sub_shout_c.wav": "히" + "이" * 10 + "!!",  # 1.60s, 1회 시도로 무음/깊은 딥 없음 통과
    "sub_shout_d.wav": "우와" + "아" * 6 + "!!",  # 1.76s, 1회 시도로 무음/깊은 딥 없음 통과
    "sub_shout_e.wav": "히" + "이" * 6 + "!!",  # 1.28s, 1회 시도로 무음/깊은 딥 없음 통과
}

# ── 1단계(Hype 닉네임 샤우팅, 실시간 TTS) ──
# 🛡️ [발음 표기] 이름 음절을 늘려 쓰는 방식("장이이인정시이인!!")은 TTS 발음 경계와 안 맞아
# "장애~인정신"처럼 들리는 문제가 로컬 프로토타입에서 확인됨 - 음절은 그대로 두고 이름 끝에
# 물결표만 붙이는 방식(B)이 더 자연스러웠고, 여기에 볼륨 스웰(D2, 뒷부분만 서서히 커짐)을
# 결합해서 "길게 끄는 느낌"을 오디오 후처리로 흉내낸다(_apply_nickname_swell). 0단계가 이미
# "우와아아아악!!" 감탄사를 셋이 같이 외치므로, 여기선 닉네임만 - 감탄사 중복 없음.
HYPE_NICKNAME_SHOUT_TEMPLATE = "{killer}~~!!"
NICKNAME_SWELL_START_RATIO = 0.55   # 이 지점부터(대략 물결표 여운 구간) 볼륨이 커지기 시작
NICKNAME_SWELL_RISE = 0.6           # 클립 끝에서 최대 몇 배(1+RISE)까지 커지는지

# ── 2단계(Sub 의문형 감탄, 정적 풀) ──
SUB_QUESTION_POOL = sorted(glob.glob(os.path.join(VOICE_DIR, "sub_question_*.wav")))
SUB_QUESTION_TEXT = {
    "sub_question_a.wav": "진짜 돌았는데요??!!", "sub_question_b.wav": "이게 실화예요??!!",
    "sub_question_c.wav": "미쳤는데요 진짜??!!",
}
# 🛡️ [영어 sub_question은 이번 라운드 범위 밖] 영어 0단계 재설계(Sterling/Carter/Atlee)와
# 리드인 필러만 이번에 추가한다 - 2단계(Sub 의문형)의 영어 정적 풀은 후속 작업으로 남겨두고,
# 렌더 코드에서는 lang=="en"일 때 이 단계를 통째로 스킵한다(빈 풀을 억지로 채우지 않음).

# ══════════════════════════════════════════════════════════
#  영어 0단계 재설계(Sterling/Carter/Atlee 캐스케이드) + 리드인 필러
# ══════════════════════════════════════════════════════════
# 🛡️ [설계] 한국어 0단계는 Main/Hype/Sub 세 목소리가 kill_t에 완전 동시 시작(닉네임 없는
# 순수 감탄사 셋이 겹쳐 울리는 효과)이다. 영어판은 이 세 역할을 재사용하되 "완전 동시"
# 대신 기존 1~3단계에 쓰던 STAGE_OVERLAP_RATIO 캐스케이드 모델을 0단계 자체에 적용한다 -
# Sterling(MAIN_EXPLODE 역할 계승, 짧은 진행 멘트, kill_t에 끝나도록 역산 배치)
# -> Carter(HYPE_EXPLODE 역할 계승, kill_t에 시작하는 폭발 리액션)
# -> Atlee(SUB_EXPLODE 역할 계승, Carter 재생 65%(STAGE_OVERLAP_RATIO) 지점과 겹치며
# 시작하는 짧은 리액션). 아직 실제 녹음 전이라 아래 세 풀과 EN_LEADIN_POOL은 텍스트만
# 채워져 있고 glob 결과는 빈 리스트다 - _run_pipeline의 lang=="en" 분기는 파일이 없는
# 자리를 그냥 스킵하도록 짜여 있어서(한국어의 CRITICAL 하드-fail과 다름), 실제 파일이
# 채워지기 전까지는 영어 렌더가 0단계/리드인 없이 hype_nickname/main_fact(실시간 TTS라
# 언어와 무관하게 이미 동작)만으로 돌아가는 게 정상이다.
STERLING_POOL = sorted(glob.glob(os.path.join(VOICE_DIR, "sterling_*.wav")))
STERLING_TEXT = {
    "sterling_a.wav": "Here it comes—",
    "sterling_b.wav": "Watch this—",
    "sterling_c.wav": "This is it—",
    "sterling_d.wav": "Right here—",
}
CARTER_POOL = sorted(glob.glob(os.path.join(VOICE_DIR, "carter_*.wav")))
CARTER_TEXT = {
    "carter_a.wav": "OHHHHHH!!",
    "carter_b.wav": "WHOOOOAAA!!",
    "carter_c.wav": "YEEEAAAHHH!!",
    "carter_d.wav": "OHHHHH MY!!",
}
ATLEE_POOL = sorted(glob.glob(os.path.join(VOICE_DIR, "atlee_*.wav")))
ATLEE_TEXT = {
    "atlee_a.wav": "Oh my!!",
    "atlee_b.wav": "No way!!",
    "atlee_c.wav": "Unreal!!",
    "atlee_d.wav": "Wow!!",
}

# ── 영어 리드인 필러(한국어 pre_buildup+EOEO 2단계 고정 구조에 대응, 1~4개 유동 배치) ──
# 🛡️ [환각 위험 차단 원칙 동일 적용] PRE_BUILDUP_TEXT와 동일한 원칙 - 위치/챔피언/구체적
# 액션을 특정하지 않는 순수 분위기 문구만 채택, 어떤 클립에 붙어도 항상 사실일 수 있는
# 문장만 사용(게임 시각/스코어처럼 렌더 시점에 실제로 확정된 정보라도, 이 필러는 실시간
# TTS가 아닌 정적 풀이라 값을 문구에 끼워 넣지 못한다 - 동적으로 하려면 실시간 TTS 전환이
# 필요하며 이번 라운드 범위 밖).
EN_LEADIN_POOL = sorted(glob.glob(os.path.join(VOICE_DIR, "en_leadin_*.wav")))
EN_LEADIN_TEXT = {
    "en_leadin_a.wav": "Nice setup here.",
    "en_leadin_b.wav": "Feels tense right now.",
    # 🛡️ [텍스트 교체] 원래 "Something's brewing."였는데 Sterling 보이스에서 축약형->
    # "brewing" 전환부에 매번 같은 미세 무음 갭이 남아(16회 전부 실패) 같은 의미 계열 안에서
    # 문구를 바꿨다(1회 시도로 통과) - 이전 라운드의 "허"/"어" 음소 문제와 동일한 유형.
    "en_leadin_c.wav": "Tension is rising.",
    "en_leadin_d.wav": "Keep an eye on this.",
    "en_leadin_e.wav": "This could go either way.",
    "en_leadin_f.wav": "Here we go.",
}
EN_LEADIN_MIN_COUNT = 1  # 목표치(코드로 강제하진 않음 - 자리가 없으면 0개까지 줄어들 수 있음)
EN_LEADIN_MAX_COUNT = 4
EN_LEADIN_START_OFFSET_SEC = PRE_BUILDUP_START_OFFSET_SEC  # 재사용: 클립 시작 후 이만큼 뒤에 첫 필러 시작
EN_LEADIN_GAP_SEC = PRE_BUILDUP_GAP_SEC                    # 재사용: 필러 사이 간격
EN_LEADIN_END_GAP_SEC = EOEO_GAP_SEC                       # 재사용: 마지막 필러 종료~kill_t 최소 여백

# ── 3단계(Main 사실 전달, 실시간 TTS) ── - _generate_commentary/SYSTEM_PROMPT/
# _commentary_names_killer/_i_or_ga/_eul_or_reul 전부 그대로 재사용, 담당 목소리만
# Hype에서 Main으로 이동.

# 🛡️ [간격 규칙] 고정 초 오프셋 대신 "이전 단계에서 가장 늦게 끝나는 목소리 길이 × RATIO"로
# 다음 단계 시작을 잡는다 - 로컬 프로토타입 v5~v9에서 검증된 방식. "완전 동시(뭉개짐)도
# 완전 순차(지루함)도 아니게" 65%로 골랐다(짧은 문장 기준 사람이 듣기에 적당한 겹침이었음).
# 다만 이 방식은 이름 길이에 비례해서 간격도 같이 늘어난다 - 실측(v9)으로 15음절 닉네임에서
# 전체 길이가 3글자 닉네임 대비 +50%까지 늘어지는 걸 확인함. Riot 닉네임 자체를 제한할 수는
# 없어서(강제 불가) /highlight 명령어 설명에 안내 문구만 추가했고(이미 반영됨), 이 트레이드
# 오프 자체는 알려진 한계로 남겨둔다.
STAGE_OVERLAP_RATIO = 0.65
RENDER_TAIL_BUFFER_SEC = 0.8      # 마지막으로 끝나는 목소리 종료 후 여유

ELEVENLABS_VOICE_IDS = {
    "main": "tlUdVt24VftfDokp32eu",  # LCK_Main_caster
    "hype": "IyAj6lA2EjUlXLg33b1o",  # LCK_Hype_Reaction
    "sub": "K4OVml3awIZZxKC33zQV",   # Lck_Sub_Analyst
}
ELEVENLABS_MODEL_ID = "eleven_v3"
# 🛡️ [output_format 명시] 예전엔 지정을 아예 안 해서 API 기본값(mp3_44100_128)을 그대로 썼다.
# Creator 티어로 업그레이드하면서 192kbps가 열려 명시적으로 올렸다 - pcm_44100(무손실)은
# Pro 티어부터라 아직 못 쓴다. 받은 mp3를 바로 ffmpeg로 WAV 변환해서 믹싱하므로(아래
# _synthesize_voice_line), 128->192kbps는 그 변환 전 손실 압축 정도를 줄여주는 효과.
ELEVENLABS_OUTPUT_FORMAT = "mp3_44100_192"


def plan_kill_sequence(stage0_dur: float, stage1_dur: float, stage2_dur: float,
                        ratio: float = STAGE_OVERLAP_RATIO) -> dict:
    """0~3단계 타이밍 계획(순수 함수, 테스트 가능). kill_t를 기준(0)으로, 각 단계 시작을
    "직전 단계에서 가장 늦게 끝나는 목소리 길이 × ratio" 지점으로 잡는다 - 로컬 프로토타입
    v5~v9에서 검증된 방식 그대로. 반환값은 kill_t 기준 상대 오프셋(t1/t2/t3)이라, 호출부에서
    kill_t를 더해 절대 시각으로 바꿔 쓴다."""
    t1 = stage0_dur * ratio
    t2 = t1 + stage1_dur * ratio
    t3 = t2 + stage2_dur * ratio
    return {"t1": t1, "t2": t2, "t3": t3}


def plan_lead_in_forward(kill_t: float, pre_buildup_dur: float, eoeo_dur: float,
                          start_offset: float = PRE_BUILDUP_START_OFFSET_SEC,
                          mid_gap: float = PRE_BUILDUP_GAP_SEC,
                          end_gap: float = EOEO_GAP_SEC) -> tuple[float | None, float | None]:
    """상황 멘트 -> "어어??" 시작 시각(순수 함수, 테스트 가능) - kill_t 역산이 아니라 클립
    시작(t=0) 기준으로 앞에서부터 배치한다(상황 멘트는 start_offset부터, "어어??"는 상황
    멘트 종료+mid_gap부터). kill_t와 겹치지 않는지만 안전장치로 검사한다:
    - 상황 멘트조차 end_gap 여유를 두고 kill_t 전에 안 끝나면(비정상적으로 짧은 클립/이른
      킬) 둘 다 스킵(None, None).
    - 상황 멘트는 들어가는데 "어어??"가 kill_t와 겹치면 "어어??"만 스킵(pre_start, None) -
      상황 멘트 혼자라도 자연스럽게 재생된다.
    예전 빌드업1/2단계의 '자리 없으면 스킵' 패턴과 동일한 원칙(억지로 겹치게 밀어넣지
    않음)."""
    pre_start = start_offset
    if pre_start + pre_buildup_dur + end_gap > kill_t:
        return None, None
    eoeo_start = pre_start + pre_buildup_dur + mid_gap
    if eoeo_start + eoeo_dur + end_gap > kill_t:
        return pre_start, None
    return pre_start, eoeo_start


def plan_leadin_fillers_en(kill_t: float, durations: list[float],
                            start_offset: float = EN_LEADIN_START_OFFSET_SEC,
                            gap: float = EN_LEADIN_GAP_SEC,
                            end_gap: float = EN_LEADIN_END_GAP_SEC) -> list[float]:
    """영어 리드인 필러 N개(순서대로 durations)의 시작 시각 리스트(순수 함수, 테스트
    가능) - plan_lead_in_forward의 "클립 시작(t=0) 기준 앞에서부터 순차 배치, kill_t와
    안 겹치면 계속 채움" 원칙을 고정 2자리에서 임의 개수로 일반화한 버전. 채워 넣다가
    다음 필러가 kill_t와 겹치는 순간 멈추고 그때까지 들어간 만큼만 반환한다(durations
    보다 짧을 수 있고, 첫 필러조차 자리가 없으면 빈 리스트) - 억지로 겹치게 밀어넣지
    않는다는 기존 원칙 그대로."""
    starts: list[float] = []
    cursor = start_offset
    for dur in durations:
        if cursor + dur + end_gap > kill_t:
            break
        starts.append(cursor)
        cursor += dur + gap
    return starts


# ══════════════════════════════════════════════════════════
#  LCK 스타일 오버레이 UI (FIRST BLOOD / SOLO KILL HUD)
# ══════════════════════════════════════════════════════════
OVERLAY_DIR = os.path.join(REPO_ROOT, "assets", "highlight_overlay")
# 🛡️ [v4 - 완성 배너로 전면 교체, 구조 단순화] 여기까지는 사선 절삭 패널(geq 베이킹) +
# 이벤트 라벨 PNG(PIL, Archivo Black) + POWERED BY KYVOBOT/킬 카운트 drawtext를 각각 따로
# 합성하는 구조였는데, 사용자가 캔바에서 배경+텍스트+스폰서 문구가 전부 포함된 완성 배너를
# 이벤트별로 직접 만들어 왔다(panel_first_blood.png/panel_solokill.png, 1920x120, 완전
# 불투명). 이제 그 완성 배너 하나만 골라 overlay하면 끝이라 드로텍스트/폰트/색상 상수/경로
# 이스케이프 헬퍼가 전부 필요 없어졌다 - 이전 버전들의 사선 절삭·알파채널·좌우비대칭·폰트
# 이스케이프 관련 교훈은 이제 이 코드가 아니라 캔바에서 완성 이미지를 만들 때 사용자가 직접
# 처리하는 영역이 됐다.
HUD_BANNER_PNGS = {
    "FIRST BLOOD": os.path.join(OVERLAY_DIR, "panel_first_blood.png"),
    "SOLO KILL": os.path.join(OVERLAY_DIR, "panel_solokill.png"),
}
# 🛡️ [레이아웃 진화 - 2/3단계에서 쓰던 overlay_frame.png 완전 폐기, 4단계 기준]
# 2단계는 사용자가 캔바로 만든 완성 프레임(overlay_frame.png, 1920x1080, 알파 채널로 뚫린
# 투명 구멍)의 실측 밴드 경계를 그대로 따라가는 구조였고, 3단계는 그 프레임을 게임 위에
# 반투명으로 얹는 하이브리드였다. 이번 4단계(상단 2단+하단 포지션 매칭 5행)는 필요한
# 공간이 그 프레임보다 훨씬 커서(공간 계산 조사 결과) 프레임 자산 자체를 안 쓰기로 했다 -
# UI_BG_COLOR 단색 배경 + drawbox/drawtext로 직접 그린다("완성 그래픽" 원칙에서 벗어나는
# 부분, 보고서에 명시함). overlay_frame.png 파일 자체는 레포에 남아있지만(다음에 이 비율에
# 맞는 새 배경 에셋을 받으면 재활용 가능) 현재 렌더 경로에서는 참조하지 않는다.

# 🛡️ [Chakra Petch -> FontKR.otf로 교체 - 실측으로 발견] 처음엔 배너와 통일감을 주려고
# Chakra Petch(Bold/SemiBold)를 썼는데, 실제 ffmpeg drawtext 렌더 결과를 눈으로 확인해보니
# 닉네임/"타워"/"킬" 등 한글이 전부 빈 사각형(tofu)으로 나왔다 - Chakra Petch는 태국어+
# 라틴 문자만 지원하는 폰트라 한글 글리프가 없다(숫자는 라틴 문자라 정상 표시됨, 그래서
# 처음엔 눈치채기 어려웠음). 이미 `cogs/welcome.py`에 똑같은 교훈이 기록돼 있었다("Font.ttf
# (Roboto Bold)는 한글 글리프가 아예 없어서... FontKR.otf(Pretendard Bold)를 로드") - 그
# 폰트(레포 루트, 다른 cog들도 이미 씀)를 그대로 재사용한다. 스코어바/KDA 전용이라 굵기
# 구분 없이 하나만 쓴다.
SCOREBAR_FONT_KR = os.path.join(REPO_ROOT, "FontKR.otf")
# 🛡️ [하단 패널 가독성 개선 - Pretendard Black] FontKR.otf(Bold)로는 패널이 작아질수록
# (실측 row_h_raw가 30px 안팎) 획이 가늘어 보여서 배경 그라데이션과 잘 안 구분됐다.
# Pretendard 프로젝트(FontKR-OFL.txt로 이미 라이선스 커버됨, 같은 저작자)의 최고 굵기인
# Black 웨이트를 jsdelivr GitHub 미러(cdn.jsdelivr.net/gh/orioncactus/pretendard@main/...)에서
# 받아 FontKR-Black.otf로 저장 - name 테이블에서 "Pretendard Black"임을 직접 확인함.
# 하단 그리드(CS/KDA/레벨배지) 전용, 상단 스코어바는 기존 Bold 그대로 유지(이미 잘 보임).
SCOREBAR_FONT_KR_BLACK = os.path.join(REPO_ROOT, "FontKR-Black.otf")
TEAM_BLUE_COLOR = "#4C8BF5"
TEAM_RED_COLOR = "#F14C4C"

HUD_SLIDE_SEC = 0.4  # 배너 슬라이드업/다운 소요 시간 - PRE_BUILDUP_START_OFFSET_SEC과 같은 템포
# 🛡️ [배너 위치 - 이번에도 하단 전체 영역을 시간대로 나눠 씀] 4단계 재설계로 하단 영역이
# 헤더+5행(약 356px @1080)으로 훨씬 커졌지만, 배너는 여전히 원본 16:1 종횡비를 유지한 채
# 캔버스 폭에 맞춰 리사이즈하면 높이가 그 356px보다 훨씬 얇아서(캔버스 1920 기준 폭
# 1920이면 높이 120, 실제로는 그보다 좁게 잡음) 세로로 가운데 정렬된다 - 위아래 남는
# 공간은 그대로 빈 배경. 같은 하단 전체 영역(헤더+포지션 5행)을 배너가 뜨는 구간엔
# 통째로 가리는 방식을 그대로 유지한다(시간대로 나눠 쓰는 기존 전략 - 3~4초짜리 이벤트
# 구간만 잠깐 가리는 쪽이 공간을 나누는 것보다 가독성이 낫다는 게 계속 확인돼서 유지).

# 🛡️ [Data Dragon] Riot API 키/rate limiter와 완전히 무관한 별개의 정적 CDN이라 10명
# 전원의 아이콘을 받아도 Riot 쪽 호출 예산에는 전혀 영향이 없다(조사에서 확인된 그대로).
# championId는 Match-v5의 championName 필드를 그대로 쓴다.
DDRAGON_VERSIONS_URL = "https://ddragon.leagueoflegends.com/api/versions.json"
DDRAGON_ICON_URL_TEMPLATE = "https://ddragon.leagueoflegends.com/cdn/{version}/img/champion/{champion_id}.png"
# 🛡️ [아이템 아이콘] item_id=0(빈 슬롯)은 Data Dragon에 애초에 없는 파일이라 요청 자체를
# 안 보낸다(호출부에서 사전 필터링).
DDRAGON_ITEM_ICON_URL_TEMPLATE = "https://ddragon.leagueoflegends.com/cdn/{version}/img/item/{item_id}.png"
# 🛡️ [4단계 - 스펠/룬 아이콘, 재조사로 확인됨] 예전 조사에서 "룬은 Data Dragon에 없을 것"
# 이라 의심했는데, 이번에 실제 네트워크 호출로 재확인한 결과 둘 다 있었다:
#   - 소환사 스펠: summoner.json에서 숫자 key(예: "4")->파일명("SummonerFlash.png") 역매핑
#     후 cdn/{version}/img/spell/{파일명} - 다운로드 성공 확인
#   - 룬: runesReforged.json에서 숫자 id(예: 8112)->아이콘 경로 역매핑 후
#     **cdn/img/{경로}** (다른 아이콘들과 달리 버전 번호가 URL에 안 들어감 - 룬만의
#     특이사항, 실제 호출로 확인됨) - Match-v5 참가자의 perks.styles[0].selections[0].perk
#     가 키스톤 룬 id.
CHAMPION_ICON_CACHE_DIR = os.path.join(OVERLAY_DIR, "champion_icons_cache")
ITEM_ICON_CACHE_DIR = os.path.join(OVERLAY_DIR, "item_icons_cache")
DDRAGON_HTTP_TIMEOUT_SECONDS = 5.0

# 🛡️ [오브젝트 아이콘 - Community Dragon, 비공식 미러] Riot 공식 Data Dragon엔 없지만
# raw.communitydragon.org의 실제 게임 에셋 덤프에 있다(실제 200 응답+PNG 바이트 확인함) -
# 타워는 minimap/icons/, 드래곤은 scoreboard/ 아래에 있다는 게 이번에 새로 확인된 경로.
# Riot이 공식 지원하는 채널이 아니라 예고 없이 경로가 바뀌거나 사라질 수 있다는 게 알려진
# 리스크.
CDRAGON_TOWER_ICON_URL = "https://raw.communitydragon.org/latest/game/assets/ux/minimap/icons/tower.png"
CDRAGON_DRAGON_ICON_URL = "https://raw.communitydragon.org/latest/game/assets/ux/scoreboard/_dragon.png"
# 🛡️ [전령/바론/공허유충 아이콘 - 직접 HTTP HEAD로 200/image-png 확인한 경로만 사용]
# 전령은 scoreboard/(드래곤과 같은 디렉토리), 바론/공허유충(그럽)은 minimap/icons/(타워와
# 같은 디렉토리)에 있다 - 실제로 존재하는 파일명 조합만 골랐다(예: _horde.png는 404).
CDRAGON_RIFTHERALD_ICON_URL = "https://raw.communitydragon.org/latest/game/assets/ux/scoreboard/_riftherald.png"
CDRAGON_BARON_ICON_URL = "https://raw.communitydragon.org/latest/game/assets/ux/minimap/icons/baron.png"
CDRAGON_HORDE_ICON_URL = "https://raw.communitydragon.org/latest/game/assets/ux/minimap/icons/grub.png"
# 🛡️ [드래곤 속성별 아이콘 - Riot monsterSubType -> CDragon 파일명 매핑] 서로 다른 명명
# 체계를 쓴다(Riot=원소 이름 그대로, CDragon=신화적 이름) - HTTP HEAD로 7종 전부 200/
# image-png 확인함(minimap/icons/dragon_<name>.png 패턴, tower/baron과 같은 디렉토리).
DRAGON_SUBTYPE_TO_CDRAGON_NAME = {
    "FIRE_DRAGON": "infernal",
    "WATER_DRAGON": "ocean",
    "EARTH_DRAGON": "mountain",
    "AIR_DRAGON": "cloud",
    "CHEMTECH_DRAGON": "chemtech",
    "HEXTECH_DRAGON": "hextech",
    "ELDER_DRAGON": "elder",
}
CDRAGON_DRAGON_VARIANT_ICON_URL_TEMPLATE = (
    "https://raw.communitydragon.org/latest/game/assets/ux/minimap/icons/dragon_{name}.png"
)
DRAGON_SEQUENCE_MAX = 4  # 최근 4마리만 표시(그 이상이면 오래된 것부터 잘림)
STATIC_ICON_CACHE_DIR = os.path.join(OVERLAY_DIR, "static_icons_cache")

# 🛡️ [6단계 - 미리캔버스 완성 배경(overlay_frame_v2.png)으로 drawbox 전면 교체] 5단계는
# drawbox로 단색/틴트 배경을 직접 그렸는데("완성 그래픽" 원칙에서 벗어난다고 명시했던
# 부분), 이번에 사용자가 그 스펙 그대로 미리캔버스로 만든 완성 PNG를 받아서 이제 진짜
# 이미지 오버레이로 교체한다. PIL로 알파 채널을 픽셀 단위 재실측(여러 행에서 교차 검증,
# 자동탐지가 아니라 직접 다중 좌표 샘플링) - overlay_frame_v2.png 자체가 1920x1080
# 네이티브라 이 실측값도 전부 1920x1080 기준 비율로 저장한다(5단계까지는 2560x1435
# 참고 사진 기준이었음 - 에셋이 바뀌었으니 비율의 기준도 그 에셋 자신으로 바꿈).
#
# 실측값(1920x1080 네이티브, overlay_frame_v2.png):
#   - 메인바: x=0~1919(전체 폭), y=0~49(높이 50) - 완전 불투명(alpha=255). 좌측
#     (11,30,246)=밝은 블루 -> 우측(236,65,70)=빨강 그라데이션.
#   - 메인바~서브바 사이(y=50~53): alpha 223->202로 서서히 감소 - 의도된 드롭섀도우
#     효과(경계가 애매한 게 아니라 디자인 요소), 좌표 계산에는 영향 없음.
#   - 서브바: x=490~1429(폭 940, 좌우 완전 하드컷 - 여러 y에서 교차 검증), y=54~80
#     (높이 27, alpha=191 고정) - 팀 색상 구분 없는 무채색 단일 톤.
#   - 하단 패널: x=550~1369(폭 819~820), y=900~1079(높이 180, 캔버스 끝까지),
#     alpha=217 고정. 좌측(12,78,249)=밝은 블루 -> 우측(5,12,32)=어두운 네이비.
#   - team100(항상 좌측에 렌더링)과 이미지의 "밝은 블루" 쪽이 메인바/하단패널 둘 다에서
#     이미 일치함을 실측 RGB로 확인함(추가 반전 로직 불필요 - 뒤집으면 오히려 어긋남).
UI_BG_COLOR = "0x0A0B0E"  # 이제 배경 그리기엔 안 쓰이지만, 혹시 남은 보조 요소용으로 유지
OVERLAY_FRAME_V2_PATH = os.path.join(OVERLAY_DIR, "overlay_frame_v2.png")

# 상단 2단 바 - 메인바(전체 폭)+서브바(중앙 940px만) 치수, 실측값 그대로.
# 🛡️ [메인바 높이 2배 확장 - 실 렌더 판단용] 50/1080 -> 100/1080. overlay_frame_v2.png도
# 같이 수정해서 메인바 그라데이션을 y=0~99로 복제 확장하고, 서브바(원래 y=50~80, 상단
# 경계 블렌드 포함 31행)를 y=100~130으로 그대로 옮겨 다시 구웠다 - 서브바 코드는
# top_main_h를 참조해서 위치를 계산하므로 이 상수만 바꾸면 자동으로 따라온다.
TOP_MAIN_BAR_HEIGHT_RATIO = 100 / 1080
TOP_SUB_BAR_HEIGHT_RATIO = 27 / 1080
TOP_SUB_BAR_X_RATIO = (490 / 1920, 1429 / 1920)
# 🛡️ [메인바 폭 65%로 축소 - 서브바와 완전히 독립된 별개 변수] 메인바를 화면 전체 폭이
# 아니라 중앙 65%짜리 좁은 바로 좁힌다. 서브바는 이미 자기만의 폭(TOP_SUB_BAR_X_RATIO,
# 48.8%)을 쓰고 있고 이번 변경과 전혀 무관 - bar_x0/x1/bar_mid_x/bar_half_w는 렌더
# 함수 안에서 이 비율로 새로 계산하는 독립 변수이고, 서브바가 쓰는 mid_x/half_w는
# 그대로 final_width 기준을 유지한다(지난 panel_mid_x 분리와 동일한 패턴).
MAIN_BAR_WIDTH_RATIO = 0.65

# 하단 통계 패널 - 실측 좌표 그대로, 중앙 정렬. 헤더 띠는 여전히 범위 밖(5단계와 동일하게
# 180px 전체를 5행에만 씀).
BOTTOM_PANEL_WIDTH_RATIO = 819 / 1920
BOTTOM_PANEL_Y_START_RATIO = 900 / 1080
BOTTOM_PANEL_HEIGHT_RATIO = 180 / 1080
BOTTOM_ROWS = 5
POSITION_ORDER = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]

# 🛡️ [침범 금지 구역 - 검증용] 실측된 좌/우 침범 금지 구역. 하단 패널은 항상 중앙
# 정렬이라 구조적으로 이 두 구역과 안 겹치지만(패널 폭 42.7% < 가용 공간), 렌더 후
# 실제로 안 겹치는지 검증 스크립트에서 이 값으로 재확인한다.
LEFT_CARD_ZONE_WIDTH_RATIO = 170 / 2560
RIGHT_MINIMAP_X_START_RATIO = 2100 / 2560

# 🛡️ [킬 배너 폭 - 하단 패널 박스 폭과 일치] 안전지대 전체(1447px)까지 키웠더니, 그
# 뒤에 항상 그려지는 하단 통계 패널의 검은 배경 박스(BOTTOM_PANEL_WIDTH_RATIO, 819px -
# 화면 중앙 정렬)가 배너보다 좁아서 배너 위쪽에 어울리지 않게 좁은 검은 박스가 삐져나와
# 보였다(실측/스크린샷으로 확인). 패널 배경 자체를 시간대별로 안 그리게 하는 방법도
# 있었지만, 그건 단일 overlay_frame_v2.png 오버레이를 지역별로 쪼개서 조건부 enable=을
# 새로 넣어야 하는 구조 변경이라 더 복잡하다 - 반면 배너 폭을 패널 박스 폭과 똑같이
# 맞추면 배너가 패널을 완전히 덮어서 검은 박스가 원천적으로 안 보인다. 훨씬 간단해서
# 이 방식을 택했다. x_start는 더 이상 고정 상수가 아니라 render 시점에 안전지대
# (카드존 끝~미니맵존 시작) 안에서 실제 banner_width 기준으로 중앙 정렬 계산한다(아래
# _render_video 참고) - 폭이 좁아진 지금은 안전지대 안에 넉넉한 여유를 두고 중앙에 온다.
HUD_BANNER_MAX_WIDTH_RATIO = BOTTOM_PANEL_WIDTH_RATIO

ROSTER_DIVIDER_COLOR = "white@0.2"
ROSTER_SHADOW_COLOR = "black@0.7"
# 🛡️ [아이템 슬롯 틀 - 빈 칸도 항상 표시] 기존 ROSTER_DIVIDER_COLOR(white@0.2, t=2)는
# 배경 그라데이션 위에서 너무 옅어서 빈 슬롯인지 그냥 배경인지 구분이 잘 안 됐다 - 어두운
# 보라 계열로 바꾸고 두께도 1px로 줄인다(요청 스펙 그대로).
ITEM_SLOT_BORDER_COLOR = "#2D274D"
# 🛡️ [챔피언 프레임 색 - 보라 vs 금색 중 금색 선택] 패널 배경 자체가 블루/네이비 계열이라
# 보라 테두리는 배경과 명도가 비슷해 묻힌다. LoL 클라이언트가 소환사 아이콘/룬 테두리에
# 표준으로 쓰는 골드(#C89B3C 계열)가 어두운 배경 위에서 확실히 도드라지고, 롤 유저에게
# 이미 익숙한 "강조 테두리" 색이라 이걸로 선택.
CHAMPION_FRAME_COLOR = "#C89B3C"
GRID_TEXT_BORDER_COLOR = "black"
# 🛡️ [테두리 완전 제거 - 0px] 2px->4px로 키웠다가, 실제 매치 데이터로 0px/1px/4px를
# 나란히 비교 렌더해서 직접 판단한 결과 0px(테두리 없음)가 실제 LCK 느낌에 가장
# 가깝다고 확인됨 - 배경(어두운 남색)과 텍스트 색(CS=크림노랑, KDA=흰색) 대비만으로
# 충분히 구분되니 borderw=0으로 되돌린다. bordercolor=/borderw= 옵션 자체는 그대로 두고
# (drawtext에서 borderw=0은 시각적으로 "테두리 없음"과 동일 - 굳이 옵션을 다 빼는 것보다
# 값 하나만 바꾸는 쪽이 더 간단해서 이 방식을 택함) 값만 0으로.
GRID_TEXT_BORDER_W = 0
# 🛡️ [CS 텍스트 색상 분리] KDA와 똑같이 "white" 리터럴을 그대로 복붙해뒀던 걸 CS만
# 별도 상수로 분리 - KDA(fontcolor=white, 그대로 유지)와 서로 독립적으로 바꿀 수 있음을
# 명시적으로 보여준다. 옅은 크림빛 노랑으로 CS와 KDA를 시각적으로 구분.
CS_TEXT_COLOR = "#FFE9A8"

# 🛡️ [골드리드 화살표 배지 가시성 - 포트레이트 중앙 갭 확장, 공유 pad는 안 건드림]
# 이전 라운드는 배지 폭을 정확히 2*pad(당시 4px)로 제한해서 초상화를 절대 못 덮게
# 했는데, 그 결과 배지가 초고배율로 확대해야만 보일 만큼 작았다. 이번엔 공유
# pad(다른 모든 내부 여백에 쓰이는 상수)는 그대로 두고, 포트레이트 위치에만 독립
# 오프셋을 더해서 중앙 갭만 넓힌다 - 조사에서 확인된 대로, 이 오프셋만큼 cs_zone_x1/
# x0도 같이 밀어줘야 포트레이트-CS 텍스트 사이 여백(pad)이 유지된다(안 밀면 포트레이트가
# CS 숫자를 침범함).
# 🛡️ [숫자 배지 추가 - 오프셋 36으로 재확장] 화살표만 있던 배지에 골드 격차 실제 숫자
# ("+2778"/"+9.9k")를 옆에 추가하면서, FontKR-Black.otf 실측 기준 최악값("+9999",
# fontsize=12) 폭이 40px이라 기존 6px 오프셋(중앙 갭 16px)로는 절대 안 들어간다 -
# 36px로 늘려서 중앙 갭을 76px까지 확보한다(조사에서 계산된 필요폭 ~60px + 여유).
PORTRAIT_GAP_EXTRA_OFFSET = 36
# 화살표 자체를 담는 작은 배경 박스는 그대로 16px 유지(요청대로 화살표 모양/색 배지는 안
# 건드림) - 숫자는 이 박스 옆에 배경 없이 팀 색상 텍스트로만 추가한다(박스를 숫자까지
# 늘리면 같은 색 배경 위에 같은 색 텍스트라 안 보이게 됨 - 그래서 숫자는 박스 밖).
GOLD_GAP_ARROW_BADGE_W = 16
GOLD_GAP_ARROW_FONT_SIZE = 10
# 🛡️ [숫자 텍스트 - 화살표와 별개 크기] 화살표 글리프(10px)와 완전히 같을 필요 없다는
# 요청대로 12px로 분리.
GOLD_GAP_NUMBER_FONT_SIZE = 12
# 🛡️ [클러스터 고정폭 방식 폐기] 화살표를 포트레이트 가장자리에 붙이고 숫자를 mid_x에
# 항상 중앙 고정하는 방식으로 바뀌면서, 예전에 "화살표+간격+숫자"를 하나의 고정폭
# 덩어리로 취급해 가운데 정렬하던 GOLD_GAP_CLUSTER_W/GOLD_GAP_INNER_PAD는 더 이상
# 어디서도 안 쓰인다(각각 mid_x/포트레이트 기준 독립 좌표로 대체됨) - 완전히 제거.


def _hud_slide_y_expr(start: float, end: float, slide: float, visible_y: str, hidden_y: str) -> str:
    """LowerThirdBanner의 슬라이드업/다운 y좌표 계산(순수 함수, 테스트 가능) - overlay/drawtext의
    y= 표현식에 그대로 쓸 문자열을 만든다. t가 [start, start+slide) 구간이면 hidden_y에서
    visible_y로 선형 보간(슬라이드업), [start+slide, end) 구간이면 visible_y 고정, [end, end+slide)
    구간이면 visible_y에서 hidden_y로 선형 보간(슬라이드다운), 그 외엔 hidden_y. visible_y/hidden_y는
    ffmpeg 표현식 문자열이라 "H-160"처럼 overlay가 제공하는 심볼(H=영상 높이)을 포함해도 된다 -
    실제 픽셀 값은 렌더 시점에 ffmpeg가 계산하므로 여기선 문자열 조립만 한다."""
    return (
        f"if(lt(t,{start:.3f}),({hidden_y}),"
        f"if(lt(t,{start + slide:.3f}),({hidden_y})+(t-{start:.3f})/{slide}*(({visible_y})-({hidden_y})),"
        f"if(lt(t,{end:.3f}),({visible_y}),"
        f"if(lt(t,{end + slide:.3f}),({visible_y})+(t-{end:.3f})/{slide}*(({hidden_y})-({visible_y})),"
        f"({hidden_y})))))"
    )


def _escape_ffmpeg_path(path: str) -> str:
    """drawtext fontfile= 등 -filter_complex 옵션 값에 넣는 절대경로 이스케이프(순수 함수,
    테스트 가능). 🛡️ [Windows 드라이브 콜론 - README에 기록된 과거 교훈 재활용] "C:"의
    콜론이 필터 옵션 구분자(:)와 충돌해서 슬래시로 통일 + 콜론을 이중 백슬래시로
    이스케이프해야 한다("C\\:/..."). 리눅스 배포 경로엔 콜론이 없어 이 replace가 안전하게
    아무 효과도 없다(윈도우 로컬 개발 전용 이슈)."""
    return path.replace("\\", "/").replace(":", "\\:")


def _escape_drawtext_text(text: str) -> str:
    """drawtext text= 값 안에 들어갈 동적 문자열(닉네임 등) 이스케이프(순수 함수). ffmpeg
    drawtext는 텍스트 안의 ':'(옵션 구분자)/'\\'(이스케이프 시작)/'%'(strftime 등 확장
    문법 시작)를 그대로 두면 필터 문법이 깨진다 - 백슬래시로 이스케이프. 홑따옴표는
    필터 전체를 감싸는 '...' 자체를 깨뜨려서 백슬래시 이스케이프가 안 통하므로, 시각적으로
    거의 동일한 유니코드 오른쪽 홑인용부호(’)로 치환한다."""
    return (text.replace("\\", "\\\\").replace(":", "\\:")
                .replace("'", "’").replace("%", "\\%"))


def _mmss_to_ms(mmss: str) -> int:
    # 🛡️ [엄격 파싱] GPT-4o-mini 비전 OCR(_read_clock)은 "MM:SS 형식으로만 답해"라고
    # 프롬프트로 지시하지만, 응답 자체를 강제하는 장치가 없어서 거부/설명문/여분의 텍스트가
    # 섞여 나올 가능성이 있다. 예전엔 split(":") + int()에만 의존했는데, 이건 우연히
    # "그럴듯하게 숫자로 파싱되는" 응답을 조용히 통과시킬 여지가 있었다(예: 콜론이 여러 개
    # 섞인 설명문 일부가 우연히 두 숫자로 쪼개지는 경우) - 정규식으로 "숫자:숫자" 형태만
    # 엄격하게 허용하고, 그 외엔 전부 예외를 던져 호출부의 실패 처리(재시도 -> 그래도 실패
    # 시 highlight_err_clock_read_failed)로 넘어가게 한다.
    match = re.fullmatch(r"(\d{1,3}):(\d{2})", mmss.strip())
    if not match:
        raise ValueError(f"MM:SS 형식이 아님: {mmss!r}")
    m, s = int(match.group(1)), int(match.group(2))
    if s > 59:
        # 초 자리가 2자리 숫자라는 것만으론 "60~99초" 같은 물리적으로 불가능한 값을 못
        # 걸러낸다(예: "05:99") - 진짜 시계라면 절대 나올 수 없는 값이라 형식 오류로 취급.
        raise ValueError(f"초가 60 이상이라 유효한 시계 값이 아님: {mmss!r}")
    return (m * 60 + s) * 1000


def _ms_to_mmss(ms: int) -> str:
    """_mmss_to_ms의 역변환(순수 함수) - 코멘터리 컨텍스트 블록에 게임 시각을 사람이 읽는
    MM:SS 형식으로 넣기 위한 용도."""
    total_sec = ms // 1000
    return f"{total_sec // 60}:{total_sec % 60:02d}"


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


# 🛡️ [비용 예측 가능성] 0~3단계 킬 리액션 시퀀스(위 plan_kill_sequence)+실시간 TTS 2회는 킬
# 1건당 비용이 고정이라, 렌더당 비용을 예측 가능하게 만들려면 클립당 킬 개수 자체를 상한 걸어야 한다.
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
            # 🛡️ [스코어바/KDA 패널용] team_id/kda는 참가자 응답에 이미 있던 필드를 그대로
            # 추가한 것뿐 - 조사에서 확인된 대로 추가 API 호출 없음.
            "team_id": p.get("teamId"),
            "kda": (p.get("kills", 0), p.get("deaths", 0), p.get("assists", 0)),
        }
    return mapping


def _extract_bans(match_detail: dict) -> dict[int, list[int]]:
    """teamId(100/200) -> 실제로 밴된 챔피언 id(숫자) 리스트. match_detail은 이미 매
    렌더마다 fetch하는 응답이라 추가 API 호출이 필요 없다 - 조사에서 확인된 그대로.
    championId=-1(솔로랭크에서 밴 시간 안에 못 고른 "밴 없음" 슬롯 - 실제 매치
    KR_8374071995에서 2건 실측 확인됨)은 걸러낸다 - 그대로 두면 존재하지 않는
    챔피언 아이콘을 찾으려다 실패하게 된다."""
    result = {}
    for t in match_detail["info"]["teams"]:
        result[t["teamId"]] = [b["championId"] for b in t.get("bans", []) if b.get("championId", -1) != -1]
    return result


def _reconstruct_kill_snapshot(timeline: dict, participant_id: int, at_ms: int) -> dict:
    """주어진 participant의 at_ms 시점 기준 아이템 목록/레벨을 타임라인 이벤트 스트림을
    처음부터 재생해서 정확하게 재구성한다. 🛡️ [조사에서 확인된 배경] participantFrames는
    60초 간격 스냅샷이라 킬 시점과 최대 ±59초 오차가 난다(실측: 97.6초 킬에 가장 가까운
    프레임이 120초 - 22초 차이) - 반면 ITEM_PURCHASED/ITEM_SOLD/ITEM_UNDO/LEVEL_UP은
    각각 정확한 타임스탬프를 갖고 있어서(전부 실제 응답에서 필드 확인됨) 이벤트를 순서대로
    재생하면 임의 시점의 정확한 상태를 만들 수 있다. 골드는 초당 자동 증가분까지 섞여있어
    이벤트만으론 정밀 재구성이 어려워 이번 범위에서 제외(조사에서 이미 확인된 한계).
    아이템은 Match-v5 타임라인에 슬롯 번호가 없어 "현재 보유 중인 아이템 id 리스트"로만
    추적한다(순서/슬롯 위치 정보 없음 - UI에서 순서대로 나열하면 됨)."""
    items: list[int] = []
    level = 1
    for frame in timeline["info"]["frames"]:
        for ev in frame.get("events", []):
            if ev.get("timestamp", 0) > at_ms:
                return {"items": items, "level": level}
            if ev.get("participantId") != participant_id:
                continue
            ev_type = ev.get("type")
            if ev_type == "ITEM_PURCHASED":
                items.append(ev["itemId"])
            elif ev_type == "ITEM_SOLD":
                if ev["itemId"] in items:
                    items.remove(ev["itemId"])
            elif ev_type == "ITEM_UNDO":
                # 🛡️ 실제 응답 필드 확인(ITEM_UNDO 샘플: beforeId=1036, afterId=0,
                # goldGain=350) - beforeId(취소 전 아이템)를 제거하고, afterId가 0이
                # 아니면(다른 아이템으로 교체된 취소) 그걸 대신 추가한다.
                before_id = ev.get("beforeId", 0)
                after_id = ev.get("afterId", 0)
                if before_id and before_id in items:
                    items.remove(before_id)
                if after_id:
                    items.append(after_id)
            elif ev_type == "LEVEL_UP":
                level = ev.get("level", level)
    return {"items": items, "level": level}


def _pair_roster_by_position(team_a: list[dict], team_b: list[dict]) -> list[tuple[dict | None, dict | None]]:
    """포지션(teamPosition) 기준으로 두 팀 참가자를 1:1로 매칭한다(순수 함수, 테스트
    가능). POSITION_ORDER(TOP/JUNGLE/MIDDLE/BOTTOM/UTILITY) 순서로 정렬해서 반환.
    🛡️ [폴백 - 알려진 한계] 이건 드래프트/랭크 계열 큐에서만 신뢰할 수 있다 - ARAM 등
    비드래프트 모드는 teamPosition이 비어있거나 다 같은 값(중복)일 수 있어서, 양쪽 다
    5개 표준 포지션이 정확히 한 번씩 있는 경우에만 포지션 매칭을 쓰고, 그렇지 않으면
    participantId 순서로 그냥 순서대로 짝짓는다(둘 중 하나라도 인원이 다르면 짧은 쪽은
    None으로 채움)."""
    a_by_pos = {p.get("position"): p for p in team_a}
    b_by_pos = {p.get("position"): p for p in team_b}
    positions_ok = (
        len(a_by_pos) == len(team_a) and len(b_by_pos) == len(team_b)
        and set(a_by_pos) == set(b_by_pos) == set(POSITION_ORDER)
    )
    if positions_ok:
        return [(a_by_pos[pos], b_by_pos[pos]) for pos in POSITION_ORDER]

    a_sorted = sorted(team_a, key=lambda p: p["participant_id"])
    b_sorted = sorted(team_b, key=lambda p: p["participant_id"])
    n = max(len(a_sorted), len(b_sorted))
    return [(a_sorted[i] if i < len(a_sorted) else None,
             b_sorted[i] if i < len(b_sorted) else None) for i in range(n)]


# 🛡️ [라인전 골드 격차 - "라인전 종료 시점" 정의] participantFrames는 60초 간격
# 스냅샷이라(조사에서 실측 확인: frameInterval=60000) 특정 순간을 정확히 짚을 수 없다 -
# 10~14분 구간 안에서 가장 가까운 스냅샷을 쓰기로 했으므로(±30초 오차는 감수), 그 구간의
# 중간값인 12분을 목표 시각으로 잡고 구간 내 프레임 중 거리로 가장 가까운 걸 고른다.
LANING_PHASE_WINDOW_MS = (10 * 60 * 1000, 14 * 60 * 1000)
LANING_PHASE_TARGET_MS = 12 * 60 * 1000


def _pick_laning_phase_frame(timeline: dict) -> dict | None:
    """10~14분 구간 안에서 12분에 가장 가까운 timeline 프레임 하나를 고른다. 그 구간에
    프레임이 하나도 없으면(10분 전에 끝난 리메이크 등 극단적으로 짧은 게임) None을
    반환한다 - 이 경우 호출부에서 라인전 격차 배지 자체를 표시하지 않는다(부정확한 값을
    억지로 만들어내지 않음)."""
    lo, hi = LANING_PHASE_WINDOW_MS
    candidates = [f for f in timeline["info"]["frames"] if lo <= f["timestamp"] <= hi]
    if not candidates:
        return None
    return min(candidates, key=lambda f: abs(f["timestamp"] - LANING_PHASE_TARGET_MS))


def _compute_laning_gold_gaps(timeline: dict, roster_pairs: list[tuple[dict | None, dict | None]]) -> list[int | None]:
    """roster_pairs(포지션별 (좌측=team100, 우측=team200) 쌍)와 같은 순서로, 라인전
    스냅샷 시점의 (좌측 totalGold - 우측 totalGold)를 반환한다(양수=좌측/블루 리드,
    음수=우측/레드 리드). 스냅샷을 못 찾거나 한쪽 참가자가 없으면 그 자리는 None(순수
    함수 - 실제 매치 데이터로 독립 재계산해서 검증 가능)."""
    frame = _pick_laning_phase_frame(timeline)
    if frame is None:
        return [None] * len(roster_pairs)
    pframes = frame["participantFrames"]
    gaps: list[int | None] = []
    for left_p, right_p in roster_pairs:
        if left_p is None or right_p is None:
            gaps.append(None)
            continue
        left_gold = pframes.get(str(left_p["participant_id"]), {}).get("totalGold")
        right_gold = pframes.get(str(right_p["participant_id"]), {}).get("totalGold")
        gaps.append(left_gold - right_gold if left_gold is not None and right_gold is not None else None)
    return gaps


def _format_match_context_block(roster_pairs: list[tuple[dict | None, dict | None]],
                                 laning_gold_gaps: list[int | None], scoreboard: dict, lang: str) -> str:
    """🛡️ [코멘터리 데이터 확장 - 킬 사실 외 참고 컨텍스트] _generate_commentary가 GPT에
    넘기는 facts_block에 이미 계산된 roster(KDA/CS/아이템)/scoreboard(팀 골드/오브젝트)/
    laning_gold_gaps를 그대로 문자열로 직렬화한다(순수 함수, 새 API 호출 없음 - 전부
    _run_pipeline에서 HUD 오버레이용으로 이미 만들어둔 값 재사용). 아이템은 Data Dragon
    이름 매핑이 없어 원본 참가자 응답의 숫자 ID뿐이라, ID를 그대로 넘기면 GPT가 아이템
    이름을 추측해서 지어낼 위험이 있다(이번 세션 내내 확인한 "닫힌 목록 밖 이름 지어내기"
    실패 패턴과 동일한 종류) - 그래서 ID 대신 "완성 아이템 개수"(0이 아닌 슬롯 수)만
    넘긴다. 킬러/피해자 조사 처리(_i_or_ga/_eul_or_reul)와 달리 이 블록은 표/목록 형태라
    한국어여도 조사가 거의 안 붙으므로, 언어별로 라벨 문자열만 바꾼다."""
    is_en = lang == "en"

    def team_line(prefix: str) -> str:
        obj = (
            f"{scoreboard[f'{prefix}_kills']}K/{scoreboard[f'{prefix}_towers']}T/"
            f"{scoreboard[f'{prefix}_dragons']}D/{scoreboard[f'{prefix}_barons']}B/"
            f"{scoreboard[f'{prefix}_riftheralds']}RH/{scoreboard[f'{prefix}_hordes']}VG"
        )
        return f"{obj}, {'gold' if is_en else '골드'} {scoreboard[f'{prefix}_gold']}"

    gold_diff = scoreboard["team100_gold"] - scoreboard["team200_gold"]
    if is_en:
        lines = [
            f"Game time: {_ms_to_mmss(scoreboard['game_time_ms'])}",
            f"Blue team: {team_line('team100')}",
            f"Red team: {team_line('team200')}",
            f"Gold gap: Blue {'leads' if gold_diff >= 0 else 'trails'} by {abs(gold_diff)}",
            "Roster by position (Blue vs Red - champion/summoner, K/D/A, CS, completed items, lane gold gap):",
        ]
    else:
        lines = [
            f"게임 시각: {_ms_to_mmss(scoreboard['game_time_ms'])}",
            f"블루팀: {team_line('team100')}",
            f"레드팀: {team_line('team200')}",
            f"골드 격차: 블루가 {abs(gold_diff)} {'앞섬' if gold_diff >= 0 else '뒤짐'}",
            "포지션별 로스터(블루 vs 레드 - 챔피언/소환사명, K/D/A, CS, 완성 아이템, 라인 골드 격차):",
        ]

    for i, (left, right) in enumerate(roster_pairs):
        gap = laning_gold_gaps[i] if i < len(laning_gold_gaps) else None
        pos = POSITION_ORDER[i] if i < len(POSITION_ORDER) else str(i)

        def side(p: dict | None) -> str:
            if p is None:
                return "N/A"
            k, d, a = p["kda"]
            item_count = sum(1 for it in p["items"] if it)
            return f"{p['champion']}({p['name']}) {k}/{d}/{a}, CS {p['cs']}, {'items' if is_en else '아이템'} {item_count}/6"

        gap_str = "N/A" if gap is None else f"{'+' if gap >= 0 else ''}{gap}"
        lines.append(f"  [{pos}] {side(left)}  vs  {side(right)}  ({'lane gold gap' if is_en else '라인 골드 격차'} {gap_str})")

    return "\n".join(lines)


def _extract_dragon_sequence(timeline: dict, team_id: int, limit: int = DRAGON_SEQUENCE_MAX) -> list[str]:
    """timeline의 frames[].events[]에서 monsterType=="DRAGON"이고 killerTeamId==team_id인
    이벤트를 시간순(frames 자체가 이미 시간순이라 재정렬 불필요)으로 뽑아 monsterSubType
    리스트로 반환한다(순수 함수). 같은 속성이 중복 등장할 수 있다(예: WATER_DRAGON이 두
    번). limit개를 넘으면 가장 오래된 것부터 잘라내고 최근 것만 남긴다."""
    subtypes = [
        e.get("monsterSubType")
        for fr in timeline["info"]["frames"]
        for e in fr.get("events", [])
        if e.get("type") == "ELITE_MONSTER_KILL" and e.get("monsterType") == "DRAGON"
        and e.get("killerTeamId") == team_id
    ]
    return subtypes[-limit:] if limit else subtypes


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


# 🛡️ [역할 이동] "감탄사+닉네임 외침"은 0단계(3인 동시 폭발)+1단계(Hype 닉네임 샤우팅)가
# 전담하므로, 이 문장(0~3단계 재설계 이후 3단계 Main 담당 - 예전엔 2단계 Hype였다가 이번에
# 다시 옮겨짐)은 순수하게 "누가 누구를 처치했는지"를 서술하는 역할만 맡는다 - 그래서
# "감탄사+이름으로 시작하라"는 구조 규칙 없이 사실 서술에만 집중한다.
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
    "- 아래 '확정된 사실 목록'에는 킬 이벤트 외에도 게임 시각/팀별 스코어(킬/타워/드래곤/바론/전령/"
    "공허유충)/골드 격차/포지션별 KDA·CS·완성 아이템 개수/라인 골드 격차 같은 참고 정보가 같이 "
    "주어질 수 있다. 이 참고 정보는 '이미 확정된 사실'이라 코멘터리에 자연스럽게 녹여도 되지만, "
    "그 정보를 근거로 킬 원인/사용 스킬/구체적 위치/상황을 추측해서 지어내는 건 여전히 절대 "
    "금지다 - 목록에 없는 내용(킬 원인, 사용 스킬, 위치, 상황 추측 등)은 절대 지어내지 마라. "
    "텐션은 말투에만 얹고, 누가 누구를 처치했는지의 사실관계는 목록 그대로 유지해라.\n"
    "- 목록에 있는 킬 이벤트는 하나도 빠짐없이 전부 다뤄야 한다. 목록에 event_index가 N개면 "
    "반드시 N개의 줄을 만들어라. 하나라도 건너뛰지 마라.\n\n"
    "반드시 아래 JSON 스키마로만 답해, 다른 텍스트는 절대 포함하지 마: "
    '{"lines": [{"event_index": int, "text": "자막 한 줄"}]}'
)

# 🛡️ [영어 버전 - 구조/규칙은 한국어와 동일, 문장만 영어] 존댓말/조사 같은 한국어 전용 규칙은
# 빼고, 그 자리에 영어 캐스터 톤(짧고 임팩트 있는 현재형/느낌표 위주) 규칙을 넣었다. "목록에
# 없는 내용은 절대 지어내지 마라" 원칙과 JSON 스키마는 한국어판과 완전히 동일하게 유지.
EN_SYSTEM_PROMPT = (
    "You are a high-energy English esports caster covering an LCK-style highlight reel. "
    "For each kill event in the 'confirmed facts list' below, write one caster line of "
    "play-by-play commentary that comes right after the crowd/hype reaction has already hit "
    "(another voice already shouted the killer's name, so your job here is to clearly state "
    "who killed whom).\n\n"
    "Tone rules (never violate):\n"
    "- Present tense, high energy, like a live broadcast (e.g. 'takes it down!', 'shuts them "
    "out!!').\n"
    "- Use exclamatory phrasing freely (e.g. 'Unbelievable!?', 'How did they land that?!').\n"
    "- Keep sentences short and punchy - one clause, two at most.\n"
    "- Use exclamation points and keep the tension high throughout. Flat, exclamation-free "
    "statements of fact (e.g. 'X killed Y.') are forbidden.\n\n"
    "Structure rules:\n"
    "- Make one sentence that clearly states who killed whom (e.g. '{killer} absolutely ends "
    "{victim}!!').\n\n"
    "Factual rules (never violate):\n"
    "- The 'confirmed facts list' below may include, besides kill events, reference info like "
    "game time / team score (kills/towers/dragons/barons/rift heralds/voidgrubs) / gold gap / "
    "per-position KDA, CS, completed-item count / lane gold gap. This reference info is already "
    "confirmed fact and may be woven into the commentary naturally, but you must still never "
    "invent a kill cause, ability used, specific location, or situational guess from it - never "
    "invent anything not in the list (kill cause, ability used, location, situational guesses, "
    "etc). Keep the tension in the delivery only; the facts of who killed whom must match the "
    "list exactly.\n"
    "- You must cover every kill event in the list, with nothing skipped. If the list has N "
    "event_index entries, you must produce exactly N lines.\n\n"
    "Respond ONLY in the following JSON schema, no other text: "
    '{"lines": [{"event_index": int, "text": "one caption line"}]}'
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

    @staticmethod
    def _apply_nickname_swell(wav_path: str, duration: float, out_path: str) -> None:
        """1단계 Hype 닉네임 샤우팅의 뒷부분(물결표 여운 구간으로 추정되는 지점)에만 볼륨을
        서서히 키워 "길게 끄는 느낌"을 더한다 - 이름 음절 자체(앞쪽)는 안 건드림. 로컬
        프로토타입 v8에서 검증된 값(시작점=길이의 55%, 최대 +60%) 그대로. 지속시간은 그대로
        유지되고(순수 볼륨 오토메이션) 음량만 바뀐다.
        🛡️ volume=eval=frame을 프레임 단위로 그대로 쓰면 프레임 경계마다 계단식 클릭 노이즈가
        남는다는 게 이 세션 초반(crowd_cheer_4.wav 빌드)에 스펙트로그램으로 실측 확인된 교훈 -
        asetnsamples로 프레임을 잘게(64샘플) 쪼개 계단을 사람 귀에 안 들릴 만큼 작게 만드는
        동일 기법을 재사용한다."""
        start_t = max(duration * NICKNAME_SWELL_START_RATIO, 0.01)
        span = max(duration - start_t, 0.05)
        vol_expr = f"if(gte(t,{start_t:.3f}),1+{NICKNAME_SWELL_RISE}*(t-{start_t:.3f})/{span:.3f},1)"
        subprocess.run(
            [FFMPEG_EXE, "-y", "-i", wav_path, "-af",
             f"asetnsamples=n=64:p=0,volume=eval=frame:volume='{vol_expr}'",
             out_path],
            capture_output=True, check=True,
        )

    def _render_video(self, video_path: str, video_duration: float, video_width: int,
                       video_height: int, schedule: dict, work_dir: str, out_mp4: str) -> str:
        """schedule = {"total_duration", "kill_t", <voice_key>...} - <voice_key>는
        pre_buildup(상황 멘트)/eoeo("어어??") (둘 다 킬 이전 리드인, 자리 없으면 없을 수도
        있음)/main_explode/hype_explode/sub_explode(0단계)/hype_nickname(1단계)/
        sub_question(2단계)/main_fact(3단계) 중 실제로 쓰인 것만 있고, 각 엔트리는
        {"wav","text","start","duration"}. 타이밍 자체는
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
        for key in ("pre_buildup", "eoeo", "main_explode", "hype_explode", "sub_explode", "hype_nickname", "sub_question", "main_fact"):
            entry = schedule.get(key)
            if entry is None:
                continue
            inputs += ["-i", entry["wav"]]
            voice_indices[key] = next_input_idx
            next_input_idx += 1

        # 🛡️ [6단계 - 배경 프레임 입력 - 항상 추가] overlay_frame_v2.png(상단 메인바+
        # 서브바+하단 패널이 통합된 미리캔버스 완성 이미지)는 매 렌더마다 항상 붙는다 -
        # 이걸로 예전 drawbox 배경(팀 틴트/서브바/하단패널 단색 박스)을 전부 대체한다.
        inputs += ["-i", OVERLAY_FRAME_V2_PATH]
        frame_v2_idx = next_input_idx
        next_input_idx += 1

        # 🛡️ [오버레이 HUD 입력] FIRST BLOOD/SOLO KILL일 때만(schedule에 "hud" 키가 있을
        # 때만) 완성 배너 PNG를 추가 입력으로 붙인다 - 해당 없는 킬(추격전 등)에서는 아예
        # 입력조차 안 넣어서 필터그래프가 더 무거워지지 않는다.
        hud = schedule.get("hud")
        hud_panel_idx = None
        if hud is not None:
            inputs += ["-i", HUD_BANNER_PNGS[hud["event_label"]]]
            hud_panel_idx = next_input_idx
            next_input_idx += 1

        # 🛡️ [4단계 - 포지션 매칭 5행용 아이콘 입력] roster_pairs = [(left_or_None,
        # right_or_None), ...] - 한쪽이 없는 행(인원 부족)도 있을 수 있어 None 체크.
        # 챔피언/아이템 아이콘 전부 fetch 실패 시 None이 이미 들어와 있으므로 입력을 안
        # 넣으면 필터그래프도 그만큼 가벼워진다. (스펠/룬 아이콘은 패널에서 완전히
        # 제거되면서 이 입력 등록 자체도 삭제됨.)
        roster_pairs = schedule.get("roster_pairs") or []
        roster_icon_idx: dict[int, int] = {}
        roster_item_idx: dict[int, list[int | None]] = {}
        for left, right in roster_pairs:
            for r in (left, right):
                if r is None:
                    continue
                if r.get("icon_path"):
                    inputs += ["-i", r["icon_path"]]
                    roster_icon_idx[r["participant_id"]] = next_input_idx
                    next_input_idx += 1
                item_indices = []
                for item_icon_path in r.get("item_icon_paths", []):
                    if item_icon_path:
                        inputs += ["-i", item_icon_path]
                        item_indices.append(next_input_idx)
                        next_input_idx += 1
                    else:
                        item_indices.append(None)
                roster_item_idx[r["participant_id"]] = item_indices

        # 🛡️ [타워/드래곤 아이콘 입력] Community Dragon 실패 시 None - 동일한 안전 처리.
        scoreboard_for_input = schedule.get("scoreboard") or {}
        tower_icon_idx = None
        if scoreboard_for_input.get("tower_icon_path"):
            inputs += ["-i", scoreboard_for_input["tower_icon_path"]]
            tower_icon_idx = next_input_idx
            next_input_idx += 1
        dragon_icon_idx = None
        if scoreboard_for_input.get("dragon_icon_path"):
            inputs += ["-i", scoreboard_for_input["dragon_icon_path"]]
            dragon_icon_idx = next_input_idx
            next_input_idx += 1
        riftherald_icon_idx = None
        if scoreboard_for_input.get("riftherald_icon_path"):
            inputs += ["-i", scoreboard_for_input["riftherald_icon_path"]]
            riftherald_icon_idx = next_input_idx
            next_input_idx += 1
        baron_icon_idx = None
        if scoreboard_for_input.get("baron_icon_path"):
            inputs += ["-i", scoreboard_for_input["baron_icon_path"]]
            baron_icon_idx = next_input_idx
            next_input_idx += 1
        horde_icon_idx = None
        if scoreboard_for_input.get("horde_icon_path"):
            inputs += ["-i", scoreboard_for_input["horde_icon_path"]]
            horde_icon_idx = next_input_idx
            next_input_idx += 1

        # 🛡️ [드래곤 시퀀스 - 팀별 가변 개수 입력] 같은 파일 경로(중복 속성)가 여러 번
        # 나와도 그냥 각각 별도 -i로 추가한다 - 최대 4개×2팀=8개뿐이라 비용 무시 가능한
        # 수준이고, ffmpeg가 같은 파일을 여러 스트림으로 여는 것도 문제없다.
        team100_dragon_icon_idxs = []
        for path in scoreboard_for_input.get("team100_dragon_icon_paths") or []:
            inputs += ["-i", path]
            team100_dragon_icon_idxs.append(next_input_idx)
            next_input_idx += 1
        team200_dragon_icon_idxs = []
        for path in scoreboard_for_input.get("team200_dragon_icon_paths") or []:
            inputs += ["-i", path]
            team200_dragon_icon_idxs.append(next_input_idx)
            next_input_idx += 1

        # ── 화면 처리 (해설은 음성 전용 - 화면에 텍스트를 그리지 않는다. 스코어바/KDA/HUD
        # 오버레이는 예외 - 오디오와 무관하게 화면에 그리는 요소들) ──
        video_filters = []
        # 🛡️ 유저가 1440p/4K 등 고해상도 클립을 올리면(크기만 100MB 이내면 통과되므로
        # 충분히 가능) 목표 비트레이트가 픽셀 수 대비 너무 낮아져 화질이 심하게 뭉개진다 -
        # 스케일을 먼저 걸어 픽셀 수 자체를 낮춰둔다. -2로 짝수 높이 보장(libx264 요구사항).
        if video_width > MAX_OUTPUT_WIDTH:
            video_filters.append(f"scale={MAX_OUTPUT_WIDTH}:-2")
            final_width = MAX_OUTPUT_WIDTH
            final_height = int(round(video_height * MAX_OUTPUT_WIDTH / video_width / 2) * 2)
        else:
            final_width, final_height = video_width, video_height

        # 🛡️ [5단계 - 게임 화면 축소 완전 폐기, 100% 원본 크기 유지] 실제 LCK 방송 캡처
        # 실측 결과를 반영한 최종 구조 - 이전 라운드들(1단계 축소+여백, 4단계 상단/하단
        # 둘 다 축소)과 달리 이번엔 scale/pad를 아예 안 쓴다. 방송 UI는 전부 이 원본
        # 크기 게임 화면 위에 직접 오버레이된다(상단은 겹쳐도 무방, 하단은 실측된 좁은
        # 폭만 중앙에 - 아래 텍스트/그리드 섹션에서 처리).
        top_main_h = int(round(final_height * TOP_MAIN_BAR_HEIGHT_RATIO))
        top_sub_h = int(round(final_height * TOP_SUB_BAR_HEIGHT_RATIO))
        top_total_h = top_main_h + top_sub_h  # 오버레이 배치 계산용(더 이상 여백 확보 용도 아님)

        # 🛡️ 원본 클립보다 렌더 길이가 길어지면(빌드업+메인+하이프+서브 꼬리가 원본 영상
        # 길이를 넘어서는 게 일반적) 영상 쪽도 늘려야 오디오가 잘려나가지 않는다. 화면을
        # 정지시키는 대신 마지막 프레임을 그대로 붙잡아 늘리는 가장 단순한 방법(tpad) -
        # 이전 프로토타입의 펀치인 줌/비네트는 이번 라운드 범위 밖.
        extra_video_sec = max(0.0, total_duration - video_duration)
        if extra_video_sec > 0.01:
            video_filters.append(f"tpad=stop_mode=clone:stop_duration={extra_video_sec:.3f}")
        video_chain = (("[0:v]" + ",".join(video_filters) + "[vgame]") if video_filters
                        else "[0:v]copy[vgame]")
        current_label = "vgame"

        # 🛡️ [6단계 - 배경 프레임 오버레이, drawbox 대체] overlay_frame_v2.png를 실제 렌더
        # 해상도로 비균등 스케일(가로/세로 따로 - 프레임이 색상 그라데이션 블록이라 비균등
        # 스케일에도 왜곡 없음, 2단계 때와 동일한 판단)해서 게임 위에 그대로 얹는다. 이
        # 한 번의 오버레이가 예전 drawbox 3~4개(팀 틴트 x2, 서브바 배경, 하단패널 배경)를
        # 전부 대체한다 - 그라데이션/그림자/테두리가 이미 이미지에 구워져 있어서 코드가
        # 더 이상 그 디테일을 신경 쓸 필요가 없다.
        video_chain += (
            f";[{frame_v2_idx}:v]scale={final_width}:{final_height}[vframe2]"
            f";[{current_label}][vframe2]overlay=x=0:y=0[vframed2]"
        )
        current_label = "vframed2"

        # 🛡️ [스코어바(상단)/KDA(하단) 텍스트 오버레이] scoreboard는 _run_pipeline에서
        # hud_event 여부와 무관하게 항상 채워서 넘어온다(정상 파이프라인에선 항상 not None -
        # 아래 None 체크는 _render_video를 단독 테스트할 때의 방어용).
        scoreboard = schedule.get("scoreboard")
        text_chain = ""
        if scoreboard is not None:
            font_kr = _escape_ffmpeg_path(SCOREBAR_FONT_KR)
            font_kr_black = _escape_ffmpeg_path(SCOREBAR_FONT_KR_BLACK)
            mid_x = final_width / 2
            half_w = mid_x
            # 🛡️ [메인바 전용 좁은 바 좌표 - mid_x/half_w와 완전히 분리] 서브바(dl_x/dr_x,
            # dragon100_num_x 등)는 여전히 mid_x/half_w(화면 전체 중심)를 그대로 참조한다 -
            # 이 블록은 절대 건드리지 않는다. 메인바 요소(타워/골드/킬)만 이 새 변수로
            # 옮긴다.
            bar_x0 = final_width * (1 - MAIN_BAR_WIDTH_RATIO) / 2
            bar_x1 = final_width - bar_x0
            bar_mid_x = (bar_x0 + bar_x1) / 2
            bar_half_w = (bar_x1 - bar_x0) / 2

            # 🛡️ [text= 대신 textfile= - 실측으로 드러난 필수 사항] 처음엔 text='...'로 한글을
            # 필터 문자열에 직접 박아 넣었는데, 실제 ffmpeg 렌더에서 "Failed to set value ...
            # for option 'filter_complex': Invalid argument"로 계속 실패했다. -/filter_complex
            # (파일에서 옵션값을 읽는 문법)로 넘긴 파일 자체는 UTF-8로 정확했지만, ffmpeg가
            # 그 파일을 다시 읽어 필터그래프 문자열에 "박아 넣는" 내부 처리에서 한글 멀티바이트
            # 시퀀스가 깨졌다. drawtext의 textfile=(표시할 텍스트를 별도 파일에서 읽는 정식
            # 옵션)을 쓰면 텍스트가 filter_complex 문자열 안에 전혀 섞이지 않아 문제가
            # 재현되지 않음을 직접 렌더로 확인했다.
            def _write_textfile(name: str, text: str) -> str:
                path = os.path.join(work_dir, f"drawtext_{name}.txt")
                with open(path, "w", encoding="utf-8") as tf:
                    tf.write(text.replace("%", "\\%"))
                return _escape_ffmpeg_path(path)

            # 🛡️ [6단계 - 배경은 overlay_frame_v2.png가 이미 그림] 팀명/팀 로고 텍스트는
            # 여전히 뺀다(색상만으로 진영 구분) - 다만 그 색상 틴트 자체가 이제 배경 이미지에
            # 구워져 있어서(메인바 좌=밝은 블루→우=빨강 그라데이션, 실측 확인됨) drawbox로
            # 따로 그릴 필요가 없어졌다. 타워/골드/킬만 좌우 완전 거울로 그 위에 얹는다.
            # 순서(바깥→안쪽): 타워, 골드, 킬 - 타워가 예전 팀로고 자리였던 가장 바깥쪽에
            # 온다. 동적 텍스트 폭에 의존하지 않도록 각 요소를 자기 진영 "가장자리 기준
            # 고정 비율" 위치에 둔다(요소별 폭이 다른데도 안정적으로 대칭이 되는 이유).
            label = current_label

            top_font_size = max(10, int(round(top_main_h * 0.42)))
            top_icon_size = max(8, int(round(top_main_h * 0.6)))
            TOWER_FRAC, GOLD_FRAC, KILL_FRAC = 0.08, 0.42, 0.75
            main_text_y_expr = f"({top_main_h}-text_h)/2"

            tower_x_l = bar_x0 + bar_half_w * TOWER_FRAC
            tower_x_r = bar_x1 - bar_half_w * TOWER_FRAC
            gold_x_l = bar_x0 + bar_half_w * GOLD_FRAC
            gold_x_r = bar_x1 - bar_half_w * GOLD_FRAC
            kill_x_l = bar_x0 + bar_half_w * KILL_FRAC
            kill_x_r = bar_x1 - bar_half_w * KILL_FRAC

            if tower_icon_idx is not None:
                icon_y = (top_main_h - top_icon_size) / 2
                text_chain += (
                    f";[{tower_icon_idx}:v]scale={top_icon_size}:{top_icon_size}[vtwL]"
                    f";[{label}][vtwL]overlay=x={int(round(tower_x_l))}:y={icon_y:.2f}[vtw1]"
                    f";[{tower_icon_idx}:v]scale={top_icon_size}:{top_icon_size}[vtwR]"
                    f";[vtw1][vtwR]overlay=x={int(round(tower_x_r - top_icon_size))}:y={icon_y:.2f}[vtw2]"
                )
                label = "vtw2"
                tower_num_x_l = str(int(round(tower_x_l + top_icon_size + 4)))
                tower_num_x_r = f"{int(round(tower_x_r - top_icon_size - 4))}-text_w"
            else:
                tower_num_x_l = str(int(round(tower_x_l)))
                tower_num_x_r = f"{int(round(tower_x_r))}-text_w"

            # 🛡️ [골드 격차 - 괄호 표기 제거] 이제 "(+X.Xk)"를 골드 텍스트에 붙이지 않고
            # 서브바 구간에 독립된 배지로 따로 그린다(아래 참고) - 여기선 순수 골드 액수만.
            gold_diff = scoreboard["team100_gold"] - scoreboard["team200_gold"]
            gap_k = abs(gold_diff) / 1000
            gold100_text = f"{scoreboard['team100_gold'] / 1000:.1f}k"
            gold200_text = f"{scoreboard['team200_gold'] / 1000:.1f}k"

            tower100_tf = _write_textfile("tower100", str(scoreboard["team100_towers"]))
            tower200_tf = _write_textfile("tower200", str(scoreboard["team200_towers"]))
            gold100_tf = _write_textfile("gold100", gold100_text)
            gold200_tf = _write_textfile("gold200", gold200_text)
            kill100_tf = _write_textfile("kill100", str(scoreboard["team100_kills"]))
            kill200_tf = _write_textfile("kill200", str(scoreboard["team200_kills"]))

            # 🛡️ [메인바 6개 - 테두리로 입체감 추가] 지금까지 순수 흰색 평면 텍스트라
            # 배경 그라데이션 위에서 다소 밋밋했다 - 하단 패널 CS/KDA에 이미 쓰는
            # GRID_TEXT_BORDER_COLOR/W와 동일한 테두리를 추가한다(그림자는 제거됨).
            top_text_style = f"bordercolor={GRID_TEXT_BORDER_COLOR}:borderw={GRID_TEXT_BORDER_W}"
            text_chain += (
                f";[{label}]drawtext=fontfile='{font_kr}':textfile='{tower100_tf}':fontsize={top_font_size}:"
                f"fontcolor=white:{top_text_style}:x={tower_num_x_l}:y='{main_text_y_expr}'[vm1]"
                f";[vm1]drawtext=fontfile='{font_kr}':textfile='{tower200_tf}':fontsize={top_font_size}:"
                f"fontcolor=white:{top_text_style}:x='{tower_num_x_r}':y='{main_text_y_expr}'[vm2]"
                f";[vm2]drawtext=fontfile='{font_kr}':textfile='{gold100_tf}':fontsize={top_font_size}:"
                f"fontcolor=white:{top_text_style}:x='{int(round(gold_x_l))}-text_w/2':y='{main_text_y_expr}'[vm3]"
                f";[vm3]drawtext=fontfile='{font_kr}':textfile='{gold200_tf}':fontsize={top_font_size}:"
                f"fontcolor=white:{top_text_style}:x='{int(round(gold_x_r))}-text_w/2':y='{main_text_y_expr}'[vm4]"
                f";[vm4]drawtext=fontfile='{font_kr}':textfile='{kill100_tf}':fontsize={top_font_size}:"
                f"fontcolor=white:{top_text_style}:x='{int(round(kill_x_l))}-text_w/2':y='{main_text_y_expr}'[vm5]"
                f";[vm5]drawtext=fontfile='{font_kr}':textfile='{kill200_tf}':fontsize={top_font_size}:"
                f"fontcolor=white:{top_text_style}:x='{int(round(kill_x_r))}-text_w/2':y='{main_text_y_expr}'[vm6]"
            )
            label = "vm6"

            # 🛡️ [골드 격차 - 서브바에서 메인바 안으로 재배치] 예전엔 서브바 구간에 독립
            # 배지로 그렸는데, gap_leader_x(=gold_x_l/gold_x_r) 자체가 애초에 메인바
            # 좌표계라 서브바 폭 밖으로 넘치는 문제가 반복됐다(지난 두 라운드). 아예 메인바
            # 안, 리드팀 골드 숫자 바로 밑으로 옮기면 좌표계가 일치해서 그 문제 자체가
            # 사라진다 - x좌표는 gold_x_l/r 그대로 재사용, y좌표만 새로 계산한다. 골드
            # 텍스트는 top_main_h 밴드 안에서 세로 중앙 정렬(main_text_y_expr)이라, 실측
            # 잉크 비율(폰트 크기 대비 약 0.645, "60.6k" 실제 렌더로 확인됨)로 잉크 하단
            # 위치를 계산하고 그 바로 밑에 작은 줄간격을 두고 diff 텍스트를 놓는다. 서브바
            # 폭 제약이 없어져서(메인바는 전체 폭) 이전의 clamp 로직은 통째로 불필요해졌다.
            if gold_diff != 0:
                gap_leader_x = gold_x_l if gold_diff > 0 else gold_x_r
                gap_badge_color = TEAM_BLUE_COLOR if gold_diff > 0 else TEAM_RED_COLOR
                arrow_char = "◀" if gold_diff > 0 else "▶"
                gap_badge_text = f"+{gap_k:.1f}k"
                # 골드 폰트(top_font_size)의 40~50% 크기 - 45%를 기준값으로 사용.
                gap_badge_font_size = max(8, int(round(top_font_size * 0.45)))
                gap_arrow_font_size = max(6, int(round(gap_badge_font_size * 0.75)))
                gap_num_half_w = max(10, int(round(gap_badge_font_size * len(gap_badge_text) * 0.62))) / 2
                gap_arrow_gap = 4

                main_ink_h = top_font_size * 0.645
                main_ink_bottom = top_main_h / 2 + main_ink_h / 2
                # 🛡️ [간격 3배 확대 - 실측으로 확인된 겹침 해소] 0.03 배수는 실제 렌더에서
                # 최소 지점(숫자 하단 둥근 곡선 아래) 기준 약 2px까지 좁혀져 겹쳐 보였다
                # (줌 크롭+픽셀 스캔으로 확인) - 0.09로 올려서 최소 지점 기준 6~8px 여유를
                # 확보한다.
                gap_line_spacing = max(2, int(round(top_main_h * 0.09)))
                gap_diff_y = main_ink_bottom + gap_line_spacing

                gap_badge_tf = _write_textfile("top_gold_gap", gap_badge_text)
                gap_arrow_tf = _write_textfile("top_gold_gap_arrow", arrow_char)

                if gold_diff > 0:
                    gap_arrow_x = f"{gap_leader_x:.2f}-{gap_num_half_w:.2f}-{gap_arrow_gap}-text_w"
                else:
                    gap_arrow_x = f"{gap_leader_x:.2f}+{gap_num_half_w:.2f}+{gap_arrow_gap}"

                text_chain += (
                    f";[{label}]drawtext=fontfile='{font_kr_black}':textfile='{gap_badge_tf}':"
                    f"fontsize={gap_badge_font_size}:fontcolor={gap_badge_color}:"
                    f"x='{gap_leader_x:.2f}-text_w/2':y='{gap_diff_y:.2f}'[vtgaptxt]"
                    f";[vtgaptxt]drawtext=fontfile='{font_kr_black}':textfile='{gap_arrow_tf}':"
                    f"fontsize={gap_arrow_font_size}:fontcolor={gap_badge_color}:"
                    f"x='{gap_arrow_x}':y='{gap_diff_y:.2f}'[vtgaparrow]"
                )
                label = "vtgaparrow"

            # ── 상단 서브바: 게임시간 중앙 + 드래곤 스택 좌우(대칭) ──
            # 🛡️ [실측 폭 그대로 - 전체 폭이 아니라 중앙 구간만] 참고 사진 실측 결과
            # 서브바는 메인바와 달리 전체 폭이 아니라 중앙 48.8%(x=650~1900 @2560
            # 기준)만 차지한다 - 배경 박스도 그 폭만 그려서 참고 사진과 같은 "메인바보다
            # 좁은 서브바" 형태를 재현한다.
            sub_x0 = int(round(final_width * TOP_SUB_BAR_X_RATIO[0]))
            sub_x1 = int(round(final_width * TOP_SUB_BAR_X_RATIO[1]))
            sub_font_size = max(8, int(round(top_sub_h * 0.55)))
            # 🛡️ [오브젝트 스택 확대 - 시간 텍스트와 폰트 변수 분리] sub_font_size는
            # time_text가 그대로 쓰고 있어서(아래 vs3), 이 값 자체를 바꾸면 시간 텍스트도
            # 같이 커진다("건드리지 마" 지시 위반) - 그래서 오브젝트 숫자 전용
            # obj_font_size를 새로 둔다. sub_icon_size는 오브젝트 스택에서만 쓰여서
            # (time_text엔 아이콘이 없음) 직접 바꿔도 안전하다.
            sub_icon_size = top_sub_h
            obj_font_size = max(8, int(round(top_sub_h * 0.75)))
            sub_text_y_expr = f"{top_main_h}+({top_sub_h}-text_h)/2"
            dragon_offset = (sub_x1 - sub_x0) / 2 * 0.3

            gm = scoreboard["game_time_ms"] // 1000
            # 🛡️ 콜론(:)은 필터 옵션 구분자와 충돌해서 홑따옴표로 감싸도 그대로 두면
            # 깨진다(fontfile의 드라이브 콜론과 같은 문제) - _escape_drawtext_text로 이스케이프.
            time_text = _escape_drawtext_text(f"{gm // 60:02d}:{gm % 60:02d}")

            # 🛡️ [드래곤 - 누적 숫자 대신 시간순 속성 아이콘 나열] 드래곤이 이제 "메인"
            # 요소라 아이콘 크기(sub_icon_size)는 그대로 유지한다. 팀별 리스트(이미 최근
            # DRAGON_SEQUENCE_MAX개로 잘려서 시간순 - _extract_dragon_sequence 참고)를
            # 그대로 순서대로 그린다: 리스트의 앞(오래된 것)을 안쪽(mid_x에 가까움)에,
            # 뒤(최근 것)일수록 바깥쪽에 배치한다 - "쌓여가는" 느낌. 숫자는 안 그린다(0마리인
            # 팀은 그냥 빈 자리로 넘어간다 - 아이콘이 없으니 자동으로 그렇게 됨).
            DRAGON_ICON_GAP = 3
            d_icon_y = top_main_h + (top_sub_h - sub_icon_size) / 2
            cursor_l = dragon_offset
            for i, icon_idx in enumerate(team100_dragon_icon_idxs):
                icon_x = mid_x - cursor_l - sub_icon_size
                text_chain += (
                    f";[{icon_idx}:v]scale={sub_icon_size}:{sub_icon_size}[vdgL{i}]"
                    f";[{label}][vdgL{i}]overlay=x={int(round(icon_x))}:y={d_icon_y:.2f}[vdgL{i}o]"
                )
                label = f"vdgL{i}o"
                cursor_l += sub_icon_size + DRAGON_ICON_GAP
            cursor_r = dragon_offset
            for i, icon_idx in enumerate(team200_dragon_icon_idxs):
                icon_x = mid_x + cursor_r
                text_chain += (
                    f";[{icon_idx}:v]scale={sub_icon_size}:{sub_icon_size}[vdgR{i}]"
                    f";[{label}][vdgR{i}]overlay=x={int(round(icon_x))}:y={d_icon_y:.2f}[vdgR{i}o]"
                )
                label = f"vdgR{i}o"
                cursor_r += sub_icon_size + DRAGON_ICON_GAP

            # 🛡️ [전령/바론/공허유충 - 기존 방식 유지, 크기만 25% 축소] 드래곤이 메인이
            # 되면서 보조 오브젝트는 시각적 위계를 두려고 아이콘/폰트를 25% 줄인다(요청한
            # 20~30% 범위 안). 드래곤 존이 팀별로 길이가 달라져서(예: team100 4마리,
            # team200 0마리) cursor_l/cursor_r을 팀별로 독립적으로 계속 이어간다 - 두 팀이
            # 더 이상 완전히 대칭이 아닐 수 있지만, 각 팀 자기 자신의 오브젝트 개수 기준으로는
            # 항상 안쪽→바깥쪽 순서가 일관된다.
            minor_icon_size = max(6, int(round(sub_icon_size * 0.75)))
            minor_font_size = max(6, int(round(obj_font_size * 0.75)))
            minor_icon_y = top_main_h + (top_sub_h - minor_icon_size) / 2
            minor_num_zone_w = max(8, int(round(minor_font_size * 2 * 0.62)))
            minor_group_gap = max(3, int(round(minor_icon_size * 0.3)))
            objective_items = [
                ("riftherald", riftherald_icon_idx, "team100_riftheralds", "team200_riftheralds"),
                ("baron", baron_icon_idx, "team100_barons", "team200_barons"),
                ("horde", horde_icon_idx, "team100_hordes", "team200_hordes"),
            ]

            for name, icon_idx, key100, key200 in objective_items:
                icon_x_l = mid_x - cursor_l - minor_icon_size
                icon_x_r = mid_x + cursor_r
                if icon_idx is not None:
                    text_chain += (
                        f";[{icon_idx}:v]scale={minor_icon_size}:{minor_icon_size}[v{name}L]"
                        f";[{label}][v{name}L]overlay=x={int(round(icon_x_l))}:y={minor_icon_y:.2f}[v{name}1]"
                        f";[{icon_idx}:v]scale={minor_icon_size}:{minor_icon_size}[v{name}R]"
                        f";[v{name}1][v{name}R]overlay=x={int(round(icon_x_r))}:y={minor_icon_y:.2f}[v{name}2]"
                    )
                    label = f"v{name}2"
                    num100_x = f"{int(round(icon_x_l - 4))}-text_w"
                    num200_x = str(int(round(icon_x_r + minor_icon_size + 4)))
                else:
                    num100_x = f"{int(round(mid_x - cursor_l))}-text_w"
                    num200_x = str(int(round(mid_x + cursor_r)))

                num100_tf = _write_textfile(f"{name}100", str(scoreboard[key100]))
                num200_tf = _write_textfile(f"{name}200", str(scoreboard[key200]))
                text_chain += (
                    f";[{label}]drawtext=fontfile='{font_kr}':textfile='{num100_tf}':fontsize={minor_font_size}:"
                    f"fontcolor={TEAM_BLUE_COLOR}:{top_text_style}:x='{num100_x}':y='{sub_text_y_expr}'[v{name}n1]"
                    f";[v{name}n1]drawtext=fontfile='{font_kr}':textfile='{num200_tf}':fontsize={minor_font_size}:"
                    f"fontcolor={TEAM_RED_COLOR}:{top_text_style}:x='{num200_x}':y='{sub_text_y_expr}'[v{name}n2]"
                )
                label = f"v{name}n2"
                cursor_l += minor_icon_size + 4 + minor_num_zone_w + minor_group_gap
                cursor_r += minor_icon_size + 4 + minor_num_zone_w + minor_group_gap

            text_chain += (
                f";[{label}]drawtext=fontfile='{font_kr}':text='{time_text}':fontsize={sub_font_size}:"
                f"fontcolor=white:{top_text_style}:x='{int(round(mid_x))}-text_w/2':y='{sub_text_y_expr}'[vs3]"
            )
            label = "vs3"

            current_label = label

            # ── 하단 통계 패널 - 실측 좌표(폭 42.7%, 중앙 정렬)로 배경만 먼저 그린다 ──
            # 🛡️ [헤더 띠 제거] 이번 라운드 요청 목록에 헤더가 없고(이전 라운드의
            # "KYVOBOT HIGHLIGHT" 띠), 실측 높이(322px @1435 기준)가 5행을 넣기에도 빠듯해서
            # 뺐다 - 패널 전체 높이를 5행에만 쓴다.
            panel_w = int(round(final_width * BOTTOM_PANEL_WIDTH_RATIO))
            panel_h = int(round(final_height * BOTTOM_PANEL_HEIGHT_RATIO))
            panel_x0 = (final_width - panel_w) // 2
            panel_y0 = int(round(final_height * BOTTOM_PANEL_Y_START_RATIO))
            panel_half_w = panel_w / 2
            # 🛡️ [패널 전용 중심점 신설] mid_x(=final_width/2, 캔버스 중심)는 상단
            # 메인바/서브바가 여전히 캔버스 전체 폭 기준 대칭이라 그대로 둬야 한다 - 대신
            # 하단 패널 전용 중심점을 따로 둔다. panel_x0가 정수 floor division이라 mid_x와
            # 완전히 같지 않을 수 있음(이번 해상도 실측: 0.5px 차이) - 패널 내부 요소는
            # 패널 자신의 중심(panel_mid_x)을 기준으로 삼는 게 개념적으로 맞다.
            panel_mid_x = panel_x0 + panel_w / 2
            row_h_raw = panel_h / BOTTOM_ROWS
            rows_y0 = panel_y0

            # 🛡️ [하단 - 포지션별 5행, 완전 거울 배치] 순서(바깥→안쪽): 스펠/룬, 아이템 6칸,
            # KDA, CS, 챔피언 초상화(중앙, 마주보기) - 요청 순서 그대로. roster_pairs는
            # _pair_roster_by_position()이 이미 포지션 매칭까지 끝내서 넘겨준 리스트라 여기선
            # 그대로 순서대로 그리기만 한다. 동적 텍스트 폭 체이닝이 불가능한 건 이전 라운드와
            # 동일 - 고정 비율 구역을 미리 나누고 오른쪽 열은 중앙선 기준 대칭 이동으로 만든다.
            # 🛡️ [5단계 - 구역 기준을 패널 폭으로 축소] 이전 라운드는 half_w가 캔버스
            # 절반(~960px)이었는데, 이번엔 패널 자체가 실측상 훨씬 좁아서(panel_half_w
            # ≈205px @1920 기준) 구역 비율 계산의 기준을 panel_half_w로 바꿨다 - 공식은
            # 그대로, 기준 폭만 좁아져서 자연스럽게 전부 축소된다.
            if hud is not None:
                grid_enable = f"not(between(t,{hud['start']:.3f},{hud['end'] + HUD_SLIDE_SEC:.3f}))"
            else:
                grid_enable = "1"

            portrait_size = int(round(row_h_raw * 0.85))
            # 🛡️ [아이템 아이콘 확대 - 조사에서 확인된 상한까지] 기존 0.32 비율(row_h의
            # ~26%)은 패널 좌우에 빈 공간을 남겼다(조사로 확인됨) - 포트레이트를 침범하지
            # 않는 상한인 portrait_size와 완전히 동일한 크기까지 올려서 그 공간을 채운다.
            item_size = portrait_size
            # 🛡️ [테두리 두께 상수화] 기존엔 t=1 리터럴이었다 - portrait_size/item_size에
            # 비례하는 값으로 바꿔서 해상도가 달라져도 같은 상대적 두께를 유지한다(이번
            # 테스트 해상도(portrait_size=23)에서는 계산해도 여전히 1px이라 시각적 차이 없음,
            # 계산으로 확인됨).
            champion_frame_border_w = max(1, round(portrait_size * 0.04))
            item_slot_border_w = max(1, round(item_size * 0.04))
            pad = max(2, int(round(row_h_raw * 0.06)))
            # 🛡️ [텍스트 가독성 1순위 - 크기 대폭 확대] 기존 0.26 비율은 실측 row_h_raw
            # (~27~30px)에서 폰트 크기가 8px까지 내려가 거의 안 보였다(하단 텍스트 가독성
            # 요청의 직접 원인). "CS " 라벨을 없애 숫자만 남기면서 자리가 남은 만큼도 반영해
            # 0.55로 올린다 - 실제 크롭 캡처로 재확인.
            grid_font_size = max(12, int(round(row_h_raw * 0.55)))
            item_gap = max(1, int(round(item_size * 0.15)))

            portrait_zone_w = portrait_size + 2 * pad
            # 🛡️ [CS/KDA zone 재분배 - 실제 텍스트 렌더 폭 기준] 조사에서 FontKR-Black.otf로
            # 실측: CS 최댓값("999" 같은 극단 3자리) 실제 렌더 폭 30px, KDA 최댓값
            # ("10/10/10") 66px. 기존 비율(panel_half_w*0.11=45px, *0.15=61.4px)은 CS는
            # 15px 남고 KDA는 4.6px 모자랐다 - CS에서 뺀 15px을 그대로 KDA로 옮긴다(합은
            # 그대로 0.26 유지). cs_zone_w는 딱 맞는 값(30px)까지 줄어서 여유가 거의 없다 -
            # 실제 렌더로 "999"가 안 잘리는지 반드시 확인 필요(계산상 폰트 메트릭과 ffmpeg
            # 실제 렌더 사이 오차가 있을 수 있음).
            cs_zone_w = panel_half_w * 0.0734
            kda_zone_w = panel_half_w * 0.1866
            # 🛡️ [CS-KDA 사이 명시적 간격 신설] 스펠/룬 제거로 확보된 공간 중 일부를 여기로
            # 돌린다 - 기존엔 두 zone이 완전히 맞붙어 있어서(kda_zone_x1 = cs_zone_x1 -
            # cs_zone_w, 사이에 더하는 항이 없었음) CS/KDA가 동시에 극단값("999"+
            # "10/10/10")이면 실측 0.06px까지 거의 붙어 보였다 - row_h 비례(pad와 같은
            # 방식)로 잡아서 다른 해상도에서도 비율이 유지되게 한다(0.6배 ≈ 16px @ 이번
            # 세션 테스트 해상도, 요청하신 "16 정도"와 일치).
            cs_kda_gap = int(round(row_h_raw * 0.6))
            items_zone_w = 6 * item_size + 5 * item_gap + 2 * pad
            # 🛡️ [구역 합이 panel_half_w를 넘으면 겹칠 수 있음 - 클램프 없이 그대로 렌더,
            # 실제 프레임으로 확인해서 보고한다] 억지로 축소하면 아이콘/텍스트가 너무
            # 작아져서 오히려 안 보이는 쪽보다 나쁠 수 있다고 판단.

            # 🛡️ [라인전 골드 격차 배지용 데이터] schedule에 없거나(구버전 호출부) 길이가
            # 안 맞으면 그냥 None 취급 - 배지를 안 그리는 쪽으로 안전하게 처리한다.
            laning_gold_gaps = schedule.get("laning_gold_gaps")

            grid_parts = []
            label = current_label
            for j in range(BOTTOM_ROWS):
                left_p, right_p = roster_pairs[j] if j < len(roster_pairs) else (None, None)
                row_y0 = rows_y0 + j * row_h_raw
                text_y_expr = f"{row_y0:.2f}+({row_h_raw:.2f}-text_h)/2"
                portrait_y = row_y0 + (row_h_raw - portrait_size) / 2
                item_y = row_y0 + (row_h_raw - item_size) / 2

                for side, r in (("L", left_p), ("R", right_p)):
                    if r is None:
                        continue
                    tag = f"{side}{j}"
                    pid = r["participant_id"]
                    if side == "L":
                        portrait_x = panel_mid_x - portrait_zone_w + pad - PORTRAIT_GAP_EXTRA_OFFSET
                        cs_zone_x1 = panel_mid_x - portrait_zone_w - PORTRAIT_GAP_EXTRA_OFFSET
                        cs_x_expr = f"{int(round(cs_zone_x1 - pad))}-text_w"
                        kda_zone_x1 = cs_zone_x1 - cs_zone_w - cs_kda_gap
                        kda_x_expr = f"{int(round(kda_zone_x1 - pad))}-text_w"
                        items_zone_x1 = kda_zone_x1 - kda_zone_w
                        items_zone_x0 = items_zone_x1 - items_zone_w
                        item_xs = [items_zone_x0 + pad + k * (item_size + item_gap) for k in range(6)]
                    else:
                        portrait_x = panel_mid_x + pad + PORTRAIT_GAP_EXTRA_OFFSET
                        cs_zone_x0 = panel_mid_x + portrait_zone_w + PORTRAIT_GAP_EXTRA_OFFSET
                        cs_x_expr = str(int(round(cs_zone_x0 + pad)))
                        kda_zone_x0 = cs_zone_x0 + cs_zone_w + cs_kda_gap
                        kda_x_expr = str(int(round(kda_zone_x0 + pad)))
                        items_zone_x0 = kda_zone_x0 + kda_zone_w
                        item_xs = [items_zone_x0 + pad + k * (item_size + item_gap) for k in range(6)]

                    icon_idx = roster_icon_idx.get(pid)
                    if icon_idx is not None:
                        grid_parts.append(f";[{icon_idx}:v]scale={portrait_size}:{portrait_size}[vr{tag}p]")
                        grid_parts.append(
                            f";[{label}][vr{tag}p]overlay=x={int(round(portrait_x))}:y={portrait_y:.2f}:"
                            f"enable='{grid_enable}'[vr{tag}a]")
                        label = f"vr{tag}a"

                        # 🛡️ [3순위 - 챔피언 프레임] 아이콘과 정확히 같은 사각형을 아이콘 위에
                        # "나중에" 그려야 1px 테두리가 아이콘 가장자리에 가려지지 않고 그 위에
                        # 얹힌 채로 보인다(먼저 그리면 오버레이가 그대로 덮어버림).
                        grid_parts.append(
                            f";[{label}]drawbox=x={int(round(portrait_x))}:y={portrait_y:.2f}:"
                            f"w={portrait_size}:h={portrait_size}:color={CHAMPION_FRAME_COLOR}:t={champion_frame_border_w}:"
                            f"enable='{grid_enable}'[vr{tag}pf]")
                        label = f"vr{tag}pf"

                    # 🛡️ [1순위 - 하단 텍스트 가독성] "CS " 라벨을 없애 숫자만 남기고(KDA는
                    # 이미 "K/D/A" 형태로 숫자뿐이라 그대로), Black 웨이트 폰트 + 검은 외곽선
                    # (borderw=2)으로 배경 그라데이션과 확실히 분리한다 - 기존 shadow만으로는
                    # 배경과 명도가 비슷한 구간에서 거의 안 보였다.
                    cs_tf = _write_textfile(f"roster_cs_{tag}", str(r['cs']))
                    kda_tf = _write_textfile(f"roster_kda_{tag}", f"{r['kda'][0]}/{r['kda'][1]}/{r['kda'][2]}")
                    grid_parts.append(
                        f";[{label}]drawtext=fontfile='{font_kr_black}':textfile='{cs_tf}':fontsize={grid_font_size}:"
                        f"fontcolor={CS_TEXT_COLOR}:bordercolor={GRID_TEXT_BORDER_COLOR}:borderw={GRID_TEXT_BORDER_W}:"
                        f"x='{cs_x_expr}':y='{text_y_expr}':enable='{grid_enable}'[vr{tag}b]")
                    label = f"vr{tag}b"
                    grid_parts.append(
                        f";[{label}]drawtext=fontfile='{font_kr_black}':textfile='{kda_tf}':fontsize={grid_font_size}:"
                        f"fontcolor=white:bordercolor={GRID_TEXT_BORDER_COLOR}:borderw={GRID_TEXT_BORDER_W}:"
                        f"x='{kda_x_expr}':y='{text_y_expr}':enable='{grid_enable}'[vr{tag}c]")
                    label = f"vr{tag}c"

                    # 🛡️ [2순위 - 아이템 6칸 슬롯 틀 - 빈 슬롯도 항상 표시] drawbox로 슬롯
                    # 테두리를 먼저 그리고, 아이콘이 있으면 그 위에 겹쳐 그린다(요청 스펙대로
                    # 어두운 보라 1px).
                    item_idx_list = roster_item_idx.get(pid, [])
                    for k in range(6):
                        grid_parts.append(
                            f";[{label}]drawbox=x={int(round(item_xs[k]))}:y={item_y:.2f}:"
                            f"w={item_size}:h={item_size}:color={ITEM_SLOT_BORDER_COLOR}:t={item_slot_border_w}:"
                            f"enable='{grid_enable}'[vr{tag}slot{k}]")
                        label = f"vr{tag}slot{k}"
                        item_idx = item_idx_list[k] if k < len(item_idx_list) else None
                        if item_idx is not None:
                            grid_parts.append(f";[{item_idx}:v]scale={item_size}:{item_size}[vr{tag}item{k}]")
                            grid_parts.append(
                                f";[{label}][vr{tag}item{k}]overlay=x={int(round(item_xs[k]))}:y={item_y:.2f}:"
                                f"enable='{grid_enable}'[vr{tag}i{k}]")
                            label = f"vr{tag}i{k}"

                    if j > 0:
                        divider_x0 = panel_x0 if side == "L" else int(round(panel_mid_x))
                        grid_parts.append(
                            f";[{label}]drawbox=x={divider_x0}:y={int(round(row_y0))}:"
                            f"w={int(round(panel_half_w))}:h=1:color={ROSTER_DIVIDER_COLOR}:t=fill:"
                            f"enable='{grid_enable}'[vr{tag}d]")
                        label = f"vr{tag}d"

                # 🛡️ [라인전 골드 격차 배지 - 화살표는 포트레이트에 밀착, 숫자는 갭
                # 정중앙 고정] 화살표(배경 없는 색상 글리프)와 숫자(팀 색상 텍스트)가 이제
                # 서로 독립된 기준점을 쓴다 - 화살표는 리드팀 포트레이트 안쪽 가장자리에
                # min_margin만 남기고 붙고, 숫자는 항상 panel_mid_x 중앙(자기 text_w로 셀프
                # 정렬)에 고정된다. 둘이 물리적으로 떨어지게 되므로, 숫자 폭이 큰 극단값
                # 에서 화살표 쪽을 침범하지 않는지는 계산+실측으로 별도 확인함.
                gap = laning_gold_gaps[j] if laning_gold_gaps and j < len(laning_gold_gaps) else None
                if gap:
                    gap_color = TEAM_BLUE_COLOR if gap > 0 else TEAM_RED_COLOR
                    arrow_char = "◀" if gap > 0 else "▶"
                    gap_abs = abs(gap)
                    gap_num_text = f"+{gap_abs}" if gap_abs < 1000 else f"+{gap_abs / 1000:.1f}k"

                    gap_badge_w = GOLD_GAP_ARROW_BADGE_W
                    gap_badge_h = max(gap_badge_w, int(round(portrait_size * 0.55)))
                    gap_badge_y = row_y0 + (row_h_raw - gap_badge_h) / 2
                    # 🛡️ [화살표 - 포트레이트 밀착] 고정 클러스터 경계 대신 실제 포트레이트
                    # 안쪽 가장자리를 기준으로 잡아서, 화살표가 리드팀 포트레이트에 최소
                    # 여백(min_margin)만 남기고 거의 붙게 한다.
                    left_inner_edge = panel_mid_x - pad - PORTRAIT_GAP_EXTRA_OFFSET
                    right_inner_edge = panel_mid_x + pad + PORTRAIT_GAP_EXTRA_OFFSET
                    min_margin = 1
                    if gap > 0:
                        arrow_box_x = left_inner_edge + min_margin
                    else:
                        arrow_box_x = right_inner_edge - min_margin - gap_badge_w

                    arrow_tf = _write_textfile(f"roster_gap_arrow_{j}", arrow_char)
                    num_tf = _write_textfile(f"roster_gap_num_{j}", gap_num_text)

                    # 🛡️ [배경 박스 제거 - 화살표 글리프 자체를 색상화] 예전엔 배경
                    # drawbox(색 배경+흰 글리프)였는데, 이제 배경 없이 화살표 글리프의
                    # fontcolor 자체를 gap_color로 바꿔서 표현한다(숫자 텍스트와 동일한
                    # 색상 판별 로직 재사용). gap_badge_w/arrow_box_x 등은 실제로 사각형을
                    # 안 그려도 좌표 계산(화살표 중심 정렬, 숫자 시작 위치)에는 그대로
                    # 쓰인다 - 지우면 그 계산들이 다 같이 깨진다.
                    grid_parts.append(
                        f";[{label}]drawtext=fontfile='{font_kr_black}':textfile='{arrow_tf}':"
                        f"fontsize={GOLD_GAP_ARROW_FONT_SIZE}:fontcolor={gap_color}:"
                        f"x='{arrow_box_x:.2f}+({gap_badge_w}-text_w)/2':"
                        f"y='{gap_badge_y:.2f}+({gap_badge_h}-text_h)/2':"
                        f"enable='{grid_enable}'[vgap{j}arrow]")
                    label = f"vgap{j}arrow"

                    # 🛡️ [숫자 - 갭 정중앙 고정] 화살표가 이제 포트레이트 쪽에 붙어서
                    # 화살표 기준 상대 위치로는 더 이상 안 맞다 - panel_mid_x에 항상 고정하고
                    # 자기 자신의 text_w로 가운데 정렬(방향 분기 필요 없음).
                    num_x_expr = f"{panel_mid_x:.2f}-text_w/2"
                    grid_parts.append(
                        f";[{label}]drawtext=fontfile='{font_kr_black}':textfile='{num_tf}':"
                        f"fontsize={GOLD_GAP_NUMBER_FONT_SIZE}:fontcolor={gap_color}:"
                        f"x='{num_x_expr}':y='{gap_badge_y:.2f}+({gap_badge_h}-text_h)/2':"
                        f"enable='{grid_enable}'[vgap{j}num]")
                    label = f"vgap{j}num"

            text_chain += "".join(grid_parts)
            current_label = label

        if hud is not None:
            # 🛡️ [배너 폭/위치 - 패널 박스와 완전히 일치] 배너 폭이 하단 패널 박스와 같은
            # 비율(HUD_BANNER_MAX_WIDTH_RATIO=BOTTOM_PANEL_WIDTH_RATIO)이므로, x_start도
            # 안전지대 중앙이 아니라 패널의 실제 위치(panel_x0, 화면 중앙 정렬)를 그대로
            # 써야 패널을 좌우 빈틈없이 완전히 덮는다 - 안전지대 중앙 정렬로 계산했더니
            # 안전지대 중심(x=851.5)과 패널/화면 중심(x=960)이 달라서 패널 우측 108px
            # 정도가 배너 밖으로 노출되는 문제가 실측으로 확인됐다(이전 라운드). 패널은
            # 원래부터 카드존/미니맵존을 항상 안전하게 피하도록 설계돼 있으므로, 배너가
            # 패널과 정확히 같은 폭/위치를 쓰면 그 두 구역도 자동으로 안 침범한다. 세로
            # 방향은 기존처럼 하단 패널 bbox(panel_y0/panel_h) 안에서 가운데 정렬 +
            # 종횡비 유지, 높이가 패널을 넘으면 높이 기준으로 다시 맞추는 방어적 클램프.
            # 🛡️ [contain/cover 로직 단순화 검토 - 유지하기로 결론] 배너 PNG를 패널 박스
            # 비율(819:134 @ 이번 테스트 해상도)에 맞춰 새로 만들어서 지금은 이 클램프가
            # 거의 안 걸린다(banner_height가 계산상 정확히 panel_h와 같아짐, 실측 확인).
            # 그렇다고 클램프 자체를 지워서 "폭 기준 고정값"으로 단순화하면 안 된다 -
            # panel_w는 final_width에만, panel_h는 final_height에만 비례해서, 패널의
            # 실제 비율(panel_w/panel_h)이 영상 종횡비(final_width/final_height)에 따라
            # 달라진다(이번 804 높이 캔버스는 6.11:1, 순정 1080 높이였다면 4.55:1 - 서로
            # 다름, 계산으로 확인됨). 즉 배너 PNG의 고정 비율(6.11:1)이 "항상" 패널과
            # 정확히 맞는다는 보장이 없어서, 이 방어적 높이 클램프는 다른 종횡비 영상에서
            # 여전히 필요하다 - 그래서 지우지 않고 그대로 둔다.
            with Image.open(HUD_BANNER_PNGS[hud["event_label"]]) as banner_im:
                banner_native_w, banner_native_h = banner_im.size
            banner_area_y0 = panel_y0
            banner_area_h = panel_h
            banner_width = int(round(final_width * HUD_BANNER_MAX_WIDTH_RATIO))
            banner_height = int(round(banner_width * banner_native_h / banner_native_w))
            if banner_height > banner_area_h:
                banner_height = banner_area_h
                banner_width = int(round(banner_height * banner_native_w / banner_native_h))
            x_start = panel_x0
            y_start_visible = banner_area_y0 + (banner_area_h - banner_height) // 2

            hud_start, hud_end, slide = hud["start"], hud["end"], HUD_SLIDE_SEC
            banner_x = str(x_start)
            visible_y = str(y_start_visible)
            hidden_y = str(final_height + 10)  # 슬라이드 시작 전/후엔 화면 밖으로
            panel_y = _hud_slide_y_expr(hud_start, hud_end, slide, visible_y, hidden_y)
            hud_visible_window = f"between(t,{hud_start:.3f},{hud_end + slide:.3f})"

            hud_chain = (
                f";[{hud_panel_idx}:v]scale={banner_width}:{banner_height}[vhudscaled]"
                f";[{current_label}][vhudscaled]overlay=x='{banner_x}':y='{panel_y}':"
                f"enable='{hud_visible_window}'[vout]"
            )
        else:
            hud_chain = "" if current_label == "vout" else f";[{current_label}]copy[vout]"

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
            audio_parts.append(f"[{idx}:a]adelay={delay_ms}|{delay_ms},volume={VOICE_MIX_GAIN_DB}dB[v_{key}];")
            mix_labels.append(f"[v_{key}]")

        n_mix = len(mix_labels)
        # 🛡️ level=false: alimiter의 기본값(자동 레벨 보정)을 꺼서 limit이 진짜 하드 천장으로
        # 작동하게 한다 - 켜져 있으면 리미터가 깎은 만큼 출력을 다시 끌어올려서 게인 값에 따라
        # 클리핑 여부가 불안정하게 튀는 게 실측으로 확인됨(VOICE_MIX_GAIN_DB 주석 참고).
        audio_parts.append(
            f"{''.join(mix_labels)}amix=inputs={n_mix}:duration=first:"
            f"dropout_transition=0:normalize=0[mixed];"
            f"[mixed]alimiter=limit={SFX_LIMITER_CEILING}:attack=5:release=50:level=false[aout]"
        )
        full_audio = "".join(audio_parts)

        filter_complex = f"{video_chain}{text_chain}{hud_chain};{full_audio}"

        # 🛡️ [파일로 넘기기 - 이번 라운드에서 새로 필요해짐, -filter_complex_script는
        # deprecated] drawtext에 한글 텍스트(닉네임/"타워"/"킬" 등)가 들어가면서, Windows
        # 에서 -filter_complex를 커맨드라인 인자로 그대로 넘기면 argv 인코딩 과정에서 한글이
        # 깨지는 게 실측으로 확인됐다(ffmpeg가 받는 시점에 이미 깨진 바이트라 "Invalid
        # argument" 에러). 첫 시도로 -filter_complex_script를 썼는데 이건 `ffmpeg -h full`
        # 확인 결과 deprecated 옵션이라 이 ffmpeg 빌드에서 아예 안 먹혔다("Invalid argument")
        # - 대체 문법 -/filter_complex(제네릭 "파일에서 옵션값 읽기" 문법, ffmpeg 문서에
        # deprecated 안내로 명시된 후계)로 실제 렌더까지 성공하는 것까지 직접 확인했다(한글이
        # 안 깨지고 정상 렌더됨). UTF-8로 직접 쓴 파일을 넘기므로 인코딩 문제 자체가 없고,
        # 부가 효과로 필터그래프가 길어져도 OS 커맨드라인 길이 제한과 무관해진다.
        filter_script_path = os.path.join(work_dir, "filter_complex.txt")
        with open(filter_script_path, "w", encoding="utf-8") as f:
            f.write(filter_complex)

        # 🛡️ crf 고정값 대신 total_duration에서 역산한 목표 비트레이트로 인코딩 -
        # 콘텐츠 복잡도/해상도와 무관하게 파일 크기가 항상 TARGET_OUTPUT_SIZE_MB 근처로
        # 수렴한다(디스코드 업로드 한도 대응). maxrate/bufsize로 순간적인 폭주만 눌러주고
        # 평균은 -b:v 그대로 나가게 하는 표준 단일 패스 VBV 제한 인코딩.
        cmd = [FFMPEG_EXE, "-y", *inputs,
               "-/filter_complex", filter_script_path,
               "-map", "[vout]", "-map", "[aout]",
               "-c:v", "libx264", "-preset", "veryfast",
               "-b:v", f"{int(target_video_kbps)}k",
               "-maxrate", f"{int(target_video_kbps * 1.5)}k",
               "-bufsize", f"{int(target_video_kbps * 2)}k",
               "-c:a", "aac", "-b:a", f"{OUTPUT_AUDIO_BITRATE_KBPS}k",
               "-t", str(total_duration),
               out_mp4]
        # 🛡️ [인코딩 명시 - 이번 라운드에서 새로 필요해짐] drawtext로 한글 텍스트(닉네임/
        # "타워"/"킬" 등)가 -filter_complex 커맨드라인에 처음으로 들어가면서, text=True가
        # 시스템 로케일(한국어 Windows는 cp949)로 stderr를 디코딩하려다 ffmpeg 출력 안의
        # UTF-8 바이트를 못 읽어 UnicodeDecodeError로 죽는 게 실측으로 확인됐다(이전엔
        # drawtext를 안 써서 커맨드라인에 한글이 없었어서 안 드러났던 잠재 버그) - 인코딩을
        # UTF-8로 명시하고, 혹시 모를 비-UTF8 바이트는 에러 대신 대체 문자로 넘어간다.
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
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
        2회 호출된다(1단계 Hype의 닉네임 샤우팅, 3단계 Main의 사실 서술). voice_key는
        ELEVENLABS_VOICE_IDS의 키("main"/"hype"/"sub") 중 하나. 나머지 네 자리(0단계 세
        목소리 + 2단계 Sub)는 닉네임이 필요 없는 순수 감정 표현이라 정적 풀에서 고른다."""
        tagged_text = f"[excited][shouts] {text}"
        voice_id = ELEVENLABS_VOICE_IDS[voice_key]
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                params={"output_format": ELEVENLABS_OUTPUT_FORMAT},
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

    async def _generate_commentary(self, kills_with_names: list[dict], lang: str,
                                    roster_pairs: list[tuple[dict | None, dict | None]] | None = None,
                                    laning_gold_gaps: list[int | None] | None = None,
                                    scoreboard: dict | None = None) -> list[dict]:
        """🛡️ [언어 분기 + 데이터 확장] lang=="en"이면 EN_SYSTEM_PROMPT + 영어 사실 문장을
        쓰고, 그 외(기본 한국어)는 기존 SYSTEM_PROMPT + _i_or_ga/_eul_or_reul 조사 처리를
        그대로 유지한다(회귀 없음). roster_pairs/laning_gold_gaps/scoreboard는 호출부
        (_run_pipeline)에서 HUD 오버레이용으로 이미 계산해둔 값을 그대로 재사용 - 새 API
        호출 없음(_format_match_context_block 참고). 셋 중 하나라도 None이면(예: 과거
        방식으로 호출하는 코드가 남아있는 경우) 컨텍스트 블록 없이 킬 사실만으로 동작한다."""
        import json
        is_en = lang == "en"
        kill_facts_lines = []
        for k in kills_with_names:
            if is_en:
                assist_str = f", assists: {', '.join(k['assists'])}" if k["assists"] else ""
                kill_facts_lines.append(
                    f"[{k['index']}] at {k['timestamp_ms']}ms - {k['killer']} kills {k['victim']}{assist_str}"
                )
            else:
                assist_str = f", 어시스트: {', '.join(k['assists'])}" if k["assists"] else ""
                kill_facts_lines.append(
                    f"[{k['index']}] {k['timestamp_ms']}ms 시점 - "
                    f"{k['killer']}{_i_or_ga(k['killer'])} {k['victim']}{_eul_or_reul(k['victim'])} 처치{assist_str}"
                )
        kill_facts_block = "\n".join(kill_facts_lines)

        context_block = ""
        if roster_pairs is not None and laning_gold_gaps is not None and scoreboard is not None:
            context_block = _format_match_context_block(roster_pairs, laning_gold_gaps, scoreboard, lang) + "\n\n"

        if is_en:
            user_content = (
                f"{context_block}Confirmed kill events "
                f"(total {len(kills_with_names)}, all must be covered):\n{kill_facts_block}"
            )
        else:
            user_content = f"{context_block}확정된 사실 목록 (총 {len(kills_with_names)}건, 전부 다뤄야 함):\n{kill_facts_block}"

        resp = await self.ai_client.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            temperature=0.8,
            messages=[
                {"role": "system", "content": EN_SYSTEM_PROMPT if is_en else SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
        )
        data = json.loads(resp.choices[0].message.content)
        lines = data["lines"]

        covered = {l["event_index"] for l in lines}
        for k in kills_with_names:
            if k["index"] not in covered:
                fallback = (
                    f"{k['killer']} takes down {k['victim']}!!" if is_en else
                    f"{k['killer']}{_i_or_ga(k['killer'])} {k['victim']}{_eul_or_reul(k['victim'])} 처치했습니다!"
                )
                lines.append({"event_index": k["index"], "text": fallback})
        lines.sort(key=lambda l: l["event_index"])
        return lines

    # ══════════════════════════════════════════════════════════
    #  Data Dragon (Riot API 키/rate limiter와 무관한 별개의 정적 CDN)
    # ══════════════════════════════════════════════════════════
    @staticmethod
    async def _download_and_cache_icon(session: aiohttp.ClientSession, url: str,
                                        cache_dir: str, filename: str) -> str:
        """URL을 받아 cache_dir/filename에 저장하고 경로를 반환하는 공통 로직(챔피언/아이템/
        고정 오브젝트 아이콘이 전부 이 패턴을 공유) - 실패하면 예외를 그대로 던진다(호출부의
        각 fetch 메서드가 자기 문맥에 맞는 로그를 남기고 None으로 변환하는 책임을 짐)."""
        os.makedirs(cache_dir, exist_ok=True)
        out_path = os.path.join(cache_dir, filename)
        timeout = aiohttp.ClientTimeout(total=DDRAGON_HTTP_TIMEOUT_SECONDS)
        async with session.get(url, timeout=timeout) as resp:
            resp.raise_for_status()
            data = await resp.read()
        with open(out_path, "wb") as f:
            f.write(data)
        # 🛡️ [손상된 캐시 방지] 디스크에 쓴 뒤 실제로 열리는 이미지인지 한 번 검증한다 - 여기서
        # 실패한 파일을 그대로 캐싱해두면 이후 모든 렌더가 같은 깨진 파일을 계속 재사용하게
        # 되므로, 검증 실패 시 파일을 지우고 예외를 던져 다음 렌더에서 다시 시도하게 한다.
        try:
            with Image.open(out_path) as im:
                im.verify()
        except Exception:
            os.remove(out_path)
            raise
        return out_path

    async def _fetch_ddragon_version(self, session: aiohttp.ClientSession) -> str:
        timeout = aiohttp.ClientTimeout(total=DDRAGON_HTTP_TIMEOUT_SECONDS)
        async with session.get(DDRAGON_VERSIONS_URL, timeout=timeout) as resp:
            resp.raise_for_status()
            versions = await resp.json(content_type=None)
        return versions[0]

    async def _fetch_champion_icon(self, champion_id: str) -> str | None:
        """챔피언 아이콘을 Data Dragon에서 받아 로컬에 캐싱하고 파일 경로를 반환한다.
        실패하면(네트워크 문제, 알 수 없는 챔피언 id, 손상된 응답 등) 예외를 던지지 않고
        None을 반환한다 - 호출부는 None이면 그냥 해당 참가자 행에 아이콘 없이 렌더링한다
        (부가 기능이 핵심 파이프라인을 막으면 안 된다는 원칙 - mapping=None sentinel과
        같은 철학)."""
        try:
            cached = glob.glob(os.path.join(CHAMPION_ICON_CACHE_DIR, f"*_{champion_id}.png"))
            if cached:
                return cached[0]
            async with aiohttp.ClientSession() as session:
                version = await self._fetch_ddragon_version(session)
                icon_url = DDRAGON_ICON_URL_TEMPLATE.format(version=version, champion_id=champion_id)
                return await self._download_and_cache_icon(
                    session, icon_url, CHAMPION_ICON_CACHE_DIR, f"{version}_{champion_id}.png")
        except Exception as e:
            print(f"[HIGHLIGHT][WARN] Champion icon fetch failed (champion={champion_id}): "
                  f"{type(e).__name__}: {e} - continuing without icon", flush=True)
            return None

    async def _fetch_item_icon(self, item_id: int) -> str | None:
        """아이템 아이콘 - item_id=0(빈 슬롯)은 Data Dragon에 파일 자체가 없어서 요청 없이
        바로 None(실패가 아니라 "표시할 게 없음"으로 취급)."""
        if not item_id:
            return None
        try:
            cached = glob.glob(os.path.join(ITEM_ICON_CACHE_DIR, f"*_{item_id}.png"))
            if cached:
                return cached[0]
            async with aiohttp.ClientSession() as session:
                version = await self._fetch_ddragon_version(session)
                icon_url = DDRAGON_ITEM_ICON_URL_TEMPLATE.format(version=version, item_id=item_id)
                return await self._download_and_cache_icon(
                    session, icon_url, ITEM_ICON_CACHE_DIR, f"{version}_{item_id}.png")
        except Exception as e:
            print(f"[HIGHLIGHT][WARN] Item icon fetch failed (item_id={item_id}): "
                  f"{type(e).__name__}: {e} - continuing without icon", flush=True)
            return None

    async def _fetch_static_icon(self, url: str, cache_name: str, cache_dir: str = STATIC_ICON_CACHE_DIR) -> str | None:
        """패치 버전 조회가 필요 없는 고정 URL 아이콘(예: Community Dragon 타워/드래곤
        아이콘)용 - 실패해도 None만 반환(위와 동일한 안전 원칙). cache_dir을 받게 해서
        룬 아이콘처럼 파일명이 안 겹치게 별도 폴더가 필요한 경우도 재사용 가능."""
        try:
            cached_path = os.path.join(cache_dir, cache_name)
            if os.path.exists(cached_path):
                return cached_path
            async with aiohttp.ClientSession() as session:
                return await self._download_and_cache_icon(session, url, cache_dir, cache_name)
        except Exception as e:
            print(f"[HIGHLIGHT][WARN] Static icon fetch failed (url={url}): "
                  f"{type(e).__name__}: {e} - continuing without icon", flush=True)
            return None

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
        # 🛡️ [언어 분기 진입점] get_msg()(cogs/base.py)와 완전히 동일한 패턴으로 guild 설정
        # 언어를 한 번만 읽어서 lang 변수로 만들고, 이후 단계(코멘터리 생성/0단계 캐스케이드/
        # 리드인 필러)에 그대로 넘긴다. 디스코드 상태 메시지(get_msg)는 이미 별도로 이 값을
        # 읽고 있어 서로 안 겹치는 두 번째 조회지만, DB가 아니라 캐시된 설정에서 읽으므로
        # 부하 문제는 없다.
        guild_settings = await self.get_guild_settings(guild_id)
        lang = guild_settings.get("language", "en")

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

        # 🛡️ [조기 종료 안전장치 - 명시적 sentinel] try/except의 return만으로도 구조상 아래
        # 매치 판별로 안 넘어가는 게 맞지만(예외 발생 시 except 블록에서 바로 return), 실제
        # 배포 사고(사실관계가 완전히 다른 매치가 선택된 사고) 조사 과정에서 "시계 인식이
        # 사실상 실패했는데도 그 이후 로직이 진행된 것처럼 보인다"는 의심이 나온 적이 있어서,
        # mapping을 None으로 시작해두고 성공 시에만 값이 들어가게 한 뒤, try/except 블록
        # 직후 "mapping이 정말로 채워졌는지"를 한 번 더 명시적으로 확인한다 - 예외 처리
        # 로직에 나중에 실수로 return이 빠지거나 흐름이 바뀌어도 이 두 번째 검사가 마지막
        # 방어선이 된다.
        mapping = None
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
        if mapping is None:
            print(f"[HIGHLIGHT][ERROR] Clock mapping is None after try/except with no exception raised - "
                  f"this should be unreachable (guild={guild_id})", flush=True)
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

        # 🛡️ [오버레이 이벤트 판별 - FIRST BLOOD / SOLO KILL] Riot API 추가 호출 없이 이미
        # 받아온 데이터로만 판별한다. FIRST BLOOD = 이 매치의 가장 이른 CHAMPION_KILL(kills가
        # _extract_champion_kills에서 timestamp_ms로 이미 정렬돼 있으므로 kills[0]과 동일
        # 시각인지 비교하면 된다). SOLO KILL = 어시스트 0명. 둘 다 해당하면(매치 첫 킬에
        # 어시스트가 없는 경우) FIRST BLOOD를 우선 표시한다. 어느 쪽도 아니면(추격전 킬 등)
        # HUD 자체를 안 띄운다 - PENTA KILL 등 나머지 이벤트는 다중 킬 백엔드가 나올 때까지
        # 보류(MAX_KILLS_PER_CLIP=1이라 판별 대상도 항상 이 킬 1건뿐).
        is_first_blood = selected[0]["timestamp_ms"] == kills[0]["timestamp_ms"]
        is_solo_kill = not selected[0]["assist_ids"]
        hud_event = "FIRST BLOOD" if is_first_blood else ("SOLO KILL" if is_solo_kill else None)

        # 🛡️ [상단 2단 스코어바용 데이터 - 추가 Riot API 호출 없음] teams[].objectives의
        # tower/champion/dragon과 participants[].goldEarned 합계 - 전부 이미 fetch된
        # chosen에서만 뽑는다. 이 스코어보드는 FIRST BLOOD/SOLO KILL 여부(hud_event)와
        # 무관하게 모든 클립에 항상 표시되는 상시 UI라서 hud_event가 None이어도 채운다.
        team_objectives = {t["teamId"]: t.get("objectives", {}) for t in chosen["info"]["teams"]}
        team100_gold = sum(p.get("goldEarned", 0) for p in chosen["info"]["participants"] if p.get("teamId") == 100)
        team200_gold = sum(p.get("goldEarned", 0) for p in chosen["info"]["participants"] if p.get("teamId") == 200)
        scoreboard = {
            "team100_towers": team_objectives.get(100, {}).get("tower", {}).get("kills", 0),
            "team200_towers": team_objectives.get(200, {}).get("tower", {}).get("kills", 0),
            "team100_kills": team_objectives.get(100, {}).get("champion", {}).get("kills", 0),
            "team200_kills": team_objectives.get(200, {}).get("champion", {}).get("kills", 0),
            "team100_dragons": team_objectives.get(100, {}).get("dragon", {}).get("kills", 0),
            "team200_dragons": team_objectives.get(200, {}).get("dragon", {}).get("kills", 0),
            "team100_riftheralds": team_objectives.get(100, {}).get("riftHerald", {}).get("kills", 0),
            "team200_riftheralds": team_objectives.get(200, {}).get("riftHerald", {}).get("kills", 0),
            "team100_barons": team_objectives.get(100, {}).get("baron", {}).get("kills", 0),
            "team200_barons": team_objectives.get(200, {}).get("baron", {}).get("kills", 0),
            "team100_hordes": team_objectives.get(100, {}).get("horde", {}).get("kills", 0),
            "team200_hordes": team_objectives.get(200, {}).get("horde", {}).get("kills", 0),
            "team100_gold": team100_gold,
            "team200_gold": team200_gold,
            "game_time_ms": selected[0]["timestamp_ms"],
        }

        # 🛡️ [드래곤 시간순 속성 시퀀스 - 팀별] timeline은 이미 fetch됨(추가 Riot API 호출
        # 없음). 순수 함수 _extract_dragon_sequence로 팀별 최근 DRAGON_SEQUENCE_MAX개의
        # monsterSubType 리스트를 뽑는다.
        team100_dragon_subtypes = _extract_dragon_sequence(timeline, 100)
        team200_dragon_subtypes = _extract_dragon_sequence(timeline, 200)

        # 🛡️ [하단 포지션별 5행 그리드용 데이터] participants 10명 전원은 chosen에 이미 다
        # fetch돼 있다(추가 Riot API 호출 없음). position(teamPosition)까지 같이 뽑아서
        # _pair_roster_by_position()이 팀 간 매칭에 쓴다.
        roster = []
        for p in chosen["info"]["participants"]:
            items = [p.get(f"item{i}", 0) for i in range(6)]
            roster.append({
                "participant_id": p["participantId"],
                "team_id": p.get("teamId"),
                "position": p.get("teamPosition") or None,
                "champion": p["championName"],
                "name": p.get("riotIdGameName") or p.get("summonerName") or "Unknown",
                "kda": (p.get("kills", 0), p.get("deaths", 0), p.get("assists", 0)),
                "cs": p.get("totalMinionsKilled", 0) + p.get("neutralMinionsKilled", 0),
                "items": items,
            })
        team100_roster = [r for r in roster if r["team_id"] == 100]
        team200_roster = [r for r in roster if r["team_id"] == 200]
        roster_pairs = _pair_roster_by_position(team100_roster, team200_roster)

        # 🛡️ [라인전 골드 격차 - timeline은 이미 fetch됨, 추가 Riot API 호출 없음]
        # roster_pairs와 정확히 같은 순서로 정렬된 리스트를 만들어서 렌더 단계에서 인덱스만
        # 맞춰 쓰면 되게 한다.
        laning_gold_gaps = _compute_laning_gold_gaps(timeline, roster_pairs)

        # 🛡️ [아이콘 전부 병렬 fetch] Data Dragon/Community Dragon 둘 다 Riot API 키/rate
        # limiter와 무관한 별개 CDN이라 전부 동시에 요청해도 안전하다 - asyncio.gather로
        # 한 번에 병렬화. item_id=0(빈 슬롯)은 _fetch_item_icon이 요청 자체를 안 보내고
        # 즉시 None을 반환하므로 안전하게 그대로 넘겨도 된다. (스펠/룬 아이콘은 패널에서
        # 제거되면서 이 fetch 자체도 삭제됨 - 더 이상 Data Dragon summoner.json/
        # runesReforged.json 요청이 나가지 않는다.)
        champion_task = asyncio.gather(*(self._fetch_champion_icon(r["champion"]) for r in roster))
        item_tasks = [asyncio.gather(*(self._fetch_item_icon(item_id) for item_id in r["items"]))
                      for r in roster]
        tower_task = self._fetch_static_icon(CDRAGON_TOWER_ICON_URL, "tower.png")
        dragon_task = self._fetch_static_icon(CDRAGON_DRAGON_ICON_URL, "dragon.png")
        riftherald_task = self._fetch_static_icon(CDRAGON_RIFTHERALD_ICON_URL, "riftherald.png")
        baron_task = self._fetch_static_icon(CDRAGON_BARON_ICON_URL, "baron.png")
        horde_task = self._fetch_static_icon(CDRAGON_HORDE_ICON_URL, "grub.png")
        # 🛡️ [드래곤 속성별 아이콘 - 중복 제거 후 한 번씩만 fetch] 두 팀 시퀀스에 같은
        # 속성이 여러 번 나와도(예: WATER_DRAGON 두 번) 캐시 파일은 하나면 되니 set으로
        # 중복 제거한다.
        dragon_variant_names = sorted({
            DRAGON_SUBTYPE_TO_CDRAGON_NAME[st]
            for st in (*team100_dragon_subtypes, *team200_dragon_subtypes)
            if st in DRAGON_SUBTYPE_TO_CDRAGON_NAME
        })
        dragon_variant_tasks = [
            self._fetch_static_icon(CDRAGON_DRAGON_VARIANT_ICON_URL_TEMPLATE.format(name=name), f"dragon_{name}.png")
            for name in dragon_variant_names
        ]

        (champion_icons, tower_icon_path, dragon_icon_path, riftherald_icon_path, baron_icon_path,
         horde_icon_path, *rest) = await asyncio.gather(
            champion_task, tower_task, dragon_task, riftherald_task, baron_task, horde_task,
            *item_tasks, *dragon_variant_tasks)
        n = len(roster)
        item_icon_lists = rest[:n]
        dragon_variant_icon_paths = rest[n:n + len(dragon_variant_tasks)]
        for r, icon_path, item_icon_paths in zip(roster, champion_icons, item_icon_lists):
            r["icon_path"] = icon_path
            r["item_icon_paths"] = item_icon_paths
        scoreboard["tower_icon_path"] = tower_icon_path
        scoreboard["dragon_icon_path"] = dragon_icon_path
        scoreboard["riftherald_icon_path"] = riftherald_icon_path
        scoreboard["baron_icon_path"] = baron_icon_path
        scoreboard["horde_icon_path"] = horde_icon_path

        # 🛡️ [팀별 드래곤 시퀀스 아이콘 경로 리스트 - 시간순 그대로] subtype이 매핑/fetch에
        # 실패하면(알려지지 않은 subtype 등) 그 자리는 조용히 건너뛴다(리스트에서 빠짐) -
        # 나머지 표시를 막을 이유가 없다.
        dragon_variant_path_by_name = dict(zip(dragon_variant_names, dragon_variant_icon_paths))

        def _dragon_icon_paths(subtypes: list[str]) -> list[str]:
            paths = []
            for st in subtypes:
                name = DRAGON_SUBTYPE_TO_CDRAGON_NAME.get(st)
                path = dragon_variant_path_by_name.get(name) if name else None
                if path:
                    paths.append(path)
            return paths

        scoreboard["team100_dragon_icon_paths"] = _dragon_icon_paths(team100_dragon_subtypes)
        scoreboard["team200_dragon_icon_paths"] = _dragon_icon_paths(team200_dragon_subtypes)

        await progress_msg.edit(content=await self.get_msg(guild_id, "highlight_progress_scripting"))
        try:
            lines_raw = await self._generate_commentary(
                kills_with_names, lang, roster_pairs=roster_pairs,
                laning_gold_gaps=laning_gold_gaps, scoreboard=scoreboard,
            )
        except Exception as e:
            print(f"[HIGHLIGHT][ERROR] Commentary generation failed (guild={guild_id}): {type(e).__name__}: {e}", flush=True)
            await progress_msg.edit(content=await self.get_msg(guild_id, "highlight_err_ai_failed"))
            return

        # MAX_KILLS_PER_CLIP=1이라 kills_with_names/lines_raw는 항상 정확히 1건.
        kill_t = kills_with_names[0]["clip_t_sec"]
        killer_name = kills_with_names[0]["killer"]
        victim_name = kills_with_names[0]["victim"]
        main_fact_text = lines_raw[0]["text"]

        # 🛡️ [킬러 이름 검증 - 3단계 Main 담당] GPT는 온도 0.8로 자유 생성돼서 "킬러 이름을
        # 강조하라"는 프롬프트 지시를 안 따르고 희생자만 부각시킨 문장을 내놓는 경우가 실제로
        # 확인됨 - 코드가 이걸 검증하는 지점이 아예 없었던 게 실질적 원인. 사실 서술 역할이
        # 3단계 Main으로 옮겨왔으므로 검증도 그대로 따라온다. LLM을 재호출하면 비용/시간이 또
        # 드니, 검증 실패 시 즉시 안전한 고정 템플릿으로 대체한다(재시도 없음).
        if not _commentary_names_killer(main_fact_text, killer_name):
            print(f"[HIGHLIGHT][WARN] Commentary text missing killer name (guild={guild_id}) - "
                  f"falling back to template. killer={killer_name!r} text={main_fact_text!r}", flush=True)
            main_fact_text = (
                f"{killer_name} takes down {victim_name}!!" if lang == "en" else
                f"{killer_name}{_i_or_ga(killer_name)} {victim_name}{_eul_or_reul(victim_name)} 처치했습니다!!"
            )

        await progress_msg.edit(content=await self.get_msg(guild_id, "highlight_progress_rendering"))

        # ── 1단계(Hype 닉네임 샤우팅) + 3단계(Main 사실 전달)만 실시간 TTS
        # (렌더당 ElevenLabs 호출 정확히 2회) - 나머지 네 자리는 정적 풀에서 고른다.
        hype_nickname_text = HYPE_NICKNAME_SHOUT_TEMPLATE.format(killer=killer_name)
        try:
            hype_nickname_wav_raw = await self._synthesize_voice_line(hype_nickname_text, "hype", work_dir, "hype_nickname_raw")
            main_fact_wav = await self._synthesize_voice_line(main_fact_text, "main", work_dir, "main_fact")
        except Exception as e:
            print(f"[HIGHLIGHT][ERROR] ElevenLabs TTS failed (guild={guild_id}): {type(e).__name__}: {e}", flush=True)
            await progress_msg.edit(content=await self.get_msg(guild_id, "highlight_err_tts_failed"))
            return

        if lang == "en":
            # 🛡️ [영어 스케줄 - 관대한 스킵] 한국어의 CRITICAL 하드-fail 게이트(풀이 하나라도
            # 비면 렌더 자체를 거부)를 그대로 쓰지 않는다 - 영어 정적 풀은 아직 실제 녹음 전이라
            # 비어있는 게 정상이고, 그때마다 렌더를 거부하면 hype_nickname/main_fact(실시간
            # TTS, 언어 무관하게 이미 동작)까지 전부 막혀버린다. 파일이 없는 자리는 그냥
            # schedule에서 빠진다(아래 각 if 블록).
            sterling_file = random.choice(STERLING_POOL) if STERLING_POOL else None
            carter_file = random.choice(CARTER_POOL) if CARTER_POOL else None
            atlee_file = random.choice(ATLEE_POOL) if ATLEE_POOL else None

            try:
                sterling_duration = (
                    await self._to_executor(self._probe_audio_duration, sterling_file) if sterling_file else 0.0
                )
                carter_duration = (
                    await self._to_executor(self._probe_audio_duration, carter_file) if carter_file else 0.0
                )
                atlee_duration = (
                    await self._to_executor(self._probe_audio_duration, atlee_file) if atlee_file else 0.0
                )
                hype_nickname_duration = await self._to_executor(self._probe_audio_duration, hype_nickname_wav_raw)
                main_fact_duration = await self._to_executor(self._probe_audio_duration, main_fact_wav)
                # 닉네임 샤우팅 뒷부분에 볼륨 스웰 후처리 - 길이는 그대로, 음량만 바뀐다.
                hype_nickname_wav = os.path.join(work_dir, "hype_nickname.wav")
                await self._to_executor(self._apply_nickname_swell, hype_nickname_wav_raw, hype_nickname_duration, hype_nickname_wav)
            except Exception as e:
                print(f"[HIGHLIGHT][ERROR] Failed to probe/post-process voice lines (guild={guild_id}): "
                      f"{type(e).__name__}: {e}", flush=True)
                await progress_msg.edit(content=await self.get_msg(guild_id, "highlight_err_render_failed"))
                return

            # 0단계 재설계: Carter가 kill_t에 시작(옛 hype_explode 자리 계승), Atlee는 Carter
            # 재생 STAGE_OVERLAP_RATIO 지점과 겹치며 시작(옛 sub_explode 자리 계승), Sterling은
            # kill_t에 끝나도록 역산 배치(옛 main_explode 자리 계승, "진행 멘트"로 역할 변경).
            schedule = {"kill_t": kill_t}
            stage0_end_times = []
            if carter_file is not None:
                carter_start = kill_t
                schedule["carter"] = {"wav": carter_file, "text": CARTER_TEXT[os.path.basename(carter_file)],
                                       "start": carter_start, "duration": carter_duration}
                stage0_end_times.append(carter_start + carter_duration)
                if atlee_file is not None:
                    atlee_start = carter_start + carter_duration * STAGE_OVERLAP_RATIO
                    schedule["atlee"] = {"wav": atlee_file, "text": ATLEE_TEXT[os.path.basename(atlee_file)],
                                          "start": atlee_start, "duration": atlee_duration}
                    stage0_end_times.append(atlee_start + atlee_duration)
            if sterling_file is not None:
                sterling_start = kill_t - sterling_duration
                if sterling_start >= 0:
                    schedule["sterling"] = {"wav": sterling_file, "text": STERLING_TEXT[os.path.basename(sterling_file)],
                                             "start": sterling_start, "duration": sterling_duration}
            stage0_dur = max(stage0_end_times) - kill_t if stage0_end_times else 0.0

            # 1/3단계(sub_question은 영어 풀이 아직 없어 이번 라운드는 스킵) - stage2_dur=0.0을
            # 넘기면 plan_kill_sequence가 "sub_question이 즉시 끝난 것"으로 계산해, main_fact가
            # hype_nickname 캐스케이드 바로 다음 지점에서 자연스럽게 시작한다.
            seq = plan_kill_sequence(stage0_dur, hype_nickname_duration, 0.0)
            hype_nickname_start = kill_t + seq["t1"]
            main_fact_start = kill_t + seq["t3"]
            schedule["hype_nickname"] = {"wav": hype_nickname_wav, "text": hype_nickname_text,
                                          "start": hype_nickname_start, "duration": hype_nickname_duration}
            schedule["main_fact"] = {"wav": main_fact_wav, "text": main_fact_text,
                                      "start": main_fact_start, "duration": main_fact_duration}

            # 리드인 필러: 한국어 pre_buildup+EOEO(고정 2자리)와 달리 1~4개를 유동적으로
            # 채운다(plan_leadin_fillers_en, 순수 함수) - 자리가 없으면 0개까지 줄어들 수 있다.
            leadin_end_times = []
            if EN_LEADIN_POOL:
                leadin_candidates = random.sample(EN_LEADIN_POOL, min(len(EN_LEADIN_POOL), EN_LEADIN_MAX_COUNT))
                leadin_durations = [await self._to_executor(self._probe_audio_duration, f) for f in leadin_candidates]
                leadin_starts = plan_leadin_fillers_en(kill_t, leadin_durations)
                for i, start in enumerate(leadin_starts):
                    f = leadin_candidates[i]
                    schedule[f"en_leadin_{i + 1}"] = {"wav": f, "text": EN_LEADIN_TEXT[os.path.basename(f)],
                                                       "start": start, "duration": leadin_durations[i]}
                    leadin_end_times.append(start + leadin_durations[i])

            end_times = stage0_end_times + leadin_end_times + [
                hype_nickname_start + hype_nickname_duration, main_fact_start + main_fact_duration,
            ]
            total_duration = max(duration, max(end_times) + RENDER_TAIL_BUFFER_SEC)
            schedule["total_duration"] = total_duration
        else:
            if not (PRE_BUILDUP_POOL and EOEO_POOL and MAIN_EXPLODE_POOL and HYPE_EXPLODE_POOL
                    and SUB_EXPLODE_POOL and SUB_QUESTION_POOL):
                print(f"[HIGHLIGHT][CRITICAL] Static voice pool missing files (guild={guild_id}): "
                      f"pre_buildup={len(PRE_BUILDUP_POOL)} eoeo={len(EOEO_POOL)} "
                      f"main_explode={len(MAIN_EXPLODE_POOL)} hype_explode={len(HYPE_EXPLODE_POOL)} "
                      f"sub_explode={len(SUB_EXPLODE_POOL)} sub_question={len(SUB_QUESTION_POOL)}", flush=True)
                await progress_msg.edit(content=await self.get_msg(guild_id, "highlight_err_unexpected"))
                return

            pre_buildup_file = random.choice(PRE_BUILDUP_POOL)
            eoeo_file = random.choice(EOEO_POOL)
            main_explode_file = random.choice(MAIN_EXPLODE_POOL)
            hype_explode_file = random.choice(HYPE_EXPLODE_POOL)
            sub_explode_file = random.choice(SUB_EXPLODE_POOL)
            sub_question_file = random.choice(SUB_QUESTION_POOL)

            try:
                pre_buildup_duration = await self._to_executor(self._probe_audio_duration, pre_buildup_file)
                eoeo_duration = await self._to_executor(self._probe_audio_duration, eoeo_file)
                main_explode_duration = await self._to_executor(self._probe_audio_duration, main_explode_file)
                hype_explode_duration = await self._to_executor(self._probe_audio_duration, hype_explode_file)
                sub_explode_duration = await self._to_executor(self._probe_audio_duration, sub_explode_file)
                hype_nickname_duration = await self._to_executor(self._probe_audio_duration, hype_nickname_wav_raw)
                main_fact_duration = await self._to_executor(self._probe_audio_duration, main_fact_wav)
                sub_question_duration = await self._to_executor(self._probe_audio_duration, sub_question_file)
                # 닉네임 샤우팅 뒷부분에 볼륨 스웰 후처리 - 길이는 그대로, 음량만 바뀐다.
                hype_nickname_wav = os.path.join(work_dir, "hype_nickname.wav")
                await self._to_executor(self._apply_nickname_swell, hype_nickname_wav_raw, hype_nickname_duration, hype_nickname_wav)
            except Exception as e:
                print(f"[HIGHLIGHT][ERROR] Failed to probe/post-process voice lines (guild={guild_id}): "
                      f"{type(e).__name__}: {e}", flush=True)
                await progress_msg.edit(content=await self.get_msg(guild_id, "highlight_err_render_failed"))
                return

            # 0단계: Main+Hype+Sub 셋 다 kill_t에 정확히 동시 시작(닉네임 없는 순수 폭발).
            # plan_kill_sequence()는 순수 함수 - 1/2/3단계 시작을 "직전 단계 최장 목소리 길이 ×
            # STAGE_OVERLAP_RATIO" 지점으로 잡는다(고정 초 아님, 0단계 길이와 무관하게 1/2/3단계
            # 상호 간격은 각자 자기 길이 × 비율로만 정해진다 - 0단계가 길어져도 t2-t1/t3-t2 간격
            # 자체는 안 변하고, 셋 다 kill_t 기준으로 똑같이 더 뒤로 밀릴 뿐이다).
            stage0_dur = max(main_explode_duration, hype_explode_duration, sub_explode_duration)
            seq = plan_kill_sequence(stage0_dur, hype_nickname_duration, sub_question_duration)
            hype_nickname_start = kill_t + seq["t1"]
            sub_question_start = kill_t + seq["t2"]
            main_fact_start = kill_t + seq["t3"]

            # 킬 이전 리드인: 클립 시작(t=0) 기준으로 상황 멘트 -> "어어??" 순서로 배치하고,
            # kill_t와 안 겹치는지만 검사한다(plan_lead_in_forward가 순수 함수로 계산).
            pre_buildup_start, eoeo_start = plan_lead_in_forward(kill_t, pre_buildup_duration, eoeo_duration)

            end_times = [
                kill_t + main_explode_duration, kill_t + hype_explode_duration, kill_t + sub_explode_duration,
                hype_nickname_start + hype_nickname_duration, sub_question_start + sub_question_duration,
                main_fact_start + main_fact_duration,
            ]
            total_duration = max(duration, max(end_times) + RENDER_TAIL_BUFFER_SEC)

            schedule = {
                "kill_t": kill_t,
                "total_duration": total_duration,
                "main_explode": {"wav": main_explode_file, "text": MAIN_EXPLODE_TEXT[os.path.basename(main_explode_file)],
                                  "start": kill_t, "duration": main_explode_duration},
                "hype_explode": {"wav": hype_explode_file, "text": HYPE_EXPLODE_TEXT[os.path.basename(hype_explode_file)],
                                  "start": kill_t, "duration": hype_explode_duration},
                "sub_explode": {"wav": sub_explode_file, "text": SUB_EXPLODE_TEXT[os.path.basename(sub_explode_file)],
                                 "start": kill_t, "duration": sub_explode_duration},
                "hype_nickname": {"wav": hype_nickname_wav, "text": hype_nickname_text,
                                   "start": hype_nickname_start, "duration": hype_nickname_duration},
                "sub_question": {"wav": sub_question_file, "text": SUB_QUESTION_TEXT[os.path.basename(sub_question_file)],
                                  "start": sub_question_start, "duration": sub_question_duration},
                "main_fact": {"wav": main_fact_wav, "text": main_fact_text,
                              "start": main_fact_start, "duration": main_fact_duration},
            }
            if eoeo_start is not None:
                schedule["eoeo"] = {"wav": eoeo_file, "text": EOEO_TEXT[os.path.basename(eoeo_file)],
                                     "start": eoeo_start, "duration": eoeo_duration}
            if pre_buildup_start is not None:
                schedule["pre_buildup"] = {"wav": pre_buildup_file, "text": PRE_BUILDUP_TEXT[os.path.basename(pre_buildup_file)],
                                            "start": pre_buildup_start, "duration": pre_buildup_duration}
        # 🛡️ [오버레이 HUD 타이밍] 명세서의 고정값이 아니라 이 렌더의 실제 schedule 타이밍을
        # 그대로 재사용한다 - kill_t(0단계, 킬 순간)에 등장해서 3단계(사실 전달)가 끝날 때
        # 같이 퇴장하는 것으로 잡았다(플레이어에게 "이 킬에 대한 설명이 끝났다"는 인상과
        # HUD 퇴장을 맞추기 위함) - plan_kill_sequence/plan_lead_in_forward와 마찬가지로
        # 새 상수를 발명하지 않고 이미 계산된 값(main_fact_start/duration)만 소비한다.
        if hud_event is not None:
            schedule["hud"] = {"event_label": hud_event, "start": kill_t,
                                "end": main_fact_start + main_fact_duration}
        # 🛡️ 스코어바/로스터 그리드는 FIRST BLOOD/SOLO KILL 여부와 무관하게 항상 표시 - hud
        # 키와 달리 조건 없이 매번 채운다.
        schedule["scoreboard"] = scoreboard
        schedule["roster_pairs"] = roster_pairs
        schedule["laning_gold_gaps"] = laning_gold_gaps

        out_mp4 = os.path.join(work_dir, "highlight_final.mp4")
        try:
            await self._to_executor(self._render_video, video_path, duration, width, height, schedule, work_dir, out_mp4)
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
    _log_ffmpeg_filter_support()
    cog = KyvoHighlight(bot)
    await bot.add_cog(cog)
    print("[⚡ HIGHLIGHT] Cog extension setup complete.", flush=True)
