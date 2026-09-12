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
# 🛡️ [buildup1_b.wav 제외] "어?! 뭔가...?!" 계열보다 "어어?!" 계열을 우선 쓰라는 요청으로,
# 풀을 buildup1_a.wav 하나로만 좁혔다(풀에 하나뿐이라 "우선"이 곧 "유일"). buildup1_b.wav
# 파일 자체는 디스크에 남아있지만 더는 참조되지 않는다.
EOEO_POOL = sorted(glob.glob(os.path.join(VOICE_DIR, "buildup1_a.wav")))
EOEO_TEXT = {
    "buildup1_a.wav": "어어?!",
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
}
HYPE_EXPLODE_TEXT = {
    "hype_a.wav": "와" + "아" * 10 + "악!!",  # 2.08s, 무음/깊은 딥 없음(정밀 기준 통과)
    "hype_b.wav": "우와" + "아" * 8 + "!!",  # 1.76s, 무음/깊은 딥 없음(정밀 기준 통과)
    "hype_c.wav": "으" + "아" * 6 + "악!",  # 1.36s, 무음/깊은 딥 없음(정밀 기준 통과)
}
SUB_EXPLODE_TEXT = {
    "sub_shout_a.wav": "우와" + "아" * 8 + "!!",  # 1.60s, 무음/깊은 딥 없음(정밀 기준 통과)
    "sub_shout_b.wav": "히" + "이" * 8 + "!!",  # 1.44s, 무음/깊은 딥 없음(정밀 기준 통과)
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
# 🛡️ [1단계 - 방송 뷰포트 축소 + 여백 프레임] 이전 버전(사용자가 제시한 정확한 공식)은
# 배너를 항상 "화면 맨 아래에 딱 붙여서" 얹었는데, 이건 게임 원본 픽셀 위에 그대로 겹치는
# 것이라 실제 라이브 클립에서는 스킬바를, 리플레이 클립에서는 스크러버를 가리는 문제가
# 실측으로 확인됐다(미해결로 남아있던 버그). 이번 라운드에서 조사한 실제 LCK 방송 레이아웃
# 관례(좌우는 거의 안 줄이고 상/하로만 여백을 확보)를 반영해, 게임 화면 자체를 가로세로
# 동일 비율로 축소(왜곡 없음)하고 캔버스 크기(final_width x final_height)는 그대로 유지한
# 채 화면 상단 중앙에 배치한다. 그 결과 화면 하단에 실제로 게임 픽셀이 전혀 없는 여백 띠가
# 생기고(좌우에도 대칭으로 작은 여백이 남지만 이번 라운드에서는 비워둠 - 3단계 사이드 패널
# 후보), 배너는 이 여백 안에만 배치되므로 어떤 클립 UI와도 구조적으로 겹칠 수 없다.
GAME_VIEWPORT_SCALE = 0.90  # 게임 화면을 가로/세로 동일 비율로 10% 축소 - 조사에서 추정한
                            # LCK 하단 여백 비율(약 10~13%)과 맞아떨어지는 값
HUD_SLIDE_SEC = 0.4  # 배너 슬라이드업/다운 소요 시간 - PRE_BUILDUP_START_OFFSET_SEC과 같은 템포
# 🛡️ [배너 크기/위치 - 여백 띠 안에서 원본 비율 유지] 배너는 이제 위에서 만든 하단 여백 띠
# (높이 = final_height - 축소된 게임 높이)를 정확히 꽉 채운다. panel_*.png 원본 종횡비
# (1920x120=16:1, PIL로 실측 확인)를 _render_video에서 런타임에 직접 읽어서 유지하므로,
# 예전처럼 폭/높이를 독립 비율로 계산하다 텍스트가 미세하게 눌리던 문제가 없다. 가로는
# 캔버스 전체 폭 기준 중앙 정렬 - 실제 LCK 하단 배너도 게임 뷰포트보다 넓게 걸치는 경우가
# 많아 이 쪽이 더 방송처럼 보인다.


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

        # 🛡️ [오버레이 HUD 입력] FIRST BLOOD/SOLO KILL일 때만(schedule에 "hud" 키가 있을
        # 때만) 완성 배너 PNG를 추가 입력으로 붙인다 - 해당 없는 킬(추격전 등)에서는 아예
        # 입력조차 안 넣어서 필터그래프가 더 무거워지지 않는다.
        hud = schedule.get("hud")
        hud_panel_idx = None
        if hud is not None:
            inputs += ["-i", HUD_BANNER_PNGS[hud["event_label"]]]
            hud_panel_idx = next_input_idx
            next_input_idx += 1

        # ── 화면 처리 (해설은 음성 전용 - 화면에 텍스트를 그리지 않는다. 단, HUD 오버레이는
        # 예외 - 오디오와 무관하게 화면에 그리는 유일한 요소) ──
        video_filters = []
        # 🛡️ 유저가 1440p/4K 등 고해상도 클립을 올리면(크기만 100MB 이내면 통과되므로
        # 충분히 가능) 목표 비트레이트가 픽셀 수 대비 너무 낮아져 화질이 심하게 뭉개진다 -
        # 스케일을 먼저 걸어 픽셀 수 자체를 낮춰둔다. -2로 짝수 높이 보장(libx264 요구사항).
        if video_width > MAX_OUTPUT_WIDTH:
            video_filters.append(f"scale={MAX_OUTPUT_WIDTH}:-2")
            # HUD 배너 비율 계산은 실제로 화면에 나오는 최종 해상도를 기준으로 해야 한다 -
            # scale=-2가 짝수로 반올림하는 것까지 그대로 흉내내서 final_width/height를 미리
            # 구해둔다(ffmpeg가 실제로 무슨 픽셀을 뽑는지와 1px 이내로 맞음, 배너 비율
            # 계산엔 그 정도 오차는 무관하다).
            final_width = MAX_OUTPUT_WIDTH
            final_height = int(round(video_height * MAX_OUTPUT_WIDTH / video_width / 2) * 2)
        else:
            final_width, final_height = video_width, video_height

        # 🛡️ [방송 뷰포트 축소] 게임 화면을 GAME_VIEWPORT_SCALE 비율로 균일 축소(가로세로
        # 동시에, 왜곡 없음)한 뒤 캔버스(final_width x final_height, 위에서 계산한 값 그대로
        # 유지)에 상단 중앙 정렬로 pad한다 - scale+pad 두 필터로 "축소 + 주변 여백 생성"이
        # 동시에 끝나서 별도 배경색 입력이나 추가 overlay 스텝이 필요 없다. -2 대신 짝수
        # 반올림을 직접 계산하는 이유는 pad의 x좌표/캔버스 크기 계산에 정확한 정수 값이
        # 바로 필요해서(MAX_OUTPUT_WIDTH 분기의 final_height 계산과 동일한 패턴).
        scaled_w = int(round(final_width * GAME_VIEWPORT_SCALE / 2) * 2)
        scaled_h = int(round(final_height * GAME_VIEWPORT_SCALE / 2) * 2)
        pad_x = (final_width - scaled_w) // 2
        video_filters.append(f"scale={scaled_w}:{scaled_h}")
        video_filters.append(f"pad={final_width}:{final_height}:{pad_x}:0:black")
        margin_height = final_height - scaled_h  # 하단 여백 띠의 실제 높이(반올림 오차까지 반영)

        # 🛡️ 원본 클립보다 렌더 길이가 길어지면(빌드업+메인+하이프+서브 꼬리가 원본 영상
        # 길이를 넘어서는 게 일반적) 영상 쪽도 늘려야 오디오가 잘려나가지 않는다. 화면을
        # 정지시키는 대신 마지막 프레임을 그대로 붙잡아 늘리는 가장 단순한 방법(tpad) -
        # 이전 프로토타입의 펀치인 줌/비네트는 이번 라운드 범위 밖.
        extra_video_sec = max(0.0, total_duration - video_duration)
        if extra_video_sec > 0.01:
            video_filters.append(f"tpad=stop_mode=clone:stop_duration={extra_video_sec:.3f}")
        video_base_label = "vbase" if hud is not None else "vout"
        video_chain = (("[0:v]" + ",".join(video_filters) + f"[{video_base_label}]") if video_filters
                        else f"[0:v]copy[{video_base_label}]")

        if hud is not None:
            # 🛡️ [여백 띠 안에 원본 비율 유지 배치] 배너 높이는 위에서 만든 하단 여백
            # (margin_height)을 그대로 꽉 채우고, 폭은 panel_*.png 원본 종횡비를 유지하도록
            # 실제 PNG 크기를 런타임에 읽어서 계산한다(하드코딩된 비율 상수가 원본과 어긋나
            # 텍스트가 눌리던 이전 버전의 문제를 근본적으로 없앰 - PIL로 1920x120=16:1 확인됨).
            # 가로는 캔버스 전체 폭 기준 중앙 정렬. min()으로 캔버스 폭을 넘지 않게 방어.
            with Image.open(HUD_BANNER_PNGS[hud["event_label"]]) as banner_im:
                banner_native_w, banner_native_h = banner_im.size
            banner_height = margin_height
            banner_width = min(final_width, int(round(banner_height * banner_native_w / banner_native_h)))
            x_start = (final_width - banner_width) // 2
            y_start_visible = final_height - banner_height  # 여백 띠의 최상단 = 축소된 게임 화면 바로 아래

            hud_start, hud_end, slide = hud["start"], hud["end"], HUD_SLIDE_SEC
            banner_x = str(x_start)
            visible_y = str(y_start_visible)
            hidden_y = str(final_height + 10)  # 슬라이드 시작 전/후엔 화면 밖으로
            panel_y = _hud_slide_y_expr(hud_start, hud_end, slide, visible_y, hidden_y)
            hud_visible_window = f"between(t,{hud_start:.3f},{hud_end + slide:.3f})"

            hud_chain = (
                f";[{hud_panel_idx}:v]scale={banner_width}:{banner_height}[vhudscaled]"
                f";[{video_base_label}][vhudscaled]overlay=x='{banner_x}':y='{panel_y}':"
                f"enable='{hud_visible_window}'[vout]"
            )
        else:
            hud_chain = ""

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

        filter_complex = f"{video_chain}{hud_chain};{full_audio}"

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
        main_fact_text = lines_raw[0]["text"]

        # 🛡️ [킬러 이름 검증 - 3단계 Main 담당] GPT는 온도 0.8로 자유 생성돼서 "킬러 이름을
        # 강조하라"는 프롬프트 지시를 안 따르고 희생자만 부각시킨 문장을 내놓는 경우가 실제로
        # 확인됨 - 코드가 이걸 검증하는 지점이 아예 없었던 게 실질적 원인. 사실 서술 역할이
        # 3단계 Main으로 옮겨왔으므로 검증도 그대로 따라온다. LLM을 재호출하면 비용/시간이 또
        # 드니, 검증 실패 시 즉시 안전한 고정 템플릿으로 대체한다(재시도 없음).
        if not _commentary_names_killer(main_fact_text, killer_name):
            print(f"[HIGHLIGHT][WARN] Commentary text missing killer name (guild={guild_id}) - "
                  f"falling back to template. killer={killer_name!r} text={main_fact_text!r}", flush=True)
            main_fact_text = f"{killer_name}{_i_or_ga(killer_name)} {victim_name}{_eul_or_reul(victim_name)} 처치했습니다!!"

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
