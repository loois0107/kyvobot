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
import itertools
import os
import random
import re
import shutil
import subprocess
import tempfile
import time
import wave
from concurrent.futures import ThreadPoolExecutor

import aiohttp
import discord
from discord import app_commands
import imageio_ffmpeg
from openai import AsyncOpenAI
from PIL import Image, ImageFont

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
# 🛡️ [배경음 이원화 - 앰비언스 레이어 신규] crowd_cheer_4.wav(킬 시점 정점) 하나만으로는
# 리드인 구간(킬 전)이 상대적으로 밋밋하다는 판단으로, ElevenLabs Sound Effects API로
# 생성한 "낮고 평탄한 경기장 웅성거림"(mean -41.3dB, 1초 창 RMS 변동폭 0.92dB - crowd_cheer
# 4종(mean -16~-21dB)보다 20dB 이상 낮음, 실측 확인)을 클립 전체에 상시 배경으로 추가로
# 깐다. 30초짜리라 최대 클립 길이(45초+꼬리)를 못 채울 수 있어 렌더 시 -stream_loop -1로
# 무한 루프시킨다(아래 _render_video).
AMBIENT_SFX_PATH = os.path.join(SFX_DIR, "ambient_crowd_low.wav")

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

# 🛡️ [하루 사용 한도 - cogs/anonymous_reports.py의 REPORT_DAILY_LIMIT/REPORT_DAILY_WINDOW_SECONDS
# 패턴 그대로 재사용] OCR(시계 인식, 렌더당 6~24회 GPT-4o-mini 비전 호출)+ElevenLabs TTS(렌더당
# 2~4회)는 기존 쿨다운(유저당 30초)·동시처리(전역 1개)로는 "하루 총 비용"을 전혀 막지 못한다 -
# 이 셋은 서로 다른 축(스팸 방지/서버 부하/일일 총량)이라 겹치지 않고 전부 같이 걸린다. ex=86400은
# "자정 리셋"이 아니라 "이 윈도우 안에서 첫 요청 시점 기준 24시간 롤링"이다(anonymous_reports와
# 동일 선택) - KST/UTC 자정 중 어느 쪽으로 고정하든 자정 직전에 몰아 쓰고 자정 직후 또 몰아 쓰는
# 우회가 가능해지는데, 롤링 윈도우는 그 우회가 원천적으로 불가능하다. 이미 이 봇의 "일일 한도"
# 기능 2곳(anonymous_reports, ticket_ai)이 전부 이 방식이라, /highlight만 자정 고정으로 다르게
# 가면 오히려 일관성이 깨진다.
HIGHLIGHT_DAILY_WINDOW_SECONDS = 86400
HIGHLIGHT_DAILY_LIMIT_USER = 5
HIGHLIGHT_DAILY_LIMIT_GUILD = 30

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
# 🛡️ [킬 존재 검증 범위 - 2 -> 5] 2차 판별에서 거리(종료 시각 근접도) 상위 몇 개까지
# timeline을 추가 조회해 실제 킬 존재를 확인할지. 처음엔 2개로 시작했는데, 사이에 게임을
# 여러 판 더 하고서야 리플레이를 녹화하는 경우 정답이 3~4번째로 밀릴 수도 있어 5개로
# 넓혔다 - 순서대로 조회하다 킬이 있는 첫 후보에서 즉시 멈추므로(조기 종료), 정답이
# 상위권에 있는 흔한 경우엔 실제로 5개를 다 조회하지 않는다.
MATCH_KILL_VERIFY_TOP_N = 5

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
# 🛡️ [앰비언스 레이어 게인 - 0dB, 추가 부스트 없음] ambient_crowd_low.wav도 crowd_cheer_2/4와
# 같은 이유로 추가 게인을 얹지 않는다 - 애초에 "낮고 평탄하게"라는 목표 레벨(mean -41.3dB)에
# 맞춰 생성/검증해둔 에셋이라, 여기에 SFX_MIX_GAIN_DB(+6dB)를 얹으면 "훨씬 낮게"라는 요구를
# 못 지킨다. 목소리(VOICE_MIX_GAIN_DB 6~9dB 부스트 후 실효 레벨)보다도 한참 아래에 남도록
# 0dB 그대로 믹스한다(실측: 최종 믹스에서 목소리 대비 25dB 이상 낮음 - 아래 검증 참고).
AMBIENT_MIX_GAIN_DB = 0.0
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
# 🛡️ [0단계 체감 강화] 킬 순간 "동시 폭발" 임팩트를 더 세게 느끼도록, 0단계 목소리
# (main_explode/hype_explode/sub_explode(KO), 그리고 0단계로 쓰이는 sterling/carter/
# atlee(EN))에만 나머지 단계(1~3단계: 닉네임/의문형/사실전달)보다 +3dB를 얹는다.
# 🛡️ [main_explode/sub_explode 둘 다 파일 단위 오버라이드로 분리] 두 풀 다 처음엔 역할
# 단위로 게인을 올렸었다(main +7.0dB, sub +5.5dB, v4가 v3 hype보다 조용해진 문제 보정용).
# 이후 두 풀 다 "v4 톤이 얇고 꽥꽥거림" 피드백으로 대부분을 v3로 원복하고 파일 하나만
# v4로 남기면서(main_explode_d, sub_shout_c - 각각 25%/19% 비중), 역할 단위 게인 하나로는
# v3로 돌아온 파일들과 v4로 남은 파일을 동시에 만족 못 시키는 문제가 생겼다(v3 파일에
# 보정용 게인을 그대로 주면 hype보다 오히려 더 커짐). 그래서 게인 조회를 "파일명 우선 ->
# 역할 단위 -> 기본값" 3단계로 확장해 v4로 남은 파일 하나씩만 파일 단위로 높은 게인을
# 준다. alimiter가 이미 하드 리미터(SFX_LIMITER_CEILING=0.65, level=false)로 걸려 있어
# 게인을 올려도 최종 출력은 안전하게 캡된다(리미터 천장을 0.65->0.75~0.8로 완화해서
# 격차 자체를 줄이는 방법도 실측했으나, 격차가 v3/v4 오디오 자체의 라우드니스 밀도
# 차이에서 오는 것이라 천장을 올려도 안 줄고(오히려 소폭 더 벌어짐) 전체 오디오만
# 다같이 커지는 부작용만 있어 폐기 - alimiter는 0단계 전용이 아니라 게임 오디오/SFX/
# 배경음/1~3단계까지 전부 공유하는 단일 최종 리미터라 파급 범위도 컸다).
# 🛡️ [main/hype/sub_explode 역할 기본값 9.0dB -> 6.0dB - v4 마스킹 부분 완화] 0단계는
# main/hype/sub_explode 3트랙이 kill_t에 완전히 동시 시작해서 하나의 amix+공유 alimiter로
# 섞인다 - v4로 남은 파일(main_explode_d/sub_shout_c)에 파일 단위로 게인을 올려도(+7.0dB)
# main+hype 두 트랙이 이미 리미터 헤드룸을 거의 다 채우고 있어서 실측 결과 v4 쪽 기여분이
# 단독 대비 -11dB 넘게 마스킹당하는 게 확인됐다(실험 로그 참고). 리미터 천장 완화/
# acompressor 둘 다 효과 없거나 역효과라 폐기하고, main/hype/sub_explode의 역할 기본값을
# 9.0dB(6.0+3.0)에서 6.0dB(6.0+0.0)로 낮추는 걸 택했다 - 실측상 이 정도 완화로는 v4
# 마스킹 격차(10dB)의 1.7dB 정도만 회복되어 완전한 해결은 아니지만, "전부 v3인 평소
# 케이스" 음량 손해가 -0.1dB로 무시할 수준이라(리미터가 이미 포화 상태라 개별 트랙
# 게인을 이 범위에서 낮춰도 최종 출력엔 거의 안 티남) 반영할 가치가 있다고 판단했다.
# sterling/carter/atlee(EN 0단계)는 이번 라운드 범위 밖이라 +3.0dB 그대로 둔다.
# 🛡️ [다음 세션 참고 - 0단계 v4 마스킹 근본 해결 메모, 오늘 범위 밖] 3트랙 동시재생+
# 단일 공유 리미터 구조 자체가 원인이라 게인 조정만으로는 완전히 못 푼다. 근본 해법
# 후보: sub만 별도 사이드체인/덕킹, 또는 0단계 전용 분리 믹싱 단계(공유 리미터를 안
# 타는 구조) - 파급 범위가 커서 별도 세션에서 설계 필요.
VOICE_MIX_GAIN_DB_OVERRIDE = {
    "main_explode": VOICE_MIX_GAIN_DB + 0.0,
    "hype_explode": VOICE_MIX_GAIN_DB + 0.0,
    "sub_explode": VOICE_MIX_GAIN_DB + 0.0,
    "sterling": VOICE_MIX_GAIN_DB + 3.0,
    "carter": VOICE_MIX_GAIN_DB + 3.0,
    "atlee": VOICE_MIX_GAIN_DB + 3.0,
}
# 🛡️ [파일명 단위 오버라이드 - 역할 단위보다 먼저 조회됨] main_explode_d.wav/sub_shout_c.wav
# 만 각각 v4로 남아 있어(나머지는 v3) 역할 기본값(+3.0dB)과 무관하게 이 파일들만 개별
# 게인을 받는다 - 둘 다 게인을 +5.5~7.0dB 범위에서 실측 스윕해본 결과 어느 지점부터는
# 게인을 더 올려도 (이미 리미터가 깊게 걸린 상태라) 최종 라우드니스가 거의 안 늘어나는
# 한계 효용 구간이라(sub_shout_c 기준 11.5dB->13.0dB total로 올려도 -12.6dB->-12.5dB로
# 0.1dB밖에 안 늘어남), 두 파일 다 +7.0dB로 통일했다 - hype(v3, -12.2dB 최종)에 근접한
# -12.5~-12.7dB까지 회복됨. 이 딕셔너리에 없는 파일은 그대로 VOICE_MIX_GAIN_DB_OVERRIDE
# (역할 단위) -> VOICE_MIX_GAIN_DB(기본값) 순으로 fallback한다.
VOICE_MIX_GAIN_DB_FILE_OVERRIDE = {
    "main_explode_d.wav": VOICE_MIX_GAIN_DB + 7.0,
    "sub_shout_c.wav": VOICE_MIX_GAIN_DB + 7.0,
    # 🛡️ [v4 비중 확대로 추가된 두 파일 - 기존 v4와 동일 보정] main_explode_e/sub_shout_f도
    # v4라 main_explode_d/sub_shout_c와 같은 마스킹 문제를 겪을 것으로 보여 동일하게
    # +7.0dB를 적용한다 - 별도로 스윕 재검증하진 않았다(기존 두 파일과 같은 풀/믹스
    # 구조를 공유하므로 같은 보정값이 합리적인 출발점).
    "main_explode_e.wav": VOICE_MIX_GAIN_DB + 7.0,
    "sub_shout_f.wav": VOICE_MIX_GAIN_DB + 7.0,
    # 🛡️ [sub_explode v3 그룹 보정 - main/hype 대비 raw 음량 약 2dB 낮음] sub_shout_c(v4)를
    # 제외한 v3 4개(a/b/d/e)의 raw 평균 음량이 main_explode/hype_explode보다 약 2dB
    # 체계적으로 낮게 실측됐다(v3/v4 문제가 아니라 sub_explode 녹음 자체와 main/hype
    # 녹음 사이의 원본 음량 격차) - 역할 단위(+0.0dB)가 아니라 이 4개 파일에만 +2.0dB를
    # 더해 main/hype 수준에 맞춘다. sub_shout_c는 이미 더 큰 상태라 대상에서 제외.
    "sub_shout_a.wav": VOICE_MIX_GAIN_DB + 2.0,
    "sub_shout_b.wav": VOICE_MIX_GAIN_DB + 2.0,
    "sub_shout_d.wav": VOICE_MIX_GAIN_DB + 2.0,
    "sub_shout_e.wav": VOICE_MIX_GAIN_DB + 2.0,
}

# ══════════════════════════════════════════════════════════
#  0~3단계 킬 리액션 시퀀스 (전면 재설계 - 오늘 저녁 로컬 프로토타입 v1~v9에서 검증) -
#  전체 순서: 상황 멘트(0단계 전) -> "어어??"(0단계 전) -> 0단계(킬 순간 3인 동시 폭발) ->
#  1단계(Hype 닉네임) -> 2단계(Sub 의문형) -> 3단계(Main 사실 전달)
#  각 단계 시작은 "이전 단계 최장 음성 길이 × STAGE_OVERLAP_RATIO" - 고정 초가 아니다(0~3단계
#  한정, 킬 이전 두 리드인 단계는 아래 별도 gap 규칙).
# ══════════════════════════════════════════════════════════
# 🛡️ [비용 설계] 실제 킬러 닉네임이 필요한 곳은 정확히 두 군데 - (1) 1단계 Hype의 닉네임
# 샤우팅(3보이스 동시 콜로 확장), (2) 3단계 Main의 사실 전달. 나머지 자리(상황 멘트+어어??+
# 0단계 세 목소리+2단계 Sub)는 닉네임이 필요 없는 순수 감정 표현이라 정적 풀로 미리 구워둔다
# - 렌더당 ElevenLabs 실시간 호출은 정확히 4회(닉네임 3보이스+사실전달 1)로 고정(문자 수
# 자체는 짧은 외침/한 문장이라 부담이 크지 않고, asyncio.gather로 전부 병렬 처리한다).
VOICE_DIR = os.path.join(REPO_ROOT, "assets", "highlight_voice")

# ── 킬 이전 리드인 1/2: 상황 멘트 -> "어어??" -> (0단계로 이어짐) ──
# 🛡️ [환각 위험 차단] "소리지르기 전에 상황 멘트"라는 요청 자체에 예시로 "탑쪽은 신경전이
# 벌어지는 중이네요" 같은 특정 라인(탑) 지목 문구가 포함돼 있었는데, 이건 실제로 위험하다 -
# 킬이 탑에서 안 났으면 명백한 오지어낸 사실이 된다(SYSTEM_PROMPT가 지키는 "목록에 없는
# 내용은 절대 지어내지 마라" 원칙과 정면으로 어긋남). 위치/챔피언을 지목하는 문장은 여전히
# 절대 금지지만, "지금 이 구도가 긴장 국면이다"라는 사실 자체는 어떤 킬 클립에도 항상
# 참이라 안전하다(누가/어디서/무엇을 했는지는 말하지 않고, "긴장하고 있다"는 상태만 서술).
# 🛡️ [글롭 패턴 충돌 수정 - PRE_BUILDUP_URGENT_POOL 도입으로 발견] "pre_buildup_*.wav"는
# 뒤에 어떤 파일명이 와도 다 매칭되는 와일드카드라, pre_buildup_urgent_01.wav 같은 파일도
# 여기 같이 잡혀버렸다(실측: 13개여야 할 pool이 17개로 나옴 - urgent 4개가 섞여 들어감).
# 기존 calm 파일이 전부 "pre_buildup_" + 두 자리 숫자(01~13) 형식이라, 언더스코어 뒤
# 첫 글자가 숫자([0-9])인 것만 매칭하도록 좁혀서 urgent_*처럼 문자로 시작하는 접미사는
# 배제한다.
PRE_BUILDUP_POOL = sorted(glob.glob(os.path.join(VOICE_DIR, "pre_buildup_[0-9]*.wav")))
# 🛡️ [2차 전면 재녹음 - "순수 추임새" -> "상황 서술형"으로 재전환] 1차 재녹음(위 주석,
# 폐기됨)에서는 "-는데요?" 서술형이 캐스터답지 않다는 피드백으로 1음절 추임새(음/어/오호 등)
# 로 갔었는데, 이번엔 반대 방향 피드백 - 추임새만으로는 리드인이 너무 밋밋하니 "지금 긴장
# 국면이다"를 서술하는 짧은 문장(2~4어절, 요체)으로 다시 바꿔달라는 요청. 위치/챔피언/액션을
# 언급하지 않고 "긴장 상태"만 서술하면 1차 재녹음 때 문제였던 환각 위험(특정 라인 지목)은
# 그대로 피할 수 있다는 게 확인되어 전면 교체를 진행했다.
# 🛡️ [무음 게이트 재검증 - 예상과 다른 결과] 1차 재녹음 주석에는 "짧을수록 통과율이 높다"고
# 적혀 있었지만, 이번엔 반대로 완성된 문장(1.3~2.3s) 14개가 14/14 전부 1~2회 시도 만에
# 무음 게이트(silencedetect noise=-35dB:d=0.15, 0.15초 이상 무음 0곳) 통과했다 - 짧은
# 감탄사보다 오히려 더 안정적이었다. 추정: 온전한 문장은 자연스러운 억양·호흡 흐름이 있어서
# 문장 중간에 뚝 끊기는 부자연스러운 무음 구간이 덜 생기고, 반면 1음절 감탄사/의성어는
# 발화가 짧아서 끝소리가 감쇠되는 구간이 통째로 -35dB 밑으로 떨어지기 쉬웠던 것으로 보인다.
# "짧을수록 안전하다"는 이전 결론은 문장 구조에 따라 뒤집힐 수 있다는 뜻이라, 앞으로도
# 매번 실측으로 재확인이 필요하다. 원본 6개(순수 추임새)는 _backup_pre_buildup_situational_
# 20260929/ 에 백업.
# 🛡️ [재녹음 - 톤을 명시적으로 차분하게] 처음 생성할 때는 voice_settings를 아예 지정하지
# 않아서(ElevenLabs API 기본값) 톤 제어가 전혀 없었다는 게 재확인됨(오늘 낮 main_explode의
# 흥분 톤 설정이 실수로 재사용된 건 아니었음 - 그냥 아무 설정도 없었던 것). 상황 서술은
# 흥분보다는 차분하게 긴장을 짚어주는 쪽이 맞다고 판단해 voice_settings={"stability": 0.75,
# "style": 0.15}(안정적/과장 적음)로 명시하고 14개 전부 재생성했다 - 역시 14/14 무음 게이트
# 통과(단, 평균 시도 횟수가 조금 늘어남 - 이전엔 대부분 1~2회였는데 이번엔 최대 4회까지
# 감, 안정성 값을 올리면 발화 편차가 줄어드는 대신 무음 경계 판정이 살짝 더 엄격해지는
# 것으로 추정). 실측 객관 지표(volumedetect로 잰 peak-mean 다이내믹 레인지)로는 이전
# 버전(평균 14.12dB)과 새 버전(평균 13.71dB)의 차이가 0.4dB뿐이라 뚜렷한 "차분해짐"을
# 이 지표로는 못 뒷받침했다 - 파일별로도 방향이 들쑥날쑥해서(일부는 오히려 range가 커짐)
# 결론적 증거는 아니다. stability/style이 실제로 바꾸는 건 피치 억양 변주·화법 속도 같은
# 요소라 단순 라우드니스 통계로는 안 잡힐 가능성이 높다 - "차분해졌는지"는 결국 직접 들어봐야
# 확인 가능하다. 이전(voice_settings 미지정) 버전은 _backup_pre_buildup_default_settings_
# 20260929/ 에 백업.
# 🛡️ [주 내레이터 교체 - main -> lck_caster_dynamic, 14 -> 13] pre_buildup 전용으로 새
# 보이스 lck_caster_dynamic(당시 voice_id=IsyjRUQuwWiaozHzGnr0)을 도입, eleven_v4 모델 +
# voice_settings={"stability": 0.55}(style 제외 - can_use_style=False 확인됨)로 14개
# 전부 재생성했다. 13개는 1~10회 시도로 무음 게이트 통과했지만 "서로 눈치만 보고
# 있어요"(구 08번) 하나는 30회(최초 10회 + 재시도 20회) 전부 실패했고 대본 자체도
# 마음에 안 든다는 판단이 겹쳐 재시도 없이 폐기했다. 빈 자리를 그대로 두면(08 결번)
# 나중에 번호만 보고 실수하기 쉬워서, 남은 13개를 01~13으로 당겨 붙여 재정렬했다
# (파일명 자체가 곧 순서라 결번 없는 쪽이 유지보수하기 더 쉽다고 판단). 기존 main
# 보이스로 녹음된 14개(구 번호 그대로)는 _backup_pre_buildup_main_voice_20260930/
# 에 백업.
# 🛡️ [lck_caster_dynamic 보이스 자체 교체 - "스푼라디오 스타일" 피드백] 위 버전의
# voice_id(IsyjRUQuwWiaozHzGnr0)는 ElevenLabs 라이브러리에서 이미 삭제/교체된 상태였고,
# 같은 이름("lck_caster_dynamic")으로 계정에 새로 만들어진 보이스가 2개 있어서(서로 다른
# voice_id) 어느 쪽이 최신인지 사용자 확인을 받아 voice_id=GriSG3WMe4Ve3jcVnYBf로 13개
# 전부 재생성했다(eleven_v4 + stability=0.55, style 제외 - 이전과 동일 파라미터, 목소리
# 소스만 교체). "긴장감이 흐르고 있어요"(09번) 하나만 15회 전부 무음 게이트 실패해서,
# 같은 뜻의 다른 문구("팽팽한 긴장감이에요")로 교체 후 5회 만에 통과했다 - 나머지 12개는
# 문구 변경 없이 그대로 재생성됨. 이전(첫 lck_caster_dynamic 버전) 13개는
# assets/highlight_voice/_backup_pre_buildup_lck_dynamic_v1_20260930/ 에 백업.
# 🛡️ [정갈한 문어체 -> 구어체/억양 유도형 전면 재작성] "국어책 읽듯 낭독한다"는 피드백으로,
# 13개 전부를 "도입부 추임새(자,/아 이게,/근데 사실,/일단은,) + 구어체·질문형 어미
# (~거든요?/~란 말이죠/~긴 한데) + 쉼표 호흡점"을 최소 1~2개씩 포함하도록 다시 썼다 -
# 존댓말(요체) 틀은 전부 유지, 반말 없음. 문장이 길어지고 쉼표(호흡점)가 늘어난 만큼
# 무음 게이트 실패율도 눈에 띄게 올라갔다(최초 시도 13개 중 6개가 15회 전부 실패) -
# 특히 여러 절+쉼표가 겹친 문장일수록 잘 걸렸고, 짧고 단순한 단일 호흡 구조로 줄이니
# 5개는 바로 통과, 1개("숨 고르는 느낌")는 쉼표를 아예 빼고("일단은 숨 고르는
# 느낌이거든요", 쉼표 없이 붙여 쓰기)서야 10회 만에 통과했다. 길이도 전체적으로 늘어남
# (이전 1.6~2.8s -> 지금 2.5~4.2s) - PRE_BUILDUP_GAP_SEC 등 스케줄링 여유값에 영향을
# 줄 수 있으니 참고. 재작성 전(정갈한 문어체) 13개는
# assets/highlight_voice/_backup_pre_buildup_written_style_20260930/ 에 백업.
# 🛡️ [미접전 단정형 -> 중립형 재작성] 13개 문장을 하나씩 검토한 결과 8개(01/02/04/06/
# 07/10/11/12)가 "아직 안 붙었다"를 명시적으로 전제하는 표현("거리 재고 있다", "누가
# 먼저 들어가나 눈치 보고 있다", "한 발짝만 다가가면 싸움 나겠다" 등)이었다 - 실제 클립에서
# 이 대사가 재생되는 시점에 이미 교전이 시작돼 있으면 대사와 화면이 모순되는 문제가
# 생긴다. 8개 전부 "거리/들어가다/물러서다/견제" 같은 공간·행동 어휘를 "기운/흐름/기세/
# 열기/첨예함/절박함" 같은 추상 상태 묘사로 바꿔 교전 여부와 무관하게 항상 말이 되도록
# 다시 썼다 - 8개 최종 문구가 서로 다른 핵심 어휘를 쓰도록(중복 없음) 신경 썼고, 09번의
# "팽팽한 긴장감"과도 겹치는 단어를 피했다. 나머지 5개(03/05/08/09/13)는 원래도 중립형
# 이라 그대로 유지. 재작성 전(미접전 단정형 8개 포함) 버전은
# assets/highlight_voice/_backup_pre_buildup_neutral_20260930/ 에 백업.
PRE_BUILDUP_TEXT = {
    "pre_buildup_01.wav": "자, 지금 기운이 예사롭지 않네요?",  # 2.88s, 1회 시도로 무음 0곳 통과 - 거든요 쏠림 해소용 어미 재작성("~거든요?" -> "~네요?"), 핵심 어휘(예사롭지 않다) 유지
    "pre_buildup_02.wav": "음, 지금 흐름이 심상치 않군요",  # 2.97s, 1회 시도로 무음 0곳 통과 - 어미 재작성("~거든요?" -> "~군요"), 핵심 어휘(심상치 않다) 유지
    "pre_buildup_03.wav": "근데 사실, 스킬 하나 잘못 쓰면 위험한 상황이거든요?",  # 4.16s, 2회 시도로 무음 0곳 통과
    "pre_buildup_04.wav": "일단은, 서로 기 싸움이 치열하단 말이죠",  # 3.20s, 1회 시도로 무음 0곳 통과 - 미접전 단정형("누가 먼저 들어가나 눈치 보고 있다")에서 중립형으로 재작성
    "pre_buildup_05.wav": "아 이게, 위험한 거리이긴 한데, 지금요",  # 3.84s, 9회 시도로 무음 0곳 통과
    "pre_buildup_06.wav": "어, 지금 느낌이 살벌한데요?",  # 2.64s, 1회 시도로 무음 0곳 통과 - 어미 재작성("~거든요?" -> "~한데요?"), 핵심 어휘(살벌하다) 유지
    "pre_buildup_07.wav": "이야, 서로 기세가 만만치 않겠는데요",  # 2.88s, 1회 시도로 무음 0곳 통과 - 어미 재작성("~거든요" -> "~겠는데요"), 핵심 어휘(만만치 않다) 유지
    "pre_buildup_08.wav": "진짜 숨 고르는 느낌이거든요",  # 2.48s, 1회 시도로 무음 0곳 통과
    "pre_buildup_09.wav": "와, 팽팽한 긴장감이거든요?",  # 2.48s, 1회 시도로 무음 0곳 통과
    "pre_buildup_10.wav": "그니까, 지금 상황이 첨예하네요?",  # 2.80s, 1회 시도로 무음 0곳 통과 - 어미 재작성("~거든요?" -> "~네요?"), 핵심 어휘(첨예하다) 유지
    "pre_buildup_11.wav": "자, 지금 열기가 대단하거든요?",  # 2.48s, 1회 시도로 무음 0곳 통과 - 미접전 단정형("불붙기 좋은 거리")에서 중립형으로 재작성
    "pre_buildup_12.wav": "근데 사실, 지금 진짜 절박하거든요",  # 3.36s, 2회 시도로 무음 0곳 통과 - 미접전 단정형("물러설 수 없는 거리")에서 중립형으로 재작성
    "pre_buildup_13.wav": "아 이게, 잘못 움직이면 그대로 끝나그든",  # 3.62s, 3회 시도로 무음 0곳 통과(트림) - 어미 재작성("~거든요?" -> 구어체 슬랭 "~그든"), 핵심 어휘(잘못 움직이면 끝나다) 유지
}
# 🛡️ [종결 어미 쏠림 해소 - 실측] 13개 중 11개가 "~거든요" 계열로 끝나 반복적으로 들리는
# 문제가 확인됨(84.6%) - 06/13(01/02/06/07/10/13)의 어미만 재작성해 거든요 비중을
# 5/13(38.5%)로 낮췄다. 본문 핵심 어휘(예사롭지 않다/심상치 않다/살벌하다/만만치 않다/
# 첨예하다/잘못 움직이면 끝나다)는 전부 그대로 유지 - 어미만 바꿔 환각 위험 재검토가
# 필요 없다(내용이 안 변했으므로).
# 🛡️ [추임새 다양화 + 중복 방지] 기존 4종(자,/아 이게,/근데 사실,/일단은,)이 13개 문장에
# 고르게 안 퍼지고 "자,"만 5개로 쏠려 있었다(재작성 실패분을 재구성하는 과정에서 우연히
# 몰림) - 한 렌더에서 최대 3개까지 뽑히는데, 같은 추임새가 2개 이상 뽑힐 확률이 약 31.5%로
# 실측됨. 10종으로 늘리고(음,/어,/이야,/진짜/와,/그니까, 추가) 본문 내용은 그대로 둔 채
# 도입부만 6개 문장(02/06/07/08/09/10)에 재배치해서 13개가 10종에 최대한 고르게(2개씩
# 3종+1개씩 7종) 나뉘도록 했다. 파일명->추임새 매핑을 별도로 둬서, 아래 커스텀 샘플링
# 함수가 "같은 추임새를 가진 문장이 한 렌더에 2개 이상 안 뽑히게" 강제한다.
PRE_BUILDUP_OPENER = {
    "pre_buildup_01.wav": "자",
    "pre_buildup_02.wav": "음",
    "pre_buildup_03.wav": "근데 사실",
    "pre_buildup_04.wav": "일단은",
    "pre_buildup_05.wav": "아 이게",
    "pre_buildup_06.wav": "어",
    "pre_buildup_07.wav": "이야",
    "pre_buildup_08.wav": "진짜",
    "pre_buildup_09.wav": "와",
    "pre_buildup_10.wav": "그니까",
    "pre_buildup_11.wav": "자",
    "pre_buildup_12.wav": "근데 사실",
    "pre_buildup_13.wav": "아 이게",
}

# 🛡️ [긴박 톤 풀 - 마지막 슬롯 전용, 실제 자산 등록] 리드인 마지막 자리(EOEO 직전, 킬에
# 가장 가까운 자리)만 짧고 격앙된 긴박형 문장으로 채우기 위한 전용 풀 - 기존 PRE_BUILDUP_
# POOL(차분 톤)과 분리한다. 처음엔 글롭 패턴만 맞춰두고 빈 풀로 시작했는데(_pick_pre_
# buildup_slots가 빈 풀이면 차분 풀로 안전하게 폴백), 비교 청취 라운드에서 "본문 내용이
# 아니라 도입부 감탄사 '아,' 자체(뒤 내용과 무관하게)가 탄식/걱정 톤을 만드는 핵심"이라는
# 게 직접 청취로 확인됐다 - 본문만 다른 여러 문장보다, 검증된 "아," 계열 도입부(쉼표/
# 말줄임/쉼표 위치 이동/"어어," 대체)를 쓴 문장들이 일관되게 더 긴박하게 들렸다. 최종
# 확정 4개 중 2개는 본문("이거 위험한데요!!")을 그대로 두고, 나머지 2개는 같은 검증된
# 도입부("아 이거,"/"어어,")에 본문만 바꿔 최소 리스크로 본문 다양성을 확보했다. 전부
# eleven_v4 + lck_caster_dynamic(voice_id=GriSG3WMe4Ve3jcVnYBf) + [excited][shouts]
# 프리픽스 + stability=0.45로 합성, 4개 전부 1회 시도로 무음 게이트(0곳) 통과(트레일링
# 무음 트림 1회씩 적용).
PRE_BUILDUP_URGENT_POOL = sorted(glob.glob(os.path.join(VOICE_DIR, "pre_buildup_urgent_*.wav")))
PRE_BUILDUP_URGENT_TEXT = {
    "pre_buildup_urgent_01.wav": "아, 이거 장난 아닌데요!!",
    "pre_buildup_urgent_02.wav": "아... 이거 묘한데요!!",
    "pre_buildup_urgent_03.wav": "아 이거, 떨리는데요!!",
    "pre_buildup_urgent_04.wav": "어어, 지금이에요!!",
}
# 🛡️ [calm/urgent 핵심 어휘 겹침 해소 - 실측] urgent_01/02가 "위험하다"를 써서 calm_03
# ("위험한 상황")/calm_05("위험한 거리")와 같은 어근이 겹쳤고, urgent_03의 "심상치 않다"는
# calm_02와 문자 그대로 똑같았다 - 검증된 도입부("아,"/"아..."/"아 이거,")는 그대로 두고
# 본문만 calm 13개 어디에도 없는 새 어휘(장난 아니다/묘하다/떨리다)로 교체해 겹침을
# 0건으로 만들었다. 3개 전부 재합성 후 1회 시도로 무음 게이트 통과.


def _sample_pre_buildup_distinct_openers(pool: list[str], opener_map: dict[str, str], k: int) -> list[str]:
    """pool에서 최대 k개를 뽑되, 같은 추임새(PRE_BUILDUP_OPENER 값)를 가진 파일이 두 개
    이상 뽑히지 않도록 하는 순수 함수(테스트 가능) - random.sample을 대체한다.
    추임새 종류(10종)가 k(PRE_BUILDUP_MAX_COUNT=3)보다 항상 많으므로 자리가 모자라
    k개를 못 채우는 경우는 현재 설정에서는 발생하지 않지만, 만약 그런 상황이 오면
    (추임새 종류를 k 밑으로 줄이는 등) k개보다 적게 반환할 수 있다(방식 자체는 안전)."""
    shuffled = list(pool)
    random.shuffle(shuffled)
    picked: list[str] = []
    used_openers: set[str] = set()
    for f in shuffled:
        if len(picked) >= k:
            break
        opener = opener_map.get(os.path.basename(f))
        if opener in used_openers:
            continue
        picked.append(f)
        used_openers.add(opener)
    return picked


def _estimate_pre_buildup_count(available: float, avg_dur: float, gap: float, max_count: int) -> int:
    """available 구간에 "평균 길이+gap" 기준으로 몇 개(N)가 들어갈지, 아직 어떤 파일이
    뽑힐지 모르는 상태에서 "대표 길이"(호출부에서는 PRE_BUILDUP_POOL 전체 평균 사용)만
    가지고 미리 추정하는 순수 함수(테스트 가능).
    🛡️ [N-먼저-추정 재구성] 예전엔 파일을 먼저 뽑고 그 실제 길이로 N을 계산했는데
    (_spread_fillers_evenly), 그러면 "마지막 슬롯은 항상 특정 풀에서" 같은 보장을 할 수
    없었다(N이 나중에 줄어들면 뒤쪽 후보가 조용히 버려짐). 이 함수로 N을 먼저 정해두면
    호출부가 정확히 N개만(마지막 1개는 별도 풀에서) 뽑을 수 있다 - 대표 길이와 실제
    뽑힐 파일의 길이가 달라 N이 ±1 오차 날 수 있음은 감내 가능한 수준으로 판단,
    별도 보정 없음."""
    if available <= 0 or avg_dur <= 0:
        return 0
    unit = avg_dur + gap
    if unit <= 0:
        return 0
    return max(0, min(max_count, int(available // unit)))


def _pick_pre_buildup_slots(n: int, calm_pool: list[str], urgent_pool: list[str],
                             opener_map: dict[str, str]) -> list[str]:
    """_estimate_pre_buildup_count로 먼저 확정된 자리 개수 n을 받아, N-1개는 calm_pool
    (오프닝 중복 방지 유지)에서, 마지막 1개(가장 킬에 가까운 자리)는 urgent_pool에서
    뽑아 정확히 n개를 순서대로 반환하는 순수 함수(테스트 가능) - 리스트의 마지막
    원소가 항상 urgent_pool 출신이 되도록 보장한다(n>=1이고 urgent_pool이 비어있지
    않을 때). urgent_pool이 비어 있으면(아직 긴박 풀 자산이 없는 상태) 마지막 자리도
    calm_pool로 채우는 안전한 폴백."""
    if n <= 0:
        return []
    if not urgent_pool:
        return _sample_pre_buildup_distinct_openers(calm_pool, opener_map, n)
    calm_picks = _sample_pre_buildup_distinct_openers(calm_pool, opener_map, n - 1)
    return calm_picks + [random.choice(urgent_pool)]


def _spread_fixed_n(start_offset: float, available: float, n: int) -> list[float]:
    """available 구간을 이미 확정된 자리 개수 n으로 등분해 각 조각 앞쪽에 배치한 절대
    시작 시각 리스트를 반환하는 순수 함수(테스트 가능) - _spread_fillers_evenly와 달리
    실제 파일 길이로 n을 다시 계산하지 않는다(재계산하면 _pick_pre_buildup_slots가
    urgent_pool에서 뽑아둔 마지막 파일이 n이 줄어들 때 조용히 사라질 위험이 있어서,
    그 경로 자체를 없앤다 - "몇 개가 들어가는지"는 이미 _estimate_pre_buildup_count가
    정했고, 이 함수는 그 개수를 시간에 펼치기만 한다)."""
    if n <= 0 or available <= 0:
        return []
    segment_width = available / n
    return [start_offset + i * segment_width for i in range(n)]


# 🛡️ [리드인 2보이스 겹침 - 신규] 지금까지 리드인 구간(상황 멘트+"어어??")은 100% Main
# 혼자였다 - 가끔(LEADIN_OVERLAY_CHANCE 확률로) Hype 또는 Sub가 짧게 끼어들어 반응하면
# 리드인이 매번 똑같이 "혼잣말"처럼 들리지 않고 다른 목소리가 있다는 인상을 준다. 파일명
# 접두사(hype_leadin_/sub_leadin_)로 두 목소리가 한 풀에 섞여 있어 random.choice 한 번이
# 곧 "Hype 또는 Sub 중 하나"를 고르는 것과 같다(대략 50/50). PRE_BUILDUP_TEXT와 동일한
# "1어절 이내 순수 추임새" 원칙 - 상황 판단 없음.
# 🛡️ [Sub 보이스 - 비음 회피] "음?"/"흠?"는 Sub 보이스에서 20회 전부 무음 게이트 실패
# (비음 끝소리 감쇠 구간이 -35dB 밑으로 떨어져 게이트에 걸리는 것으로 추정) - 받침 없는
# 텍스트로 교체하니 각각 3회 만에 통과. Hype는 셋 다 문제 없었다(2~6회).
# 🛡️ [v4 일괄 재생성 - 트레일링 무음 트림] 아래 11개 정적 풀(EOEO/MAIN_EXPLODE/
# HYPE_EXPLODE/SUB_EXPLODE/SUB_QUESTION/ATLEE_SUB_QUESTION/STERLING/CARTER/ATLEE/
# EN_LEADIN/LEADIN_OVERLAY, 총 48개 파일) 전부를 eleven_v4 + voice_settings=
# {"stability": 0.55}(style 제외)로 일괄 재생성했다. 실측 결과 완전한 문장(EOEO/
# EN_LEADIN)은 그대로 잘 통과했지만, 짧은 감탄사·모음반복 텍스트 계열(나머지 9개 풀,
# 35/48)은 v3와 달리 파일 끝에 0.15~0.4초짜리 트레일링 감쇠 구간이 거의 항상 남아
# 무음 게이트에 걸렸다 - 재시도로는 해결이 안 되는 v4 자체의 구조적 특성으로 판단,
# 무음 시작 직전(+0.05s 여유)까지 잘라내는 후처리로 32개를 해결했다(평균 14.0%,
# 최대 36.4% 길이 감소). 이 트림만으로도 40% 이상 잘려나가는 극단적으로 짧은 3개
# (hype_leadin_c/sub_leadin_a/sub_leadin_b, 전부 1음절)만 "1어절 이내" 원칙을
# 유지한 채 텍스트를 살짝 늘려(음절 반복) 재생성 - 아래 값은 그 결과다. 기존
# v3 버전 48개 전부는 assets/highlight_voice/_backup_v3_pool_20260930/ 에 백업.
# 🛡️ [HYPE_EXPLODE_POOL만 재차 v3로 원복] 위 일괄 전환 직후 "0단계 3보이스가 다 같은
# 목소리 같다"는 피드백으로 피치 실측(아래 HYPE_EXPLODE_TEXT 앞 주석 참고) 후
# HYPE_EXPLODE_POOL(hype_a~f.wav, 이 LEADIN_OVERLAY_POOL의 hype_leadin_*와는 다른
# 파일) 6개만 다시 v3로 되돌렸다 - 이 문단이 말하는 "48개 전부 v4"는 그 이전 상태이며,
# 현재는 MAIN_EXPLODE/SUB_EXPLODE 등 나머지 42개만 v4, HYPE_EXPLODE 6개는 v3다.
LEADIN_OVERLAY_POOL = (sorted(glob.glob(os.path.join(VOICE_DIR, "hype_leadin_*.wav")))
                       + sorted(glob.glob(os.path.join(VOICE_DIR, "sub_leadin_*.wav"))))
LEADIN_OVERLAY_TEXT = {
    "hype_leadin_a.wav": "오!",  # 0.56s(트림), v4
    "hype_leadin_b.wav": "와",  # 0.65s(트림), v4
    "hype_leadin_c.wav": "어어?",  # 0.74s(재생성+트림) - "어?"가 트림 42.2%로 과도해 텍스트 교체
    "sub_leadin_a.wav": "오오?",  # 0.74s(재생성+트림) - "오?"가 트림 42.2%로 과도해 텍스트 교체
    "sub_leadin_b.wav": "어어",  # 0.65s(재생성+트림) - "어"가 트림 42.5%로 과도해 텍스트 교체
    "sub_leadin_c.wav": "허어",  # 0.74s(트림), v4
}
LEADIN_OVERLAY_CHANCE = 0.4
# 🛡️ [EOEO("어어??") 완전 제거] 상황 멘트와 0단계 폭발 사이에 짧게 끼워 넣던 "이상 감지"
# 반응(buildup1_*.wav, Main 목소리 정적 풀)이었는데, 다중 리액션(_pack_reaction_chain)이
# 리드인 전체를 킬 직전까지 끊김 없이 채우는 지금 구조에서는 "킬 직전에 고정으로 한 번
# 끼어드는 짧은 멘트"라는 역할이 다중 리액션과 겹치면서 더 설 자리가 없어져 제거했다.
# EOEO_POOL/EOEO_TEXT와 그 전신 와일드카드(buildup1_*.wav) 정의는 모두 지웠다 - 실제 wav
# 파일 자체(assets/highlight_voice/buildup1_*.wav)는 건드리지 않았으니 되돌릴 일이 생기면
# git 이력에서 이 블록만 복구하면 된다. EOEO_GAP_SEC은 이름은 그대로 남기되 "다중 리액션
# 종료~킬 시점" 등 범용 안전 여백으로 재사용한다(아래 정의 참고).
# 🛡️ [앵커링 기준 = 클립 시작(t=0), kill_t 역산 아님] 처음엔 "0단계(킬) 직전에 끝나도록"
# kill_t에서 거꾸로 역산했는데, 실제로 들어보니 "영상 시작하자마자" 나와야 한다는 요구와
# 다른 결과가 나왔다(kill_t가 클립 중간쯤이면 리드인도 자동으로 중간쯤에 옴 - 클립 길이
# 자체는 계산식에 아예 안 들어가서 "초반"을 보장 못 함, 실측으로 확인된 버그 아닌 설계
# 오해). 이번엔 클립 시작(t=0) 기준으로 앞에서부터 배치하고, kill_t와 안 겹치는지만
# 안전장치로 검사한다 - 자리가 없으면(비정상적으로 짧은 클립/이른 킬) 예전 빌드업1/2단계와
# 같은 원칙으로 스킵한다(억지로 겹치게 밀어넣지 않음).
PRE_BUILDUP_START_OFFSET_SEC = 0.4  # 상황 멘트: 클립 시작 후 이만큼 뒤에 시작(0.3~0.5 범위)
# 🛡️ [텀 확보 - 0.2 -> 0.6] 상황 멘트들 사이 간격을 늘려서 "다다다닥" 몰아치는 느낌 대신
# 숨 쉴 틈을 만든다. EN_LEADIN_GAP_SEC은 이 상수에서 분리된 독립 상수라 이 변경이 영어
# 리드인 간격에는 영향을 주지 않는다.
PRE_BUILDUP_GAP_SEC = 0.6  # 상황 멘트 슬롯 사이(및 마지막 슬롯 뒤) 간격
# 🛡️ [여백 소폭 확대 - 0.2 -> 0.6] "어어??"가 킬과 너무 바짝 붙어서 나온다는 체감을
# 개선하려 늘렸다. 시뮬레이션 결과 kill_t=6s 근방 클립은 0.2->0.4 사이에서 이미 상황
# 멘트 슬롯이 2개->1개로 줄어들고(available 공간이 "평균 문장 길이+gap" 단위 2개를
# 못 채우게 됨), 0.6~0.8 구간 안에서는 추가로 더 줄어드는 지점이 없어(테스트한
# kill_t=5/6/7/10/17.86s 전부 동일 슬롯 개수 유지) 범위 내에서 손해가 가장 적은 하단
# 값(0.6)을 택했다. EN_LEADIN_END_GAP_SEC이 이 상수를 그대로 재사용하므로 영어 리드인의
# 마지막 필러~킬 여백도 같이 0.6으로 늘어난다(의도된 재사용 - 분리 대상이 아님).
# 🛡️ [EOEO 제거 후 범용 안전 여백으로 재사용] 원래 "어어??" 종료~킬 시점 사이 여백이었는데,
# EOEO 자체가 없어지면서 이제 pre_buildup_available(plan_lead_in_forward_eoeo)과 다중
# 리액션 종료 한계(_pack_reaction_chain의 kill_t - EOEO_GAP_SEC) 둘 다에 쓰이는 "킬 직전
# 범용 안전 여백"이 됐다. 이름은 호출부를 더 안 건드리려고 그대로 남겼다.
# 🛡️ [0.6 -> 0.3 재축소 - 환호 지연 체감 개선] 리딩 무음 트림(main/hype/sub_explode)
# 이후에도 "다중 리액션 종료~환호 시작" 간격이 여전히 ~0.63초였는데, 그 대부분이 파일
# 리딩 무음이 아니라 이 상수(구조적 안전 여백) 자체에서 온다는 게 실측으로 확인돼
# 다시 줄였다. 위 "0.2->0.6" 결정 당시엔 PRE_BUILDUP_POOL이 지금과 다른 구성(평균 길이가
# 더 짧음)이라 kill_t=6s 근방에서 손해가 있었지만, 지금의 PRE_BUILDUP_POOL(문장형 교체
# 이후 평균 3.06s)로 재시뮬레이션한 결과 kill_t=5/6/7/10/15/17.86/30s 전부 0.6->0.2
# 범위에서 pre_buildup 슬롯 개수(N)가 전혀 안 줄었다(available만 소폭 늘어남) - 지금은
# 손해 없이 줄일 수 있는 상태로 확인돼 0.3으로 내렸다. 다중 리액션 쪽은 end_limit이
# kill_t-EOEO_GAP_SEC으로 정의돼 있어 이 값이 작아질수록 오히려 채울 수 있는 여백이
# 늘어난다(손해 아님 - _pack_reaction_chain이 end_limit을 절대 넘지 않으므로 킬 시점
# 침범 위험도 없음, EOEO_GAP_SEC>0인 한 항상 보장).
EOEO_GAP_SEC = 0.3

# 🛡️ ["진입 멘트" 도입 후 제거 - 타이밍 맞출 방법 없음] kill_t 역산 고정 위치("자, 들어갔
# 습니다"를 EOEO 앞에 배치)로 한때 시도했으나, 실측 결과(kill_t=17.86s 클립에서 진입 멘트가
# kill_t 기준 85.9~91.7% 지점에 위치) 체감("싸움의 70%")과 실제 위치가 안 맞았고, 근본
# 원인이 "실제 전투 시작 시점을 모른 채 kill_t에서 거꾸로 추측 배치"라는 구조적 한계라
# (오늘 별도 조사 - 신호처리/비전 기반 실시간 감지 둘 다 신뢰도 미확보) 고칠 방법이 없다고
# 판단해 기능 자체를 제거했다. 아래 3개 상수(ENTRY_LINE_GAP_SEC/POOL/TEXT)와
# entry_line_01~04.wav 파일은 코드에서 더 이상 참조하지 않지만, 나중에 다른 용도로 재사용할
# 수도 있어 삭제하지 않고 그대로 둔다(현재는 죽은 코드/미사용 에셋).
ENTRY_LINE_GAP_SEC = 0.4
ENTRY_LINE_POOL = sorted(glob.glob(os.path.join(VOICE_DIR, "entry_line_*.wav")))
ENTRY_LINE_TEXT = {
    "entry_line_01.wav": "자, 들어갔습니다",
    "entry_line_02.wav": "결국 들어가네요",
    "entry_line_03.wav": "붙었습니다 지금",
    "entry_line_04.wav": "드디어 붙는데요",
}

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
# 🛡️ [glob 충돌 버그 수정 - 리드인 2보이스 겹침 추가로 발견] "hype_*.wav"는 새로 추가된
# "hype_leadin_*.wav"(리드인 겹침용, LEADIN_OVERLAY_POOL)까지 그대로 삼켜서, 0단계
# 캐스케이드에 리드인 파일이 섞여 뽑히면 HYPE_EXPLODE_TEXT에 없는 키라 KeyError가 났다
# (ATLEE_POOL에서 이미 한 번 겪었던 것과 동일한 glob 충돌 패턴). "hype_" 뒤에 글자 하나만
# 오는 파일(a~f)만 정확히 잡도록 좁힌다.
HYPE_EXPLODE_POOL = sorted(glob.glob(os.path.join(VOICE_DIR, "hype_[a-z].wav")))
SUB_EXPLODE_POOL = sorted(glob.glob(os.path.join(VOICE_DIR, "sub_shout_*.wav")))
# 🛡️ [발성 강도 재녹음 - "국어책 읽는 느낌" 피드백] 게인만 올렸을 뿐(VOICE_MIX_GAIN_DB_OVERRIDE)
# 발성 자체의 텐션은 그대로였다는 피드백으로, 텍스트(모음 반복 구조)는 그대로 두고 태그/
# voice_settings만 바꿔 재녹음했다. 1차로 [SCREAMING][terrified excitement]+stability=0.0+
# style=0.9(가장 낮은 안정성·가장 높은 스타일 과장)를 채택했으나, 이후 "발음이 무너지고
# 모음만 늘어진다(호우오호호우우우)"는 피드백으로 재조정 - 텍스트/태그는 그대로 두고
# stability/style만 0.5/0(원본)와 0.0/0.9(무너진 값) 사이 중간 지점 4가지(0.35/0.4,
# 0.35/0.6, 0.2/0.4, 0.2/0.6)를 비교해, 무음 게이트 통과·길이·attack_rise가 가장
# 균형 잡힌 stability=0.35/style=0.6 조합을 최종 채택했다(발음 붕괴 없이 attack_rise가
# 오히려 stability=0.0 버전보다 높게 나옴 - 실측 근거는 커밋 메시지 참고). a/b/c 전부
# 동일 파라미터로 통일 재녹음 - 하나만 바뀌고 나머지가 다른 톤으로 남는 풀 내 불일치를
# 피했다. 이전(stability=0.0/style=0.9) 버전은 _backup_main_explode_screaming_creative_*/,
# 최초 원본(stability=0.5/style=0)은 _backup_main_explode_*/ 에 각각 백업.
# 🛡️ [4번째 후보 추가] 중간 지점 비교 실험 중 나온 stability=0.2/style=0.6(mid_d) 후보도
# 실제 0단계 캐스케이드(main+hype+sub 동시재생) 테스트까지 마친 뒤 풀에 정식 추가했다 -
# a/b/c보다 stability가 더 낮아(과장 더 큼) 세트 안에서 유일하게 다른 파라미터지만,
# 무음 게이트/길이/attack_rise 전부 기준 통과해 별도 텍스트 없이 풀만 확장.
# 🛡️ [아래 TEXT 딕셔너리들, 텍스트는 그대로/오디오만 v4로 교체] 텍스트(모음 개수)를 바꿀
# 이유가 없어 문구는 유지했다 - 인라인 주석의 "stability=X/style=Y"와 "정밀 기준 통과"는
# 전부 v3 시절 기록이라 지금 파일에는 더 이상 안 맞는다(지금은 위 LEADIN_OVERLAY_TEXT 앞
# 주석에 적은 v4+트림 절차로 재생성됨) - 길이만 트림 후 최종값으로 갱신.
# 🛡️ [main_explode a/b/c만 재차 v3로 원복 - "얇고 꽥꽥거리는" v4 톤이 메인 표준으로는
# 안 맞는다는 피드백] v4 게인을 +7.0dB까지 올려봤지만(hype_explode v3는 +3.0dB) 통합
# 라우드니스(loudnorm) 실측 결과 main_explode(-11.50LUFS)가 hype(-10.38LUFS)보다 여전히
# 작았다 - alimiter로 인한 라우드니스 감소분 자체는 main/hype/sub 셋이 거의 동일(-8LU
# 안팎)해서 리미터가 main만 더 세게 누르는 게 아니라, limiter 통과 *전*부터 이미 v4쪽
# 통합 라우드니스가 더 낮았다(v4 오디오 자체의 "라우드니스 밀도"가 v3보다 낮은 것으로
# 추정 - 게인을 더 올려도 리미터 심화 구간이라 한계 효용이 낮음). 게인만으로는 못 고치는
# 문제라 판단해 톤 자체를 되돌리기로 하고, main_explode_d(원래도 "세트 안에서 유일하게
# 다른 파라미터"로 도입된 슬롯)만 v4로 남기고 a/b/c는 v3 원본(_backup_v3_pool_20260930/)
# 으로 되돌렸다 - 길이 가중 랜덤 선택(MAIN_EXPLODE_POOL 랜덤 픽 로직) 기준으로 d가 뽑힐
# 확률은 대략 27%(d의 v4 길이 1.58s ÷ 전체 4개 길이 합) - "가끔 얇고 꽥꽥거리는 것도
# 섞여 나오는" 정도의 포지션을 의도한 비율과 근접.
# 🛡️ [main_explode_d - "악" 종결 제거, 감정 톤 재조정] v4 main_explode_d가 "겁에 질린"
# 느낌으로 들린다는 피드백 - "악!!"으로 끝나는 텍스트가 v4의 감정 추론에 영향을 줬을
# 가능성으로 보고 "악" 없는 순수 모음 반복("!!"로만 종결)으로 바꿔봤다. 실측(attack_rise,
# 초반 급격도) 결과 "악!!" 버전은 0.206dB/ms인데 "악" 제거 버전은 0.112~0.163dB/ms로
# 셋 다 확실히 낮게 나옴 - 시작부터 더 큰 소리로 시작해서 완만하게 피크로 올라가는 패턴
# (급격한 어택=놀라서 지르는 느낌, 완만한 상승=점점 커지는 함성/환호에 더 가까운 특성).
# mean/peak dB는 기존과 비슷하거나 오히려 피크가 살짝 큼(-3.9~-5.1dB vs 기존 -5.6dB) -
# 조용해지는 부작용은 없다. "악" 제거 버전은 raw 상태로는 무음 게이트 실패율이 높아짔다
# (10회 전부 실패, 원인은 항상 파일 끝 트레일링 감쇠 - main_explode/sub_explode v4 재생성
# 때와 같은 패턴) - 트림 후처리로 해결.
# 🛡️ [main_explode_e 추가 - v4(고텐션) 비중 확대] "환호가 다양성 때문에 가끔 약하게
# 느껴진다"는 피드백 이후 v4 비중을 늘리는 작업 - main_explode_d와 달리 일부러 "악!!"
# 종결을 유지했다(바로 위 주석대로 "악!!"이 attack_rise를 높인다는 게 실측으로 확인돼
# 있었기 때문 - no_ak 스타일을 추가하면 오히려 완만한 쪽이 하나 더 느는 역효과가 난다).
# 실측 attack_rise=0.231dB/ms로 main_explode_d의 no_ak 버전(0.112~0.163)보다 확실히
# 높고 기존 "악!!" 버전(main a/b/c, v3) 수준과 비슷하다. 다만 main_explode_d를 "악" 없는
# 버전으로 바꾼 원래 이유가 "v4+악!! 조합이 겁에 질린 느낌"이라는 피드백이었던 점은
# 그대로 남아있는 리스크다 - 실제 렌더로 직접 들어보고 판단 필요. 무음 게이트 1회 통과,
# 길이 가중 선택 비중은 MAIN_EXPLODE_POOL 전체 5개 기준 아래 시뮬레이션 참고.
MAIN_EXPLODE_TEXT = {
    "main_explode_a.wav": "우와" + "아" * 10 + "악!!",  # 1.28s, v3(원복)
    "main_explode_b.wav": "우와" + "아" * 8 + "악!!",  # 1.52s, v3(원복)
    "main_explode_c.wav": "우와" + "아" * 12 + "악!!",  # 1.36s, v3(원복)
    "main_explode_d.wav": "우와" + "아" * 12 + "!!",  # 2.23s(트림), v4, "악" 제거 버전(no_ak_a)
    "main_explode_e.wav": "와" + "아" * 9 + "악!!",  # 1.27s(리딩 트림), v4, attack_rise=0.231dB/ms
}
# 🛡️ [v4 -> v3 원복, 0단계 3보이스 음색 분리 문제] main/hype/sub_explode를 전부 v4로
# 옮긴 뒤 "0단계 3보이스가 다 같은 목소리처럼 들린다"는 피드백이 나와 실측했더니, 자기상관
# 기반 median pitch가 v3에서는 세 보이스 평균 pairwise 차이 107.8Hz였는데 균일하게
# stability=0.55로 생성한 v4에서는 79.4Hz로 좁혀져 있었다(특히 main-hype 간격이
# 161.6Hz->50.9Hz로 급격히 줄어듦 - main 피치는 올라가고 hype 피치는 내려가면서 서로
# 수렴). stability를 보이스별로 다르게(hype=0.3/sub=0.8) 줘서 실측했지만 평균 77.8Hz로
# 거의 개선이 없었다 - stability는 애초에 피치 레지스터를 조절하는 파라미터가 아니라는
# 기존 결론과 일치. 대신 HYPE_EXPLODE_POOL만 v3 원본(assets/highlight_voice/
# _backup_v3_pool_20260930/)으로 되돌리고 MAIN_EXPLODE/SUB_EXPLODE는 v4를 유지했더니
# 평균 pairwise 차이가 101.0Hz로 v3 수준에 근접 회복됐다(main-hype 83.1Hz, main-sub
# 68.3Hz, hype-sub 151.4Hz) - 당시엔 이 6개가 v3 원본이었다(아래 보이스 교체로 더 이상
# 해당 없음).
# 🛡️ [Hype -> lck_caster_dynamic 교체] 0단계 3인조에서 Hype 보이스를 빼고 그 자리에
# lck_caster_dynamic(pre_buildup 전용 내레이터)을 겸직으로 추가했다 - "셋이 겹쳐서
# 한 명처럼 들린다"는 피드백 대응의 일환(완전 동시 시작 문제와 별개로, 세 번째 목소리
# 자체가 더 뚜렷이 구분되도록). Hype 페르소나는 1단계 닉네임 샤우팅과 리드인 겹침
# (LEADIN_OVERLAY_POOL)에는 그대로 남아 있다 - 0단계에서만 빠진다. 텍스트(모음 반복
# 패턴)는 그대로 유지, eleven_v4 + stability=0.55(태그 없음, 기존 lck_caster_dynamic
# 관례) + 트레일링 무음 트림으로 재생성했고 6개 전부 1회 시도로 무음 게이트 통과했다.
# 게인은 기존 hype_explode 역할 기본값(VOICE_MIX_GAIN_DB_OVERRIDE, 6.0dB)을 그대로
# 쓴다 - 파일 단위 오버라이드(VOICE_MIX_GAIN_DB_FILE_OVERRIDE)는 추가하지 않았다(이미
# main/sub와 동일한 6.0dB 기반이라 새로운 마스킹 구조를 만들지 않음). 기존 v3 원본은
# assets/highlight_voice/_backup_hype_explode_v3_20261001/ 에 백업.
# 🛡️ [b/c/e 교체 - 텐션 편차 문제] 6개 전부 실측한 attack_rise가 a=0.295/b=0.041/
# c=0.029/d=0.102/e=0.037/f=0.256로 절반(b/c/e)이 0.1 미만으로 유독 낮았다 - "환호가
# 전반적으로 약하다"가 아니라 이 낮은 쪽이 뽑힐 때 약하게 느껴지는 문제였다. 다양성은
# 유지하되(텍스트/길이 계속 다름) 극단적으로 낮은 세 개만 새 텍스트로 재생성해 교체
# 했다 - 전부 1~4회 시도 안에 기준(attack_rise>=0.15) 통과, 무음 게이트도 통과. 기존
# b/c/e는 _backup_stage0_low_tension_hype_20261002/ 에 백업. 재측정 결과 새 b=0.249/
# c=0.257/e=0.186로 풀 전체 최솟값이 0.102(d)까지 올라갔다(기존 최솟값 0.029 대비
# 큰 폭 개선).
HYPE_EXPLODE_TEXT = {
    "hype_a.wav": "와" + "아" * 10 + "악!!",  # 1.67s(트림), lck_caster_dynamic, attack_rise=0.295
    "hype_b.wav": "으" + "아" * 8 + "악!!",  # 1.46s(리딩 트림), lck_caster_dynamic, attack_rise=0.249
    "hype_c.wav": "와" + "아" * 6 + "악!!",  # 1.37s(리딩 트림), lck_caster_dynamic, attack_rise=0.257
    "hype_d.wav": "와" + "아" * 8 + "!!",  # 1.67s(트림), lck_caster_dynamic, attack_rise=0.102
    "hype_e.wav": "으" + "아" * 12 + "악!!",  # 1.56s(리딩 트림), lck_caster_dynamic, attack_rise=0.186
    "hype_f.wav": "으" + "아" * 8 + "악!!",  # 1.67s(트림), lck_caster_dynamic, attack_rise=0.256
}
# 🛡️ [sub_shout도 main_explode와 같은 패턴 - 일부만 v3 원복] main_explode와 동일하게
# "v4 톤이 얇고 꽥꽥거림" 피드백으로 5개 중 1개만 v4로 남기고 나머지는 v3 원본으로
# 되돌렸다. main_explode_d 같은 "원래부터 다른 파라미터로 도입된 슬롯" 이력은
# sub_explode 쪽엔 없어서(5개 다 동일 stability/style로 통일 재녹음됐던 이력), 대신
# 텍스트 자체가 유일하게 다른 sub_shout_c("우아"로 시작 - 나머지 4개는 전부 "우와")를
# 자연스러운 v4 잔류 후보로 골랐다. 길이 가중 랜덤 선택 기준 c가 뽑힐 확률은 약 19%
# (main_explode_d의 27%와 비슷한 자릿수) - "가끔 v4 톤이 섞여 나오는" 의도한 비중과
# 부합. 게인은 VOICE_MIX_GAIN_DB_FILE_OVERRIDE 참고.
SUB_EXPLODE_TEXT = {
    "sub_shout_a.wav": "우와" + "아" * 8 + "!!",  # 1.60s, v3(원복)
    # 🛡️ [재녹음 - "우와아아악" 계열로 통일] "히"+"이" 계열("허"+"어" 실패 이후 택했던 회피
    # 전략)이 main/hype와 계열 자체가 달라 이질감이 있었다는 피드백으로, "우와"/"우아" 도입부
    # 뒷모음 개수만 다르게 변주하는 전략으로 재시도 - 이번엔 3개 다 1회 시도 만에 무음 0곳
    # 통과(noise=-30dB:d=0.02 기준, edge_margin=0.1s).
    "sub_shout_b.wav": "우와" + "아" * 6 + "악!!",  # 1.68s, v3(원복)
    # 🛡️ [sub_shout_c - "악" 종결 제거] main_explode_d와 같은 이유(겁에 질린 느낌)로
    # sub 보이스로 새로 합성 - "우아"+아*8+"악!!" -> "우아"+아*10+"!!"(모음 2개 늘려서
    # "악" 제거분 보완, main의 no_ak_a와 동일 전략). raw 상태로는 트레일링 감쇠로 무음
    # 게이트 실패해서 트림 후처리 적용.
    "sub_shout_c.wav": "우아" + "아" * 10 + "!!",  # 1.49s(트림), v4, "악" 제거 버전(sub_no_ak_a)
    "sub_shout_d.wav": "우와" + "아" * 6 + "!!",  # 1.76s, v3(원복)
    "sub_shout_e.wav": "우와" + "아" * 4 + "악!!",  # 1.36s, v3(원복)
    # 🛡️ [sub_shout_f 추가 - v4(고텐션) 비중 확대] main_explode_e와 동일한 이유 -
    # "악!!" 종결 유지(no_ak 스타일 추가는 오히려 완만한 쪽을 늘리는 역효과). TTS 변동폭이
    # 커서(같은 텍스트로 1~4회 시도가 전부 0.045~0.048dB/ms로 낮게 나오다가 6회째에
    # 0.170으로 기준 통과) 6회 재시도 끝에 attack_rise=0.170dB/ms인 결과를 채택했다.
    "sub_shout_f.wav": "우와" + "아" * 7 + "악!!",  # 1.27s(리딩 트림), v4, attack_rise=0.170dB/ms
}

# ══════════════════════════════════════════════════════════
#  전투 지속 리액션 (0단계 ~ 1단계 사이, 조건부) - 설계 검토 라운드에서 확정된 결론을
#  그대로 구현한다: kill_t부터 1단계 시작까지의 간격은 "뽑힌 환호 파일 길이"일 뿐이라
#  전투 지속시간과 무관하고, 대신 이미 계산되는 pre_buildup_slot_count(N, 리드인에 상황
#  멘트가 몇 개나 들어갔는지)가 "리드인이 충분히 길었다 = 어느 정도 공방이 있었을
#  가능성이 높다"는 간접 신호로 쓸 만하다고 판단했다 - 새 임계값을 발명하는 대신 이미
#  1000회 시뮬레이션으로 검증된 이 값을 그대로 재사용한다(BATTLE_REACTION_MIN_PRE_
#  BUILDUP_SLOTS=2, N=3이 최대이므로 "최대 아니면 적어도 2"가 기준).
#  0~3단계는 전부 85% 겹치게 설계돼 있어("빈 시간"이 없음) 삽입할 자리를 새로 만드는
#  대신, 0단계->1단계 전환 지점(t1 = stage0_dur*ratio, 기존과 동일)에 전투 리액션을
#  먼저 앵커링하고, 발동 시에만 1단계 시작을 그 리액션 길이만큼 통째로 뒤로 미는
#  방식을 택했다(plan_kill_sequence에 battle_reaction_dur 파라미터 추가, 0이면 기존
#  공식과 완전히 동일 - 회귀 없음).
#  🛡️ [LEADIN_OVERLAY_CHANCE와 다른 구조] 그건 보이스 1개가 확률적으로 짧게 끼어드는
#  "원-오프" 패턴이다. 이건 오늘 0단계에서 만든 패턴(main/lck_caster_dynamic/sub 3보이스가
#  거의 동시에, 0~150ms 각자 랜덤 오프셋으로 겹쳐 말함)과 구조적으로 동일하다 -
#  _stage0_track_starts/_stage0_duration을 이름 그대로 재사용한다(둘 다 범용적으로
#  이미 작성돼 있어 "어느 단계냐"를 모른 채로도 동작함).
# 🛡️ [비활성화 - 앵커링 위치 오류] 구현 직후 "환호+난입 리액션+닉네임이 연달아 쌓여
# 난리/개판처럼 들린다"는 피드백으로 확인됨 - 의도는 "킬 나기 전, 싸우는 도중(리드인
# 구간)"에 끼워 넣는 것이었는데, 실제 구현은 "킬 난 직후(0단계->1단계 사이)"에
# 앵커링돼 완전히 다른 자리였다. plan_kill_sequence의 battle_reaction_dur 파라미터/
# _pick_battle_reaction_files/BATTLE_MAIN_POOL 등 인프라와 15개 문구 음성 파일은
# 그대로 남겨둔다(콘텐츠 자체는 재사용 가능) - 아래 플래그 하나로 트리거만 끈다.
# 리드인 구간에 올바르게 앵커링하는 재설계는 별도 작업으로 진행한다.
BATTLE_REACTION_ENABLED = False
BATTLE_REACTION_MIN_PRE_BUILDUP_SLOTS = 2
# 🛡️ [문구 5개, 서로 다른 포인트] "팩트를 지어내지 않는" 원칙 그대로 - 챔피언/행동을
# 지목하지 않는 순수 격앙 반응만. 매 렌더 3개를 겹치지 않게(중복 없이) 뽑아 세 보이스가
# 각자 다른 문구를 동시에 외치게 한다(아래 _run_pipeline 참고). LCK 캐스터 특유의
# "같은 말을 반복하며 흥분을 쌓는" 스타일로 재작성("계속 때려주고!" 단발성 →
# "때려야 돼요, 때려야 돼요!!" 반복형) - c/e는 짧은 "안전장치" 문구로 설계해, 리드인
# 여백이 타이트한 케이스(kill_t=10s, 여백 1.95s)에서도 자동 스킵 없이 들어갈 수 있게 함.
BATTLE_REACTION_PHRASES = {
    "a": "때려야 돼요, 때려야 돼요!!",
    "b": "돌아가고, 돌아가고!!",
    "c": "다시, 다시!!",
    "d": "밀어붙여요, 밀어붙여요!!",
    "e": "지금이에요, 지금!!",
}
# 🛡️ [f~i: 짧은 끼어들기 추임새 - 놀람/기대 계열만] 처음 제안했던 "흠" 등 차분/숙고
# 계열은 "싸움이 막 시작된 순간"이라는 지금 자리의 톤과 안 맞아 전부 뺐다 - 해설자가
# "어? 뭔가 시작된 건가?" 하며 놀라고 기대하는 느낌만 남겼다(차분한 톤 없음). 받침
# 없는 모음 종결 또는 비(非)비음 받침만 썼다 - LEADIN_OVERLAY_POOL 쪽 "음?/흠?"가 sub
# 보이스에서 20회 전부 무음 게이트 실패(비음 받침 감쇠 문제)했던 전례를 피하기 위함.
# a~e(반복형 긴 문구)와 같은 BATTLE_MAIN_POOL/BATTLE_SUB_POOL에 섞여 들어가므로,
# _pack_reaction_chain이 가끔 이 짧은 추임새를 긴 문구 사이에 자연스럽게 섞어 고른다.
BATTLE_REACTION_PHRASES.update({
    "f": "오!",
    "g": "어?!",
    "h": "엇!",
    "i": "와!?",
})
# 🛡️ [sub 전용 단문 대체] "X, X!!" 대칭 반복 구조가 sub 보이스에서는 발화 길이가
# ~1.5초를 넘기면 구조적으로 무음 게이트를 통과하지 못했다(a/b/d 각각 15/15 전부
# 실패 - 반복 단어를 더 짧게 바꿔도 동일하게 전부 실패해 랜덤 변동이 아닌 구조적
# 한계로 확인됨). c/e는 원래 짧아 반복형 그대로 1회 통과했으므로 그대로 두고,
# a/b/d만 반복 없는 단문으로 대체(의미 계열은 동일 유지).
BATTLE_SUB_PHRASES = {
    "a": "계속 때려요!!",
    "b": "돌아가요!!",
    "d": "밀어붙여요!!",
}
BATTLE_MAIN_POOL = sorted(glob.glob(os.path.join(VOICE_DIR, "battle_main_*.wav")))
BATTLE_LCK_POOL = sorted(glob.glob(os.path.join(VOICE_DIR, "battle_lck_*.wav")))
BATTLE_SUB_POOL = sorted(glob.glob(os.path.join(VOICE_DIR, "battle_sub_*.wav")))
# 🛡️ [합성 설정] eleven_v4 + [excited][shouts] 태그 + stability=0.45 - 오늘 확립된
# "짧고 격앙된 문구"(urgent 풀) 레시피를 main/lck_caster_dynamic/sub 공통으로 그대로
# 적용했다. 전부 정적 풀(사전 녹음)이라 실시간 경로의 트림 함수가 필요 없고, 생성
# 시점에 이미 트레일링 무음을 트림해둔다(정적 풀 생성 관례 그대로).
# 🛡️ [발화 속도 - voice_settings.speed는 효과 없음, ffmpeg atempo로 후처리] "더 급박하게
# 들리게" 하려고 voice_settings에 speed(0.25~4.0, 기본 1.0)를 0.7~2.0으로 바꿔가며
# 실측했는데 길이가 전혀 안 변했다(노이즈 수준 변동만) - 이 TTS 엔드포인트(eleven_v4,
# /v1/text-to-speech)에서는 해당 필드가 무시되는 것으로 보임(ElevenLabs의 "speed
# control" 문서는 Conversational AI 에이전트 플랫폼 전용으로 보이고, 이 배치 합성
# 엔드포인트에는 적용 안 됨). 대신 무음 게이트를 통과한 결과물에 ffmpeg
# atempo=1.2를 적용해(피치 보존, 길이만 ~17% 단축) 최종 파일로 저장했다 - atempo는
# 단순 시간 압축이라 기존에 0이던 무음 개수가 늘어날 리 없고, 15개 전부 재검증해서
# 무음 0건 확인됨.
# 🛡️ [BATTLE_SUB_POOL만 eleven_v3 원복 - main/hype 수렴 문제와 동일 처방] 자기상관
# 기반 median pitch 실측 결과 main(v4)-sub(v4) 평균 pairwise 차이가 21.4Hz까지 좁혀져
# 있었다(0단계 main-hype 수렴 실패 사례의 50.9Hz보다도 더 심함) - stability를 보이스별로
# 다르게 주는 시도는 그 사건에서 이미 효과 없음이 확인됐으므로(79.4Hz->77.8Hz, 거의
# 개선 없음) 다시 시도하지 않고, hype를 v3로 되돌려 해결했던 것과 동일하게 sub만
# eleven_v3로 재생성했다(stability=0.45는 그대로, 여기도 바꾸지 않음). 재측정 결과
# main-sub 평균 pairwise가 40.4Hz로 늘긴 했으나 v3 기준 참고치(107.8Hz)에는 못 미쳤고,
# sub 자체의 문구 간 피치 변동폭(234.6~355.6Hz, 121Hz)이 main의 변동폭(38Hz)보다 훨씬
# 커서 "같은 보이스인데 문구마다 톤이 들쭉날쭉하다"는 새로운 변수도 생겼다 - 수치만으로는
# "뚜렷이 구분된다"고 단정할 수 없어 실제 렌더로 직접 들어봐야 하는 상태. atempo=1.2는
# v3 재생성본에도 동일 적용(5개 전부 무음 0건 재확인).
BATTLE_MAIN_TEXT = {f"battle_main_{k}.wav": v for k, v in BATTLE_REACTION_PHRASES.items()}
BATTLE_LCK_TEXT = {f"battle_lck_{k}.wav": v for k, v in BATTLE_REACTION_PHRASES.items()}
BATTLE_SUB_TEXT = {
    f"battle_sub_{k}.wav": BATTLE_SUB_PHRASES.get(k, v)
    for k, v in BATTLE_REACTION_PHRASES.items()
}


def _pick_battle_reaction_files() -> dict[str, str] | None:
    """BATTLE_REACTION_PHRASES 중 서로 다른 3개를 뽑아 main/lck_caster_dynamic/sub에
    무작위로 배정한 파일 경로 매핑을 반환하는 순수 함수(테스트 가능) - 세 풀 중 하나라도
    비어 있으면 안전하게 None(발동 스킵)을 반환한다. 문구-보이스 배정도 매번 섞어서,
    "항상 같은 보이스가 같은 포인트를 외치는" 패턴이 안 생기게 한다."""
    if not (BATTLE_MAIN_POOL and BATTLE_LCK_POOL and BATTLE_SUB_POOL):
        return None
    letters = random.sample(list(BATTLE_REACTION_PHRASES.keys()), 3)
    voice_order = ["main", "lck_caster_dynamic", "sub"]
    random.shuffle(voice_order)
    pool_by_voice = {"main": BATTLE_MAIN_POOL, "lck_caster_dynamic": BATTLE_LCK_POOL, "sub": BATTLE_SUB_POOL}
    prefix_by_voice = {"main": "battle_main_", "lck_caster_dynamic": "battle_lck_", "sub": "battle_sub_"}
    result = {}
    for vk, letter in zip(voice_order, letters):
        fname = f"{prefix_by_voice[vk]}{letter}.wav"
        match = next((p for p in pool_by_voice[vk] if os.path.basename(p) == fname), None)
        if match is None:
            return None  # 풀에 해당 글자 파일이 없으면(생성 누락 등) 안전하게 스킵
        result[vk] = match
    return result


# 🛡️ [리드인 재앵커링 - 올바른 위치] 위 BATTLE_REACTION_ENABLED 기능은 "0단계->1단계
# 사이"(킬 난 직후)에 앵커링돼 있었는데, 실제로 원했던 건 "리드인(킬 나기 전, 싸우는
# 도중)" 안에서 여러 명이 겹쳐 떠드는 효과였다 - 완전히 다른 자리라 새 트리거로
# 재구현한다. LEADIN_OVERLAY_CHANCE(보이스 1개, 확률적 원-오프)와는 다르게, 이미
# 배치된 상황 멘트(pre_buildup) 슬롯 하나의 시작 시점에 맞춰 2보이스(main+sub)가
# _stage0_track_starts와 동일한 패턴(0~150ms 각자 랜덤 오프셋)으로 겹쳐 들어간다 -
# "새 구간을 만들어 뒤로 미는" 0단계 방식이 아니라, 기존 리드인 타임라인을 그대로 두고
# 그 위에 겹쳐 재생만 하는 방식이라 pre_buildup_starts 등 기존 스케줄링은 전혀 안
# 바뀐다(회귀 위험 없음).
# 🛡️ [lck_caster_dynamic 제외] pre_buildup 자체가 이미 lck_caster_dynamic 혼자 말하고
# 있는 자리라, 같은 보이스가 자기 자신과 겹쳐 두 문장을 동시에 말하는 것처럼 들리는 걸
# 피하려고 "나머지" 보이스인 main+sub만 쓴다(BATTLE_MAIN_POOL/BATTLE_SUB_POOL에서 각자
# 독립적으로 문구를 이어 붙인다 - _pick_battle_reaction_files는 더 쓰지 않음, 비활성화된
# 구버전 전투 리액션 쪽에서만 계속 쓰인다).
BATTLE_LEADIN_OVERLAY_ENABLED = True


def pick_leadin_battle_anchor(pre_buildup_starts: list[float], pre_buildup_durations: list[float],
                               last_slot_is_urgent: bool = False) -> float | None:
    """리드인 전투 리액션(main+sub 2보이스)이 겹쳐 들어갈 앵커 시각을 고르는 순수 함수
    (테스트 가능).
    🛡️ [urgent 종료 이후 여백 활용 - 설계 검토 결론] urgent 슬롯 자체와 겹치면
    3보이스(urgent+main+sub)가 한 순간에 몰려 또 "난리" 위험이 있다는 게 설계 검토에서
    확인됐다 - 대신 urgent 문구가 끝난 시점부터 kill_t - EOEO_GAP_SEC까지 남는 여백
    (실측 1.9~3.1초+, _spread_fixed_n이 urgent보다 긴 "차분 풀 평균 길이" 기준으로
    세그먼트를 배정해서 항상 남는 자투리 - EOEO 제거 후로는 그 뒤로 kill_t 직전까지
    전부 포함)을 쓴다 - urgent 발화 자체는 전혀 안 건드리고, 그 "종료 시점"(urgent_
    start + urgent_duration)을 앵커로 반환해 main/sub가 거기서부터 겹쳐 채우기
    시작한다.
    urgent 슬롯이 없으면(PRE_BUILDUP_URGENT_POOL이 비어 있거나 last_slot_is_urgent=
    False) 예전처럼 "가장 늦은 calm 슬롯"의 시작 시점으로 폴백한다(urgent 자체가
    없으니 그 종료 시점이라는 개념도 없음).
    슬롯이 아예 없으면(N=0) None. 실제로 이 여백에 몇 개가 들어갈지는(각 보이스
    duration을 probe해야 알 수 있어 이 함수 밖에서) 호출부가 kill_t - EOEO_GAP_SEC과
    비교해 _pack_reaction_chain으로 따로 채운다."""
    if not pre_buildup_starts:
        return None
    idx = len(pre_buildup_starts) - 1
    if last_slot_is_urgent:
        return pre_buildup_starts[idx] + pre_buildup_durations[idx]
    return pre_buildup_starts[idx]


BATTLE_LEADIN_JOIN_MIN_DELAY_SEC = 0.3
BATTLE_LEADIN_JOIN_MAX_DELAY_SEC = 0.6


BATTLE_PREOVERLAP_MIN_SEC = 0.3
BATTLE_PREOVERLAP_MAX_SEC = 0.5
BATTLE_SHORT_INTERJECTION_LETTERS = {"f", "g", "h", "i", "j", "k", "l", "m"}


def _filter_short_interjection_pool(pool: list[str]) -> list[str]:
    """BATTLE_MAIN_POOL/BATTLE_SUB_POOL에서 짧은 끼어들기 추임새(f~i: "오!"/"어?!"/
    "엇!"/"와!?")만 골라내는 순수 함수(테스트 가능) - urgent 종료 직전 겹침에는
    긴 반복형 문구(a~e)가 아니라 이 짧은 추임새만 써야 urgent 본문을 가리지 않는다.
    파일명 규칙(battle_{voice}_{letter}.wav)의 마지막 글자로 판별한다.
    j~m은 EN 전용 호탕한 웃음 리액션("Whoa-ho-ho!!" 등, Carter/Atlee만 보유, KO
    쪽(battle_main/battle_sub)엔 해당 글자 파일이 없어 이 확장은 KO 동작에 영향 없음)."""
    return [p for p in pool
            if os.path.splitext(os.path.basename(p))[0].rsplit("_", 1)[-1] in BATTLE_SHORT_INTERJECTION_LETTERS]


def pick_urgent_preoverlap_starts(anchor: float,
                                   preoverlap_min: float = BATTLE_PREOVERLAP_MIN_SEC,
                                   preoverlap_max: float = BATTLE_PREOVERLAP_MAX_SEC,
                                   second_min: float = BATTLE_LEADIN_JOIN_MIN_DELAY_SEC,
                                   second_max: float = BATTLE_LEADIN_JOIN_MAX_DELAY_SEC,
                                   rng: random.Random | None = None) -> dict:
    """pick_staggered_join_starts를 대체 - "urgent 혼자 조용히 끝남 -> 갑자기 2인
    합창 시작"으로 뚝 끊기던 전환을, urgent가 끝나기(anchor) 0.3~0.5초 전부터 main/
    sub 중 하나(랜덤)가 짧은 추임새로 먼저 겹쳐 들어오게 해서 자연스럽게 잇는다.
    먼저 끼어드는 쪽(preoverlap_voice)이 그대로 다중 리액션의 "첫 합류자"를 겸하므로
    (anchor 이전에 시작), 같은 목소리가 끊김 없이 계속 말하는 느낌을 노린다. 나머지
    하나는 기존과 동일하게 anchor + U(second_min, second_max)에 합류한다.
    반환값: {"starts": {"main": ..., "sub": ...}, "preoverlap_voice": "main"|"sub"} -
    starts는 그대로 _pack_reaction_chain의 start_time으로 쓰인다(음수 방지로 0.0
    하한 clamp)."""
    _rng = rng if rng is not None else random
    preoverlap_voice = _rng.choice(["main", "sub"])
    other_voice = "sub" if preoverlap_voice == "main" else "main"
    preoverlap_delay = _rng.uniform(preoverlap_min, preoverlap_max)
    second_delay = _rng.uniform(second_min, second_max)
    return {
        "starts": {
            preoverlap_voice: max(0.0, anchor - preoverlap_delay),
            other_voice: anchor + second_delay,
        },
        "preoverlap_voice": preoverlap_voice,
    }


# ── 1단계(Hype 닉네임 샤우팅, 실시간 TTS) ──
# 🛡️ [발음 표기] 이름 음절을 늘려 쓰는 방식("장이이인정시이인!!")은 TTS 발음 경계와 안 맞아
# "장애~인정신"처럼 들리는 문제가 로컬 프로토타입에서 확인됨 - 음절은 그대로 두고 이름 끝에
# 물결표만 붙이는 방식(B)이 더 자연스러웠고, 여기에 볼륨 스웰(D2, 뒷부분만 서서히 커짐)을
# 결합해서 "길게 끄는 느낌"을 오디오 후처리로 흉내낸다(_apply_nickname_swell). 0단계가 이미
# "우와아아아악!!" 감탄사를 셋이 같이 외치므로, 여기선 닉네임만 - 감탄사 중복 없음.
HYPE_NICKNAME_SHOUT_TEMPLATE = "{killer}~~!!"
# 🛡️ [하이픈 늘려 부르기 - "장이이인정시이인!!"과는 다른 방식] 위 주석의 음절 내부 반복
# 방식(오발음 확인돼 폐기)과 달리, 이건 음절 "사이"에만 하이픈을 끼운다("페이커" ->
# "페-이-커") - 오늘 재현 실험(하이픈 1개/2개 × main/lck_caster_dynamic × 각 5회, 총
# 20회)에서 중간 무음(오발음 징후) 0건이었고, 뒤에 짧은 리액션 문장을 이어 붙이면
# 단독 발화보다 억양이 더 자연스러워진다는 것도 실측 확인됐다(attack_rise 단독 0.079
# -> 문맥 포함 0.127dB/ms, +60%). 물결표 2개+느낌표 4개(물결표가 ElevenLabs 텍스트
# 정규화에서 "길게 끄는 소리"로 해석되는 것으로 추정)까지 포함해 실측된 조합 그대로
# 가져온다. 리액션 문구는 팩트를 전혀 포함하지 않는 순수 반응이라(오늘 원칙과 동일)
# 어떤 킬에도 안전하게 붙일 수 있다.
HYPE_NICKNAME_SHOUT_STRETCHED_TEMPLATE = "{hyphenated}~~!!!! 그냥 돌아버렸는데요??!!"
# 🛡️ [협공 킬 - 팀명 샤우팅] 어시스트가 있는 킬(2대1 등 협공)은 킬러 개인 닉네임 대신
# 팀명을 3보이스로 외친다 - team100=블루팀/team200=레드팀 매핑은 _format_match_context_block
# (대본 컨텍스트 블록)에서 이미 쓰던 것과 동일하게 재사용.
TEAM_ID_TO_NAME_KO = {100: "블루팀", 200: "레드팀"}
TEAM_ID_TO_NAME_EN = {100: "Team Blue", 200: "Team Red"}
NICKNAME_SWELL_START_RATIO = 0.55   # 이 지점부터(대략 물결표 여운 구간) 볼륨이 커지기 시작
NICKNAME_SWELL_RISE = 0.6           # 클립 끝에서 최대 몇 배(1+RISE)까지 커지는지
# 🛡️ [진짜 "늘려 부르기" - 타임스트레치 추가] 볼륨 스웰은 끝부분이 커지는 것뿐이라("강조") 실제
# 발음이 길게 늘어지는("페이커~~"처럼) 효과는 아니었다는 게 재확인됐다. 텍스트로 음절을 늘려
# 쓰는 방식은 이미 두 번(닉네임/팀명) 신뢰성 없음이 실측 확인됐으므로(TTS가 늘어난 텍스트를
# 늘어난 길이로 발음해준다는 보장이 없음, 오히려 오발음 위험), 대신 오디오 레벨에서 물결표
# 여운 구간만 잘라내 ffmpeg atempo(재생속도만 변경, 피치 보존)로 실제로 늦춰서 다시 이어
# 붙인다 - 이미 정확히 발음된 음성을 그대로 쓰므로 발음 붕괴 위험이 없다. 0.5/0.6/0.7 세
# 배율을 실측 비교한 결과(무음 게이트/피크 레벨 전부 이상 없음, 원본에 이미 있던 자연스러운
# 꼬리 감쇠 구간 외 새 무음/클리핑 없음) 배율이 낮을수록(더 느리게) 더 길게 늘어나는 게
# 예측 가능하게 확인됨(0.5→+40~44%, 0.6→+27~28%, 0.7→+17~19%, 개인명/팀명·KO/EN 4개
# 샘플 공통) - 다만 "로봇틱하게 안 들리는지"는 라우드니스/무음 통계로는 판단이 안 되고
# 직접 들어봐야 하는 영역이다. 가장 강한 0.5는 ffmpeg atempo 허용 범위(0.5~100)의 하한
# 경계값이라 품질 저하 위험이 상대적으로 더 크다고 보고, 0.6/0.7 중 "확실히 늘어지는 느낌"에
# 더 가까운 중간값 0.6을 기본값으로 채택했다 - 세 배율 전부 실제 파일로 만들어 비교해봤으니
# 직접 들어보고 이 상수만 바꿔 조정 가능하다.
NICKNAME_SWELL_ATEMPO_RATIO = 0.6
# 🛡️ [개인 닉네임 타임스트레치 - 글자 수 분기 범위] 한글 기준 len(name)이 음절 수와 정확히
# 일치한다는 게 실측 확인됨(len("페이커")==3 등) - 이 범위(3~5자)만 스트레치 적용, 2자
# 이하/6자 이상은 원본 그대로(볼륨 스웰만). 팀명 샤우팅은 이 분기 대상이 아니고 항상
# 스트레치 적용(고정 문자열 4개뿐이라 이미 개별 실측 검증됨).
NICKNAME_STRETCH_MIN_LEN = 3
NICKNAME_STRETCH_MAX_LEN = 5
# 🛡️ [하이픈 늘려 부르기 - 3음절로 범위 축소, 실측 발견] 위 3~5자 범위는 기존 오디오
# 후처리(atempo 스웰) 기준이다 - 텍스트에 하이픈을 직접 끼우는 새 기능(아래 use_hyphen_
# stretch)은 "페이커"(3음절) 기준으로만 오늘 45회(20+25) 실측 검증됐다. 실제 구현 후
# 4/5음절("쇼메이커"/"데프트초롱")로 재검증했더니 둘 다 하이픈 이름과 뒤에 붙인 리액션
# 문장 사이에 새로운 중간 무음(1.3~1.5s 지점, 트레일링과 별개)이 생겨 무음 2곳으로
# 걸렸다 - _trim_trailing_silence는 "정확히 1곳"일 때만 안전하게 자르므로 이 경우
# 원본을 그대로 둔다(섣불리 첫 무음 앞까지 자르면 리액션 문장 전체가 날아감). 3음절만
# 검증된 상태로 4~5음절까지 넓히는 건 근거가 부족하다고 판단해, 하이픈 늘려 부르기는
# 정확히 3음절에서만 적용하고 4~5자는 기존 atempo 스웰 경로로 그대로 폴백한다(아래
# NICKNAME_STRETCH_MIN_LEN<=len<=NICKNAME_STRETCH_MAX_LEN elif가 자연히 받아줌) -
# 4~5음절 하이픈 늘려 부르기는 별도 조사 후 넓힐 수 있는 여지로 남겨둔다.
NICKNAME_HYPHEN_STRETCH_MAX_LEN = 3
# 🛡️ [스웰 시작 비율 동적화 - 짧은 이름 실측 문제 수정] "넥스"(2음절) 실측 결과 고정
# 0.55가 마지막 음절("스", 실측 경계 약 72% 지점)이 아니라 첫 음절("넥") 중간에 걸리는
# 게 확인됐다 - 음절마다 실제 길이가 크게 다르고("넥"이 "스"보다 훨씬 길었음) 음절
# 경계를 직접 감지할 신뢰할 만한 방법도 없어서(별도 조사 참고) 완벽한 해결책은 아니지만,
# 최소한 "짧은 이름에서 첫 음절 중간에 걸리는" 명백한 오류는 음절 수 기반 근사로 피할
# 수 있다. 한글 기준(len(name)==음절 수)에서만 정확하고, 영어 이름은 여전히 근사치일
# 뿐이다(팀명 "Team Blue"/"Team Red"는 글자 수가 9/8이라 이 표에서는 그냥 else(0.55)
# 로 떨어져 기존 그대로 - 변경 없음).
NICKNAME_SWELL_START_RATIO_2SYL = 0.72   # 실측: "넥스"에서 "스" 시작 지점(약 0.749/1.04=0.72)과 일치
NICKNAME_SWELL_START_RATIO_3SYL = 0.6


def _nickname_swell_start_ratio(name: str) -> float:
    """이름 길이(한글 기준 음절 수)에 맞춰 스웰/스트레치 시작 비율을 고른다(순수 함수,
    테스트 가능). 2음절 이하는 0.72, 3음절은 0.6, 4음절 이상(팀명 EN 포함, else로 귀결)
    은 기존 기본값(NICKNAME_SWELL_START_RATIO=0.55) 그대로."""
    n = len(name)
    if n <= 2:
        return NICKNAME_SWELL_START_RATIO_2SYL
    if n == 3:
        return NICKNAME_SWELL_START_RATIO_3SYL
    return NICKNAME_SWELL_START_RATIO


_HANGUL_SYLLABLE_RE = re.compile(r"^[가-힣]+$")


def _is_pure_hangul(name: str) -> bool:
    """name이 한글 음절(가-힣)로만 이루어졌는지 확인하는 순수 함수(테스트 가능) -
    len(name)==음절 수라는 전제가 성립하는 범위를 한정한다. 롤 닉네임은 영문/숫자/특수
    문자도 흔해서("Nyx", "Hide on bush" 등) 길이만 보고 하이픈을 끼우면 "N-y-x"처럼
    글자 단위로 끊어 읽는 무의미한 표기가 될 위험이 있다(실측으로 발견 - 길이 조건만
    쓰던 기존 atempo 스웰 분기는 오디오 후처리라 이 문제가 없었지만, 텍스트에 직접
    하이픈을 끼우는 이 기능은 다르다)."""
    return bool(_HANGUL_SYLLABLE_RE.fullmatch(name))


def _hyphenate_korean_name(name: str) -> str:
    """한글 이름을 음절(글자) 단위로 하이픈을 끼워 늘려 부르기 표기로 바꾸는 순수 함수
    (테스트 가능) - "페이커" -> "페-이-커". 호출부가 _is_pure_hangul로 걸러낸 순수 한글
    이름 3~5자 범위에서만 쓴다(HYPE_NICKNAME_SHOUT_STRETCHED_TEMPLATE 참고)."""
    return "-".join(name)


# 🛡️ [EN 비영문 닉네임 미발화 원칙] 실제 매치 데이터(KR_8393410432)에서 10명 중 6명이
# 한글/혼합 스크립트 닉네임이었음을 확인 - 로마자 변환/GPT 음역 시도는 전부 "그럴듯하게
# 들리지만 부정확한 발음"만 만들어낼 뿐이라 포기하고, "영문이 아니면 아예 안 부른다"는
# 원칙으로 간다. isascii()는 한글/한자/일본어/이모지 등 비ASCII 문자를 전부 걸러내고,
# 알파벳이 하나도 없는 경우(순수 숫자/기호 닉네임)도 "부를 이름"으로서는 의미가 없어
# 같이 걸러낸다.
def _is_ascii_name(name: str) -> bool:
    """닉네임이 순수 영문(ASCII: 알파벳/숫자/공백/일반 특수문자)으로만 이루어져 있고
    알파벳을 하나 이상 포함하는지 판별하는 순수 함수(테스트 가능)."""
    return bool(name) and name.isascii() and any(c.isalpha() for c in name)


# 🛡️ [포지션 기반 대체 표현] Riot API의 teamPosition 값("TOP"/"JUNGLE"/"MIDDLE"/
# "BOTTOM"/"UTILITY", 비드래프트 모드는 빈 문자열) -> 캐스터가 실제로 쓰는 역할 명칭.
EN_POSITION_ROLE_WORDS = {
    "TOP": "top laner", "JUNGLE": "jungler", "MIDDLE": "mid laner",
    "BOTTOM": "bot laner", "UTILITY": "support",
}


def _en_display_name(name: str, position: str | None, side: str) -> str:
    """EN 전용 - 닉네임이 영문이면 그대로, 아니면 "부르지 않고" 역할/대명사로 대체한
    표시용 이름을 반환하는 순수 함수(테스트 가능). side는 "killer"/"victim"/"assist" -
    같은 역할(예: 양쪽 다 미드라이너)이 킬러/피해자로 동시에 나와도 "the enemy mid
    laner"(킬러) vs "the mid laner"(피해자)로 구분되게 접두사를 다르게 둔다. 포지션
    정보가 없으면(비드래프트 등) 일반 대명사로 대체 - 이 치환은 GPT 프롬프트에 원문
    이름을 아예 넘기지 않는 단계(호출부)에서 쓰이므로, "이름을 부르지 말라"는 지시에
    기대지 않고 애초에 원문을 노출시키지 않아 지시 불이행 위험 자체가 없다."""
    if _is_ascii_name(name):
        return name
    role_word = EN_POSITION_ROLE_WORDS.get(position or "")
    if role_word:
        if side == "killer":
            return f"the enemy {role_word}"
        if side == "victim":
            return f"the {role_word}"
        return f"their {role_word}"
    if side == "killer":
        return "the enemy"
    if side == "victim":
        return "his opponent"
    return "a teammate"


# ── 2단계(Sub 의문형 감탄, 정적 풀) ──
SUB_QUESTION_POOL = sorted(glob.glob(os.path.join(VOICE_DIR, "sub_question_*.wav")))
SUB_QUESTION_TEXT = {
    "sub_question_a.wav": "진짜 돌았는데요??!!", "sub_question_b.wav": "이게 실화예요??!!",
    "sub_question_c.wav": "미쳤는데요 진짜??!!",
}
# 🛡️ [영어 2단계 - Atlee, 한국어 SUB_QUESTION_POOL과 같은 역할] 사실 정보 없이 순수하게
# 놀라는 의문문만 담는다(한국어 3개와 동일한 원칙) - 어떤 킬에 붙어도 항상 성립하는 문장이라
# 정적 풀로 미리 구워도 안전함. lang=="en"일 때 SUB_QUESTION_POOL/TEXT 대신 이 풀을 쓴다
# (스케줄 딕셔너리 키 이름은 "sub_question"으로 공유 - _render_video 쪽은 언어와 무관하게
# 이미 그 키 하나만 보므로 새 키가 필요 없다).
ATLEE_SUB_QUESTION_POOL = sorted(glob.glob(os.path.join(VOICE_DIR, "atlee_sub_question_*.wav")))
# 🛡️ [텍스트 확정 경위] b/c는 원래 더 긴 문장(예: "Wait, did that seriously just happen?!")을
# 시도했는데, 이 보이스에서 문장 중후반부에 매번 미세 무음 갭이 남아 16회씩 세 차례(각기 다른
# 문구로) 전부 실패했다 - 성공한 a처럼 쉼표 없는 아주 짧은 단일 절로 줄이자 1회 만에 통과했다.
ATLEE_SUB_QUESTION_TEXT = {
    "atlee_sub_question_a.wav": "Is this actually real?!",
    "atlee_sub_question_b.wav": "Seriously?!",
    "atlee_sub_question_c.wav": "How?!",
}

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
# 🛡️ [0단계 환호 풀 - LCK 스타일 전면 재녹음] 기존 4개(차분한 진행 멘트/짧은 감탄사)는
# 실제 들어보면 톤이 차분해서 "빠르고 몰아치는" LCK 느낌이 안 났다 - KO main_explode/
# hype_explode/sub_explode의 "순수 모음 반복"(우와+아*N+!!) 패턴을 영어로 그대로
# 이식해서(WOOO+O*N+!! 류) 3보이스 전부 5개씩 재작성/재녹음했다. eleven_v4 +
# [excited][shouts] 태그 + stability=0.45(KO urgent 풀과 동일 레시피) + atempo=1.2
# 후처리까지 적용(27개 전부 무음 게이트 0건 재검증 완료).
STERLING_POOL = sorted(glob.glob(os.path.join(VOICE_DIR, "sterling_*.wav")))
STERLING_TEXT = {
    "sterling_a.wav": "WOOOOOOOOOO!!",
    "sterling_b.wav": "WHOAAAAAAAAAAH!!",
    "sterling_c.wav": "YEAHHHHHH!!",
    "sterling_d.wav": "OHHHHHHHHHH!!",
    "sterling_e.wav": "WOAHAAAAAAA!!",
}
CARTER_POOL = sorted(glob.glob(os.path.join(VOICE_DIR, "carter_*.wav")))
CARTER_TEXT = {
    "carter_a.wav": "OHHHHHHHHH!!",
    "carter_b.wav": "WHOOOOOOOOOOAAH!!",
    "carter_c.wav": "YEAAAAAAAAAAH!!",
    "carter_d.wav": "WOOOOOOOOO!!",
    "carter_e.wav": "AHHHHHHHHHHHHHH!!",
}
# 🛡️ [glob 충돌 버그 수정 - 실제 렌더 테스트로 발견] "atlee_*.wav"는 나중에 추가된
# "atlee_sub_question_*.wav"(2단계용, ATLEE_SUB_QUESTION_POOL)까지 그대로 삼켜서, 0단계
# 캐스케이드에 sub_question 파일이 섞여 뽑히면 ATLEE_TEXT에 없는 키라 KeyError가 났다(이번
# 세션 내내 경계했던 바로 그 glob 충돌 패턴). "atlee_" 뒤에 글자 하나만 오는 파일(a~e)만
# 정확히 잡도록 패턴을 좁혀서 sub_question 파일과 겹치지 않게 한다.
ATLEE_POOL = sorted(glob.glob(os.path.join(VOICE_DIR, "atlee_[a-z].wav")))
ATLEE_TEXT = {
    "atlee_a.wav": "WHOAAAAAA!!",
    "atlee_b.wav": "OHHHHH!!",
    "atlee_c.wav": "YEAHHH!!",
    "atlee_d.wav": "WOOOOOO!!",
    "atlee_e.wav": "AAAAHHH!!",
}

# ── 영어 리드인 필러(한국어 pre_buildup(N개, 유동)+EOEO(마지막 1개) 구조에 대응, 1~4개 유동 배치) ──
# 🛡️ [환각 위험 차단 원칙 동일 적용] PRE_BUILDUP_TEXT와 동일한 원칙 - 위치/챔피언/구체적
# 액션을 특정하지 않는 순수 분위기 문구만 채택, 어떤 클립에 붙어도 항상 사실일 수 있는
# 문장만 사용(게임 시각/스코어처럼 렌더 시점에 실제로 확정된 정보라도, 이 필러는 실시간
# TTS가 아닌 정적 풀이라 값을 문구에 끼워 넣지 못한다 - 동적으로 하려면 실시간 TTS 전환이
# 필요하며 이번 라운드 범위 밖).
# 🛡️ [LCK 스타일 전면 재작성 - 짧은 감탄사 -> 완결 문장] 기존 6개는 톤이 차분해서
# "빠르고 극적인 빌드업" 느낌이 안 났다 - KO에서 확인된 원칙("짧은 감탄사보다 완결 문장이
# 오히려 더 안정적으로 무음 게이트를 통과한다")을 그대로 따라 9개 전부 완결 문장으로
# 재작성했다. 전부 챔피언/위치/행동을 특정하지 않는 중립 문구(환각 위험 차단 원칙은
# 아래 그대로 유지) - Sterling 보이스, eleven_v4, stability=0.55, atempo=1.2 후처리
# 적용(9개 전부 1회 시도로 무음 게이트 0건 통과).
EN_LEADIN_POOL = sorted(glob.glob(os.path.join(VOICE_DIR, "en_leadin_*.wav")))
EN_LEADIN_TEXT = {
    "en_leadin_a.wav": "Tension is building here!",
    "en_leadin_b.wav": "This could go either way!",
    "en_leadin_c.wav": "Someone's about to make a move!",
    "en_leadin_d.wav": "The air feels electric right now!",
    "en_leadin_e.wav": "Anything could happen here!",
    "en_leadin_f.wav": "This is heating up fast!",
    "en_leadin_g.wav": "Eyes are locked in on this moment!",
    "en_leadin_h.wav": "It's all coming down to this!",
    "en_leadin_i.wav": "The pressure is real right now!",
}
EN_LEADIN_MIN_COUNT = 1  # 목표치(코드로 강제하진 않음 - 자리가 없으면 0개까지 줄어들 수 있음)
EN_LEADIN_MAX_COUNT = 4
EN_LEADIN_START_OFFSET_SEC = PRE_BUILDUP_START_OFFSET_SEC  # 재사용: 클립 시작 후 이만큼 뒤에 첫 필러 시작
# 🛡️ [독립 상수로 분리] 예전엔 PRE_BUILDUP_GAP_SEC을 그대로 재사용했는데, 그러면 한국어
# 쪽 간격(PRE_BUILDUP_GAP_SEC)을 조정할 때마다 영어 간격도 같이 흔들렸다. 값 자체는
# 분리 시점의 PRE_BUILDUP_GAP_SEC(0.2)과 동일하게 유지 - 영어는 그대로 0.2.
EN_LEADIN_GAP_SEC = 0.2
EN_LEADIN_END_GAP_SEC = EOEO_GAP_SEC                       # 재사용: 마지막 필러 종료~kill_t 최소 여백
# 🛡️ [EN 실시간 합성물 속도 압축 - "쉬지 않고 빠르게 몰아친다"는 LCK 톤의 핵심 요소]
# voice_settings.speed가 이 TTS 엔드포인트(/v1/text-to-speech, eleven_v4)에서 무시된다는
# 게 조사로 확인됐다 - 대신 KO battle_main/lck/sub 정적 풀 생성 때 검증된 그대로, 합성
# 후 ffmpeg atempo로 재생 속도만 압축한다(피치 보존). hype_nickname/main_fact(실시간
# TTS) + 새로 녹음한 0단계 환호/리드인 정적 풀(생성 시점에 이미 베이크됨) 전부 동일
# 비율로 맞춘다. KO에 전혀 영향 없음(이 상수는 lang=="en" 분기에서만 참조됨).
EN_REALTIME_ATEMPO_RATIO = 1.2
# 🛡️ [한국어 리드인도 N슬롯으로 확장] 상황멘트(PRE_BUILDUP) 자리 수를 EN_LEADIN과 같은 상한으로
# 맞춘다 - 렌더당 최대 이만큼 "상황멘트류"가 순차 배치된다(자리가 없으면 더 적게). EOEO
# 제거 이후로는 이 뒤에 고정으로 오는 요소가 없고, kill_t - EOEO_GAP_SEC까지가 그대로
# 가용 구간의 끝이다(plan_lead_in_forward_eoeo 참고).
# 🛡️ [4 -> 3으로 하향 - 문장형 교체에 따른 재조정] PRE_BUILDUP_POOL이 1어절 추임새에서
# "긴장 국면" 서술 문장(2~4어절, 1.3~2.3s)으로 바뀌면서, 긴 클립(kill_t=15s 시뮬레이션)에서
# 실제로 4개가 전부 배치되는 경우가 나왔다 - 4개 전부 "긴장하고 있다" 계열 문장이라 연속
# 재생되면 같은 말을 4번 반복하는 것처럼 산만해질 위험이 큼(추임새는 짧고 의미가 없어
# 반복돼도 안 걸렸지만, 완전한 문장은 반복되면 티가 남). 3으로 낮춰서 반복 체감을 줄이되,
# 긴 클립에서 커버리지가 너무 줄지 않는 균형점으로 판단(시뮬레이션: kill_t=15s 기준 4개
# 배치 시 필러 간격 약 3.1~3.4s, 3개는 약 4.5s, 2개는 약 6.8s로 이미 텀이 벌어지기
# 시작함 - 2까지 줄이면 마지막 필러~"어어??" 사이 공백이 너무 길어져 3을 채택). EN_LEADIN_
# MAX_COUNT(4)와는 의도적으로 갈라짐 - EN_LEADIN_POOL 콘텐츠는 이번에 안 바꿨으므로 그대로 둠.
PRE_BUILDUP_MAX_COUNT = 3

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
# 🛡️ [0단계 환호 비중 확대 - 0.65 -> 0.85] "길이 가중 랜덤 선택"(기존 풀에서 긴 파일이
# 조금 더 자주 뽑히게 하는 방법)을 먼저 시도했으나 실측 결과 stage0_dur 평균이 겨우
# +1.1%(약 20ms) 늘어나는 수준이라 체감 불가능한 크기였다 - 폐기하지 않고 그대로 유지는
# 하되(부작용 없음, 아래 설명), 효과가 큰 이 방법으로 갈아탄다. plan_kill_sequence가
# t1(0->1)/t2(1->2)/t3(2->3) 세 전환 모두 이 하나의 ratio를 공유하므로, 0.65->0.85로
# 올리면 0단계가 더 오래 화면을 차지하는 것뿐 아니라 1->2, 2->3 전환도 같이 더 느슨해진다
# (전부 같은 비율로 뒤로 밀림 - 별도 상수로 0단계 전환만 분리하는 대신, 전체적으로 "덜
# 급하게" 넘어가는 쪽이 자연스러워 보여 하나의 상수를 그대로 올리는 방향을 택함).
# 이로 인해 킬 시점~3단계(Main 사실 전달) 종료까지의 전체 구간이 길어지고, total_duration
# 산식(max(원본 클립 길이, 마지막 종료 시각+RENDER_TAIL_BUFFER_SEC))이 그만큼 커져 영상이
# 뒷단계를 압축하는 게 아니라 꼬리쪽(tpad로 마지막 프레임 고정, apad로 게임 오디오 루프)이
# 늘어나는 방식으로 흡수한다 - 실측 결과는 커밋 메시지 참고.
STAGE_OVERLAP_RATIO = 0.85
RENDER_TAIL_BUFFER_SEC = 0.8      # 마지막으로 끝나는 목소리 종료 후 여유
# 🛡️ [0단계 3트랙 시차 - "한 명처럼 들린다" 피드백] main/hype/sub_explode 셋 다 "start":
# kill_t로 완전히 동일한 순간에 시작한다는 게 실측 확인됐다(delay_ms까지 밀리초 단위로
# 동일) - 완전 동시 시작이 셋이 하나로 뭉쳐 들리는 원인일 수 있다고 판단해, 각 트랙에
# 독립적인 0~150ms 랜덤 오프셋을 준다(_stage0_track_starts). 150ms는 "따로 외치는"
# 느낌을 주면서도 킬 임팩트 타이밍 자체가 체감될 만큼 밀리지는 않는 범위로 잡았다 -
# 더 정밀한 값은 직접 들어보고 조정 가능.
STAGE0_OFFSET_MAX_SEC = 0.15

ELEVENLABS_VOICE_IDS = {
    "main": "tlUdVt24VftfDokp32eu",  # LCK_Main_caster
    # 🛡️ [1단계 닉네임 샤우팅 - hype -> lck_caster_dynamic 교체] 0단계(환호, 정적 풀)가
    # 아니라 1단계(닉네임 샤우팅, 실시간 TTS)가 원래 의도한 교체 자리였다 - 지난 라운드는
    # 착오로 0단계에 적용했었고, 그건 되돌리지 않고 그대로 둔 채(0단계와 1단계 둘 다
    # lck_caster_dynamic을 쓰게 됨) 이번에 1단계도 추가로 바꾼다. "hype" 키는 이제
    # nickname_voice_keys(KO)에서만 쓰였는데 그 자리가 교체되면서 완전히 미사용이 돼
    # 삭제했다(voice_id=IyAj6lA2EjUlXLg33b1o, LCK_Hype_Reaction - 필요하면 git 이력에서
    # 복구 가능).
    "lck_caster_dynamic": "GriSG3WMe4Ve3jcVnYBf",
    "sub": "K4OVml3awIZZxKC33zQV",   # Lck_Sub_Analyst
    # 🛡️ [영어 실시간 합성용] hype_nickname/main_fact 호출부에서 lang=="en"일 때만 골라 쓴다 -
    # _synthesize_voice_line 자체는 voice_key 문자열 하나만 보고 조회할 뿐 언어를 모르므로 건드리지 않음.
    "sterling": "3hQzcLsCrO9a7MOEtScA",
    "carter": "LymGX871eqlpoxSlhtzG",
    # 🛡️ [3보이스 동시 콜용] hype_nickname이 1콜(carter만)에서 main+hype+sub/sterling+
    # carter+atlee 3콜 동시 호출로 바뀌면서, 기존엔 정적 wav 풀(ATLEE_POOL) 전용이던
    # atlee도 실시간 TTS 보이스로 추가됐다.
    "atlee": "yhtcul5bvNND79PLysM4",
}
# 🛡️ [v3 -> v4 전환] GET /v1/models로 실측 확인: eleven_v4는 eleven_v3와 동일하게
# can_use_style=False(둘 다 style 파라미터를 모델이 실제로 반영 안 함 - 기존 stability/style
# 튜닝에서 style이 효과 없어 보였던 이유일 가능성), max_characters_request는 v3(5000)보다
# 큰 10000. 실제 계정으로 status=200 합성 테스트도 통과했다(등급 문제 없음). 코드 안에서
# 이 상수를 참조하는 곳은 실시간 합성 경로(_synthesize_voice_line) 하나뿐이고, 정적 풀
# 생성용 스크래치 스크립트들도 하드코딩 없이 이 상수를 그대로 참조하므로 여기 하나만
# 바꾸면 전체에 일괄 적용된다.
ELEVENLABS_MODEL_ID = "eleven_v4"
# 🛡️ [output_format 명시] 예전엔 지정을 아예 안 해서 API 기본값(mp3_44100_128)을 그대로 썼다.
# Creator 티어로 업그레이드하면서 192kbps가 열려 명시적으로 올렸다 - pcm_44100(무손실)은
# Pro 티어부터라 아직 못 쓴다. 받은 mp3를 바로 ffmpeg로 WAV 변환해서 믹싱하므로(아래
# _synthesize_voice_line), 128->192kbps는 그 변환 전 손실 압축 정도를 줄여주는 효과.
ELEVENLABS_OUTPUT_FORMAT = "mp3_44100_192"


def plan_kill_sequence(stage0_dur: float, stage1_dur: float, stage2_dur: float,
                        battle_reaction_dur: float = 0.0,
                        ratio: float = STAGE_OVERLAP_RATIO) -> dict:
    """0~3단계 타이밍 계획(순수 함수, 테스트 가능). kill_t를 기준(0)으로, 각 단계 시작을
    "직전 단계에서 가장 늦게 끝나는 목소리 길이 × ratio" 지점으로 잡는다 - 로컬 프로토타입
    v5~v9에서 검증된 방식 그대로. 반환값은 kill_t 기준 상대 오프셋(t1/t2/t3)이라, 호출부에서
    kill_t를 더해 절대 시각으로 바꿔 쓴다.
    🛡️ [전투 지속 리액션 삽입 - battle_reaction_dur] 0단계->1단계 전환 지점(t1 = stage0_dur
    * ratio)은 그대로 두고, 그 지점에서 전투 리액션이 재생된 뒤에 1단계가 시작하도록
    t1에 battle_reaction_dur*ratio를 더한다(같은 "겹침 철학"을 리액션->1단계 전환에도
    동일하게 적용 - 새 전환 하나가 늘었을 뿐 규칙은 안 바꿈). battle_reaction_dur=0.0
    (기본값, 조건 미충족/EN)이면 t1 = stage0_dur*ratio로 기존 공식과 완전히 동일하다 -
    회귀 없음."""
    t_battle = stage0_dur * ratio
    t1 = t_battle + battle_reaction_dur * ratio
    t2 = t1 + stage1_dur * ratio
    t3 = t2 + stage2_dur * ratio
    return {"t_battle": t_battle, "t1": t1, "t2": t2, "t3": t3}


def _stage0_track_starts(kill_t: float, max_offset_sec: float = STAGE0_OFFSET_MAX_SEC,
                          rng: random.Random | None = None) -> tuple[float, float, float]:
    """0단계 3트랙(main/hype/sub_explode) 각각에 독립적인 [0, max_offset_sec) 랜덤
    오프셋을 더한 시작 시각(main, hype, sub 순)을 반환하는 순수 함수(테스트 가능, rng를
    주입하면 결정적으로 테스트 가능) - 셋 다 kill_t에 완전히 동시 시작하던 걸 깨서
    "한 명처럼 들린다"는 문제를 완화한다."""
    _rng = rng if rng is not None else random
    return (
        kill_t + _rng.uniform(0, max_offset_sec),
        kill_t + _rng.uniform(0, max_offset_sec),
        kill_t + _rng.uniform(0, max_offset_sec),
    )


def _stage0_duration(starts: tuple[float, float, float], durations: tuple[float, float, float],
                      kill_t: float) -> float:
    """0단계가 끝나는 시점(세 트랙 중 가장 늦게 끝나는 지점)을 kill_t 기준 상대값으로
    계산하는 순수 함수(테스트 가능) - 예전엔 셋 다 kill_t에서 동시 시작해 "길이의
    최댓값=종료 시점의 최댓값"이 성립했지만, _stage0_track_starts로 트랙마다 시작이
    달라지면 더는 아니다 - "시작+길이"의 최댓값으로 계산해야 정확하다."""
    return max(s + d for s, d in zip(starts, durations)) - kill_t


BATTLE_LEADIN_CHAIN_GAP_SEC = 0.15

# 🛡️ [EN 다중 리액션 체인 - Carter+Atlee] KO의 BATTLE_MAIN_POOL/BATTLE_SUB_POOL과
# 동일한 역할을 하는 EN 전용 풀. Sterling은 이미 en_leadin 보이스로 쓰여서 제외했다
# (KO가 lck_caster_dynamic을 pre_buildup과 겹친다고 제외한 것과 동일한 논리) -
# Carter+Atlee 둘만 반복형 긴 문구 5개("Get him! Get him!!" 류) + 짧은 끼어들기
# 8개(f~m, BATTLE_SHORT_INTERJECTION_LETTERS와 동일한 글자 규칙)를 쓴다. KO와 똑같은
# 구조적 문제(긴 반복형이 특정 보이스에서 중간 무음으로 15회 전부 실패)가 실제로
# 재현됐다 - Carter는 a/c/d가 전부 실패해 반복 제거한 단문으로 교체했고(아래 TEXT의
# 실제 값 참고), Atlee는 c만 실패했다. eleven_v4 + [excited][shouts] + stability=0.45
# (KO urgent 레시피 재사용) + atempo=1.2(나머지 EN 신규 콘텐츠와 톤 일관성 유지).
# 🛡️ [호탕한 웃음 리액션 정식 등록 - j~m] 단일 내레이션 재설계 후 "메인 내레이터가
# 계속 말하는 동안 제3자가 짧게 리액션만 얹는다" 구조로 바뀌면서, 담백한 감탄사(f~i)
# 외에 "호탕하게 웃으며 감탄"하는 버전도 필요해 추가 실험 후 채택한 4개 - "하하/호호"
# 반복 텍스트 접근(비교 실험의 그룹 B 중 1개 포함)과 웃음 섞인 감탄사(그룹 A 전부)
# 중, 그룹 A 3개 + 그룹 B 1개가 채택됐다. 나머지 그룹 B 2개("Ha-ha, no way!!"/
# "Ha-ha-ha!!")는 폐기(에셋 미등록) - Carter/Atlee 둘 다 동일 텍스트로 녹음.
EN_BATTLE_CARTER_POOL = sorted(glob.glob(os.path.join(VOICE_DIR, "battle_carter_*.wav")))
EN_BATTLE_ATLEE_POOL = sorted(glob.glob(os.path.join(VOICE_DIR, "battle_atlee_*.wav")))
EN_BATTLE_CARTER_TEXT = {
    "battle_carter_a.wav": "Get him!!",  # 반복형("Get him! Get him!!")이 15/15 무음 실패 -> 단문 교체
    "battle_carter_b.wav": "Don't let him go! Don't let him go!!",
    "battle_carter_c.wav": "Push!!",  # 반복형이 15/15 실패 -> 단문 교체
    "battle_carter_d.wav": "Take it!!",  # 반복형이 15/15 실패 -> 단문 교체
    "battle_carter_e.wav": "Go now!!",  # 반복형이 15/15 실패 -> 단문 교체
    "battle_carter_f.wav": "Oh!?",
    "battle_carter_g.wav": "Whoa!",
    "battle_carter_h.wav": "Come on!",
    "battle_carter_i.wav": "Yes!!",
    "battle_carter_j.wav": "Whoa-ho-ho!!",
    "battle_carter_k.wav": "Wooo-hoo!!",
    "battle_carter_l.wav": "Oho-ho-ho!!",
    "battle_carter_m.wav": "Ho-ho, unbelievable!!",
}
EN_BATTLE_ATLEE_TEXT = {
    "battle_atlee_a.wav": "Get him! Get him!!",
    "battle_atlee_b.wav": "Don't let him go! Don't let him go!!",
    "battle_atlee_c.wav": "Push!!",  # 반복형이 15/15 실패 -> 단문 교체(Carter와 달리 이 문구만 실패)
    "battle_atlee_d.wav": "Take it! Take it!!",
    "battle_atlee_e.wav": "Go now! Go now!!",
    "battle_atlee_f.wav": "Oh!?",
    "battle_atlee_g.wav": "Whoa!",
    "battle_atlee_h.wav": "Come on!",
    "battle_atlee_i.wav": "Yes!!",
    "battle_atlee_j.wav": "Whoa-ho-ho!!",
    "battle_atlee_k.wav": "Wooo-hoo!!",
    "battle_atlee_l.wav": "Oho-ho-ho!!",
    "battle_atlee_m.wav": "Ho-ho, unbelievable!!",
}


def _arrangable_without_adjacent_repeat(combo: tuple[str, ...]) -> bool:
    """combo(멀티셋)를 같은 항목이 연속되지 않게 한 줄로 배열할 수 있는지 판정하는
    순수 함수 - 가장 많이 나온 항목의 개수가 (전체 개수+1)//2를 넘으면 수학적으로
    반드시 어딘가에서 연속될 수밖에 없다(고전적인 "항목 재배열" 조건)."""
    if not combo:
        return True
    max_count = max(combo.count(k) for k in set(combo))
    return max_count <= (len(combo) + 1) // 2


def _best_fill_combo(pool_durations: dict[str, float], available: float, gap_sec: float,
                      max_items: int = 6) -> list[str]:
    """available(초) 안에 pool_durations(중복 허용)에서 고른 문구들을 gap_sec
    간격으로 채웠을 때, 빈틈(= available - (문구 길이 합 + gap_sec*(개수-1)))이
    가장 작아지는 조합을 완전탐색으로 찾는 순수 함수(테스트 가능) - 매 단계 가장
    긴 것부터 그리디로 고르면 "긴 것 하나를 먼저 써버려서 그 뒤로 아무것도 못
    들어가는" 경우가 실측 시뮬레이션(여백 3.1s)에서 빈틈 1.15s로 나타났는데, 짧은
    문구 2개(합 2.60s)를 쓰면 빈틈 0.35s로 더 작다 - 문구가 5개 내외뿐이라
    (조합 개수가 작아) 완전탐색이 충분히 싸다. 순서 없는 키 리스트(멀티셋)를
    반환하고, 실제 배치 순서는 호출부(_pack_reaction_chain)가 정한다.
    🛡️ [같은 문구 연속 반복 금지 - 빈틈보다 다양성 우선] 빈틈 최소화만 보면 같은
    문구 2~3개를 그대로 반복하는 조합이 뽑혀 "매크로 돌린 느낌"이 난다는 피드백을
    받았다 - _arrangable_without_adjacent_repeat로 애초에 "연속 없이 배열 불가능한"
    조합(예: 셋 다 동일 문구)은 아무리 빈틈이 작아도 후보에서 제외한다. 빈틈이 좀
    늘어나더라도 다양성이 있는 조합만 고른다."""
    keys = list(pool_durations.keys())
    best_combo: list[str] = []
    best_sum = 0.0
    for n in range(1, max_items + 1):
        if n * min(pool_durations.values()) + gap_sec * (n - 1) > available:
            break  # 문구가 가장 짧아도 n개는 더 이상 못 들어감 - 그 이상 n은 볼 필요 없음
        for combo in itertools.combinations_with_replacement(keys, n):
            if not _arrangable_without_adjacent_repeat(combo):
                continue
            total = sum(pool_durations[k] for k in combo)
            used = total + gap_sec * (n - 1)
            if used <= available and total > best_sum:
                best_sum = total
                best_combo = list(combo)
    return best_combo


def _shuffle_avoiding_adjacent_repeats(items: list[str], rng: random.Random) -> list[str]:
    """같은 문구가 바로 이어지지 않게 배열하는 순수 함수(테스트 가능) - 매번 "아직
    남은 것 중 가장 많이 남은 것(동률이면 무작위)"을 직전과 다르게 고르는 고전
    그리디(LeetCode "Reorganize String"과 동일한 알고리즘) - 배열 가능한 멀티셋이면
    반드시 성공한다는 게 증명돼 있다.
    🛡️ [버그 수정 - 셔플+국소 스왑 방식은 끝부분 중복을 못 고침] 이전 구현(무작위
    셔플 후 인접 중복을 "뒤쪽에서" 찾아 스왑)은 중복이 배열 맨 끝에 몰리면(예:
    [a,d,d]) 스왑할 "뒤쪽" 후보가 없어 못 고쳤다 - 실측 시뮬레이션(여백 3.1s)에서
    배열 가능한 조합인데도 20000회 중 6771회(34%)나 연속 반복이 새어나간 게
    확인됨. 이 그리디는 그런 경우가 없다."""
    counts: dict[str, int] = {}
    for it in items:
        counts[it] = counts.get(it, 0) + 1
    result: list[str] = []
    prev: str | None = None
    for _ in range(len(items)):
        candidates = [k for k, c in counts.items() if c > 0 and k != prev]
        if not candidates:
            candidates = [k for k, c in counts.items() if c > 0]
        max_count = max(counts[k] for k in candidates)
        top = [k for k in candidates if counts[k] == max_count]
        chosen = rng.choice(top)
        result.append(chosen)
        counts[chosen] -= 1
        prev = chosen
    return result


def _pack_reaction_chain(pool_durations: dict[str, float], start_time: float, end_limit: float,
                          gap_sec: float = BATTLE_LEADIN_CHAIN_GAP_SEC,
                          rng: random.Random | None = None) -> list[tuple[str, float]]:
    """pool_durations(파일명 -> 길이)에서 start_time부터 end_limit까지의 구간을
    빈틈이 최소가 되도록 채운 (파일명, 시작시각) 리스트를 반환하는 순수 함수(테스트
    가능, rng 주입하면 결정적) - "urgent 종료~EOEO 시작" 여백을 문구 하나만 넣고
    비워두지 않기 위해 _best_fill_combo로 최적 조합을 찾은 뒤, 같은 문구가 바로
    이어지는 건 가능하면 피하도록 순서를 섞어 gap_sec 간격으로 배치한다."""
    _rng = rng if rng is not None else random
    available = end_limit - start_time
    if available <= 0 or not pool_durations:
        return []
    combo = _best_fill_combo(pool_durations, available, gap_sec)
    if not combo:
        return []
    ordered = _shuffle_avoiding_adjacent_repeats(combo, _rng)
    result: list[tuple[str, float]] = []
    cursor = start_time
    for key in ordered:
        result.append((key, cursor))
        cursor += pool_durations[key] + gap_sec
    return result


def _concat_wav_chain(paths: list[str], gap_sec: float, out_path: str) -> None:
    """정적 풀 wav들(전부 동일 포맷 - 생성 스크립트 관례상 44.1kHz mono)을 gap_sec
    무음으로 이어 붙여 out_path에 하나의 파일로 합친다. ffmpeg filter_complex 없이
    stdlib wave만 사용(입력이 전부 PCM wav라 안전)."""
    with wave.open(paths[0], "rb") as w0:
        params = w0.getparams()
    chunks = []
    for i, p in enumerate(paths):
        with wave.open(p, "rb") as w:
            chunks.append(w.readframes(w.getnframes()))
        if i < len(paths) - 1:
            n_frames = int(round(gap_sec * params.framerate))
            chunks.append(b"\x00" * (n_frames * params.sampwidth * params.nchannels))
    with wave.open(out_path, "wb") as out:
        out.setparams(params)
        out.writeframes(b"".join(chunks))


def _spread_fillers_evenly(available: float, durs: list[float], gap: float, max_count: int) -> list[float]:
    """0부터 시작하는 available 구간 안에 durs(순서대로) 최대 max_count개를 "평균 길이+gap"
    기준으로 자연스럽게 들어갈 개수 N을 정한 뒤, 그 구간을 N등분해 각 조각 앞쪽에 필러
    하나씩 배치한 오프셋 리스트를 반환하는 순수 함수(테스트 가능). N이 0이면(구간이 필러
    하나 자리도 안 될 만큼 짧으면) 빈 리스트 - 억지로 겹치게 밀어넣지 않는다는 기존 원칙
    그대로."""
    if available <= 0 or not durs:
        return []
    avg_dur = sum(durs) / len(durs)
    unit = avg_dur + gap
    n = min(len(durs), max_count, int(available // unit)) if unit > 0 else 0
    if n <= 0:
        return []
    segment_width = available / n
    return [i * segment_width for i in range(n)]


def plan_lead_in_forward_eoeo(kill_t: float,
                               start_offset: float = PRE_BUILDUP_START_OFFSET_SEC,
                               end_gap: float = EOEO_GAP_SEC) -> float:
    """상황 멘트(pre_buildup)가 쓸 수 있는 시간(available)을 계산하는 순수 함수(테스트
    가능) - start_offset부터 kill_t - end_gap까지 전체가 사용 가능한 시간이다. 반환된
    available은 _estimate_pre_buildup_count -> _pick_pre_buildup_slots -> _spread_fixed_n
    순서로 이어지는 호출부가 사용한다.
    🛡️ [EOEO 제거 - EN 리드인과 동일 패턴으로 통일] 원래 "어어??"(EOEO)를 kill_t 직전에
    고정 배치하고 그 앞의 남는 시간만 상황 멘트에 줬었는데(eoeo_dur만큼 available이 줄어듦),
    다중 리액션이 리드인 전체를 끊김 없이 채우는 지금 구조에서는 EOEO가 "항상 한 번
    끼어드는 고정 멘트"로서 더 역할이 없어져 완전히 제거했다 - 영어 리드인(plan_leadin_
    fillers_en)이 애초에 EOEO 같은 고정 앵커 없이 "start_offset부터 kill_t - end_gap까지
    전체"를 available로 쓰던 것과 정확히 동일한 패턴으로 통일한다. 함수 이름(forward_eoeo)은
    호출부를 더 안 건드리려고 그대로 남겼다(과거엔 실제로 "EOEO 쪽으로 forward 배치"하는
    함수였던 이름의 흔적).
    available이 음수가 될 수 있는 경우(비정상적으로 짧은 클립/이른 킬)는 0.0으로 clamp -
    하위 호출부(_estimate_pre_buildup_count 등)가 이미 available<=0을 "자리 없음"으로
    안전하게 처리하므로 별도 None 분기가 필요 없어졌다(기존엔 EOEO 자체가 안 들어가는
    경우를 구분해야 해서 None을 반환했었음)."""
    return max(0.0, kill_t - end_gap - start_offset)


def plan_leadin_fillers_en(kill_t: float, durations: list[float],
                            start_offset: float = EN_LEADIN_START_OFFSET_SEC,
                            gap: float = EN_LEADIN_GAP_SEC,
                            end_gap: float = EN_LEADIN_END_GAP_SEC) -> list[float]:
    """영어 리드인 필러 N개(순서대로 durations)의 시작 시각 리스트(순수 함수, 테스트
    가능) - 한국어 pre_buildup이 이전에 쓰던 "평균 길이+gap 기준 개수 산정 + N등분 균등
    분산" 원칙을 그대로 쓴다(_spread_fillers_evenly) - 영어 쪽은 차분/긴박 풀 분리를
    적용하지 않아 이 함수는 바꾸지 않았다. 영어 쪽은 EOEO에 해당하는 고정 앵커
    요소가 없어서, start_offset부터 kill_t - end_gap까지 전체가 "사용 가능한 시간"이다.
    자리가 하나도 안 나오면 빈 리스트(억지로 겹치게 밀어넣지 않는다는 기존 원칙 그대로)."""
    available = kill_t - end_gap - start_offset
    offsets = _spread_fillers_evenly(available, durations, gap, EN_LEADIN_MAX_COUNT)
    return [start_offset + off for off in offsets]


def pick_leadin_overlay_start(pre_buildup_starts: list[float], pre_buildup_durs: list[float],
                               eoeo_start: float | None, eoeo_dur: float,
                               overlay_dur: float, kill_t: float,
                               overlap_ratio: float = 0.5,
                               end_gap: float = EOEO_GAP_SEC) -> float | None:
    """리드인 2보이스 겹침(LEADIN_OVERLAY_POOL)이 낄 자리를 고르는 순수 함수(테스트
    가능) - "어어??"(EOEO)가 있으면 그 슬롯의 중간 지점(overlap_ratio=0.5)에 겹치게
    시작한다(Main이 "어어?!" 하는 도중에 Hype/Sub가 짧게 반응하는 그림). EOEO가
    스킵된 경우(클립이 짧아 자리가 없던 경우)엔 마지막 상황 멘트 슬롯의 중간 지점으로
    폴백한다. 상황 멘트도 EOEO도 둘 다 없으면(리드인 자체가 통째로 스킵) None -
    억지로 자리를 만들지 않는다는 기존 리드인 설계 원칙 그대로.

    🛡️ [kill_t 침범 방지 - 실측으로 발견] overlay 후보 파일 길이가 제각각이라(0.48~0.64s),
    "중간 지점에서 시작"만으로는 더 긴 파일(예: 0.64s)이 kill_t를 최대 0.04s 넘겨버리는
    게 시뮬레이션으로 실측 확인됨(0단계 폭발과 살짝 겹침) - overlay_dur/kill_t를 받아서
    "그 지점에서 시작 시 kill_t - end_gap을 넘기면 안 넘기는 가장 늦은 시각"으로 clamp한다.
    EOEO 길이가 짧아 실제로는 이 clamp가 거의 항상 걸리는데, 그 결과 overlay가 "어어??"가
    끝나는 시점(kill_t - end_gap) 근처에서 끝나도록 자연스럽게 수렴한다 - 오히려 "어어?!"가
    마무리되는 순간에 맞춰 반응하는 그림이 되어 의도와도 잘 맞는다."""
    if eoeo_start is not None:
        target = eoeo_start + eoeo_dur * overlap_ratio
    elif pre_buildup_starts:
        target = pre_buildup_starts[-1] + pre_buildup_durs[-1] * overlap_ratio
    else:
        return None
    latest_start = kill_t - end_gap - overlay_dur
    if latest_start < 0:
        return None  # overlay 자체가 물리적으로 들어갈 자리가 없음(극단적으로 이른 킬)
    return min(target, latest_start)


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
# 🛡️ [메인바 골드 색 - 킬 스코어와 구분용] 순백색(킬)보다 살짝 톤 다운된 회색 - 배경
# 그라데이션 위에서도 충분히 읽히면서 킬의 순백색만큼 강조되지는 않게 한다.
TOP_GOLD_TEXT_COLOR = "#C9C9C9"
# 🛡️ [타이머 뱃지 - 서브바 반투명화 후 알파 보강] 0.25는 서브바 자체가 불투명(255)일
# 때는 충분했는데, 서브바를 반투명(alpha 190)으로 낮춘 뒤에는 밝은 게임 배경(잔디 등)이
# 이중으로 비쳐서 흰 타이머 텍스트 가독성이 떨어지는 게 실측으로 확인됨 - 0.5로 올려서
# 배경이 밝아도 텍스트가 항상 또렷하게 보이도록 보강.
TIMER_BADGE_COLOR = "black@0.5"

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
# 🛡️ [룬/스펠 아이콘 복원 - 실제 LCK 2025 방송 화면에 있음이 재확인됨] 과거엔 "방송에
# 없다"는 판단으로 패널에서 완전히 제거했는데, 최신 방송 화면을 다시 보니 실제로 있어서
# 복원한다. URL 패턴은 과거 조사 그대로(이번에 실제 네트워크 호출로 재검증 완료):
#   - 소환사 스펠: summoner.json에서 숫자 key(예: "4")->파일명("SummonerFlash.png") 역매핑
#     후 cdn/{version}/img/spell/{파일명} - 다운로드 성공 확인(RGB, 64x64, 불투명 정사각형).
#   - 룬: runesReforged.json에서 숫자 id(예: 8112)->아이콘 경로 역매핑 후
#     **cdn/img/{경로}** (다른 아이콘들과 달리 버전 번호가 URL에 안 들어감 - 룬만의
#     특이사항, 실제 호출로 확인됨) - Match-v5 참가자의 perks.styles[0].selections[0].perk
#     가 키스톤 룬 id. 다운로드 파일 자체가 RGBA 원형 투명 PNG라(실측: 모서리 alpha=0,
#     중앙 alpha=255) 포트레이트와 달리 별도 원형 마스킹이 필요 없다.
DDRAGON_SUMMONER_SPELL_MAP_URL_TEMPLATE = "https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/summoner.json"
DDRAGON_SPELL_ICON_URL_TEMPLATE = "https://ddragon.leagueoflegends.com/cdn/{version}/img/spell/{filename}"
DDRAGON_RUNES_REFORGED_URL_TEMPLATE = "https://ddragon.leagueoflegends.com/cdn/{version}/data/en_US/runesReforged.json"
DDRAGON_RUNE_ICON_URL_TEMPLATE = "https://ddragon.leagueoflegends.com/cdn/img/{icon_path}"
CHAMPION_ICON_CACHE_DIR = os.path.join(OVERLAY_DIR, "champion_icons_cache")
ITEM_ICON_CACHE_DIR = os.path.join(OVERLAY_DIR, "item_icons_cache")
SPELL_ICON_CACHE_DIR = os.path.join(OVERLAY_DIR, "spell_icons_cache")
RUNE_ICON_CACHE_DIR = os.path.join(OVERLAY_DIR, "rune_icons_cache")
DDRAGON_HTTP_TIMEOUT_SECONDS = 5.0

# 🛡️ [원형 포트레이트 시도 -> 사각형으로 최종 복귀] 한때 alphamerge+그레이스케일
# 마스크로 10개 전체를 원형 마스킹했었는데, Worlds 참고 사진을 다시 확인한 결과
# 실제 방송은 사각형이었고 원형 전환이 "레벨 숫자가 모서리 밖으로 튀어나옴" +
# "같은 공간에서 얼굴이 작아 보임" 문제의 원인이었다 - 사각형으로 되돌리면서 마스크
# 입력 자체를 더 이상 만들지 않는다. 마스크 생성 코드/에셋(portrait_circle_mask.png)은
# git 이력에 남아있어 필요하면 복구 가능.

# 🛡️ [골드 갭 "꺾쇠(chevron)" 에셋 - 상단바/로스터 그리드 통일] 상단 메인바와
# 로스터 그리드가 각자 drawtext "◀"/"▶" 글리프로 따로 그려지고 있었는데(실측 결과
# 상단바도 단순 글리프였음), 두 곳 다 이 PNG 하나로 통일한다. 처음엔 "바+꽉 찬
# 삼각형"(플래그 모양)으로 만들었는데, 실제 LoL 클라이언트 참고 스크린샷을 보니
# 꽉 찬 삼각형이 아니라 "두 개의 가는 선이 한 점에서 만나는 꺾쇠"(">"/"‹" 모양,
# 속이 빈 얇은 윤곽선)였다 - PIL ImageDraw.line(폭=스트로크, joint="curve") +
# 양 끝/꼭짓점에 작은 원(캡)으로 다시 그렸다. RGBA(도형=흰색 불투명, 배경=투명)로
# 구워서(4x 슈퍼샘플+LANCZOS 다운스케일 - portrait_circle_mask와 동일한
# 안티앨리어싱 기법) ffmpeg lutrgb로 팀 컬러를 직접 입힌다(알파는 그대로 유지) -
# alphamerge처럼 별도 "색상 입력"이 필요 없어서 공유 입력 1개만으로 끝난다. 기본
# 도형은 "오른쪽을 가리키는" 방향(">") - 왼쪽을 가리켜야 할 때는 ffmpeg hflip
# 필터로 뒤집는다(에셋을 2벌 만들 필요 없음 - 모양이 바뀌어도 lutrgb/hflip 로직
# 자체는 손댈 필요 없이 그대로 재사용 가능함을 확인함).
GOLD_GAP_BAR_MASK_PATH = os.path.join(OVERLAY_DIR, "gold_gap_bar_mask.png")


def _hex_to_rgb_ints(hex_color: str) -> tuple[int, int, int]:
    """TEAM_BLUE_COLOR/TEAM_RED_COLOR 같은 "#RRGGBB" 문자열을 ffmpeg lutrgb가
    받는 (r, g, b) 정수 3개로 변환한다."""
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


# 🛡️ [에셋 자체의 가로:세로 비율 - 렌더 시점 크기 계산에 재사용] PIL로 24x70(약
# 0.343:1 - 꺾쇠라 가로보다 세로가 훨씬 긴 비율)로 구웠다. 이전 "바+삼각형"
# 버전(60x22, 가로가 긴 비율)과 정반대 - 상단바/로스터 각자 다른 목표 크기에 맞춰
# scale할 때 이 비율로 나머지 변을 같이 계산해서 도형이 찌그러지지 않게 한다.
GOLD_GAP_BAR_ASSET_ASPECT = 24 / 70

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
# 🛡️ [메인바 중앙 장식 트로피 - 실루엣 교체] 기존 골드 트로피(trophy_icon.png, PIL
# 자체 제작)를 검은 배경 위 흰색 실루엣 디자인(trophy_icon_new.png)으로 교체 -
# make_trophy_transparent.py로 배경(순수 검정, RGB 18/18/18 균일)을 알파로
# 변환해 투명화한 결과물을 실제로 사용한다(trophy_icon_new_transparent.png).
# 원본 골드 버전(trophy_icon.png)은 삭제하지 않고 보관만 하며 더 이상 참조하지 않는다.
TROPHY_ICON_PATH = os.path.join(OVERLAY_DIR, "trophy_icon_new_transparent.png")
# 🛡️ [골드 동전 아이콘 - PIL 자체 제작] "속이 빈 동전 윤곽선" - 원 2개(바깥 두꺼운
# 테두리+안쪽 얇은 테두리, 동전 특유의 이중 엠보싱 디테일)를 RGBA(도형=TOP_GOLD_TEXT_COLOR
# 고정색, 배경=투명)로 그렸다(4x 슈퍼샘플+LANCZOS 다운스케일, gold_gap_bar_mask.png와
# 동일 기법). 팀 컬러를 입힐 필요가 없어(골드는 항상 같은 회색) 런타임 lutrgb 없이
# 그대로 scale+overlay만 하면 된다.
GOLD_COIN_ICON_PATH = os.path.join(OVERLAY_DIR, "gold_coin_icon.png")

# 상단 2단 바 - 메인바(전체 폭)+서브바(중앙 940px만) 치수, 실측값 그대로.
# 🛡️ [메인바+서브바 높이 축소 - LCK 실측 비교, 방안A 채택] 이전엔 50->100/1080으로 2배
# 확장했었는데, 실제 LCK 방송 캡처와 직접 대조한 결과 합계(100+31=131/1080=12.13%)가
# 목표치(화면 세로 대비 5~6%)의 2배 이상으로 과도하게 두꺼웠다. 6%(50+15/1080)까지
# 줄여봤으나 실제 렌더에서 텍스트가 눌리고 답답해 보인다는 피드백으로, 목표를
# 7.5~8%(LCK보다 살짝 크게, 이전보다는 확실히 작게)로 재조정 - 메인바:서브바 비율(약
# 3.2:1)은 유지한 채 64/1080 + 20/1080 = 84/1080(7.78%)로 최종 확정(방안A). 폰트는
# "덜 줄이는" 별도 보정 없이 바 높이와 완전 비례로 축소 - 바 자체가 6% 안보다 여유
# 있어서 완전 비례로도 충분히 읽힘(실측 확인). overlay_frame_v2.png도 이 비율에 맞춰
# 리크롭했다(기존 y=0~130 영역을 세로로만 리샘플, 가로 경계는 안 건드림 - 코드
# 비율값과 이미지가 반드시 같이 바뀌어야 그림자 띠 버그가 재발하지 않는다).
TOP_MAIN_BAR_HEIGHT_RATIO = 64 / 1080
TOP_SUB_BAR_HEIGHT_RATIO = 20 / 1080
# 🛡️ [서브바 폭 재축소 - 39.6%도 여전히 넓다는 재피드백] 처음엔 안전 여유를 넉넉히 두고
# 39.6%(580~1340)로 줄였는데, LCK 원본 대비 여전히 넓다는 피드백으로 안전 여유를 최소로
# 줄여 재계산했다 - dragon_offset(드래곤 시작 전 여백)이 예전엔 "서브바 half-width의
# 30%"라는 임의 비율이라 실제 필요치(타이머 뱃지 폭)보다 훨씬 컸던 게 원인 중 하나였음을
# 발견 - 아래 dragon_offset 계산 자체를 타이머 뱃지 폭 기준으로 바꿨다. 마지막 오브젝트의
# 숫자 폭 여유도 "최대 4자리 가정" 대신 실제 표시 값(보통 1~2자리) 기준으로 줄였다.
# 재계산 결과 필요 half-width ≈261px -> 27.6%(695~1225/1920). overlay_frame_v2.png도
# 이 범위에 맞춰 다시 리크롭 + 좌우 각 18px alpha 190->0 선형 페더 처리(하드 엣지 대신
# 부드럽게 사라지는 형태) - 코드 좌표와 이미지가 어긋나면 그림자 띠 버그와 같은 종류의
# 문제가 재발한다.
TOP_SUB_BAR_X_RATIO = (695 / 1920, 1225 / 1920)
# 🛡️ [메인바 폭 65%로 축소 - 서브바와 완전히 독립된 별개 변수] 메인바를 화면 전체 폭이
# 아니라 중앙 65%짜리 좁은 바로 좁힌다. 서브바는 이미 자기만의 폭(TOP_SUB_BAR_X_RATIO,
# 27.6%)을 쓰고 있고 이번 변경과 전혀 무관 - bar_x0/x1/bar_mid_x/bar_half_w는 렌더
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

# 🛡️ [행간 구분선 더 은은하게] 0.2는 눈에 잘 띄어서 0.05(매우 은은한 수준)로 낮췄다 -
# 요청 스펙 그대로.
ROSTER_DIVIDER_COLOR = "white@0.05"
# 🛡️ [홀/짝수 행 톤 차이] 2/4번째 행에만 깔아 행 구분을 돕는 아주 옅은 배경.
ROW_STRIPE_COLOR = "white@0.03"
ROSTER_SHADOW_COLOR = "black@0.7"
# 🛡️ [아이템 슬롯 틀 - 빈 칸도 항상 표시] 기존 ROSTER_DIVIDER_COLOR(white@0.2, t=2)는
# 배경 그라데이션 위에서 너무 옅어서 빈 슬롯인지 그냥 배경인지 구분이 잘 안 됐다 - 어두운
# 보라 계열로 바꾸고 두께도 1px로 줄인다(요청 스펙 그대로).
ITEM_SLOT_BORDER_COLOR = "#2D274D"
# 🛡️ [아이템 아이콘 개별 식별 - 간격+외곽선] 6칸이 서로 붙어 그려져 개별 아이콘 구분이 어렵다는
# 피드백 반영. ITEM_SLOT_BORDER_COLOR(위)는 슬롯 자체의 틀이라 아이콘이 그 위에 덮어 그려지면
# (item_idx가 있는 칸) 완전히 가려진다 - 실제로 보이는 건 빈 슬롯뿐이라, 아이콘이 있는 칸에는
# 지금까지 어떤 외곽선도 없었다(코드 검토로 확인, 중복 아님). 이 외곽선은 아이콘을 그린 "뒤에"
# 별도로 얹어서 채워진 칸에서도 항상 보이게 한다.
# 🛡️ [진한 검정 vs 옅은 밝은 톤 비교 - 실측 렌더로 결정] black@0.5는 잘 안 보인다는 피드백에
# black@0.8(더 진한 검정)과 white@0.3(옅은 밝은 톤) 둘 다 실제 렌더로 비교했다 - 아이템
# 아이콘 자체가 어두운 모서리를 가진 경우가 많아서(LoL 아이콘 특성상 흔함), 검정 계열은
# 0.8까지 올려도 아이콘의 원래 어두운 부분과 잘 구분되지 않았다. 반대로 밝은 톤은 아이콘이
# 밝든 어둡든 거의 항상 대비가 생겨서 훨씬 뚜렷하게 개별 식별됨(실측 스크린샷으로 확인) -
# 과하게 튀지도 않아서(옅은 은색 라인 정도) white@0.3을 최종 채택.
ITEM_ICON_OUTLINE_COLOR = "white@0.3"
# 🛡️ [금색 사각형 테두리 -> 완전 제거] 보라 vs 금색 비교, 이후 금색 -> 흰색 헤어라인
# 재조정을 거쳤지만, 원형 포트레이트 전환과 함께 사각형 drawbox 테두리 자체를 없앴다
# (원형 마스크 가장자리가 경계 역할을 대신함) - CHAMPION_FRAME_COLOR/
# CHAMPION_FRAME_BORDER_W_PX 상수는 더 이상 쓰이지 않아 삭제, 필요하면 git 이력에서
# 복구 가능.
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
# 🛡️ [레벨 숫자 배지 - 사각형 포트레이트 좌하단 모서리 안쪽] 원형 포트레이트 라운드
# 때 썼던 "포트레이트보다 큰 볼드+그림자 텍스트"(사각형 모서리 밖으로 튀어나옴)를
# 폐기하고, 사각형으로 복귀하며 배지 방식(배경 박스+숫자)으로 되돌아간다 - 포트레이트
# 변 길이의 42%로 제한해서 좌하단 모서리 안쪽에 완전히 들어가게 한다(모서리 좌표를
# portrait_x/portrait_y+portrait_size 기준으로 직접 맞춰서 벗어날 수 없는 구조).
LEVEL_BADGE_SIZE_RATIO = 0.42
# 🛡️ [반투명 배경 박스 - 가독성 1차 수단] 그림자만으로는 밝은 포트레이트 위에서
# 대비가 부족했던 전적이 있어(레벨 배지 1차 작업 때 실측 확인됨) 어두운 반투명 박스를
# 다시 깐다 - 그림자는 보조 수단이라 과하지 않게(아래 drawtext의 shadowcolor=black@0.6,
# 이전 라운드의 black@0.9보다 옅음).
LEVEL_BADGE_BG_COLOR = "black@0.6"
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


def _commentary_avoids_seumnida(text: str) -> bool:
    """GPT가 생성한 문장에 습니다체가 섞여 있지 않은지 확인 - SYSTEM_PROMPT가 "습니다체
    절대 금지, 요체만" 이라고 명시하지만, 온도 0.8 자유생성이 이 지시를 항상 지키는 건
    아니라는 게 실제 배포에서 확인됨(예: "완전히 찢어버렸습니다"). _commentary_names_killer와
    동일한 원칙 - 프롬프트 지시만 믿지 않고 코드로 사후 검증한다. 영어는 존댓말 어미 개념
    자체가 없어 호출부에서 한국어 렌더에만 이 검증을 적용한다."""
    return "습니다" not in text


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
            # 🛡️ [EN 비영문 닉네임 대체용] teamPosition도 이미 응답에 있는 필드 - 추가 API
            # 호출 없이 _en_display_name의 역할 명칭(예: "the jungler") 계산에 쓴다.
            "position": p.get("teamPosition") or None,
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


def _compute_scoreboard_at_time(timeline: dict, participants: list[dict], kill_game_ms: float) -> dict:
    """킬 시점(kill_game_ms) 기준으로 팀별 타워/드래곤/전령/바론/공허유충/챔피언킬/골드를
    timeline에서 다시 계산한다(순수 함수, _compute_laning_gold_gaps/_extract_dragon_sequence와
    동일한 스타일) - 예전엔 매치 상세(chosen["info"]["teams"]/participants)의 "게임 최종
    종료 시점" 누적치를 그대로 썼는데, 화면에 찍히는 시간(킬 시점)과 기준 시점이 서로 달라
    사고가 났다(예: 킬이 4분에 났는데 드래곤 "2마리"가 뜸 - 드래곤은 보통 5분 이후 스폰이라
    시점 불일치가 바로 드러남). BUILDING_KILL/ELITE_MONSTER_KILL/CHAMPION_KILL 이벤트를
    timestamp<=kill_game_ms로만 필터링해 팀별로 다시 센다.

    🛡️ [BUILDING_KILL.teamId 반전 - 실측 2매치 교차검증] 이 이벤트의 teamId는 "파괴한 팀"이
    아니라 "타워를 잃은 팀"이다 - 서로 다른 두 매치(KR_8393538099, KR_8393410432)에서
    timeline 원본 teamId별 집계와 매치 상세의 최종 타워킬 수를 대조했더니 둘 다 정확히
    뒤집혀 나왔다(예: KR_8393538099는 timeline teamId=100 집계 5회인데 매치상세 team100
    최종 타워킬은 7회, team200이 5회). 그래서 100의 이벤트는 200의 타워파괴 수에 더한다.
    ELITE_MONSTER_KILL(killerTeamId)/CHAMPION_KILL(killerId->참가자 팀)은 반전이 필요
    없다 - 같은 두 매치에서 매치 상세 최종값(드래곤/바론/전령/공허유충/챔피언킬)과 정확히
    일치함을 확인했다."""
    participant_team = {p["participantId"]: p["teamId"] for p in participants}
    towers = {100: 0, 200: 0}
    dragons = {100: 0, 200: 0}
    barons = {100: 0, 200: 0}
    heralds = {100: 0, 200: 0}
    hordes = {100: 0, 200: 0}
    champ_kills = {100: 0, 200: 0}

    for frame in timeline["info"]["frames"]:
        for ev in frame.get("events", []):
            ts = ev.get("timestamp")
            if ts is None or ts > kill_game_ms:
                continue
            etype = ev.get("type")
            if etype == "BUILDING_KILL" and ev.get("buildingType") == "TOWER_BUILDING":
                lost_team = ev.get("teamId")
                if lost_team == 100:
                    towers[200] += 1
                elif lost_team == 200:
                    towers[100] += 1
            elif etype == "ELITE_MONSTER_KILL":
                team = ev.get("killerTeamId")
                if team not in (100, 200):
                    continue
                monster = ev.get("monsterType")
                if monster == "DRAGON":
                    dragons[team] += 1
                elif monster == "BARON_NASHOR":
                    barons[team] += 1
                elif monster == "RIFTHERALD":
                    heralds[team] += 1
                elif monster == "HORDE":
                    hordes[team] += 1
            elif etype == "CHAMPION_KILL":
                team = participant_team.get(ev.get("killerId"))
                if team in (100, 200):
                    champ_kills[team] += 1

    # 🛡️ [골드 - 가장 가까운 프레임 스냅샷] _compute_laning_gold_gaps와 동일한 원리(고정
    # 타깃 대신 kill_game_ms에 가장 가까운 프레임을 고른다) - participantFrames는 약 60초
    # 간격이라 최대 ±30초 오차가 있을 수 있지만, "게임 최종 골드"를 쓰던 것보다는 훨씬 더
    # 킬 시점에 가깝다.
    gold = {100: 0, 200: 0}
    closest_frame = min(timeline["info"]["frames"], key=lambda f: abs(f["timestamp"] - kill_game_ms))
    for pid_str, pframe in closest_frame["participantFrames"].items():
        team = participant_team.get(int(pid_str))
        if team in (100, 200):
            gold[team] += pframe.get("totalGold", 0)

    return {
        "team100_towers": towers[100], "team200_towers": towers[200],
        "team100_kills": champ_kills[100], "team200_kills": champ_kills[200],
        "team100_dragons": dragons[100], "team200_dragons": dragons[200],
        "team100_riftheralds": heralds[100], "team200_riftheralds": heralds[200],
        "team100_barons": barons[100], "team200_barons": barons[200],
        "team100_hordes": hordes[100], "team200_hordes": hordes[200],
        "team100_gold": gold[100], "team200_gold": gold[200],
    }


def _compute_participant_frame_stats_at_time(timeline: dict, kill_game_ms: float) -> dict[int, dict]:
    """킬 시점(kill_game_ms) 기준 각 참가자의 레벨/CS를 timeline에서 계산하는 순수 함수
    (테스트 가능) - _compute_scoreboard_at_time의 골드 계산과 정확히 같은 "가장 가까운
    프레임" 패턴을 재사용한다(participantFrames는 약 60초 간격이라 최대 ±30초 오차가
    있을 수 있지만, "매치 최종값"을 쓰는 것보다 킬 시점에 훨씬 가깝다). 레벨/CS 둘 다
    같은 closest_frame 하나로 끝나므로(조사 라운드에서 확인된 그대로) 함수를 합쳐서
    중복 계산을 없앴다 - 원래 이름(_compute_participant_levels_at_time)은 CS 추가 전
    유일한 호출부(roster 생성 루프)에서 그대로 갱신."""
    closest_frame = min(timeline["info"]["frames"], key=lambda f: abs(f["timestamp"] - kill_game_ms))
    return {
        int(pid_str): {
            "level": pframe.get("level", 1),
            "cs": pframe.get("minionsKilled", 0) + pframe.get("jungleMinionsKilled", 0),
        }
        for pid_str, pframe in closest_frame["participantFrames"].items()
    }


def _compute_participant_kda_at_time(timeline: dict, kill_game_ms: float) -> dict[int, tuple[int, int, int]]:
    """킬 시점(kill_game_ms) 기준 각 참가자의 K/D/A를 timeline의 CHAMPION_KILL 이벤트로
    계산하는 순수 함수(테스트 가능) - _compute_scoreboard_at_time과 똑같이 timestamp<=
    kill_game_ms로 이벤트를 필터링한 뒤, killerId/victimId/assistingParticipantIds를
    각각 kills/deaths/assists에 누적한다. 실제 매치(KR_8393410432)로 교차검증 완료 -
    이 함수로 계산한 팀별 킬 합계가 _compute_scoreboard_at_time의 team_kills와 정확히
    일치함을 확인했다(조사 라운드 investigate_kill_time_roster.py 참고)."""
    kda: dict[int, list[int]] = {p["participantId"]: [0, 0, 0] for p in timeline["info"]["participants"]} \
        if "participants" in timeline["info"] else {}
    if not kda:
        # 🛡️ timeline 응답엔 participants가 없을 수 있다(매치 상세 쪽에만 있음) - 그 경우
        # 이벤트에 실제로 등장하는 participantId를 집계 과정에서 바로 등록한다.
        kda = {}
    for frame in timeline["info"]["frames"]:
        for ev in frame.get("events", []):
            ts = ev.get("timestamp")
            if ts is None or ts > kill_game_ms:
                continue
            if ev.get("type") != "CHAMPION_KILL":
                continue
            killer_id = ev.get("killerId")
            victim_id = ev.get("victimId")
            if killer_id:
                kda.setdefault(killer_id, [0, 0, 0])[0] += 1
            if victim_id:
                kda.setdefault(victim_id, [0, 0, 0])[1] += 1
            for aid in ev.get("assistingParticipantIds", []):
                kda.setdefault(aid, [0, 0, 0])[2] += 1
    return {pid: tuple(v) for pid, v in kda.items()}


# 🛡️ [장신구(트린켓) 식별 - 실제 매치 item6 값으로 교차검증] KR_8393410432의 10명 전원
# 최종 item6(트린켓 전용 슬롯, item0~5와 완전히 별개)을 직접 열어 확인한 값 - 와딩 토템
# 계열(3340)/시야석(3364)/원시 시야(3363) 3개뿐이었다. 이벤트 스트림(ITEM_PURCHASED 등)에는
# 트린켓 교체도 똑같이 섞여 들어오므로, 6칸 시뮬레이션에서 명시적으로 걸러내야 한다 - 다른
# 아이템 메타데이터(Data Dragon item.json)를 새로 받아올 필요 없이 이 고정 세트로 충분하다
# (라이엇이 새 트린켓을 추가하면 갱신 필요 - 팀명 매핑 등 다른 하드코딩 상수와 같은 유지보수
# 성격).
TRINKET_ITEM_IDS = {3340, 3363, 3364}


def _compute_participant_items_at_time(timeline: dict, kill_game_ms: float) -> dict[int, list[int]]:
    """킬 시점(kill_game_ms) 기준 각 참가자의 6칸 아이템 슬롯을 timeline의 아이템 이벤트
    (ITEM_PURCHASED/ITEM_SOLD/ITEM_UNDO/ITEM_DESTROYED)를 시간순으로 재생해 시뮬레이션하는
    순수 함수(테스트 가능) - 실제 클라이언트의 "첫 빈 칸에 배치" 동작을 그대로 흉내낸다.
    장신구(TRINKET_ITEM_IDS)는 이벤트에 섞여 들어오지만 item0~5(트린켓은 item6 별도 슬롯)와
    의미를 맞추기 위해 제외한다. 소모품은 걸러내지 않고 그대로 슬롯에 표시한다(사용자 지시 -
    "소모품도 실제로 그 순간 들고 있었다"는 사실 자체는 왜곡이 아니라고 판단).

    🛡️ [안전장치 - "확신이 안 서는 슬롯만" 비워두는 설계] 이 함수는 "성공이 확실한 조작만
    수행"한다 - 제거할 아이템을 현재 슬롯 어디서도 못 찾거나(상태가 이미 어긋났다는 신호),
    배치할 빈 칸이 하나도 없으면(6개 초과 보유는 정상 플레이에서 발생하지 않음) 그 개별
    이벤트만 조용히 건너뛴다. 전체 인벤토리를 리셋하거나 틀린 칸에 억지로 끼워넣지 않으므로,
    잘못 건드려진 슬롯은 항상 "확신이 선 마지막 상태"(비어있음 포함) 그대로 남는다 - 틀린
    값을 보여주는 것보다 안전하다는 판단(조사 라운드 결론).

    🛡️ [ITEM_UNDO 재료 복원 - 실제 버그 발견 후 수정, Data Dragon 폴백은 실측으로 기각]
    beforeId(취소 직전 보유하던 아이템)를 제거하고 afterId(취소 후 되돌아갈 아이템)를
    복원하는 것까지는 맞지만, afterId=0이면서 beforeId가 "조합 아이템"인 경우(완성템
    구매를 취소 = 재료 환불) 예전 코드는 그냥 아무 것도 복원하지 않았다 - 실제 매치
    (futuresavior, Zhonya's Hourglass 조합 직후 UNDO)로 재료 2개(Needlessly Large Rod/
    Seeker's Armguard)가 증발하는 버그를 확인했다(investigate_item_mismatch_cause.py).
    beforeId 구매 당시(_last_purchase_ts로 추적) 같은 timestamp에 같이 파괴된 아이템들
    (_destroyed_batch)을 복원 대상으로 쓴다 - "이 조합에 실제로 들어간 재료"라는 확실한
    신호다. 처음엔 이 신호가 없을 때 Data Dragon item.json의 "from"(일반 조합 레시피)으로
    대체하는 폴백도 넣었지만, 실제 캐시된 매치 20개(~200명) 전체로 정확도를 측정해보니
    오히려 악화됐다(82.0%->79.2%) - UNDO가 "조합 취소"가 아니라 그냥 "직접 구매 취소"인
    경우(같은 timestamp에 파괴된 게 없음 = 재료를 실제로 안 썼다는 뜻)에도 from 목록을
    억지로 끼워넣어 엉뚱한 재료가 생겨버리는 사례가 더 많았다. 같은 timestamp 파괴 신호만
    쓰면(폴백 없음) 82.4%로 오히려 개선됐다 - 그래서 Data Dragon 폴백은 뺐다. 그 신호조차
    없으면(직접구매였거나 데이터 누락) 예전처럼 아무 것도 복원하지 않는다(안전장치 유지,
    틀린 값보다 빈 칸)."""
    slots: dict[int, list[int]] = {}
    last_purchase_ts: dict[tuple[int, int], int] = {}
    destroyed_batch: dict[tuple[int, int], list[int]] = {}

    def _place(pid: int, item_id: int) -> None:
        if item_id in TRINKET_ITEM_IDS:
            return
        row = slots.setdefault(pid, [0] * 6)
        for i, v in enumerate(row):
            if v == 0:
                row[i] = item_id
                return
        # 6칸이 전부 찬 상태에서 또 배치하라는 신호 - 정상 플레이에선 발생하지 않는다.
        # 어느 칸을 덮어쓸지 확신할 수 없으니 이 구매 이벤트는 그냥 버린다(안전장치).

    def _remove(pid: int, item_id: int) -> None:
        if item_id in TRINKET_ITEM_IDS:
            return
        row = slots.setdefault(pid, [0] * 6)
        for i, v in enumerate(row):
            if v == item_id:
                row[i] = 0
                return
        # 현재 슬롯 어디에도 없는 아이템을 제거하라는 신호 - 상태가 이미 어긋났다는 뜻이니
        # 아무 것도 건드리지 않는다(안전장치, "확신이 안 서는 슬롯만 비워둔다"의 핵심).

    for frame in timeline["info"]["frames"]:
        for ev in frame.get("events", []):
            ts = ev.get("timestamp")
            if ts is None or ts > kill_game_ms:
                continue
            etype = ev.get("type")
            pid = ev.get("participantId")
            if not pid:
                continue
            if etype == "ITEM_PURCHASED":
                item_id = ev.get("itemId", 0)
                _place(pid, item_id)
                last_purchase_ts[(pid, item_id)] = ts
            elif etype in ("ITEM_SOLD", "ITEM_DESTROYED"):
                item_id = ev.get("itemId", 0)
                _remove(pid, item_id)
                if etype == "ITEM_DESTROYED":
                    destroyed_batch.setdefault((pid, ts), []).append(item_id)
            elif etype == "ITEM_UNDO":
                before_id = ev.get("beforeId", 0)
                after_id = ev.get("afterId", 0)
                if before_id:
                    _remove(pid, before_id)
                    if not after_id:
                        # 🛡️ [조합 구매 취소 - 재료 복원] before_id가 실제로 구매된 시점에
                        # 같이 파괴된 아이템들만 복원한다(그 조합에 진짜로 들어간 재료라는
                        # 확실한 신호). 그 신호가 없으면(같은 timestamp에 파괴된 게 없음)
                        # 직접구매를 취소한 것으로 보고 아무 것도 복원하지 않는다 - Data
                        # Dragon from으로 무조건 대체하면 오히려 정확도가 떨어짐을 실측으로
                        # 확인했다(위 docstring 참고).
                        purchase_ts = last_purchase_ts.get((pid, before_id))
                        materials = destroyed_batch.get((pid, purchase_ts), []) if purchase_ts is not None else []
                        for material_id in materials:
                            _place(pid, material_id)
                if after_id:
                    _place(pid, after_id)

    return slots


def _pick_match_for_clip(matches_detail: list[dict], clip_creation: datetime.datetime,
                          max_staleness_sec: float = MATCH_GAME_TIME_MAX_STALENESS_SEC) -> list[dict]:
    """1차 판별 - clip_creation이 실제 값(None 아님)일 때만 호출부가 이 함수를 쓴다(None이면
    날짜 기반 판별 자체가 불가능하므로 호출부가 아예 2차 경로로 보낸다).
    🛡️ [반환형 변경 - dict|None -> list[dict]] 예전엔 창(시작-2분~종료+2분)에 맞는 첫 매치
    하나만 반환했는데, 2차(_pick_match_by_game_time_range)와 동일하게 "창에 맞는 후보
    전부"를 반환하도록 바꿔서, 호출부가 2차와 똑같이 킬 존재 교차검증을 할 수 있게
    한다(실제로는 같은 계정이 동시에 두 매치를 뛸 수 없어 현실적으로 0개 아니면 1개뿐이지만,
    구조를 2차와 통일해둔다).
    🛡️ [staleness 체크 추가] 창에 들어가도 매치 종료 시각이 clip_creation보다
    max_staleness_sec 이상 먼 매치는 제외한다. ±2분 창 자체가 이미 이보다 훨씬 좁아서
    실제로 이 필터에 걸릴 일은 거의 없지만(창을 통과했다는 건 이미 거의 동시간대라는 뜻),
    "매치 종료 6시간 이내"라는 기존 안내 문구(highlight_err_match_not_found)의 약속을
    1차 판별도 명시적으로 지키도록 방어적 일관성을 맞춘다."""
    matched = []
    for md in matches_detail:
        info = md["info"]
        start = datetime.datetime.fromtimestamp(info["gameStartTimestamp"] / 1000, tz=datetime.timezone.utc)
        end = datetime.datetime.fromtimestamp(
            (info["gameStartTimestamp"] + info["gameDuration"] * 1000) / 1000, tz=datetime.timezone.utc
        )
        if not (start - datetime.timedelta(minutes=2) <= clip_creation <= end + datetime.timedelta(minutes=2)):
            continue
        if (clip_creation - end).total_seconds() > max_staleness_sec:
            continue
        matched.append(md)
    matched.sort(key=lambda md: abs((clip_creation - datetime.datetime.fromtimestamp(
        (md["info"]["gameStartTimestamp"] + md["info"]["gameDuration"] * 1000) / 1000,
        tz=datetime.timezone.utc)).total_seconds()))
    return matched


def _has_bot_participant(match_detail: dict) -> bool:
    """🛡️ AI 상대 대전 후보를 걸러내려고 gameType/gameMode/queueId를 먼저 확인해봤는데,
    실제로 발견된 문제 사례(KR_8364744586)는 gameType=MATCHED_GAME, gameMode=SWIFTPLAY로
    "봇으로 채워진 정식 스위프트플레이"라 그 기준으로는 안 걸러졌다(Riot이 이걸 진짜
    매치메이드로 분류함). 대신 참가자 데이터에서 확인되는 훨씬 확실한 공식 신호를 쓴다 -
    AI로 채워진 슬롯은 participant.puuid가 리터럴 문자열 "BOT"(길이 3)이다(실제 이 매치로
    실측 확인). 사람 플레이어의 puuid는 항상 78자 고유 문자열이라 오탐 위험이 없다."""
    return any(p.get("puuid") == "BOT" for p in match_detail["info"]["participants"])


def _pick_match_by_game_time_range(matches_detail: list[dict], clip_creation: datetime.datetime,
                                    game_ms_end: float) -> list[dict]:
    """_pick_match_for_clip(1차)이 실패했을 때만 쓰는 2차 판별 - 주로 리플레이 뷰어를 녹화한
    클립처럼 creation_time(파일을 "본" 시각)을 못 믿는 경우를 위한 것. 클립이 게임 내 시계
    기준 game_ms_end 시점까지 진행된 걸 보여주므로, 그보다 짧게 끝난 매치는 확실히 아니다 -
    이걸로 후보를 추리고, 남은 후보를 실제 게임 "종료" 시각이 clip_creation에 가까운 순서로
    정렬해 반환한다(리플레이는 항상 게임이 끝난 "후"에나 볼 수 있으므로 종료 시각이 자연스러운
    기준점). AI 상대 대전(봇으로 채워진 매치 포함)은 애초에 후보에서 제외한다(_has_bot_participant).

    🛡️ [반환형 변경 - dict|None -> list[dict]] 예전엔 "가장 가까운 것 1개"만 골라 반환했는데,
    실제 사고(오늘 KR_8393538099 vs KR_8393410432)에서 "종료 시각이 가장 가깝다"는 이유만으로
    고른 매치가 오답인 사례가 나왔다 - 사용자가 게임을 끝낸 뒤 다른 게임을 더 하고 나서야
    리플레이를 녹화하면, 그 사이에 플레이한 "더 최근에 끝난 다른 게임"이 실제 정답보다
    creation_time에 가까워서 오답으로 뽑힐 수 있다(이 함수 docstring이 예전부터 "알려진
    한계"로 명시해뒀던 바로 그 케이스). 이제 이 함수는 순수하게 "거리순 정렬 후보 목록"만
    반환하고(I/O 없음, 테스트 용이성 유지), 호출부(_run_pipeline)가 상위 1~2개에 대해 실제
    킬 존재 여부까지 추가로 검증한 뒤 최종 후보를 고른다.

    🛡️ [abs() 제거 - 방향 제한] 리플레이는 항상 게임이 끝난 "후"에나 볼 수 있으므로, 아직
    안 끝난(clip_creation보다 나중에 끝나는) 매치는 원천적으로 후보가 될 수 없다 - 예전엔
    abs()로 방향을 안 가려서 이런 매치도 이론상 후보가 될 여지가 있었다.

    🛡️ [안전장치 유지] 가장 가까운 후보조차 MATCH_GAME_TIME_MAX_STALENESS_SEC보다 더 멀리
    떨어져 있으면, 확신할 수 없는 추측을 내놓는 대신 빈 리스트를 반환해 명확한 실패로
    처리한다(원본 클립이 너무 오래돼서 최근 매치 목록에 애초에 정답이 없는 경우를 위한 방어).
    """
    candidates = [md for md in matches_detail
                  if md["info"]["gameDuration"] * 1000 >= game_ms_end and not _has_bot_participant(md)]

    def end_of(md):
        info = md["info"]
        return datetime.datetime.fromtimestamp(
            (info["gameStartTimestamp"] + info["gameDuration"] * 1000) / 1000, tz=datetime.timezone.utc
        )

    past_candidates = [md for md in candidates if end_of(md) <= clip_creation]
    if not past_candidates:
        return []

    ranked = sorted(past_candidates, key=lambda md: (clip_creation - end_of(md)).total_seconds())
    closest_gap_sec = (clip_creation - end_of(ranked[0])).total_seconds()
    if closest_gap_sec > MATCH_GAME_TIME_MAX_STALENESS_SEC:
        return []
    return ranked


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
    "- 항상 존댓말을 써라. 단, 습니다체(합니다/습니다)는 절대 쓰지 말고, 항상 요체(해요/예요/"
    "이에요/했어요)로 문장을 끝내라. 반말체 감탄사(예: '대박이다', '미쳤다', '쩐다')는 "
    "절대 쓰지 말고 반드시 요체로 바꿔라(예: '대박이에요', '미쳤어요').\n"
    "- 의문형 감탄을 적극 활용해라 (예: '미쳤는데요?!', '이걸 잡아요?!', '지금 뭘 한 거예요?!').\n"
    "- 문장을 끝맺을 땐 습니다체가 아니라 요체를 유지해라 (예: '완전히 뒤집어버렸어요!!').\n\n"
    "구조 규칙:\n"
    "- 누가 죽였는지(킬러 이름)는 반드시 문장 어딘가에 등장해야 한다(피해자 이름은 없어도 "
    "된다 - 아래 6~8번처럼 결과 중심으로만 서술해도 됨). 화법은 여러 가지가 있을 수 있다 "
    "(아래는 화법의 다양성을 보여주는 예시일 뿐이니 그대로 베끼지 말고 매번 새롭게 응용해라):\n"
    "  1) 사실 보고형: '{killer}가 {victim} 잡았어요.'\n"
    "  2) 감탄형: '완전 미쳤네요, {killer}가 {victim}를!!'\n"
    "  3) 믿기지 않는다는 반응형: '{killer}가 {victim} 저걸 잡아버리네요, 돌았는데요 진짜로??!!'\n"
    "  4) 과격한 표현형: '{killer}가 {victim}를 박살내버렸어요!!'\n"
    "  5) 단정 마무리형: '{killer}가 {victim}를 완전히 끝내버렸어요!!'\n"
    "  6) 결과 우선형(피해자 이름 생략 가능): '{killer}, 이걸 그대로 압살해버리네요!!'\n"
    "  7) 도치 강조형(피해자 이름 생략 가능): '완전히 끝나버렸어요, {killer} 손에!!'\n"
    "  8) 임팩트 단정형(피해자 이름 생략 가능): '그냥 압살이에요, {killer}!!'\n"
    "- 매 호출마다 위 1~8번 중 하나를 고르되, 특정 화법(특히 5번 '완전히 끝내버렸어요'류의 "
    "단정 마무리형)에 치우치지 말고 8개를 최대한 고르게 순환해서 골라라 - 매번 같은 화법만 "
    "반복하면 안 된다. 킬 이벤트가 여러 건이면(줄이 여러 개면) 그 줄들 사이에서도 서로 "
    "다른 화법을 섞어써라.\n"
    "- 문장은 짧고 임팩트 있게 끊어라. 한 줄에 절 하나, 길어도 두 절.\n"
    "- 느낌표를 적극 사용하고 텐션을 끝까지 올려라. 감탄사 없는 밋밋한 사실 전달문('OO가 XX를 처치했습니다' "
    "같은 문장)은 금지.\n"
    "- 어시스트 규칙: 사실 목록의 킬 이벤트에 어시스트가 표시돼 있으면(예: '어시스트: OOO'), 그 어시스트를 "
    "반드시 문장에 언급해라 - 빠뜨리면 안 된다. 다만 매번 똑같은 패턴만 쓰지 말고 표현은 다양하게 바꿔써라 "
    "(아래는 예시일 뿐이니 그대로 베끼지 마라):\n"
    "  a) 협공 강조형: '{killer}가 {victim} 잡았어요, {assist}가 어시스트했어요!!'\n"
    "  b) 공동 마무리형: '{killer}랑 {assist}가 같이 {victim}를 끝장내버렸어요!!'\n"
    "  c) 셋업-마무리형: '{assist}가 깔아준 걸 {killer}가 그대로 마무리했어요!!'\n"
    "어시스트가 없는 킬은 위 1~5번처럼 킬러/피해자 둘만 다루는 문장으로 만들어라.\n\n"
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
    "- Make one sentence that clearly states who killed whom. Style can vary (these are examples "
    "showing the range of styles, don't copy them verbatim - come up with a fresh phrasing every "
    "time):\n"
    "  1) Plain impact: '{killer} takes down {victim}!!'\n"
    "  2) Exclamation: 'Unreal, {killer} just erases {victim}!!'\n"
    "  3) Disbelief: 'Did {killer} really just do that to {victim}?!'\n"
    "  4) Brutal: '{victim} never stood a chance against {killer}!!'\n"
    "  5) Death-sentence metaphor: 'That's a death sentence for {victim}, signed by {killer}!!'\n"
    "  6) No-escape metaphor: 'No escape for {victim} - {killer} closes the door!!'\n"
    "  7) Cooked metaphor (slang for 'finished/doomed'): '{victim} is cooked, and {killer} is "
    "the one serving it!!'\n"
    "  8) Result-first, victim name optional: '{killer} with the execution - clean, brutal, "
    "done!!'\n"
    "- Pick one of the 8 styles above each call, and cycle through them as evenly as possible - "
    "don't lean on the same style (especially #1) over and over. If there are multiple kill "
    "events (multiple lines), vary the style across those lines too.\n"
    "- These are stylistic flourishes only (metaphor, exaggeration, slang) - never let the "
    "metaphor imply a specific cause, ability, or location that isn't in the facts list (e.g. "
    "'cooked'/'no escape'/'death sentence' describe the outcome, not an invented method).\n"
    "- Assist rule: if a kill event's facts include an assist (e.g. 'assists: X'), you must "
    "mention that assist in the sentence too - never drop it. Vary the phrasing each time though "
    "(these are examples only, don't copy them verbatim):\n"
    "  a) '{killer} finishes it, {assist} set it up!!'\n"
    "  b) '{killer} and {assist} combine to end {victim}!!'\n"
    "  c) '{assist} softens them up and {killer} closes it out!!'\n"
    "Kills with no assist stay a plain killer/victim sentence like the example above.\n"
    "- Some facts below may already show a role (e.g. 'the jungler', 'the enemy mid laner') or a "
    "generic word (e.g. 'the enemy', 'his opponent', 'a teammate') in place of a name - this is "
    "intentional (that player's name isn't announcer-friendly), so treat it exactly like a name "
    "and build the sentence naturally around it. Never invent a real name to replace it, and "
    "never add 'the' in front of it if it's already there.\n\n"
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
# 🛡️ [스타일 균등 순환 지시만으로는 부족 - 실측 확인] "고르게 순환해라" 지시문(위
# Structure rules의 "cycle through them as evenly as possible")만으로는 베이스라인
# 10회 호출 중 10회 전부 1번(Plain impact)로 수렴했다(실험 라운드 실측, KO의 유사
# 지시문과 강도가 거의 동일했는데도 KO보다 훨씬 심하게 쏠림). 실험 결과 "이번엔 반드시
# 스타일 N을 써라"처럼 매 호출마다 스타일을 직접 지정하는 방식이 압도적으로 효과적이었다
# (30% vs temperature 상향 80%, few-shot 순서 셔플 70%) - 아래 EN_STYLE_NAMES를
# _generate_commentary가 호출 시점에 무작위로 골라 프롬프트에 추가 지시로 꽂는다.
# 번호/이름은 위 Structure rules의 1~8번 목록과 정확히 같은 순서로 맞춰야 한다.
EN_STYLE_NAMES = [
    "Plain impact", "Exclamation", "Disbelief", "Brutal", "Death-sentence metaphor",
    "No-escape metaphor", "Cooked metaphor", "Result-first",
]
# 🛡️ [어시스트 있는 킬 - 스타일 후보 제한] 스타일 강제가 어시스트 규칙을 뭉개는 회귀가
# 실측으로 확인됐다(어시스트 있는 킬 5회 중 4회가 어시스트 누락 - no-escape/result-first
# 등 어시스트를 자연스럽게 끼워 넣을 자리가 없는 템플릿으로 강제됐을 때 특히 심함).
# 어시스트가 있을 때는 원래 어시스트 예시(a/b/c)가 자연스럽게 들어맞는 스타일만 후보로
# 좁힌다(1=Plain impact, 2=Exclamation, 3=Disbelief, 8=Result-first - 전부 "{killer}
# [동사] {victim}" 뒤에 어시스트 절을 덧붙이기 쉬운 구조). 어시스트 없는 킬은 8개 전부.
EN_ASSIST_FRIENDLY_STYLE_INDICES = (1, 2, 3, 8)

# 🛡️ [EN 리드인 재설계 - "여러 보이스가 짧게 겹쳐 떠드는 체인"은 완전히 잘못된 방향이었다]
# 실제 LCK/LCS 구조는 해설자 한 명이 빌드업~킬 순간까지 끊김 없이 혼자 이어서 말하고,
# 환호는 목소리가 아니라 배경 관중 SFX(crowd_cheer_4.wav/ambient_crowd_low.wav, 이미
# 존재함)의 몫이다 - 이 프롬프트는 그 "한 사람이 쭉 이어가는 빌드업" 역할만 맡는다.
# 킬러/피해자/구체적 결과는 절대 다루지 않는다 - 그건 main_fact(_generate_commentary,
# EN_SYSTEM_PROMPT)가 이미 따로 담당하므로 여기서 또 언급하면 내용 중복/상충 위험이
# 있다. {target_words}/{target_sec}는 호출부가 kill_t로부터 역산한 가용 시간을 바로
# 넘겨서, 사전 길이 못박기 없이도 GPT가 대략적인 분량 감을 잡게 한다.
# 🛡️ [중립형 원칙(양방향) - KO "미접전 단정형" 문제의 EN 재현 확인 후 추가] 실험
# 라운드에서 베이스라인 10개 중 8개가 "they close in"/"circle each other"류로 "아직 안
# 붙었다"를 단정해 KO와 동일한 문제가 재현됨을 확인 - 중립형 절 추가 후 0/10으로
# 사라졌으나, 대신 2/10이 반대로 "이미 붙었다"(clash/unleash)를 단정하는 새 패턴을
# 보여 양방향 모두 명시적으로 금지하도록 보강했다. 완벽한 보장은 아니다 - 텍스트는
# kill_t 역산 시간만 알 뿐 실제 화면 상태를 모르므로, 두 방향의 단정을 "줄일" 뿐
# "0%로 만들" 수는 없다.
# 🛡️ [금지 예시에 standoff류 추가 - 실제 렌더에서 새어나온 사례 확인 후] 양방향 절
# 추가 후 10회 재호출에선 1/10만 "standoff"로 걸렸는데, 그 1건이 실제 프로덕션 렌더
# (verify_mainfact_anchor_render_test)에서도 그대로 나왔다 - "locked in a fierce
# standoff"는 "아직 안 붙었다"를 노골적으로 전제하는 표현. "standoff"와 같은 계열
# (face-off/stare-down/stalemate, 전부 "대치만 하고 아직 안 붙었다"를 명시)을 금지
# 예시에 직접 추가했다 - 이번에 걸린 단어는 확실히 막되, 다른 유사 표현이 새로
# 새어나올 가능성 자체는 여전히 남는다(아래 검증 라운드에서 재확인).
# 🛡️ [converge 추가 - standoff 수정 후 재검증에서 또 발견] standoff류를 막은 직후
# 10회 재검증에서 자동 키워드 집계는 0/10이었지만 직접 다시 읽어보니 "as forces
# converge"가 "closing the distance"/"circling"과 같은 계열(서로 다가가는 중 ->
# 아직 안 붙었다 암시)이라 추가로 금지했다 - standoff류와 동일한 패턴: "이번에
# 걸린 단어 하나씩 막아나가는" 대증적 조치이지 완전 차단이 아니다.
# 🛡️ [Escalation 지시 - KO urgent 슬롯과 유사한 효과 노림] KO는 마지막 슬롯을 별도
# 녹음된 "긴박 톤" 전용 풀로 교체하지만, EN은 한 문단을 한 번에 합성하는 구조라
# 똑같은 방식을 쓸 수 없다 - 대신 "뒤로 갈수록 문장이 더 짧고 격해지도록" 텍스트
# 차원의 지시만 추가한다. TTS가 실제로 뒷부분을 더 격앙되게 "읽어주는 것"까지는
# 보장 못 한다(ElevenLabs가 텍스트 단서엔 어느 정도 반응하지만 KO의 stability 조정급
# 보장은 아님) - 어디까지나 "확률을 낮추는" 개선이지 "보장"이 아니다.
EN_NARRATION_SYSTEM_PROMPT = (
    "You are a high-energy esports caster calling the lead-up to a kill in a League of Legends "
    "highlight clip. Write ONE continuous paragraph of building-tension commentary - a single "
    "flowing narration, not separate lines, not a list.\n\n"
    "Rules (never violate):\n"
    "- Do not mention any champion name, ability, specific location, or state who kills whom - "
    "that is handled by a separate line elsewhere. This paragraph is pure atmosphere/tension "
    "build-up only, and every sentence must stay true no matter what actually happens in the "
    "clip (no guesses about which side wins the fight).\n"
    "- Neutral-truth rule: every sentence must stay true whether the two sides have ALREADY "
    "started fighting by the time it plays, or haven't yet - never write a sentence that assumes "
    "either state as fact. Avoid phrases that assume the clash HASN'T started yet (e.g. 'closing "
    "the distance', 'sizing each other up', 'waiting for an opening', 'circling', 'inching "
    "closer', 'converge', 'standoff', 'face-off', 'stare-down', 'stalemate'). Also avoid phrases "
    "that assume it HAS already started (e.g. 'they clash', 'unleashes a flurry of attacks', "
    "'exchanging blows'). Describe the abstract atmosphere/energy/momentum instead, so the line "
    "reads true regardless of the actual state.\n"
    "- Escalate gradually: later sentences (closer to the end of the paragraph) should feel more "
    "urgent and intense than earlier ones - shorter clauses, sharper words, rising energy - "
    "building toward the capstone line below.\n"
    "- End the paragraph with a vague, abstract sense that the moment has arrived or someone "
    "couldn't hold on (e.g. '...and there's no way out now!', '...and it's already over!') - "
    "still no specific names, champions, or details, just a dramatic capstone line.\n"
    "- High energy, present tense, exclamatory - like a live broadcast build-up.\n"
    "- Target length: approximately {target_words} words (about {target_sec:.1f} seconds at a "
    "caster's speaking pace). A little under is fine; do not go noticeably over - being too long "
    "is worse than being a bit short.\n\n"
    "Respond ONLY in the following JSON schema, no other text: "
    '{{"narration": "the full paragraph text"}}'
)
# 🛡️ [평균 발화 속도 추정치 - 검증 라운드에서 실측 비교 후 조정 가능] 흥분한 캐스터 톤의
# 대략적인 초당 단어 수. 이 값으로 GPT에게 목표 단어 수를 미리 알려주지만, 실제 TTS
# 길이는 합성 후에만 정확히 알 수 있다 - 그래서 atempo 보정(아래)이 최종 안전장치다.
EN_NARRATION_WORDS_PER_SEC = 2.5
# 🛡️ [atempo 보정 상한 - "자르기보다 약한 속도 보정"] 내레이션이 가용 시간보다 길면
# 최대 이 비율까지만 압축한다 - 그 이상 압축해야 하는 경우(가용 시간이 너무 짧음)는
# 그냥 가용 시간 하한(EN_LEADIN_START_OFFSET_SEC)에서 시작하게 두고 약간의 침범을
# 감내한다(완전히 잘라내는 것보다 자연스러움 손실이 적다고 판단).
EN_NARRATION_MAX_ATEMPO_RATIO = 1.3
# 🛡️ [최소 가용 시간 - 너무 짧으면 내레이션 자체를 스킵] 1~2단어짜리 내레이션은 어색하고
# 의미가 없다 - 기존 리드인/체인 기능들의 "자리가 없으면 그냥 스킵" 원칙을 그대로 따른다.
EN_NARRATION_MIN_AVAILABLE_SEC = 2.0


class KyvoHighlight(KyvoBaseCog):
    def __init__(self, bot):
        super().__init__(bot)
        self.ai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
        self.render_executor = ThreadPoolExecutor(max_workers=HIGHLIGHT_MAX_WORKERS, thread_name_prefix="kyvo-highlight")
        self.render_semaphore = asyncio.Semaphore(HIGHLIGHT_MAX_CONCURRENT)
        # 🛡️ [anonymous_reports.py의 daily_limit_cache와 동일한 정신] Redis 장애 시에도 진짜
        # 작동하는 방어 - 프로세스 재시작 시 초기화되지만(로컬 캐시의 알려진 한계), Redis가
        # 정상이면 애초에 이 캐시를 거치지 않는다.
        self.daily_limit_cache: dict[str, tuple[int, float]] = {}

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
    #  하루 사용 한도 (cogs/anonymous_reports.py의 Redis 카운터 패턴 그대로 - SET NX로 첫 요청
    #  시점에만 TTL을 걸고 INCR로 누적, 길드/유저 두 축이라 키+한도를 인자로 받는 형태만
    #  cogs/ticket_ai.py의 범용화를 따른다)
    # ══════════════════════════════════════════════════════════
    async def _check_daily_limit(self, key: str, limit: int) -> bool:
        """True면 아직 한도 이내(허용), False면 한도 초과. 호출될 때마다 카운터가 1 증가한다
        (허용/차단 여부와 무관하게 "시도 자체"를 센다 - anonymous_reports/ticket_ai와 동일)."""
        try:
            await self.bot.redis.set(key, 0, ex=HIGHLIGHT_DAILY_WINDOW_SECONDS, nx=True)
            count = await self.bot.redis.incr(key)
            return count <= limit
        except Exception as e:
            print(f"[HIGHLIGHT][WARN] Redis daily-limit check failed for '{key}', falling back to local: "
                  f"{type(e).__name__}: {e}", flush=True)
            return self._check_daily_limit_local(key, limit)

    def _check_daily_limit_local(self, key: str, limit: int) -> bool:
        now = time.time()
        count, expiry = self.daily_limit_cache.get(key, (0, now + HIGHLIGHT_DAILY_WINDOW_SECONDS))
        if now > expiry:
            count, expiry = 0, now + HIGHLIGHT_DAILY_WINDOW_SECONDS
        count += 1
        self.daily_limit_cache[key] = (count, expiry)
        return count <= limit

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
    def _probe_duration_and_creation(video_path: str) -> tuple[float, datetime.datetime | None, tuple[int, int]]:
        r = subprocess.run([FFMPEG_EXE, "-i", video_path], capture_output=True, text=True)
        stderr = r.stderr
        dur_m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", stderr)
        if not dur_m:
            raise ValueError("ffmpeg가 영상 길이를 읽지 못함 - 손상되었거나 지원하지 않는 형식")
        h, m, s = dur_m.groups()
        duration = int(h) * 3600 + int(m) * 60 + float(s)
        # 🛡️ [버그 수정 - "창작 시각 불명"을 "방금"으로 둔갑시키던 문제] 컨테이너에
        # creation_time 메타데이터가 없는 클립(녹화 도구에 따라 흔함, 재인코딩/트리밍으로
        # 메타데이터가 날아간 경우 등)을 예전엔 datetime.now()로 대체했다 - 그러면 "녹화
        # 시각"이 아니라 "방금 업로드한 시각"을 기준으로 매치를 찾게 되어, 1차 판별(날짜
        # 범위 제한이 없었음)이 그 시각 근처에 끝난 전혀 무관한 최근 매치를 통과시킬 수
        # 있었다(실제 위험 확인됨 - 엉뚱한 매치의 선수 이름/KDA/아이템이 그대로 입혀진
        # 하이라이트가 조용히 완성될 수 있었음). 이제 메타데이터가 없으면 None을 그대로
        # 반환해서 "모른다"는 상태를 호출부가 명시적으로 다르게(더 엄격하게) 처리하게 한다.
        ct_m = re.search(r"creation_time\s*:\s*([\d\-T:.Z]+)", stderr)
        creation = datetime.datetime.fromisoformat(ct_m.group(1).replace("Z", "+00:00")) if ct_m else None
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
    def _apply_atempo(src_path: str, out_path: str, ratio: float) -> float:
        """ffmpeg atempo로 재생 속도만 압축하고(피치 보존) 새 길이를 반환한다. KO
        battle_main/lck/sub 정적 풀 생성 때 검증된 것과 동일한 후처리 - voice_settings.
        speed가 이 TTS 엔드포인트에서 무시된다는 게 확인됐으므로(EN_SYSTEM_PROMPT 주석
        근처 "발화 속도" 조사 참고), 합성 후 ffmpeg로 압축하는 게 유일한 실제 작동 방법."""
        subprocess.run([FFMPEG_EXE, "-y", "-i", src_path, "-filter:a", f"atempo={ratio}", out_path],
                        capture_output=True, check=True)
        r = subprocess.run([FFMPEG_EXE, "-i", out_path], capture_output=True, text=True)
        m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", r.stderr)
        if not m:
            raise ValueError(f"ffmpeg가 atempo 처리 후 길이를 읽지 못함: {out_path}")
        h, mi, s = m.groups()
        return int(h) * 3600 + int(mi) * 60 + float(s)

    @staticmethod
    def _count_long_silences(wav_path: str, noise_db: float = -35.0,
                              max_breath_sec: float = 0.4) -> int:
        """🛡️ [긴 내레이션 전용 무음 판정 - 기존 짧은 발화 게이트와 완전히 분리]
        기존 게이트(예: _trim_trailing_silence)는 d=0.15로 "0.15초 이상 무음이면 전부
        실패"였다 - 짧은 한두 문장짜리 정적 풀에는 맞지만, 여러 문장이 이어지는 긴
        내레이션은 문장 사이 자연스러운 숨쉬기 무음이 당연히 여러 번 생긴다. d 값을
        max_breath_sec(기본 0.4초)로 올리면 ffmpeg silencedetect 자체가 그보다 짧은
        구간은 아예 리포트하지 않으므로(동작 자체는 동일, 임계값만 다름), "짧은
        숨쉬기는 허용, 비정상적으로 긴 무음(TTS 글리치)만 걸러낸다"는 요구를 기존
        함수를 전혀 건드리지 않고 새 함수로 달성한다."""
        r = subprocess.run(
            [FFMPEG_EXE, "-i", wav_path, "-af",
             f"silencedetect=noise={noise_db}dB:d={max_breath_sec}", "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        return len(re.findall(r"silence_start:\s*([\d.]+)", r.stderr))

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
    def _apply_nickname_swell(wav_path: str, duration: float, out_path: str,
                               atempo_ratio: float = NICKNAME_SWELL_ATEMPO_RATIO,
                               start_ratio: float = NICKNAME_SWELL_START_RATIO) -> float:
        """1단계 Hype 닉네임 샤우팅의 뒷부분(물결표 여운 구간으로 추정되는 지점)에 볼륨
        스웰 + 실제 타임스트레치를 함께 적용해 "길게 끄는 느낌"을 낸다 - 이름 음절 자체
        (앞쪽)는 완전히 그대로 유지. 로컬 프로토타입 v8에서 검증된 볼륨 값(최대 +60%)은
        그대로 두고, 이번에 뒷부분을 ffmpeg atempo(재생속도 변경, 피치 보존)로 실제로
        늦춰서 물리적으로 더 길게 만드는 단계를 추가했다 - 볼륨만 커지는 건 "강조"일 뿐
        "늘어짐"이 아니라는 게 확인돼서다. 앞부분(head)은 원본 그대로 잘라 붙이고,
        뒷부분(tail)만 atempo 적용 후 스웰까지 씌워서 이어붙인다.
        🛡️ [start_ratio 파라미터화 - 음절 수 동적 조정] 원래 고정 0.55였는데, "넥스"(2음절)
        실측 결과 55% 지점이 마지막 음절("스")이 아니라 첫 음절("넥") 중간에 걸리는 게
        확인돼서 호출부(_nickname_swell_start_ratio)가 이름 길이에 맞는 비율을 계산해
        넘길 수 있게 파라미터로 뺐다 - 기본값은 기존 0.55 그대로라 호출부에서 안 넘기면
        회귀 없음.
        🛡️ [반환값 변경 - 길이가 실제로 늘어남] 이전엔 길이가 안 바뀌어서 호출부가 원래
        duration을 그대로 스케줄에 썼는데, 이제는 tail이 실제로 길어지므로(atempo_ratio가
        1보다 작을수록 더 길어짐) 새 총 길이를 반환한다 - 호출부는 반드시 이 반환값을
        스케줄(hype_nickname_durations)에 다시 반영해야 한다(안 그러면 뒤쪽이 total_duration
        산정에서 빠져 믹스에서 잘릴 위험).
        🛡️ volume=eval=frame을 프레임 단위로 그대로 쓰면 프레임 경계마다 계단식 클릭 노이즈가
        남는다는 게 이 세션 초반(crowd_cheer_4.wav 빌드)에 스펙트로그램으로 실측 확인된 교훈 -
        asetnsamples로 프레임을 잘게(64샘플) 쪼개 계단을 사람 귀에 안 들릴 만큼 작게 만드는
        동일 기법을 재사용한다(atempo로 늘어난 tail에도 그대로 적용)."""
        start_t = max(duration * start_ratio, 0.01)
        tail_span = max(duration - start_t, 0.05)
        new_tail_span = tail_span / atempo_ratio
        new_duration = start_t + new_tail_span
        filter_complex = (
            f"[0:a]atrim=0:{start_t:.3f},asetpts=PTS-STARTPTS[head];"
            f"[0:a]atrim={start_t:.3f}:{duration:.3f},asetpts=PTS-STARTPTS,atempo={atempo_ratio}[tailstretched];"
            f"[tailstretched]asetnsamples=n=64:p=0,volume=eval=frame:"
            f"volume='1+{NICKNAME_SWELL_RISE}*t/{new_tail_span:.3f}'[tailfinal];"
            f"[head][tailfinal]concat=n=2:v=0:a=1[out]"
        )
        subprocess.run(
            [FFMPEG_EXE, "-y", "-i", wav_path, "-filter_complex", filter_complex, "-map", "[out]", out_path],
            capture_output=True, check=True,
        )
        return new_duration

    @staticmethod
    def _trim_trailing_silence(wav_path: str, duration: float, out_path: str,
                                noise_db: float = -35.0, min_silence_sec: float = 0.15,
                                margin_sec: float = 0.05) -> float:
        """하이픈 늘려 부르기(HYPE_NICKNAME_SHOUT_STRETCHED_TEMPLATE) 전용 후처리 -
        _apply_nickname_swell 대신 쓴다(둘을 같이 적용하면 이미 하이픈+물결표로 늘어진
        발화를 또 atempo로 늦추는 이중 적용이 되고, 스웰의 볼륨 페이드업 구간 가정도
        뒤에 붙은 리액션 문장 때문에 안 맞게 됨). 오늘 재현 실험으로 확인된 "무음이
        정확히 1곳, 항상 클립 끝(트레일링 꼬리)"이라는 패턴을 그대로 적용한다 - 실시간
        경로라 재시도가 불가능하므로, 패턴과 다르면(무음 0곳 또는 2곳 이상 - 중간에도
        무음이 있다는 뜻이라 섣불리 자르면 내용이 잘릴 위험) 원본을 그대로 복사하고
        duration을 안 바꾼다("모르면 건드리지 않는다")."""
        r = subprocess.run(
            [FFMPEG_EXE, "-i", wav_path, "-af",
             f"silencedetect=noise={noise_db}dB:d={min_silence_sec}", "-f", "null", "-"],
            capture_output=True, text=True,
        )
        starts = [float(x) for x in re.findall(r"silence_start:\s*([\d.]+)", r.stderr)]
        if len(starts) == 1:
            cut_at = starts[0] + margin_sec
            subprocess.run([FFMPEG_EXE, "-y", "-i", wav_path, "-t", str(cut_at), "-c", "copy", out_path],
                            capture_output=True, check=True)
            return cut_at
        subprocess.run([FFMPEG_EXE, "-y", "-i", wav_path, "-c", "copy", out_path],
                        capture_output=True, check=True)
        return duration

    def _render_video(self, video_path: str, video_duration: float, video_width: int,
                       video_height: int, schedule: dict, work_dir: str, out_mp4: str) -> str:
        """schedule = {"total_duration", "kill_t", <voice_key>...} - <voice_key>는
        pre_buildup(상황 멘트)/leadin_overlay(리드인 2보이스 겹침, 확률적,
        Hype 또는 Sub)/leadin_battle_main·leadin_battle_sub(리드인 전투 리액션, 상황
        멘트 슬롯 하나에 겹쳐 재생, pre_buildup_slot_count>=2일 때만, 이제 킬 직전
        kill_t-EOEO_GAP_SEC까지 끊김 없이 채운다) (셋 다 킬 이전 리드인, 자리 없으면
        없을 수도 있음)/main_explode/hype_explode/sub_explode(0단계)/
        battle_main·battle_lck_caster_dynamic·battle_sub(비활성화된 구버전 전투 리액션 -
        BATTLE_REACTION_ENABLED=False라 현재는 항상 없음)/hype_nickname_1~3(1단계,
        3보이스 동시 콜)/sub_question(2단계)/main_fact(3단계) 중 실제로 쓰인 것만 있고,
        각 엔트리는 {"wav","text","start","duration"}. 타이밍 자체는
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
        # 🛡️ [배경음 이원화 - 앰비언스 레이어] ambient_crowd_low.wav(30초, 평탄한 저음량
        # 웅성거림)를 리드인 시작(t=0)부터 클립 끝까지 항상 깔아둔다. 30초보다 긴 렌더(최대
        # MAX_CLIP_DURATION_SECONDS=45초+꼬리)를 대비해 "-stream_loop -1"로 입력 자체를
        # 무한 반복시킨다 - amix의 duration=first가 game0(비디오 오디오, total_duration까지
        # apad됨) 길이에서 알아서 끊어주므로 별도 트림 로직 없이도 항상 끝까지 채워진다.
        inputs += ["-stream_loop", "-1", "-i", AMBIENT_SFX_PATH]
        # 🛡️ [버그 수정] len(inputs)//2로 인덱스를 역산하던 방식은 모든 입력이 정확히
        # ["-i", path] 2칸짜리라는 가정에 의존했다 - cheer 입력에 "-ss"가 붙으면(4칸) 그 뒤
        # 모든 목소리의 인덱스가 통째로 틀어져서 "Invalid file index" 에러가 났다(실측으로
        # 발견). ffmpeg 입력 인덱스를 별도 카운터로 직접 추적해서 CLI 인자 개수와 무관하게
        # 정확한 인덱스를 매긴다.
        ambient_idx = 2
        next_input_idx = 3  # 0=video, 1=cheer, 2=ambient
        voice_indices = {}
        # 🛡️ [영어 스케줄 키 추가] sterling/carter/atlee(0단계 캐스케이드)와 en_leadin_1~4(리드인
        # 필러)는 KO 스케줄에는 애초에 안 생기는 키라 schedule.get()이 None을 반환해 조용히
        # 스킵된다(아래 for 루프 동일) - KO 렌더 경로에는 아무 영향 없음.
        # 🛡️ [한국어 리드인 N슬롯화] "pre_buildup" 단일 키가 en_leadin_1~4와 같은 방식으로
        # pre_buildup_1~4(PRE_BUILDUP_MAX_COUNT개)로 늘어났다 - 자리가 없는 슬롯은 schedule에
        # 아예 안 생겨서(아래 for 루프에서 None으로 조용히 스킵) 렌더당 실제로 쓰이는 개수가
        # 1~4개로 유동적이다.
        # 🛡️ [3보이스 동시 콜] "hype_nickname" 단일 키가 3보이스 동시 콜에 맞춰
        # hype_nickname_1/2/3(KO: main+hype+sub, EN: sterling+carter+atlee)로 늘어났다.
        for key in ("pre_buildup_1", "pre_buildup_2", "pre_buildup_3", "pre_buildup_4",
                    "leadin_overlay", "leadin_battle_main", "leadin_battle_sub",
                    "main_explode", "hype_explode", "sub_explode",
                    "battle_main", "battle_lck_caster_dynamic", "battle_sub",
                    "hype_nickname_1", "hype_nickname_2", "hype_nickname_3", "sub_question", "main_fact",
                    "sterling", "carter", "atlee",
                    "en_narration", "en_reaction_carter", "en_reaction_atlee"):
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

        # 🛡️ [메인바 중앙 트로피 - 항상 추가] 자체 제작 장식 아이콘, 매 렌더마다 고정 입력.
        inputs += ["-i", TROPHY_ICON_PATH]
        trophy_icon_idx = next_input_idx
        next_input_idx += 1

        # 🛡️ [골드 동전 아이콘 - 항상 추가] CDragon에서 화폐 아이콘 URL을 못 찾아서(추측한
        # 3개 경로 전부 404) 트로피와 동일한 패턴(네트워크 없이 자체 제작 PNG를 고정
        # 입력으로) 재사용한다 - make_gold_coin_icon.py로 PIL 생성(gold_gap_bar_mask.png
        # 만들 때와 동일한 4x 슈퍼샘플+LANCZOS 다운스케일 기법), 팀컬러와 무관한 고정색
        # (TOP_GOLD_TEXT_COLOR)을 이미 구워 넣어서 런타임 색상 처리가 필요 없다.
        inputs += ["-i", GOLD_COIN_ICON_PATH]
        gold_coin_icon_idx = next_input_idx
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
        # 챔피언/아이템/스펠/룬 아이콘 전부 fetch 실패 시 None이 이미 들어와 있으므로
        # 입력을 안 넣으면 필터그래프도 그만큼 가벼워진다.
        roster_pairs = schedule.get("roster_pairs") or []
        roster_icon_idx: dict[int, int] = {}
        roster_item_idx: dict[int, list[int | None]] = {}
        roster_spell_idx: dict[int, list[int | None]] = {}
        roster_rune_idx: dict[int, int] = {}
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
                spell_indices = []
                for spell_icon_path in r.get("spell_icon_paths", []):
                    if spell_icon_path:
                        inputs += ["-i", spell_icon_path]
                        spell_indices.append(next_input_idx)
                        next_input_idx += 1
                    else:
                        spell_indices.append(None)
                roster_spell_idx[r["participant_id"]] = spell_indices
                if r.get("rune_icon_path"):
                    inputs += ["-i", r["rune_icon_path"]]
                    roster_rune_idx[r["participant_id"]] = next_input_idx
                    next_input_idx += 1

        # 🛡️ [원형 포트레이트 마스크 입력 - 더 이상 추가 안 함, 사각형으로 복귀] Worlds
        # 참고 사진을 다시 확인한 결과 실제 방송은 원형이 아니라 사각형 프레임을 쓰고
        # 있었고, 원형 전환이 "레벨 숫자가 포트레이트 밖으로 튀어나옴 + 얼굴이 작아
        # 보임" 문제의 근본 원인이었다 - 마스크 입력 자체를 추가하지 않으므로 아래
        # 포트레이트 렌더링이 항상 사각형 scale 경로를 탄다(마스크 생성 코드/에셋은
        # git 이력에 남아있어 필요하면 복구 가능).

        # 🛡️ [골드 갭 바+화살표 마스크 입력 - 1개만] 상단 메인바 1곳 + 로스터 그리드
        # 최대 5행, 총 최대 6곳이 이 입력 1개를 공유한다(원형 포트레이트 마스크와 동일한
        # "입력 1개, 여러 곳에서 재사용" 패턴) - RGBA(흰색 도형+투명 배경)라 ffmpeg
        # lutrgb로 팀 컬러를 직접 입히고(알파는 그대로 유지돼 투명한 부분은 안 덮임),
        # hflip으로 좌우 방향을 뒤집는다 - alphamerge+별도 색상 입력 없이 단일 입력만으로
        # 색상화+방향 전환이 전부 가능함을 실제 ffmpeg 호출로 미리 확인함.
        gold_gap_bar_mask_idx: int | None = None
        if os.path.exists(GOLD_GAP_BAR_MASK_PATH):
            inputs += ["-i", GOLD_GAP_BAR_MASK_PATH]
            gold_gap_bar_mask_idx = next_input_idx
            next_input_idx += 1

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
            # 🛡️ [킬 스코어 강조 - 골드와 구분 안 되는 문제] 타워/골드/킬이 전부 같은 크기+흰색
            # 이라 뭐가 더 중요한 정보인지 구분이 안 된다는 피드백 - 킬만 25% 키우고(20~30%
            # 권장 범위 중간값), 골드는 흰색 대신 톤 다운된 회색(TOP_GOLD_TEXT_COLOR)으로
            # 바꿔 킬의 순백색과 명확히 구분되게 한다. 타워는 건드리지 않음(피드백 대상 아님).
            KILL_FONT_SIZE = max(10, int(round(top_font_size * 1.25)))
            top_icon_size = max(8, int(round(top_main_h * 0.6)))
            # 🛡️ [세 요소 간격 재배치 - 고른 분포] 이전엔 TOWER=0.23/GOLD=0.42로 바깥쪽에
            # 붙여두고 KILL만 0.92로 트로피에 바짝 당겨서, tower-gold 간격(118.6px)보다
            # gold-kill 간격(312px)이 2.6배나 커 "가운데가 휑하다"는 피드백을 받았다 -
            # 세 요소를 0.30/0.60/0.85로 다시 잡아서 tower-gold(187px)/gold-kill(156px)
            # 간격을 비슷하게 맞췄다(비율 1.2:1, 훨씬 고르게 분산). KILL_FRAC을 0.92->0.85로
            # 살짝 물렸지만 트로피와의 간격은 여전히 59px로 가깝게 유지된다(기존 121.5px
            # 대비 확실히 좁음 - "트로피에 가깝게"라는 원래 요청도 계속 충족).
            # 🛡️ [충돌 재확인 - 극단값 포함] 골드 아이콘+숫자(최악값 "99.9k") 우측 끝
            # (738px)과 킬 숫자(최악값 "99") 좌측 끝(832px) 사이 94px 여유, 타워 숫자
            # 시작(556px)과 골드 아이콘 좌측 끝(652px) 사이 96px 여유 - 전부 사전 계산으로
            # 재확인했다.
            # 🛡️ [세트 스코어 사각형을 메인바 안으로 이동 - TOWER_FRAC 재조정] 세트 스코어
            # 사각형(아래, 가로 60px 필요)을 팀 라벨 바로 옆 메인바 안쪽(LABEL_DIVIDER_FRAC=
            # 0.23 지점과 타워 아이콘 사이의 빈 공간)에 넣으려고 TOWER_FRAC을 0.30->0.34로
            # 늘렸다 - 라벨/구분선 기준점(LABEL_DIVIDER_FRAC)은 그대로 둬서 라벨 폰트 크기에
            # 영향이 없고(사용자 지시), 빈 공간만 68.6px로 넓어져 60px 그룹이 여유 있게
            # 들어간다. 타워-골드 충돌도 재계산: 최악값("99" 타워 숫자 끝 607.2px vs
            # "99.9k" 골드 아이콘 좌측 끝 649.4px) 42.2px 여유로 안전(기존 96px보다 줄었지만
            # 아이콘 크기(29px)보다 커서 충돌 없음). 골드-킬 간격은 TOWER_FRAC과 무관해
            # 그대로 112px 유지.
            TOWER_FRAC, GOLD_FRAC, KILL_FRAC = 0.34, 0.60, 0.85
            # 🛡️ [팀 라벨/구분선은 분리된 고정 기준점 유지] TOWER_FRAC을 0.23->0.30으로
            # 올리면서 BLUE/RED 라벨과 구분선이 tower_x_l에 바로 종속돼 있어(아래
            # divider_x_l/blue_text_x/red_text_x 계산) 같이 밀려버렸다 - 타워 "아이콘+숫자"
            # 위치만 중앙으로 당기고, 라벨/구분선은 원래 자리(옛 TOWER_FRAC=0.23) 그대로
            # 두기 위해 둘을 분리한다.
            LABEL_DIVIDER_FRAC = 0.23
            main_text_y_expr = f"({top_main_h}-text_h)/2"

            label_divider_x_l = bar_x0 + bar_half_w * LABEL_DIVIDER_FRAC
            label_divider_x_r = bar_x1 - bar_half_w * LABEL_DIVIDER_FRAC
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

            # 🛡️ [골드 동전 아이콘 - 숫자 바깥쪽(중앙에서 먼 쪽)에 배치] 타워 아이콘과
            # 동일한 "아이콘은 항상 바깥쪽, 숫자는 안쪽" 규칙을 따른다. 골드 숫자는
            # gold_x_l/r에 "중앙 정렬"돼 있어(타워처럼 가장자리 고정이 아님) 아이콘
            # 위치를 고정 비율로 잡을 수 없다 - 팀 라벨/골드 갭 배지와 같은 패턴으로
            # PIL이 실제 렌더 폭을 그대로 측정해서(ffmpeg self-reference text_w 대신)
            # 숫자의 실제 좌/우 가장자리를 구하고, 그 바로 바깥에 아이콘을 붙인다.
            _gold_probe_font = ImageFont.truetype(SCOREBAR_FONT_KR, top_font_size)
            gold100_text_w = _gold_probe_font.getlength(gold100_text)
            gold200_text_w = _gold_probe_font.getlength(gold200_text)
            gold_icon_gap = 4
            gold_icon_y = (top_main_h - top_icon_size) / 2
            gold_icon_x_l = gold_x_l - gold100_text_w / 2 - gold_icon_gap - top_icon_size
            gold_icon_x_r = gold_x_r + gold200_text_w / 2 + gold_icon_gap
            text_chain += (
                f";[{gold_coin_icon_idx}:v]scale={top_icon_size}:{top_icon_size}[vgcL]"
                f";[{label}][vgcL]overlay=x={int(round(gold_icon_x_l))}:y={gold_icon_y:.2f}[vgc1]"
                f";[{gold_coin_icon_idx}:v]scale={top_icon_size}:{top_icon_size}[vgcR]"
                f";[vgc1][vgcR]overlay=x={int(round(gold_icon_x_r))}:y={gold_icon_y:.2f}[vgc2]"
            )
            label = "vgc2"

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
            # 🛡️ [팀 사이드 라벨 - BLUE/RED 보강] 메인바 좌우 맨 끝(65% 폭 시작점)과 타워
            # 아이콘 사이 자리에 팀 라벨을 추가한다. _render_video는 언어를 모르는 구조라
            # (스케줄만 받아 그림, 실측으로 확인된 기존 설계 원칙) KO/EN 분기를 새로 만들지
            # 않고, LCK 등 실제 방송에서도 "BLUE"/"RED" 사이드 표기를 그대로 영어로 쓰는
            # 관례를 따라 언어 무관 고정 텍스트로 둔다.
            # 🛡️ [폰트 굵기] FontKR(Bold)보다 더 두꺼운 FontKR-Black(하단 패널 CS/KDA에
            # 이미 쓰는 최고 굵기 웨이트)으로 교체 - 실측 확인 결과 두 폰트의 advance
            # width는 거의 같지만(BLUE 기준 248 vs 250), Black 웨이트가 같은 폭에서
            # 획이 훨씬 두꺼워 "더 각지고 두꺼운" 느낌에 부합한다.
            # 🛡️ [자간(letter-spacing) 확보] ffmpeg drawtext에는 자간 조정 파라미터가
            # 아예 없다(letter_spacing/tracking 옵션 없음, 공식 문서 확인) - 유일한
            # 실용적 우회책은 텍스트 자체에 공백 문자를 끼워 넣는 것이라("B L U E") 이
            # 방식을 적용한다. PIL로 이 정확한 문자열의 실측 폭을 미리 재서(ffmpeg의
            # self-reference text_w 대신) 아이콘/배경 블록 좌표를 전부 파이썬에서
            # 확정값으로 계산한다.
            # 🛡️ [깃발 아이콘 제거 + 텍스트 최대 확대] 깃발 심볼을 빼고 그 자리까지 전부
            # 텍스트에 할당해서 가용 폭을 최대한 꽉 채운다. 가로/세로 두 제약을 모두
            # 계산해서 더 작은 쪽을 최종 폰트 크기로 쓴다:
            # - 가로 제약: 구분선(타워 쪽) 앞까지 남는 폭(edge_pad 두 번 제외)에 "B L U E"
            #   (더 넓은 쪽) 실측 폭이 딱 맞도록 - bar_x0/bar_x1은 항상 final_width=1920
            #   기준이라 이 값은 해상도(세로)와 무관하게 거의 고정된다.
            # - 세로 제약: top_main_h를 넘지 않도록 0.72배를 안전 상한으로 둔다(실측
            #   확인 - KILL_FONT_SIZE가 이미 0.525배로 문제없이 들어갔던 전례 대비 여유
            #   있게 잡음).
            # 🛡️ [배경 블록 제거] 텍스트가 충분히 크고 두꺼워져(FontKR-Black+자간) 별도
            # 배경 없이도 시인성이 확보된다는 피드백 - 검정 반투명 블록을 없애고 팀 컬러
            # 배경(메인바 그라데이션) 위에 텍스트만 직접 그린다.
            # 🛡️ [세트 스코어 사각형 geometry - 라벨 폰트 계산보다 먼저] 참고 이미지(HLE/GEN
            # 실제 방송 그래픽) 확대 재대조 결과 배열 방향이 틀렸었다 - 사각형 3개가
            # 가로로 나열된 게 아니라, 바 가장자리에서 세로로 3개가 쌓여 있고 위에서부터
            # 아래로 팀 컬러가 채워지는 구조다(1세트 승리 -> 맨 위 칸만 채움, 2세트 승리
            # -> 위 두 칸, …). 개별 사각형 1개의 크기(세로 28%, 세로:가로 ≈2.2:1)는 이전
            # 라운드에서 이미 맞게 잡혔으므로 그대로 유지하고, 배치만 가로→세로로 바꾼다.
            # 이 블록을 라벨 폰트 크기 계산보다 먼저 두는 이유: avail_text_w(라벨 텍스트가
            # 쓸 수 있는 가로폭)가 이제 사각형 그룹 폭만큼 줄어야 해서, 폰트 크기 계산 전에
            # 그룹 폭을 먼저 알아야 한다. 세로로 쌓으면서 그룹이 차지하는 가로폭은 사각형
            # 1개 폭(set_bar_w)뿐이라, 가로로 나열했을 때(3*w+2*gap)보다 훨씬 좁아져 라벨
            # 텍스트가 쓸 수 있는 폭이 늘어난다.
            set_bar_h = max(4, int(round(top_main_h * 0.28)))
            set_bar_w = max(2, int(round(set_bar_h * 0.45)))
            set_bar_gap = max(1, int(round(set_bar_h * 0.15)))  # 칸 사이 세로 여백
            set_group_w = set_bar_w
            set_group_h = 3 * set_bar_h + 2 * set_bar_gap
            set_group_y0 = (top_main_h - set_group_h) / 2
            SET_BAR_EDGE_PAD = 6  # 바 가장자리~사각형 그룹 사이 여백(기존 edge_pad와 동일 관례)
            SET_BAR_LABEL_GAP = 6  # 사각형 그룹~팀 이름 텍스트 사이 여백

            blue_spaced_text = "B L U E"
            edge_pad = 6
            divider_gap_for_label = 6
            divider_w_for_label = 2
            _probe_font = ImageFont.truetype(SCOREBAR_FONT_KR_BLACK, 100)
            _blue_ratio = _probe_font.getlength(blue_spaced_text) / 100
            # 🛡️ [가로 가용폭에서 사각형 그룹 몫을 먼저 뺀다] 기존엔 bar_x0+edge_pad에서
            # 바로 텍스트가 시작했는데, 이제 그 자리에 사각형 그룹이 먼저 오고 텍스트는
            # 그 뒤(SET_BAR_LABEL_GAP만큼 띄워서)부터 시작해야 한다.
            avail_text_w = (label_divider_x_l - divider_gap_for_label - divider_w_for_label - divider_gap_for_label) \
                - (bar_x0 + SET_BAR_EDGE_PAD + set_group_w + SET_BAR_LABEL_GAP) - edge_pad
            font_size_by_width = int(avail_text_w / _blue_ratio)
            font_size_by_height = int(round(top_main_h * 0.72))
            team_label_font_size = max(8, min(font_size_by_width, font_size_by_height))

            _label_font = ImageFont.truetype(SCOREBAR_FONT_KR_BLACK, team_label_font_size)
            blue_text_w = _label_font.getlength(blue_spaced_text)

            # 🛡️ [정렬 기준 - 바 가장자리가 아니라 구분선] BLUE/RED를 각각 자기 쪽 바
            # 가장자리에서부터 고정 여백으로 정렬하면, 폰트 크기가 더 넓은 단어("B L U E")
            # 기준으로 정해지기 때문에 더 짧은 단어("R E D")는 안쪽(구분선 쪽)에 남는
            # 여백이 훨씬 커 보여 "오른쪽 끝에 붙어 있으려는" 것처럼 비대칭으로 보인다는
            # 피드백 - 바 가장자리 대신 구분선을 기준점으로 삼아, 두 라벨 모두 "구분선에서
            # divider_gap_for_label만큼 떨어진 지점"에서 시작/끝나도록 통일한다(BLUE는
            # 구분선 방향으로 끝나고, RED는 구분선 방향에서 시작).
            blue_text_x = label_divider_x_l - divider_gap_for_label - divider_w_for_label \
                - divider_gap_for_label - blue_text_w
            red_text_x = label_divider_x_r + divider_gap_for_label + divider_w_for_label \
                + divider_gap_for_label

            blue_label_tf = _write_textfile("team_label_blue", blue_spaced_text)
            text_chain += (
                f";[{label}]drawtext=fontfile='{font_kr_black}':textfile='{blue_label_tf}':fontsize={team_label_font_size}:"
                f"fontcolor=white:{top_text_style}:x={int(round(blue_text_x))}:y='{main_text_y_expr}'[vmlbl1]"
            )
            label = "vmlbl1"

            # 🛡️ [RED 글자 간격 늘려서 영역 꽉 채우기] "R E D"는 "B L U E"보다 글자 수가
            # 적어서 같은 폰트 크기, 같은 (한 칸) 자간으로는 BLUE가 채우는 폭(blue_text_w)
            # 만큼 채우지 못하고 오른쪽에 빈 공간이 남는다는 피드백 - R/E/D 각 글자를
            # 개별 drawtext로 따로 그리고, 글자 사이 간격을 넓혀서 전체 폭이 정확히
            # blue_text_w와 같아지도록(=구분선에서 시작해 BLUE와 대칭인 지점까지 꽉 차게)
            # 만든다.
            red_letters = ["R", "E", "D"]
            red_letter_w = [_label_font.getlength(ch) for ch in red_letters]
            red_gap = max(0.0, (blue_text_w - sum(red_letter_w)) / (len(red_letters) - 1))

            red_x = red_text_x
            for i, ch in enumerate(red_letters):
                ch_tf = _write_textfile(f"team_label_red_{i}", ch)
                text_chain += (
                    f";[{label}]drawtext=fontfile='{font_kr_black}':textfile='{ch_tf}':fontsize={team_label_font_size}:"
                    f"fontcolor=white:{top_text_style}:x={int(round(red_x))}:y='{main_text_y_expr}'[vmlblr{i}]"
                )
                label = f"vmlblr{i}"
                red_x += red_letter_w[i] + red_gap

            # 🛡️ [라벨-타워 세로 구분선] 얇은(2px) 밝은 반투명 선으로 라벨 블록과 타워 통계
            # 영역을 시각적으로 분리한다.
            TEAM_LABEL_DIVIDER_COLOR = "white@0.2"
            divider_w = 2
            divider_gap = 6
            divider_h = int(round(top_main_h * 0.6))
            divider_y = (top_main_h - divider_h) / 2
            divider_x_l = label_divider_x_l - divider_gap - divider_w
            divider_x_r = label_divider_x_r + divider_gap
            text_chain += (
                f";[{label}]drawbox=x={int(round(divider_x_l))}:y={divider_y:.2f}:"
                f"w={divider_w}:h={divider_h}:color={TEAM_LABEL_DIVIDER_COLOR}:t=fill[vmdiv1]"
                f";[vmdiv1]drawbox=x={int(round(divider_x_r))}:y={divider_y:.2f}:"
                f"w={divider_w}:h={divider_h}:color={TEAM_LABEL_DIVIDER_COLOR}:t=fill[vmdiv2]"
            )
            label = "vmdiv2"

            # 🛡️ [세트 스코어 표시 - 순수 연출용 랜덤] LCK 스타일 "이번 세트 승수" 표시 -
            # 실제 시리즈 데이터가 없으므로 매 렌더마다 무작위로 생성한다. 한쪽이 이미
            # 3개를 다 채우면 시리즈가 끝났을 상황이라 부자연스러우므로, 리더 팀은 1~2개만
            # 채우고 상대는 그보다 적은(0~리더-1) 개수만 채운 상태만 나오게 한다(3-3 등
            # 대칭/완주 조합은 절대 나오지 않음). geometry(set_bar_w/h/gap/group_w/h)는
            # 라벨 폰트 크기 계산 전에 이미 위에서 확정했다 - 거기서 그 이유 설명.
            # 🛡️ [위치 재수정 - 라벨 "안쪽"이 아니라 바 가장자리, 라벨보다 바깥쪽] 참고
            # 이미지(HLE/GEN) 재대조 결과 사각형이 "[사각형][팀로고/이름]" 순서로 라벨보다
            # 화면 가장자리에 더 가깝게 와야 했는데, 지난 라운드엔 반대로 divider와 타워
            # 사이(라벨보다 안쪽)에 넣어버렸다. 이제 bar_x0/bar_x1 가장자리에서
            # SET_BAR_EDGE_PAD만큼만 떨어진 자리에 두고, 라벨 텍스트가 그 뒤
            # (SET_BAR_LABEL_GAP만큼 띄워서)부터 시작하도록 avail_text_w 쪽에서 이미
            # 공간을 비워뒀다.
            left_group_x0 = bar_x0 + SET_BAR_EDGE_PAD
            right_group_x0 = bar_x1 - SET_BAR_EDGE_PAD - set_group_w

            _set_leader = random.choice(["left", "right"])
            _set_leader_filled = random.randint(1, 2)
            _set_other_filled = random.randint(0, _set_leader_filled - 1)
            if _set_leader == "left":
                left_filled, right_filled = _set_leader_filled, _set_other_filled
            else:
                right_filled, left_filled = _set_leader_filled, _set_other_filled

            for side_tag, side_x0, filled_count, bar_color in (
                ("l", left_group_x0, left_filled, TEAM_BLUE_COLOR),
                ("r", right_group_x0, right_filled, TEAM_RED_COLOR),
            ):
                for i in range(3):
                    bar_y = set_group_y0 + i * (set_bar_h + set_bar_gap)
                    bar_t = "fill" if i < filled_count else "1"
                    next_label = f"vmset_{side_tag}{i}"
                    text_chain += (
                        f";[{label}]drawbox=x={int(round(side_x0))}:y={bar_y:.2f}:"
                        f"w={set_bar_w}:h={set_bar_h}:color={bar_color}:t={bar_t}[{next_label}]"
                    )
                    label = next_label

            text_chain += (
                f";[{label}]drawtext=fontfile='{font_kr}':textfile='{tower100_tf}':fontsize={top_font_size}:"
                f"fontcolor=white:{top_text_style}:x={tower_num_x_l}:y='{main_text_y_expr}'[vm1]"
                f";[vm1]drawtext=fontfile='{font_kr}':textfile='{tower200_tf}':fontsize={top_font_size}:"
                f"fontcolor=white:{top_text_style}:x='{tower_num_x_r}':y='{main_text_y_expr}'[vm2]"
                f";[vm2]drawtext=fontfile='{font_kr}':textfile='{gold100_tf}':fontsize={top_font_size}:"
                f"fontcolor={TOP_GOLD_TEXT_COLOR}:{top_text_style}:x='{int(round(gold_x_l))}-text_w/2':y='{main_text_y_expr}'[vm3]"
                f";[vm3]drawtext=fontfile='{font_kr}':textfile='{gold200_tf}':fontsize={top_font_size}:"
                f"fontcolor={TOP_GOLD_TEXT_COLOR}:{top_text_style}:x='{int(round(gold_x_r))}-text_w/2':y='{main_text_y_expr}'[vm4]"
                f";[vm4]drawtext=fontfile='{font_kr}':textfile='{kill100_tf}':fontsize={KILL_FONT_SIZE}:"
                f"fontcolor=white:{top_text_style}:x='{int(round(kill_x_l))}-text_w/2':y='{main_text_y_expr}'[vm5]"
                f";[vm5]drawtext=fontfile='{font_kr}':textfile='{kill200_tf}':fontsize={KILL_FONT_SIZE}:"
                f"fontcolor=white:{top_text_style}:x='{int(round(kill_x_r))}-text_w/2':y='{main_text_y_expr}'[vm6]"
            )
            label = "vm6"

            # 🛡️ [메인바 중앙 장식 트로피] 킬 숫자(kill_x_l/kill_x_r) 사이, 메인바
            # 정중앙(final_width/2 - bar_x0/x1이 항상 대칭이라 이게 곧 bar 중앙)에 작은
            # 장식 아이콘을 넣는다. 이 자리는 킬 텍스트 폭을 감안해도 실측 기준 약
            # 290px 이상 비어 있어(1080 기준 kill_x_l=804, kill_x_r=1116) 여유가 크다.
            # 트로피 실루엣(TROPHY_ICON_PATH)은 검은 배경+흰 실루엣 PNG를
            # make_trophy_transparent.py로 알파 투명화한 것으로, 정사각형이 아니라
            # (437x242, 약 1.81:1) 종횡비를 유지해서 스케일해야 한다.
            # 🛡️ [크기 확대 - 1.6배] "너무 작아 보인다"는 피드백으로 이전 50%에서
            # 80%로 키움(1.6배, 요청한 1.5~2배 범위 안). 가로로는 킬 텍스트 사이 실측
            # 여유(296px, 1080 기준)에 비해 2배(top_main_h*1.0, 폭 약 116px)를 적용해도
            # 전혀 안 부딪히지만, top_main_h*1.0은 세로 여백이 0이 되어(trophy_y=0)
            # 바 위아래 경계에 아이콘이 딱 붙어버리는 문제가 있어 위아래 살짝 여백을
            # 남기는 0.8로 확정했다.
            with Image.open(TROPHY_ICON_PATH) as _trophy_im:
                _trophy_native_w, _trophy_native_h = _trophy_im.size
            trophy_icon_h = max(6, int(round(top_main_h * 0.8)))
            trophy_icon_w = max(6, int(round(trophy_icon_h * _trophy_native_w / _trophy_native_h)))
            trophy_center_x = final_width / 2
            trophy_x = trophy_center_x - trophy_icon_w / 2
            trophy_y = (top_main_h - trophy_icon_h) / 2
            text_chain += (
                f";[{trophy_icon_idx}:v]scale={trophy_icon_w}:{trophy_icon_h}[vtrophy_s]"
                f";[{label}][vtrophy_s]overlay=x={int(round(trophy_x))}:y={trophy_y:.2f}[vtrophy]"
            )
            label = "vtrophy"

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
                # 🛡️ [중앙 정렬 - 글자 수 추정 대신 실측 폭] 기존엔 "글자수*0.62"로 폭을
                # 대충 추정해서 자릿수가 바뀌면(예: "+0.8k" vs "+12.3k") 화살표 위치가
                # 실제 렌더 폭과 미묘하게 안 맞아 흔들릴 수 있었다 - drawtext와 동일한
                # 폰트(SCOREBAR_FONT_KR_BLACK)로 PIL이 실제 렌더 폭을 그대로 측정해서
                # 자릿수와 무관하게 항상 정확한 절반 폭을 쓴다.
                gap_num_half_w = ImageFont.truetype(SCOREBAR_FONT_KR_BLACK, gap_badge_font_size).getlength(gap_badge_text) / 2
                # 🛡️ [화살표-숫자 간격 확대] 4px는 렌더에서 붙어서 찌그러져 보인다는
                # 피드백으로 6px로 확대(+2px).
                gap_arrow_gap = 6

                main_ink_h = top_font_size * 0.645
                main_ink_bottom = top_main_h / 2 + main_ink_h / 2
                # 🛡️ [간격 3배 확대 - 실측으로 확인된 겹침 해소] 0.03 배수는 실제 렌더에서
                # 최소 지점(숫자 하단 둥근 곡선 아래) 기준 약 2px까지 좁혀져 겹쳐 보였다
                # (줌 크롭+픽셀 스캔으로 확인) - 0.09로 올려서 최소 지점 기준 6~8px 여유를
                # 확보한다.
                gap_line_spacing = max(2, int(round(top_main_h * 0.09)))
                gap_diff_y = main_ink_bottom + gap_line_spacing

                gap_badge_tf = _write_textfile("top_gold_gap", gap_badge_text)

                text_chain += (
                    f";[{label}]drawtext=fontfile='{font_kr_black}':textfile='{gap_badge_tf}':"
                    f"fontsize={gap_badge_font_size}:fontcolor={gap_badge_color}:"
                    f"x='{gap_leader_x:.2f}-text_w/2':y='{gap_diff_y:.2f}'[vtgaptxt]"
                )
                label = "vtgaptxt"

                # 🛡️ [화살표 글리프 -> "바+화살표" PNG로 교체] 로스터 그리드와 동일한
                # 에셋(GOLD_GAP_BAR_MASK_PATH)을 공유 입력으로 재사용 - 위치 계산은
                # 기존 text_w 기반 ffmpeg 표현식 대신, 이미지 폭(gap_arrow_img_w)이
                # Python에서 미리 정확히 계산되므로 숫자로 바로 계산한다(ffmpeg 표현식에
                # 의존할 필요가 없어져서 오히려 더 정확함).
                gap_arrow_img_h = gap_arrow_font_size
                # 🛡️ [최소 폭 안전장치] 꺾쇠 에셋은 세로로 긴 비율(GOLD_GAP_BAR_ASSET_ASPECT
                # ≈0.343)이라, 높이를 상단바의 작은 폰트(gap_arrow_font_size, 실측 7px급)에
                # 그대로 맞추면 폭이 2px까지 줄어 h264 압축 영상에서 사실상 안 보인다(실측
                # 확인됨) - 4px 하한을 둬서 작은 해상도에서도 "두 선이 만나는" 형태가 최소한
                # 살아남게 한다(종횡비가 살짝 틀어지지만, GOLD_GAP_ARROW_FONT_SIZE 등 이미
                # 코드 전체에서 쓰는 "max(n, ...)" 하한 클램프와 같은 철학).
                gap_arrow_img_w = max(4, int(round(gap_arrow_img_h * GOLD_GAP_BAR_ASSET_ASPECT)))
                gap_arrow_img_y = gap_diff_y + (gap_badge_font_size - gap_arrow_img_h) / 2
                if gold_diff > 0:
                    gap_arrow_img_x = gap_leader_x - gap_num_half_w - gap_arrow_gap - gap_arrow_img_w
                else:
                    gap_arrow_img_x = gap_leader_x + gap_num_half_w + gap_arrow_gap

                if gold_gap_bar_mask_idx is not None:
                    gap_r, gap_g, gap_b = _hex_to_rgb_ints(gap_badge_color)
                    # 🛡️ [방향 - 에셋 기본형은 오른쪽을 가리킴] gold_diff>0(왼쪽/블루팀
                    # 우세)일 때만 hflip으로 왼쪽을 가리키게 뒤집는다.
                    flip = "hflip," if gold_diff > 0 else ""
                    text_chain += (
                        f";[{gold_gap_bar_mask_idx}:v]scale={gap_arrow_img_w}:{gap_arrow_img_h},{flip}"
                        f"lutrgb=r={gap_r}:g={gap_g}:b={gap_b}[vtgapimg]"
                        f";[{label}][vtgapimg]overlay=x={gap_arrow_img_x:.2f}:y={gap_arrow_img_y:.2f}[vtgaparrow]"
                    )
                    label = "vtgaparrow"
                else:
                    # 🛡️ [에셋 누락 시 안전 폴백] 기존 drawtext 글리프 방식 그대로.
                    gap_arrow_tf = _write_textfile("top_gold_gap_arrow", arrow_char)
                    text_chain += (
                        f";[{label}]drawtext=fontfile='{font_kr_black}':textfile='{gap_arrow_tf}':"
                        f"fontsize={gap_arrow_font_size}:fontcolor={gap_badge_color}:"
                        f"x='{gap_arrow_img_x:.2f}':y='{gap_diff_y:.2f}'[vtgaparrow]"
                    )
                    label = "vtgaparrow"

            # ── 상단 서브바: 게임시간 중앙 + 드래곤 스택 좌우(대칭) ──
            # 🛡️ [실측 폭 그대로 - 전체 폭이 아니라 중앙 구간만] 참고 사진 실측 결과
            # 서브바는 메인바와 달리 전체 폭이 아니라 중앙 48.8%(x=650~1900 @2560
            # 기준)만 차지한다 - 배경 박스도 그 폭만 그려서 참고 사진과 같은 "메인바보다
            # 좁은 서브바" 형태를 재현한다.
            sub_x0 = int(round(final_width * TOP_SUB_BAR_X_RATIO[0]))
            sub_x1 = int(round(final_width * TOP_SUB_BAR_X_RATIO[1]))
            # 🛡️ [타이머 폰트 확대] 0.55 -> 0.57로 올려서, 서브바 높이 확대분(top_sub_h
            # 증가)과 합쳐 기존 대비 총 ~18% 커지게 계산했다(요청한 15~20% 범위 안,
            # 실측: 이번 테스트 해상도 기준 11px -> 13px). sub_font_size는 오직 타이머
            # 전용(위 주석 참고)이라 오브젝트 숫자에는 영향 없음.
            sub_font_size = max(8, int(round(top_sub_h * 0.57)))
            # 🛡️ [오브젝트 스택 확대 - 시간 텍스트와 폰트 변수 분리] sub_font_size는
            # time_text가 그대로 쓰고 있어서(아래 vs3), 이 값 자체를 바꾸면 시간 텍스트도
            # 같이 커진다("건드리지 마" 지시 위반) - 그래서 오브젝트 숫자 전용
            # obj_font_size를 새로 둔다. sub_icon_size는 오브젝트 스택에서만 쓰여서
            # (time_text엔 아이콘이 없음) 직접 바꿔도 안전하다.
            # 🛡️ [아이콘 1.4배 확대 - 서브바 띠 높이는 그대로, 세로로 살짝 오버플로 허용]
            # overlay_frame_v2.png의 서브바 띠(TOP_SUB_BAR_X_RATIO/HEIGHT_RATIO)는 플레인한
            # 반투명 단색 밴드일 뿐 아이콘 전용 여백 구조가 PNG 안에 따로 없다(직접 열어서
            # 확인) - sub_icon_size가 이미 top_sub_h를 여백 없이 100% 채우고 있어서, 띠
            # 자체를 키우지 않는 한 "여백을 줄여서 키운다"는 접근 자체가 불가능했다. 대신
            # 띠 높이(top_sub_h, 전체 바 비율 7.78%)는 그대로 두고 아이콘만 1.4배로 키워
            # 위아래로 살짝 넘치게 둔다 - d_icon_y 공식이 이미 "(top_sub_h-sub_icon_size)/2"
            # 라 아이콘이 띠보다 커지면 음수가 되어 자동으로 위아래 대칭으로 넘치며 세로
            # 중앙 정렬은 그대로 유지된다(별도 보정 불필요). minor_icon_size(전령/바론/
            # 공허유충)는 sub_icon_size*0.75로 derive돼 있어 같이 비례 확대된다.
            sub_icon_size = int(round(top_sub_h * 1.4))
            obj_font_size = max(8, int(round(top_sub_h * 0.75)))
            # 🛡️ [세로 정렬 보정 - 실측으로 발견] ffmpeg drawtext의 text_h는 폰트의 전체
            # 행간(어센더+디센더 포함) 기준이라, 디센더를 안 쓰는 숫자/콜론 글리프의 실제
            # 잉크는 이 박스 중앙보다 아래로 치우친다 - 실측(스크린샷 픽셀 스캔) 결과
            # 아이콘 중앙(y=85.5) 대비 타이머 텍스트 잉크 중앙이 y=87.5로 약 2px
            # 낮았다(이번 해상도 top_sub_h=23 기준, 비율로 환산해 다른 해상도에서도
            # 비슷하게 보정). 오브젝트 숫자도 같은 폰트/포뮬러를 쓰므로 동일 보정 적용.
            TIMER_VALIGN_CORRECTION_RATIO = 0.087
            sub_text_y_expr = f"{top_main_h}+({top_sub_h}-text_h)/2-{top_sub_h * TIMER_VALIGN_CORRECTION_RATIO:.2f}"
            # 🛡️ [dragon_offset - 실제 필요치(타이머 뱃지 폭) 기준으로 변경] 예전엔
            # "서브바 half-width의 30%"라는 임의 비율이라, 서브바가 넓을 때는 실제 필요한
            # 여백(타이머 뱃지와 안 겹칠 정도)보다 훨씬 컸다 - 서브바를 좁힐 때 이 여유가
            # 그대로 발목을 잡는 구조였음. 타이머 뱃지 폭(아래에서 재계산하는 것과 동일
            # 공식)의 절반 + 최소 여백(4px)으로 바꿔서, 서브바 폭과 무관하게 딱 필요한
            # 만큼만 여백을 둔다.
            _timer_badge_pad_x_for_offset = max(4, int(round(sub_font_size * 0.5)))
            _timer_badge_w_for_offset = int(round(sub_font_size * 3.0)) + 2 * _timer_badge_pad_x_for_offset
            dragon_offset = _timer_badge_w_for_offset / 2 + 4

            # 🛡️ [서브바 타이머 - 실제 재생 시간에 맞춰 흐르게] 예전엔 킬 시점 스냅샷
            # 하나(gm)로 고정해서 클립 전체에서 "12:43"처럼 안 움직였다(조사 라운드에서
            # 확인됨) - 타워/골드/킬스코어/KDA/CS/아이템은 여전히 그 스냅샷 고정을
            # 유지하되, 타이머만 재생 시간(t)에 맞춰 1초씩 흐르게 바꾼다. 실제 drawtext
            # 체인은 타이머 배지 geometry(timer_border_w 등)가 계산되는 아래 지점에서
            # 만든다 - 여기선 틱별 텍스트 파일만 미리 써둔다.
            # 🛡️ [클립 시작 시점 게임 시간 역산] kill_t(킬이 클립의 몇 초 지점인지)와
            # scoreboard["game_time_ms"](킬 시점의 절대 게임 시간, kill_game_ms)가 이미
            # 둘 다 이 함수 안에 있어 새 데이터 없이 바로 역산 가능 - 클립 t=0 시점의
            # 게임 시간 = kill_game_ms - kill_t*1000(게임 시계는 ±15% 슬로프 게이트로
            # 이미 실시간 1배속임이 보장됨, 조사 라운드 결론).
            clip_start_game_ms = scoreboard["game_time_ms"] - schedule["kill_t"] * 1000
            # 🛡️ [textfile + enable 체인 - text_expr(%{eif:...}) 대신 선택] 조사 라운드
            # 판단 그대로: ffmpeg text= 안에 %{eif:...} 식을 직접 넣는 방식은 이 코드베이스가
            # 이미 콜론 충돌 때문에 피해온 패턴(위 과거 주석 "콜론은 필터 옵션 구분자와
            # 충돌" 참고)을 식 안에서 더 크게 재현할 위험이 있다 - 대신 1초 단위로 MM:SS
            # 텍스트 파일을 미리 만들어두고(_write_textfile 재사용) FIRST BLOOD/SOLO KILL
            # 배너 전환에 이미 쓰는 enable='between(t,X,Y)' 패턴을 그대로 재사용한다.
            # 클립 길이(MAX_CLIP_DURATION_SECONDS=45 상한)만큼만 생성하므로 파일 수가
            # 적어(최대 46개) 렌더 시간에 체감되는 영향이 없다(아래 검증 라운드에서 실측).
            num_timer_ticks = int(video_duration) + 1
            timer_tick_textfiles = []
            for i in range(num_timer_ticks):
                tick_gm_sec = max(0, int((clip_start_game_ms + i * 1000) // 1000))
                tick_text = f"{tick_gm_sec // 60:02d}:{tick_gm_sec % 60:02d}"
                timer_tick_textfiles.append(_write_textfile(f"timer_tick_{i}", tick_text))

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
                # 🛡️ [0마리 오브젝트 - 드래곤과 동일한 완전 스킵 방식] 처음엔 30% 알파로
                # 흐리게 처리했는데, 아예 그리지 않는 쪽으로 재변경 - 드래곤이 이미 쓰는
                # "0마리면 자리 자체가 없음" 패턴을 그대로 재사용한다. 개수가 0인 쪽만
                # 아이콘+숫자를 스킵하고 그 쪽 cursor도 전진시키지 않는다 - 그래야 바로
                # 다음(더 안쪽) 오브젝트가 스킵된 자리를 자동으로 메우며 당겨진다(드래곤이
                # 0마리일 때 리스트가 비어서 자동으로 자리가 안 생기는 것과 동일한 효과).
                # 좌/우(team100/200)가 서로 다른 개수를 가질 수 있어 완전히 독립적으로
                # 처리한다(cursor_l/cursor_r 각자 자기 쪽만 조건부 전진).
                show100 = scoreboard[key100] > 0
                show200 = scoreboard[key200] > 0

                if show100:
                    icon_x_l = mid_x - cursor_l - minor_icon_size
                    if icon_idx is not None:
                        text_chain += (
                            f";[{icon_idx}:v]scale={minor_icon_size}:{minor_icon_size}[v{name}L]"
                            f";[{label}][v{name}L]overlay=x={int(round(icon_x_l))}:y={minor_icon_y:.2f}[v{name}L2]"
                        )
                        label = f"v{name}L2"
                        num100_x = f"{int(round(icon_x_l - 4))}-text_w"
                    else:
                        num100_x = f"{int(round(mid_x - cursor_l))}-text_w"
                    num100_tf = _write_textfile(f"{name}100", str(scoreboard[key100]))
                    text_chain += (
                        f";[{label}]drawtext=fontfile='{font_kr}':textfile='{num100_tf}':fontsize={minor_font_size}:"
                        f"fontcolor={TEAM_BLUE_COLOR}:{top_text_style}:x='{num100_x}':y='{sub_text_y_expr}'[v{name}n1]"
                    )
                    label = f"v{name}n1"
                    cursor_l += minor_icon_size + 4 + minor_num_zone_w + minor_group_gap

                if show200:
                    icon_x_r = mid_x + cursor_r
                    if icon_idx is not None:
                        text_chain += (
                            f";[{icon_idx}:v]scale={minor_icon_size}:{minor_icon_size}[v{name}R]"
                            f";[{label}][v{name}R]overlay=x={int(round(icon_x_r))}:y={minor_icon_y:.2f}[v{name}R2]"
                        )
                        label = f"v{name}R2"
                        num200_x = str(int(round(icon_x_r + minor_icon_size + 4)))
                    else:
                        num200_x = str(int(round(mid_x + cursor_r)))
                    num200_tf = _write_textfile(f"{name}200", str(scoreboard[key200]))
                    text_chain += (
                        f";[{label}]drawtext=fontfile='{font_kr}':textfile='{num200_tf}':fontsize={minor_font_size}:"
                        f"fontcolor={TEAM_RED_COLOR}:{top_text_style}:x='{num200_x}':y='{sub_text_y_expr}'[v{name}n2]"
                    )
                    label = f"v{name}n2"
                    cursor_r += minor_icon_size + 4 + minor_num_zone_w + minor_group_gap

            # 🛡️ [타이머 뱃지 - 서브바 중앙 시간 강조] 지금까지 타이머는 서브바 공통 배경
            # 위에 텍스트만 있어서 다른 요소와 시각적으로 구분이 안 됐다 - 텍스트 뒤에 살짝
            # 더 어두운 톤(black@0.25)의 작은 박스를 얹어 "여기 독립된 요소"라는 느낌만
            # 준다(튀지 않게 은은한 정도). 폭은 "MM:SS" 실측 최악값(FontKR.otf 기준
            # fontsize 대비 약 2.92배, "00:00"이 가장 넓음) + 여유 3.0배로 고정폭 계산 -
            # time_text가 매번 달라져도(예: "9:05" vs "62:30") 항상 안전하게 담긴다.
            # dragon_offset(오브젝트 존 시작 전 mid_x 기준 최소 여백, 이번 해상도 기준
            # 141px)이 이 뱃지 절반 폭보다 훨씬 커서 좌우 오브젝트 아이콘과 구조적으로
            # 겹칠 수 없다 - 실제 렌더로도 재확인.
            timer_badge_pad_x = max(4, int(round(sub_font_size * 0.5)))
            timer_badge_w = int(round(sub_font_size * 3.0)) + 2 * timer_badge_pad_x
            timer_badge_h = int(round(top_sub_h * 0.82))
            timer_badge_x = mid_x - timer_badge_w / 2
            timer_badge_y = top_main_h + (top_sub_h - timer_badge_h) / 2
            # 🛡️ [타이머 검정 외곽선 - 서브바 반투명화 대응] 배지 알파를 올려도 밝은 게임
            # 배경(잔디 등) 위에서는 흰 글자가 묻히는 게 실측으로 확인됨 - 배경 박스만으로는
            # 임의의 비디오 배경에 대응하기 부족해서, 방송 그래픽에서 흔히 쓰는 검정 스트로크
            # 외곽선을 직접 추가한다(top_text_style은 GRID_TEXT_BORDER_W=0이라 테두리가
            # 없어서 이 텍스트만 로컬로 별도 지정).
            timer_border_w = max(1, int(round(sub_font_size * 0.15)))
            text_chain += (
                f";[{label}]drawbox=x={timer_badge_x:.2f}:y={timer_badge_y:.2f}:"
                f"w={timer_badge_w}:h={timer_badge_h}:color={TIMER_BADGE_COLOR}:t=fill[vtimerbadge]"
            )
            label = "vtimerbadge"
            # 🛡️ [타이머 틱 체인 - FIRST BLOOD/SOLO KILL 배너와 동일한 enable 패턴]
            # timer_tick_textfiles(위에서 미리 써둔 1초 단위 MM:SS 파일들)를 각자
            # between(t,i,i+1) 구간에만 보이게 체인으로 쌓는다 - grid_enable/
            # hud_visible_window가 이미 증명한 patter 그대로, 콤마도 그대로(홑따옴표
            # 안이라 이스케이프 불필요, 기존 관례와 동일). 마지막 틱만 다음 정수 초가
            # 아니라 total_duration까지 연장해서, video_duration 이후 게임 영상이
            # tpad로 마지막 프레임에 고정되는 구간(위 [vgame])에서도 시계가 그 마지막
            # 값에 멈춘 채로 같이 정지한다(끝까지 뭔가 보이도록 - 빈 구간 없음).
            for i, tick_tf in enumerate(timer_tick_textfiles):
                window_end = total_duration if i == num_timer_ticks - 1 else i + 1
                tick_enable = f"between(t,{i},{window_end:.3f})"
                next_label = f"vtimer{i}"
                text_chain += (
                    f";[{label}]drawtext=fontfile='{font_kr}':textfile='{tick_tf}':fontsize={sub_font_size}:"
                    f"fontcolor=white:bordercolor=black:borderw={timer_border_w}:"
                    f"x='{int(round(mid_x))}-text_w/2':y='{sub_text_y_expr}':"
                    f"enable='{tick_enable}'[{next_label}]"
                )
                label = next_label

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
            # 🛡️ [사각형 챔피언 프레임 제거 - 원형 마스크로 경계 표현] 참고 이미지가
            # 뚜렷한 테두리 없이 미니멀한 스타일이라, 원형 마스크 가장자리 자체가
            # 경계 역할을 하도록 사각형 drawbox 테두리(champion_frame_border_w/
            # CHAMPION_FRAME_COLOR)를 완전히 제거했다.
            item_slot_border_w = max(1, round(item_size * 0.04))
            pad = max(2, int(round(row_h_raw * 0.06)))
            # 🛡️ [텍스트 가독성 1순위 - 크기 대폭 확대] 기존 0.26 비율은 실측 row_h_raw
            # (~27~30px)에서 폰트 크기가 8px까지 내려가 거의 안 보였다(하단 텍스트 가독성
            # 요청의 직접 원인). "CS " 라벨을 없애 숫자만 남기면서 자리가 남은 만큼도 반영해
            # 0.55로 올린다 - 실제 크롭 캡처로 재확인.
            grid_font_size = max(12, int(round(row_h_raw * 0.55)))
            # 🛡️ [간격 추가 확대 - 여전히 붙어 보인다는 피드백] 0.15->0.25로 한 번 늘렸는데도
            # 여전히 붙어 보인다는 재피드백으로 0.35까지 추가 확대. 여유 공간 재계산(한쪽 편
            # 전체 폭 합 vs panel_half_w) 결과 margin이 72px(25%)에서 62px(35%)로 줄었을 뿐
            # 여전히 넉넉하다 - 이론상 안전 상한은 item_size의 약 89%(margin=0 지점)라
            # 35%는 그 절반에도 못 미친다.
            item_gap = max(1, int(round(item_size * 0.35)))

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

            # 🛡️ [룬/스펠 아이콘 복원 - 아이템 구역 바깥쪽(패널 가장자리 쪽)에 배치]
            # 과거 설계 주석("바깥→안쪽: 스펠/룬, 아이템, KDA, CS, 포트레이트")대로 아이템
            # 구역보다 더 바깥쪽에 둔다 - 단, 옛 레이아웃을 그대로 복사하지 않고 지금
            # 레이아웃(아이템 23px, panel_x0~items_zone_x0 사이 실측 여유 ~42px @1920
            # 기준)에 맞춰 새로 계산했다. 룬(키스톤, 다운로드 PNG 자체가 이미 원형
            # 투명이라 별도 마스킹 불요)은 조금 크게, 스펠 2개는 세로로 쌓아서 룬 옆에
            # 붙인다 - 실측 결과 42px 여유 안에 30px 클러스터가 10px 마진을 두고 들어가고,
            # 세로로도 row_h_raw(~26.8px) 안에 스택된 스펠(~22px)이 들어간다(계산+실제
            # 렌더 스크린샷으로 재확인함).
            rune_size = max(6, int(round(item_size * 0.8)))
            spell_size = max(4, int(round(item_size * 0.42)))
            spell_gap_v = max(1, int(round(item_size * 0.08)))
            spell_rune_gap = max(1, int(round(item_size * 0.08)))
            spell_rune_cluster_w = spell_size + spell_rune_gap + rune_size

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

                # 🛡️ [홀/짝수 행 톤 차이] 2번째/4번째 행(j=1,3)에만 아주 옅은 흰색
                # 배경(white@0.03)을 패널 전체 폭으로 깔아 행 구분을 돕는다 - 반드시
                # 다른 요소(포트레이트/텍스트/아이템)보다 먼저 그려야 그 위에 덮이지 않는다.
                if j in (1, 3):
                    grid_parts.append(
                        f";[{label}]drawbox=x={panel_x0}:y={row_y0:.2f}:"
                        f"w={panel_w}:h={row_h_raw:.2f}:color={ROW_STRIPE_COLOR}:t=fill:"
                        f"enable='{grid_enable}'[vrow{j}stripe]")
                    label = f"vrow{j}stripe"

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
                        # 🛡️ [룬/스펠 클러스터 - "룬이 스펠의 오른쪽" 원칙으로 통일]
                        # 처음엔 "룬=바깥쪽/스펠=안쪽"으로 양 팀을 완전 대칭 미러링했는데,
                        # 그러면 L팀은 룬이 스펠의 왼쪽(바깥쪽=패널 왼쪽이라), R팀은 룬이
                        # 스펠의 오른쪽(바깥쪽=패널 오른쪽)이 되어 두 팀이 서로 반대로
                        # 보였다 - 검토 결과 R팀 쪽 배치("룬이 스펠 오른쪽")가 선택돼서,
                        # 화면상 "룬이 항상 스펠의 오른쪽"이 되도록 L팀은 반대로 뒤집는다
                        # (룬을 아이템 쪽/안쪽으로, 스펠을 패널 가장자리 쪽/바깥쪽으로) -
                        # 그 결과 클러스터의 "패널 안에서의 위치"(바깥쪽 가장자리)는 팀마다
                        # 여전히 미러링되지만, 클러스터 "내부의 룬/스펠 좌우 순서"는 두 팀이
                        # 화면 기준으로 동일하게 보인다.
                        rune_x = items_zone_x0 - pad - rune_size
                        spell_x = rune_x - spell_rune_gap - spell_size
                    else:
                        portrait_x = panel_mid_x + pad + PORTRAIT_GAP_EXTRA_OFFSET
                        cs_zone_x0 = panel_mid_x + portrait_zone_w + PORTRAIT_GAP_EXTRA_OFFSET
                        cs_x_expr = str(int(round(cs_zone_x0 + pad)))
                        kda_zone_x0 = cs_zone_x0 + cs_zone_w + cs_kda_gap
                        kda_x_expr = str(int(round(kda_zone_x0 + pad)))
                        items_zone_x0 = kda_zone_x0 + kda_zone_w
                        item_xs = [items_zone_x0 + pad + k * (item_size + item_gap) for k in range(6)]
                        # 🛡️ [룬/스펠 클러스터 - "룬이 스펠의 오른쪽" 원칙, R팀 기준은
                        # 원래부터 이 모양이라 그대로] 스펠이 안쪽(아이템 쪽), 룬이
                        # 바깥쪽(패널 가장자리 쪽) - 위 L팀 쪽 주석 참고.
                        spell_x = items_zone_x0 + items_zone_w + pad
                        rune_x = spell_x + spell_size + spell_rune_gap

                    spell1_y = row_y0 + (row_h_raw - (2 * spell_size + spell_gap_v)) / 2
                    spell2_y = spell1_y + spell_size + spell_gap_v
                    rune_y = row_y0 + (row_h_raw - rune_size) / 2

                    icon_idx = roster_icon_idx.get(pid)
                    if icon_idx is not None:
                        # 🛡️ [사각형 포트레이트로 복귀] Worlds 참고 사진을 다시 확인한
                        # 결과 실제 방송은 원형이 아니라 사각형이었고, 원형 전환이
                        # "레벨 숫자가 바깥으로 튀어나옴" + "같은 영역에서 얼굴이 더 작아
                        # 보임"(사각형이 원보다 같은 변 길이에서 이미지를 더 많이 보여줌)
                        # 문제의 근본 원인이었다 - alphamerge 원형 마스킹 분기를 완전히
                        # 제거하고 단순 scale+overlay만 쓴다.
                        grid_parts.append(f";[{icon_idx}:v]scale={portrait_size}:{portrait_size}[vr{tag}p]")
                        grid_parts.append(
                            f";[{label}][vr{tag}p]overlay=x={int(round(portrait_x))}:y={portrait_y:.2f}:"
                            f"enable='{grid_enable}'[vr{tag}a]")
                        label = f"vr{tag}a"

                        # 🛡️ [테두리 - 참고 사진 재확인 결과 없음] Worlds 참고 사진을 픽셀
                        # 단위로 다시 확인했는데, 포트레이트 가장자리에 금색/흰색/회색 등
                        # 뚜렷한 프레임 라인이 보이지 않았다(패널 배경에 바로 맞닿아 있음) -
                        # 오늘 초반의 "흰색 헤어라인" 버전보다 "테두리 없음"이 실제 참고
                        # 사진에 더 가깝다고 판단해 드로박스 테두리를 복원하지 않는다
                        # (CHAMPION_FRAME_COLOR 등은 git 이력에서 복구 가능).

                        # 🛡️ [레벨 숫자 - 좌하단 모서리 안쪽, 반투명 박스+옅은 그림자]
                        # 포트레이트 전체를 덮는 큰 텍스트(원형 라운드 때 썼던 방식)는
                        # 사각형에서는 모서리 밖으로 튀어나오는 문제가 있었다 - 배지 크기를
                        # portrait_size 이하로 명시적으로 clamp하고, 박스 좌표를
                        # 포트레이트의 왼쪽/아래쪽 가장자리에 정확히 맞춰서(level_badge_x=
                        # portrait_x, level_badge_y=portrait_y+portrait_size-level_badge_size)
                        # 어떤 경우에도 포트레이트 경계를 벗어나지 않는다. 그림자는
                        # black@0.9의 진한 그림자 대신 black@0.6의 옅은 그림자로 낮추고,
                        # 반투명 배경 박스를 가독성의 1차 수단으로 삼는다(그림자는 보조).
                        level_val = r.get("level")
                        if level_val is not None:
                            level_badge_size = min(portrait_size, max(10, int(round(portrait_size * LEVEL_BADGE_SIZE_RATIO))))
                            level_badge_x = portrait_x
                            level_badge_y = portrait_y + portrait_size - level_badge_size
                            level_tf = _write_textfile(f"roster_level_{tag}", str(level_val))
                            grid_parts.append(
                                f";[{label}]drawbox=x={int(round(level_badge_x))}:y={level_badge_y:.2f}:"
                                f"w={level_badge_size}:h={level_badge_size}:color={LEVEL_BADGE_BG_COLOR}:t=fill:"
                                f"enable='{grid_enable}'[vr{tag}lvlbox]")
                            label = f"vr{tag}lvlbox"
                            level_font_size = max(8, int(round(level_badge_size * 0.62)))
                            level_shadow_off = max(1, int(round(level_font_size * 0.06)))
                            grid_parts.append(
                                f";[{label}]drawtext=fontfile='{font_kr_black}':textfile='{level_tf}':"
                                f"fontsize={level_font_size}:fontcolor=white:"
                                f"shadowcolor=black@0.6:shadowx={level_shadow_off}:shadowy={level_shadow_off}:"
                                f"x='{int(round(level_badge_x))}+({level_badge_size}-text_w)/2':"
                                f"y='{level_badge_y:.2f}+({level_badge_size}-text_h)/2':"
                                f"enable='{grid_enable}'[vr{tag}lvl]")
                            label = f"vr{tag}lvl"

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
                            # 🛡️ [아이콘 개별 외곽선 - ITEM_SLOT_BORDER_COLOR 위 참고 주석대로
                            # 슬롯 틀은 아이콘에 가려져 채워진 칸엔 안 보이므로 별개로 추가]
                            # 아이콘을 "그린 뒤"에 얹어야 테두리가 아이콘 위에 살아있는다.
                            grid_parts.append(
                                f";[{label}]drawbox=x={int(round(item_xs[k]))}:y={item_y:.2f}:"
                                f"w={item_size}:h={item_size}:color={ITEM_ICON_OUTLINE_COLOR}:t=1:"
                                f"enable='{grid_enable}'[vr{tag}io{k}]")
                            label = f"vr{tag}io{k}"

                    # 🛡️ [룬/스펠 아이콘 복원] 둘 다 fetch 실패 시 None이 이미 roster_spell_idx/
                    # roster_rune_idx에 들어와 있으므로(아이템 아이콘과 동일한 안전 패턴) 그냥
                    # 조용히 스킵한다 - 네트워크 실패가 핵심 렌더 파이프라인을 막지 않는다.
                    # 스펠 아이콘은 Data Dragon 원본이 불투명 정사각형이라 아이템과 동일하게
                    # scale+overlay만 하고(테두리는 크기가 너무 작아(~10px) 생략), 룬은 다운로드
                    # PNG 자체가 이미 원형 투명이라 별도 마스킹 없이 바로 overlay한다.
                    spell_idx_list = roster_spell_idx.get(pid, [None, None])
                    for k, (spell_x_k, spell_y_k) in enumerate(((spell_x, spell1_y), (spell_x, spell2_y))):
                        spell_idx = spell_idx_list[k] if k < len(spell_idx_list) else None
                        if spell_idx is not None:
                            grid_parts.append(f";[{spell_idx}:v]scale={spell_size}:{spell_size}[vr{tag}spell{k}]")
                            grid_parts.append(
                                f";[{label}][vr{tag}spell{k}]overlay=x={int(round(spell_x_k))}:y={spell_y_k:.2f}:"
                                f"enable='{grid_enable}'[vr{tag}sp{k}]")
                            label = f"vr{tag}sp{k}"

                    rune_idx = roster_rune_idx.get(pid)
                    if rune_idx is not None:
                        grid_parts.append(f";[{rune_idx}:v]scale={rune_size}:{rune_size}[vr{tag}rune]")
                        grid_parts.append(
                            f";[{label}][vr{tag}rune]overlay=x={int(round(rune_x))}:y={rune_y:.2f}:"
                            f"enable='{grid_enable}'[vr{tag}rn]")
                        label = f"vr{tag}rn"

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

                    num_tf = _write_textfile(f"roster_gap_num_{j}", gap_num_text)

                    # 🛡️ [좌우 여백 확보 - 은은한 배경 박스] 텍스트/화살표가 배경과 바로
                    # 맞닿아 빽빽해 보인다는 피드백 - 폰트를 줄이는 대신(가독성 저하) 타이머
                    # 뱃지(TIMER_BADGE_COLOR)와 같은 패턴으로 각각 뒤에 여유 있는 반투명
                    # 박스를 깔아 좌우 패딩을 만든다. 숫자 박스 폭은 "+99.9k" 실측 최악값
                    # (fontsize 대비 약 3.54배)에 여유를 둔 3.6배로 고정.
                    _gap_box_color = "black@0.25"
                    _num_box_w = int(round(GOLD_GAP_NUMBER_FONT_SIZE * 3.6))
                    _num_box_x = panel_mid_x - _num_box_w / 2
                    grid_parts.append(
                        f";[{label}]drawbox=x={arrow_box_x:.2f}:y={gap_badge_y:.2f}:"
                        f"w={gap_badge_w}:h={gap_badge_h}:color={_gap_box_color}:t=fill:"
                        f"enable='{grid_enable}'[vgap{j}box1]")
                    label = f"vgap{j}box1"
                    grid_parts.append(
                        f";[{label}]drawbox=x={_num_box_x:.2f}:y={gap_badge_y:.2f}:"
                        f"w={_num_box_w}:h={gap_badge_h}:color={_gap_box_color}:t=fill:"
                        f"enable='{grid_enable}'[vgap{j}box2]")
                    label = f"vgap{j}box2"

                    # 🛡️ [화살표 글리프 -> "바+화살표" PNG로 교체 - 상단바와 동일 에셋/방식]
                    # 기존 박스(gap_badge_w x gap_badge_h) 안에 종횡비를 유지한 채
                    # letterbox로 맞춘다(박스를 넓히면 숫자 박스/포트레이트와의 기존
                    # 간격 예산이 깨질 위험이 있어 박스 크기 자체는 건드리지 않음) -
                    # GOLD_GAP_BAR_ASSET_ASPECT(약 2.73:1)가 박스보다 옆으로 길어서 실제론
                    # 폭 기준으로 맞춰지고, 결과적으로 "가로로 긴 얇은 바" 그대로의 비율이
                    # 작게 축소되어 들어간다(의도한 모양 그대로 유지, 억지로 안 찌그러짐).
                    _img_pad = 2
                    _avail_w = max(1, gap_badge_w - _img_pad)
                    _avail_h = max(1, gap_badge_h - _img_pad)
                    # 🛡️ [최소 폭 안전장치 - 상단바와 동일 이유] 극단적으로 작은 portrait_size
                    # (다른 해상도 등)에서 꺾쇠 폭이 압축 영상에 묻힐 만큼 얇아지는 걸 방지.
                    if _avail_w / _avail_h > GOLD_GAP_BAR_ASSET_ASPECT:
                        gap_arrow_img_h = _avail_h
                        gap_arrow_img_w = max(4, int(round(_avail_h * GOLD_GAP_BAR_ASSET_ASPECT)))
                    else:
                        gap_arrow_img_w = _avail_w
                        gap_arrow_img_h = max(4, int(round(_avail_w / GOLD_GAP_BAR_ASSET_ASPECT)))
                    gap_arrow_img_x = arrow_box_x + (gap_badge_w - gap_arrow_img_w) / 2
                    gap_arrow_img_y = gap_badge_y + (gap_badge_h - gap_arrow_img_h) / 2

                    if gold_gap_bar_mask_idx is not None:
                        gap_r, gap_g, gap_b = _hex_to_rgb_ints(gap_color)
                        # 🛡️ [방향 - 상단바와 동일한 규칙] gap>0(왼쪽 팀 우세)일 때만 hflip.
                        flip = "hflip," if gap > 0 else ""
                        grid_parts.append(
                            f";[{gold_gap_bar_mask_idx}:v]scale={gap_arrow_img_w}:{gap_arrow_img_h},{flip}"
                            f"lutrgb=r={gap_r}:g={gap_g}:b={gap_b}[vgap{j}img]")
                        grid_parts.append(
                            f";[{label}][vgap{j}img]overlay=x={gap_arrow_img_x:.2f}:y={gap_arrow_img_y:.2f}:"
                            f"enable='{grid_enable}'[vgap{j}arrow]")
                        label = f"vgap{j}arrow"
                    else:
                        # 🛡️ [에셋 누락 시 안전 폴백] 기존 drawtext 글리프 방식 그대로.
                        arrow_tf = _write_textfile(f"roster_gap_arrow_{j}", arrow_char)
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

        # 🛡️ [배경음 이원화] 앰비언스는 리드인 시작(t=0)부터 항상 깔리므로 delay가 필요 없다 -
        # -stream_loop -1로 이미 무한 반복 입력이라 apad도 불필요(amix duration=first가
        # game0 길이에서 알아서 잘라준다).
        audio_parts.append(f"[{ambient_idx}:a]volume={AMBIENT_MIX_GAIN_DB}dB[ambient0];")
        mix_labels.append("[ambient0]")

        for key, idx in voice_indices.items():
            entry = schedule[key]
            delay_ms = max(0, int(entry["start"] * 1000))
            # 🛡️ [파일명 우선 -> 역할 단위 -> 기본값] 정적 풀 하나가 여러 버전(v3/v4)이
            # 섞여 있을 때(main_explode_d.wav만 v4인 경우 등) 파일 하나만 게인을 다르게 줄
            # 수 있도록 조회 우선순위를 3단계로 둔다.
            file_key = os.path.basename(entry["wav"])
            gain_db = VOICE_MIX_GAIN_DB_FILE_OVERRIDE.get(
                file_key, VOICE_MIX_GAIN_DB_OVERRIDE.get(key, VOICE_MIX_GAIN_DB))
            audio_parts.append(f"[{idx}:a]adelay={delay_ms}|{delay_ms},volume={gain_db}dB[v_{key}];")
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
               "-filter_complex_script", filter_script_path,
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
        4회 호출된다(1단계 닉네임 샤우팅 3보이스 동시 콜 + 3단계 Main의 사실 서술).
        voice_key는 ELEVENLABS_VOICE_IDS의 키("main"/"lck_caster_dynamic"/"sub"/"sterling"/
        "carter"/"atlee") 중 하나. 나머지 자리(0단계 세 목소리 + 2단계 Sub)는 닉네임이
        필요 없는 순수 감정 표현이라 정적 풀에서 고른다.
        🛡️ [stability 최초 명시] 이 경로는 지금까지 voice_settings를 아예 안 보내서 API
        기본값으로 돌아가고 있었다 - 이번에 처음으로 stability=0.55를 명시한다. style은
        같이 안 보낸다(can_use_style=False 모델이라 반영 안 됨이 확인됨 - 값을 넣어도
        무의미하고 혼동만 준다)."""
        tagged_text = f"[excited][shouts] {text}"
        voice_id = ELEVENLABS_VOICE_IDS[voice_key]
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                params={"output_format": ELEVENLABS_OUTPUT_FORMAT},
                headers={"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"},
                json={"text": tagged_text, "model_id": ELEVENLABS_MODEL_ID,
                      "voice_settings": {"stability": 0.55}},
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

    async def _generate_leadin_narration(self, available_sec: float) -> str:
        """🛡️ [EN 리드인 재설계 - 단일 긴 내레이션] 빌드업~킬 임박 직전까지를 다루는
        한 문단짜리 내레이션을 GPT로 생성한다 - 킬러/피해자/스킬/위치는 전혀 다루지
        않는다(main_fact가 그 역할을 따로 맡음, 중복/상충 방지). available_sec(렌더
        시점에 이미 확정된 kill_t로부터 역산한 가용 시간)을 바탕으로 목표 단어 수를
        프롬프트에 직접 넘겨서 분량을 사전에 유도한다 - 그래도 실제 TTS 길이는 합성
        후에만 알 수 있으므로, 호출부가 atempo 보정으로 최종 안전장치를 건다."""
        target_words = max(5, int(round(available_sec * EN_NARRATION_WORDS_PER_SEC)))
        prompt = EN_NARRATION_SYSTEM_PROMPT.format(target_words=target_words, target_sec=available_sec)
        resp = await self.ai_client.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            temperature=0.8,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"Write the narration now (target ~{target_words} words)."},
            ],
        )
        import json
        data = json.loads(resp.choices[0].message.content)
        return data["narration"]

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
        # 🛡️ [EN 비영문 닉네임 미발화] killer_display/victim_display/assist_displays는
        # _run_pipeline이 kills_with_names를 만들 때 이미 계산해둔 표시용 이름(영문이면
        # 원문 그대로, 아니면 역할/대명사로 치환됨, _en_display_name 참고) - 여기서는 그걸
        # 그대로 facts 블록에 써서 GPT에 넘긴다. 키가 없는 호출부(과거 방식/테스트 스크립트
        # 등)를 위해 .get()으로 원문 이름 폴백을 유지한다(하위 호환, 회귀 없음).
        for k in kills_with_names:
            if is_en:
                killer_disp = k.get("killer_display", k["killer"])
                victim_disp = k.get("victim_display", k["victim"])
                assist_disp = k.get("assist_displays", k["assists"])
                assist_str = f", assists: {', '.join(assist_disp)}" if assist_disp else ""
                kill_facts_lines.append(
                    f"[{k['index']}] at {k['timestamp_ms']}ms - {killer_disp} kills {victim_disp}{assist_str}"
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

        # 🛡️ [EN 전용 - 스타일 직접 지정] 호출마다 스타일 번호를 무작위로 골라 "이번엔
        # 반드시 이 스타일을 써라"는 지시를 시스템 프롬프트에 덧붙인다(실험으로 확인된
        # 가장 효과적인 쏠림 완화 방법, EN_STYLE_NAMES 선언부 주석 참고). 어시스트가 있는
        # 킬이 하나라도 있으면 EN_ASSIST_FRIENDLY_STYLE_INDICES로 후보를 좁혀서, 어시스트를
        # 자연스럽게 못 넣는 템플릿이 강제돼 어시스트 규칙이 뭉개지는 회귀를 막는다 - 그
        # 경우엔 "어시스트가 있으면 이 스타일로 강제하더라도 어시스트 규칙이 항상 우선"
        # 이라는 문구도 같이 넣어 이중으로 보강한다(실험에서 단순히 "어시스트 친화적"
        # 스타일로 좁히기만 해도 가끔 빠지는 사례가 있었음).
        system_prompt = EN_SYSTEM_PROMPT if is_en else SYSTEM_PROMPT
        if is_en:
            has_assist = any(k["assists"] for k in kills_with_names)
            style_pool = EN_ASSIST_FRIENDLY_STYLE_INDICES if has_assist else tuple(range(1, 9))
            style_idx = random.choice(style_pool)
            style_name = EN_STYLE_NAMES[style_idx - 1]
            style_instruction = (
                f"\n\nFor THIS call specifically, you MUST use style #{style_idx} ({style_name}) "
                "from the list above for every line - no other style is acceptable this time."
            )
            if has_assist:
                style_instruction += (
                    " This still does not override the assist rule above: if a kill event has an "
                    "assist, you must still naturally work that assist into the sentence even while "
                    "using this style (e.g. adding a short clause naming the assist) - never drop it."
                )
            system_prompt = system_prompt + style_instruction

        resp = await self.ai_client.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            temperature=0.8,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
        )
        data = json.loads(resp.choices[0].message.content)
        lines = data["lines"]

        covered = {l["event_index"] for l in lines}
        for k in kills_with_names:
            if k["index"] not in covered:
                # 🛡️ [EN 비영문 닉네임 미발화 - 폴백도 표시용 이름 사용] 여기서 원문
                # k['killer']/k['victim']를 그대로 쓰면 비ASCII 닉네임이 폴백 경로로
                # 되살아나 버린다 - 위 facts 블록과 동일하게 display 이름을 쓴다.
                fallback = (
                    f"{k.get('killer_display', k['killer'])} takes down "
                    f"{k.get('victim_display', k['victim'])}!!" if is_en else
                    f"{k['killer']}{_i_or_ga(k['killer'])} {k['victim']}{_eul_or_reul(k['victim'])} 처치했어요!"
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

    async def _fetch_summoner_spell_map(self) -> dict[str, str] | None:
        """소환사 스펠 숫자 key(예: "4")->파일명("SummonerFlash.png") 역매핑을 받아온다.
        참가자별로 매번 다시 받을 필요 없이 로스터 전체에서 한 번만 호출해 공유한다 -
        실패하면 None을 반환하고, 호출부는 맵이 없으면 그 어떤 참가자의 스펠 아이콘도
        시도하지 않고 전부 조용히 건너뛴다(맵 없이는 역매핑 자체가 불가능하므로)."""
        try:
            async with aiohttp.ClientSession() as session:
                version = await self._fetch_ddragon_version(session)
                url = DDRAGON_SUMMONER_SPELL_MAP_URL_TEMPLATE.format(version=version)
                timeout = aiohttp.ClientTimeout(total=DDRAGON_HTTP_TIMEOUT_SECONDS)
                async with session.get(url, timeout=timeout) as resp:
                    resp.raise_for_status()
                    data = await resp.json(content_type=None)
            return {v["key"]: v["image"]["full"] for v in data["data"].values()}
        except Exception as e:
            print(f"[HIGHLIGHT][WARN] Summoner spell map fetch failed: "
                  f"{type(e).__name__}: {e} - skipping spell icons for this render", flush=True)
            return None

    async def _fetch_rune_map(self) -> dict[int, str] | None:
        """룬 숫자 id(예: 8112)->아이콘 경로("perk-images/Styles/.../Electrocute.png")
        역매핑 - 모든 트리/슬롯을 평탄화해서 키스톤이든 아니든 어떤 perk id든 조회
        가능하게 한다(지금은 키스톤만 쓰지만 평탄화 자체는 전체 트리 기준이 더 안전함).
        실패하면 None - 스펠 맵과 동일한 안전 원칙."""
        try:
            async with aiohttp.ClientSession() as session:
                version = await self._fetch_ddragon_version(session)
                url = DDRAGON_RUNES_REFORGED_URL_TEMPLATE.format(version=version)
                timeout = aiohttp.ClientTimeout(total=DDRAGON_HTTP_TIMEOUT_SECONDS)
                async with session.get(url, timeout=timeout) as resp:
                    resp.raise_for_status()
                    trees = await resp.json(content_type=None)
            mapping: dict[int, str] = {}
            for tree in trees:
                for slot in tree.get("slots", []):
                    for rune in slot.get("runes", []):
                        mapping[rune["id"]] = rune["icon"]
            return mapping
        except Exception as e:
            print(f"[HIGHLIGHT][WARN] Rune map fetch failed: "
                  f"{type(e).__name__}: {e} - skipping rune icons for this render", flush=True)
            return None

    async def _fetch_summoner_spell_icon(self, spell_id: int | None,
                                          spell_map: dict[str, str] | None) -> str | None:
        """spell_map이 None(맵 자체 fetch 실패)이거나 spell_id가 없으면 바로 None -
        아이템 아이콘의 item_id=0 사전 필터링과 동일한 패턴."""
        if not spell_id or not spell_map:
            return None
        filename = spell_map.get(str(spell_id))
        if not filename:
            return None
        try:
            cached = glob.glob(os.path.join(SPELL_ICON_CACHE_DIR, f"*_{filename}"))
            if cached:
                return cached[0]
            async with aiohttp.ClientSession() as session:
                version = await self._fetch_ddragon_version(session)
                icon_url = DDRAGON_SPELL_ICON_URL_TEMPLATE.format(version=version, filename=filename)
                return await self._download_and_cache_icon(
                    session, icon_url, SPELL_ICON_CACHE_DIR, f"{version}_{filename}")
        except Exception as e:
            print(f"[HIGHLIGHT][WARN] Summoner spell icon fetch failed (spell_id={spell_id}): "
                  f"{type(e).__name__}: {e} - continuing without icon", flush=True)
            return None

    async def _fetch_rune_icon(self, perk_id: int | None, rune_map: dict[int, str] | None) -> str | None:
        """룬 아이콘 URL은 다른 Data Dragon 아이콘들과 달리 버전 번호가 안 들어간다
        (cdn/img/{경로} - 위 DDRAGON_RUNE_ICON_URL_TEMPLATE 주석 참고) - 그래서
        _fetch_static_icon과 같은 "버전 불필요" 패턴을 쓰되, 캐시 디렉토리는 룬 전용으로
        분리한다(아이콘 경로의 '/'를 '_'로 바꿔 파일명 충돌 없이 캐싱)."""
        if not perk_id or not rune_map:
            return None
        icon_path = rune_map.get(perk_id)
        if not icon_path:
            return None
        cache_name = icon_path.replace("/", "_")
        try:
            cached_path = os.path.join(RUNE_ICON_CACHE_DIR, cache_name)
            if os.path.exists(cached_path):
                return cached_path
            async with aiohttp.ClientSession() as session:
                icon_url = DDRAGON_RUNE_ICON_URL_TEMPLATE.format(icon_path=icon_path)
                return await self._download_and_cache_icon(session, icon_url, RUNE_ICON_CACHE_DIR, cache_name)
        except Exception as e:
            print(f"[HIGHLIGHT][WARN] Rune icon fetch failed (perk_id={perk_id}): "
                  f"{type(e).__name__}: {e} - continuing without icon", flush=True)
            return None

    # ══════════════════════════════════════════════════════════
    #  Riot API (rate limit/재시도는 tier_verify 코그의 공유 리미터+로직을 그대로 재사용)
    # ══════════════════════════════════════════════════════════
    async def _riot_get(self, tv_cog, session: aiohttp.ClientSession, url: str):
        return await tv_cog._riot_request(session, url, extra_headers=BROWSER_USER_AGENT_HEADER)

    async def _verify_candidates_by_kill(self, tv_cog, session: aiohttp.ClientSession, regional_route: str,
                                          candidates: list[dict], mapping: tuple[float, float], duration: float,
                                          guild_id: int, max_verify_n: int, stage_label: str):
        """candidates(이미 호출부가 원하는 순서로 정렬해서 넘김) 상위 max_verify_n개의
        timeline을 하나씩 조회하면서, 클립의 추정 game_ms 구간(_select_kills_in_clip)에
        실제 킬이 있는 첫 후보를 찾는 즉시 멈춘다. 기존에 2차 판별(_pick_match_by_game_
        time_range) 전용으로 인라인돼 있던 로직을 그대로 추출한 것 - 이제 1차 판별도
        creation_time 불명 경로도 전부 이 하나의 구현을 공유한다(로직이 두 군데서
        미묘하게 갈라지는 걸 방지).
        반환: (매치 또는 None, 그 매치의 timeline 또는 None, {matchId: timeline} 캐시).
        전부 킬이 없으면 (None, None, 조회해둔 timeline들) - 호출부가 "가장 가까운 후보로
        폴백"할지 "완전히 실패 처리"할지는 각자 다르므로(1차는 폴백, creation_time 불명
        경로는 날짜 신뢰 근거가 없어 폴백하지 않고 실패 처리) 여기선 검증 결과만 반환하고
        폴백 정책은 호출부에 맡긴다. fetched_timelines를 같이 돌려주는 이유는 호출부가
        폴백하기로 했을 때(candidates[0] 선택) 이미 조회한 timeline이면 재조회를 피하기
        위함."""
        top_candidates = candidates[:max_verify_n]
        fetched_timelines: dict[str, dict] = {}
        for cand in top_candidates:
            cand_id = cand["metadata"]["matchId"]
            timeline_url = f"https://{regional_route}.api.riotgames.com/lol/match/v5/matches/{cand_id}/timeline"
            cand_timeline = await self._riot_get(tv_cog, session, timeline_url)
            fetched_timelines[cand_id] = cand_timeline
            cand_kills = _extract_champion_kills(cand_timeline)
            has_kill = bool(_select_kills_in_clip(cand_kills, mapping, duration))
            print(f"[HIGHLIGHT][INFO] {stage_label} 후보 킬 검증: {cand_id} "
                  f"has_kill_in_range={has_kill} (guild={guild_id})", flush=True)
            if has_kill:
                return cand, cand_timeline, fetched_timelines
        return None, None, fetched_timelines

    # ══════════════════════════════════════════════════════════
    #  /highlight
    # ══════════════════════════════════════════════════════════
    @app_commands.command(name="highlight", description="Turn a gameplay clip into an AI-narrated highlight with real match facts (run /tier_verify first).")
    @app_commands.describe(
        video="mp4, clock top-right, record 7s+ before kill. Watch replay right after the game ends.",
        style="Choose this to make just this clip in a different style. Leave it out to follow the server's default setting.",
    )
    @app_commands.choices(style=[
        app_commands.Choice(name="🇰🇷 LCK 스타일 (한국어)", value="ko"),
        app_commands.Choice(name="🇺🇸 LCS 스타일 (English)", value="en"),
    ])
    @app_commands.checks.cooldown(1, 30.0, key=lambda i: i.user.id)
    async def highlight(self, interaction: discord.Interaction, video: discord.Attachment,
                         style: app_commands.Choice[str] = None):
        guild_id = interaction.guild_id
        await interaction.response.defer(ephemeral=True)

        # 🛡️ [선택적 style 파라미터 - 이번 호출 전체에 일관되게 적용] style을 고르면 길드
        # 설정과 무관하게 이번 한 번만 그 언어로 강제한다. get_msg()(cogs/base.py)가 받는
        # lang_override에 이 값을 그대로 넘기도록, 이 함수 안에서만 쓰는 로컬 클로저로
        # self.get_msg(guild_id, ...)를 감싼다 - 호출부 34곳 전부가 guild_id/override를
        # 매번 안 반복해도 자동으로 같은 값을 쓰게 되어, 하나라도 빠뜨릴 위험이 없다.
        style_override = style.value if style else None
        get_msg = lambda key, **kw: self.get_msg(guild_id, key, lang_override=style_override, **kw)

        tv_cog = self._tier_verify_cog()
        if tv_cog is None:
            await interaction.followup.send(await get_msg("highlight_err_unexpected"), ephemeral=True)
            return

        # 1. 길드 지역 설정 확인 (tier_verify와 동일한 사전 조건, 메시지도 그대로 재사용)
        platform_region = await tv_cog._get_platform_region(guild_id)
        if not platform_region:
            await interaction.followup.send(await get_msg("tier_verify_err_region_not_set"), ephemeral=True)
            return
        regional_route = PLATFORM_TO_REGIONAL.get(platform_region)
        if regional_route is None:
            await interaction.followup.send(await get_msg("tier_verify_err_region_not_set"), ephemeral=True)
            return

        # 2. 티어 인증(puuid) 확인 - party.py의 min_tier 미인증 차단과 동일한 원칙: 비용 발생 전에 막는다
        puuid = await self._get_verified_puuid(guild_id, interaction.user.id)
        if puuid is None:
            await interaction.followup.send(await get_msg("highlight_err_not_verified"), ephemeral=True)
            return

        # 3. 하루 사용 한도 확인 - OCR/TTS 호출(비용 발생)보다 먼저, 첨부파일 다운로드보다도
        # 먼저 막는다. 길드 전체 한도(더 넓은 게이트)를 먼저 보고, 그다음 유저 개인 한도를
        # 본다 - ticket_ai.py와 동일한 순서 원칙. style 선택과 무관하게 항상 guild_id/user_id
        # 기준으로만 집계하므로(한도 자체는 style별로 안 나뉨), 선택 여부가 한도 작동에
        # 영향을 주지 않는다.
        guild_daily_key = f"highlight_daily:guild:{guild_id}"
        if not await self._check_daily_limit(guild_daily_key, HIGHLIGHT_DAILY_LIMIT_GUILD):
            await interaction.followup.send(
                await get_msg("highlight_err_daily_limit_guild", limit=HIGHLIGHT_DAILY_LIMIT_GUILD),
                ephemeral=True,
            )
            return
        user_daily_key = f"highlight_daily:user:{guild_id}:{interaction.user.id}"
        if not await self._check_daily_limit(user_daily_key, HIGHLIGHT_DAILY_LIMIT_USER):
            await interaction.followup.send(
                await get_msg("highlight_err_daily_limit_user", limit=HIGHLIGHT_DAILY_LIMIT_USER),
                ephemeral=True,
            )
            return

        # 4. 첨부파일 형식/크기 확인 (다운로드 전에 메타데이터만으로 판단)
        if not (video.content_type or "").startswith("video/"):
            await interaction.followup.send(await get_msg("highlight_err_invalid_attachment"), ephemeral=True)
            return
        if video.size > MAX_ATTACHMENT_BYTES:
            await interaction.followup.send(await get_msg("highlight_err_invalid_attachment"), ephemeral=True)
            return

        progress_msg = await interaction.followup.send(
            await get_msg("highlight_progress_queued"), ephemeral=True, wait=True
        )

        work_dir = tempfile.mkdtemp(prefix="kyvo_highlight_")
        try:
            async with self.render_semaphore:
                await self._run_pipeline(interaction, guild_id, video, work_dir, progress_msg, tv_cog, regional_route,
                                          puuid, style_override)
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    async def _run_pipeline(self, interaction, guild_id, video, work_dir, progress_msg, tv_cog, regional_route, puuid,
                             style_override=None):
        # 🛡️ [언어 분기 진입점] get_msg()(cogs/base.py)와 완전히 동일한 패턴으로 guild 설정
        # 언어를 한 번만 읽어서 lang 변수로 만들고, 이후 단계(코멘터리 생성/0단계 캐스케이드/
        # 리드인 필러)에 그대로 넘긴다. 디스코드 상태 메시지(get_msg)는 이미 별도로 이 값을
        # 읽고 있어 서로 안 겹치는 두 번째 조회지만, DB가 아니라 캐시된 설정에서 읽으므로
        # 부하 문제는 없다.
        # 🛡️ [style_override 우선] highlight()에서 유저가 style을 명시적으로 골랐으면
        # 길드 설정을 완전히 무시하고 그 값을 그대로 쓴다 - or 단축평가라 style_override가
        # None/빈 문자열이면 자동으로 기존 길드 설정 경로로 폴백한다(회귀 없음). 진행/에러
        # 메시지도 같은 lang을 따르도록, 여기서도 같은 lang_override를 쓰는 로컬 get_msg
        # 클로저를 만든다(highlight()의 것과 동일한 값 - style_override를 그대로 다시 넘김).
        get_msg = lambda key, **kw: self.get_msg(guild_id, key, lang_override=style_override, **kw)
        guild_settings = await self.get_guild_settings(guild_id)
        # 🛡️ [버그 수정 - get_msg()와 동일한 문제] .get("language", "en")은 DB 컬럼이 NULL이라
        # 키는 있고 값만 None인 경우 기본값이 안 먹혀서 lang이 None이 되고, 이후 lang == "en"
        # 비교가 전부 실패해 의도와 무관하게 한국어(else) 분기로 샐 수 있었다.
        lang = style_override or (guild_settings.get("language") or "en")

        video_path = os.path.join(work_dir, "input.mp4")
        await video.save(video_path)

        try:
            duration, creation, (width, height) = await self._to_executor(self._probe_duration_and_creation, video_path)
        except Exception as e:
            print(f"[HIGHLIGHT][ERROR] Failed to probe attachment (guild={guild_id}): {type(e).__name__}: {e}", flush=True)
            await progress_msg.edit(content=await get_msg("highlight_err_invalid_attachment"))
            return

        if duration > MAX_CLIP_DURATION_SECONDS:
            await progress_msg.edit(content=await get_msg("highlight_err_clip_too_long", max=int(MAX_CLIP_DURATION_SECONDS)))
            return

        aspect_ratio = width / height
        if aspect_ratio < MIN_LANDSCAPE_ASPECT_RATIO:
            print(f"[HIGHLIGHT][INFO] Rejected non-landscape aspect ratio {width}x{height} "
                  f"(ratio={aspect_ratio:.3f}, min={MIN_LANDSCAPE_ASPECT_RATIO:.2f}, guild={guild_id})", flush=True)
            await progress_msg.edit(content=await get_msg("highlight_err_unsupported_aspect_ratio"))
            return

        await progress_msg.edit(content=await get_msg("highlight_progress_analyzing"))

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
        # 🛡️ [진단성] 어느 크롭이 최종적으로 성공했는지 나중에(highlight_err_no_kills 진단
        # 로그에서) 알 수 있게 추적만 해둔다 - 동작 자체는 그대로.
        crop_used = "NORMAL"
        try:
            try:
                mapping = await try_crop_ratio(CLOCK_CROP_RATIO_NORMAL)
            except Exception as e:
                print(f"[HIGHLIGHT][INFO] Normal clock crop failed ({type(e).__name__}: {e}) - "
                      f"retrying with replay-viewer crop (guild={guild_id})", flush=True)
                crop_used = "REPLAY"
                mapping = await try_crop_ratio(CLOCK_CROP_RATIO_REPLAY)
        except Exception as e:
            print(f"[HIGHLIGHT][ERROR] Clock OCR/mapping failed (guild={guild_id}): {type(e).__name__}: {e}", flush=True)
            await progress_msg.edit(content=await get_msg("highlight_err_clock_read_failed"))
            return
        if mapping is None:
            print(f"[HIGHLIGHT][ERROR] Clock mapping is None after try/except with no exception raised - "
                  f"this should be unreachable (guild={guild_id})", flush=True)
            await progress_msg.edit(content=await get_msg("highlight_err_clock_read_failed"))
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

                match_pick_stage = "primary"  # 🛡️ [진단성] no_kills 로그에서 참고할 매치 판별 단계 추적
                # 🛡️ [실제 킬 존재 검증용] 선택된 후보의 timeline을 먼저 당겨보고 그 후보가
                # 최종 선택되면, 바로 아래 "timeline 조회" 단계에서 같은 매치를 또 조회하는
                # 중복 호출을 막기 위한 캐시.
                prefetched_timeline = None
                chosen = None

                if creation is None:
                    # 🛡️ [창작 시각 불명 - 날짜 기반 판별 자체를 건너뜀] creation_time
                    # 메타데이터가 없으면 "방금"으로 대체하던 예전 동작이 엉뚱한 최근 매치를
                    # 조용히 통과시키는 구멍이었다(실제 위험 확인됨) - 더 이상 어떤 날짜
                    # 비교도 하지 않는다(1차의 ±2분 창도, 2차의 staleness/거리 폴백도 전부
                    # "믿을 수 있는 시각"이 있어야 의미가 있는데 그게 없으므로). 대신 후보
                    # 폭을 넓혀(count=20) duration/봇 매치만 거르고, by-puuid가 이미 최신순으로
                    # 주는 순서 그대로 상위 MATCH_KILL_VERIFY_TOP_N개의 실제 킬 존재만으로
                    # 판별한다 - 날짜 근접성이라는 신뢰할 수 없는 신호에 기대는 대신, "그
                    # 순간에 실제로 킬이 있었는가"라는 콘텐츠 신호만 받아들인다. 전부 킬이
                    # 없으면(=날짜 신뢰 근거가 없는 상태에서 거리 기준 폴백은 너무 위험하다고
                    # 판단) 거리 기준으로 대충 고르지 않고 명확히 실패 처리한다.
                    print(f"[HIGHLIGHT][INFO] 클립에 creation_time 메타데이터가 없음 - 1차 판별을 "
                          f"건너뛰고 킬 존재 교차검증만으로 매치를 찾습니다 (guild={guild_id})", flush=True)
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

                    no_date_candidates = [md for md in fallback_details
                                           if md["info"]["gameDuration"] * 1000 >= game_ms_end
                                           and not _has_bot_participant(md)]
                    chosen, prefetched_timeline, _ = await self._verify_candidates_by_kill(
                        tv_cog, session, regional_route, no_date_candidates, mapping, duration, guild_id,
                        MATCH_KILL_VERIFY_TOP_N, "창작시각불명")
                    if chosen is None:
                        print(f"[HIGHLIGHT][WARN] 창작 시각 불명 + 킬 교차검증 전부 실패 - "
                              f"game_ms_end={game_ms_end:.0f}ms 이상 진행된 후보 {len(no_date_candidates)}개 "
                              f"전부 해당 시점 킬 없음 (guild={guild_id})", flush=True)
                        await progress_msg.edit(content=await get_msg("highlight_err_match_not_found"))
                        return
                    match_pick_stage = "unknown_creation_verified"
                    print(f"[HIGHLIGHT][INFO] 창작 시각 불명 경로로 매치 선택됨: "
                          f"{chosen['metadata']['matchId']} (guild={guild_id})", flush=True)
                else:
                    primary_candidates = _pick_match_for_clip(details, creation)
                    if primary_candidates:
                        # 🛡️ [1차 판별도 킬 존재 교차검증] 예전엔 창에 맞는 첫 매치를 그냥 바로
                        # 썼다 - 같은 계정이 동시에 두 매치를 뛸 수 없으니 현실적으로 후보는
                        # 거의 항상 1개뿐이라, 이 교차검증은 "틀린 매치를 걸러낸다"기보다
                        # "OCR 매핑이 살짝 어긋나 아예 다른 순간을 가리키는 경우"를 잡아내는
                        # 역할에 가깝다. 어차피 아래에서 timeline을 곧 조회해야 하므로(캐시
                        # 안 하면 이중 조회), 여기서 미리 당겨써도 정상 케이스엔 API 호출이
                        # 늘지 않는다 - 그저 "언제 조회하느냐"만 앞당겨질 뿐.
                        chosen, prefetched_timeline, fetched_timelines_p = await self._verify_candidates_by_kill(
                            tv_cog, session, regional_route, primary_candidates, mapping, duration, guild_id,
                            MATCH_KILL_VERIFY_TOP_N, "1차 판별")
                        if chosen is None:
                            # 날짜(±2분 창)는 이미 신뢰할 수 있는 상태이므로, 2차와 동일한
                            # 철학으로 가장 가까운 후보로 안전하게 폴백한다(완전 실패 처리
                            # 대신) - OCR 매핑 오차 같은 정상적인 노이즈까지 전부 차단하면
                            # 평소에 잘 되던 케이스가 깨질 수 있다.
                            chosen = primary_candidates[0]
                            prefetched_timeline = fetched_timelines_p.get(chosen["metadata"]["matchId"])
                        match_pick_stage = "primary_verified"
                    else:
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
                                reason = "within window but filtered by staleness check"
                            diag_lines.append(
                                f"  {d['metadata']['matchId']}: window=[{window_start.isoformat()}, "
                                f"{window_end.isoformat()}] - {reason}"
                            )
                        print(f"[HIGHLIGHT][WARN] No candidate match window contains clip creation_time "
                              f"(guild={guild_id}):\n" + "\n".join(diag_lines), flush=True)

                        # 🛡️ [2차: creation_time은 있지만 1차 창에 안 맞는 경우 - 주로 리플레이
                        # 뷰어 녹화본] 1차가 실패했을 때만, 후보 폭을 넓혀(count=20) 다시
                        # 조회하고 "클립이 보여주는 게임시각까지 실제로 진행됐는가"로 후보를
                        # 추린 뒤 creation_time이 가장 가까운 걸 고른다. 1차가 성공하는 일반
                        # 클립은 이 블록 자체를 안 타서 조회량이 늘지 않는다.
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

                        ranked_candidates = _pick_match_by_game_time_range(fallback_details, creation, game_ms_end)
                        if not ranked_candidates:
                            print(f"[HIGHLIGHT][WARN] 2차(게임시각+creation_time 근접) 판별도 실패 - "
                                  f"game_ms_end={game_ms_end:.0f}ms 이상 진행된 후보가 {len(fallback_details)}개 "
                                  f"중 없음 (guild={guild_id})", flush=True)
                            await progress_msg.edit(content=await get_msg("highlight_err_match_not_found"))
                            return

                        # 🛡️ [실제 킬 존재 검증 - 오늘 실사고(KR_8393538099 vs KR_8393410432) 수정]
                        # "종료 시각이 가장 가깝다"는 이유만으로 고르면, 게임을 끝낸 뒤 다른 게임을
                        # 더 하고 나서야 리플레이를 녹화한 경우 그 사이에 플레이한 "더 최근에 끝난
                        # 다른 게임"이 실제 정답보다 가까워서 오답으로 뽑히는 사고가 실측으로
                        # 확인됐다. 상위 최대 MATCH_KILL_VERIFY_TOP_N개 후보(그 이상은 확인하지
                        # 않음 - API 호출 상한)의 timeline을 거리가 가까운 순서대로 하나씩 추가
                        # 조회하면서, 클립의 추정 game_ms 구간(_select_kills_in_clip, 기존 로직
                        # 그대로 재사용)에 실제 킬이 있는 첫 후보를 찾는 즉시 멈춘다(조기 종료 -
                        # 정답이 상위권일 때 나머지를 조회하는 낭비가 없음) - 거리 순서보다
                        # "실제 킬 존재"를 우선한다. 후보가 1개뿐이면 이 검증 자체를 스킵해서
                        # (추가 API 호출 0회) 기존과 동일하게 빠르게 처리된다 - 상위 후보 전부
                        # 킬이 없으면 예전과 동일한 "거리가 가장 가까운 것" 안전망으로 폴백한다.
                        chosen = None
                        top_candidates = ranked_candidates[:MATCH_KILL_VERIFY_TOP_N]
                        fetched_timelines = {}  # 🛡️ 폴백 시(전부 킬 없음) 이미 조회한 timeline 재사용용
                        if len(top_candidates) >= 2:
                            chosen, prefetched_timeline, fetched_timelines = await self._verify_candidates_by_kill(
                                tv_cog, session, regional_route, ranked_candidates, mapping, duration, guild_id,
                                MATCH_KILL_VERIFY_TOP_N, "2차 판별")
                        if chosen is None:
                            chosen = ranked_candidates[0]
                            # 검증한 후보 전부 킬이 없어 거리 기준으로 폴백하는 경우 -
                            # ranked_candidates[0]은 top_candidates에 항상 포함되므로(len>=2일 때)
                            # 이미 조회했다면 재조회하지 않는다.
                            prefetched_timeline = fetched_timelines.get(chosen["metadata"]["matchId"])
                        match_pick_stage = "fallback_game_time_range"
                        print(f"[HIGHLIGHT][INFO] 2차 판별로 매치 선택됨: {chosen['metadata']['matchId']} "
                              f"(game_ms_end={game_ms_end:.0f}ms, guild={guild_id})", flush=True)
                match_id = chosen["metadata"]["matchId"]
                if prefetched_timeline is not None:
                    timeline = prefetched_timeline
                else:
                    riot_call_stage = "timeline"
                    riot_call_url = f"https://{regional_route}.api.riotgames.com/lol/match/v5/matches/{match_id}/timeline"
                    timeline = await self._riot_get(tv_cog, session, riot_call_url)
        except RiotAuthError as e:
            print(f"[HIGHLIGHT][CRITICAL] Riot API auth failure (status={e.status}, guild={guild_id})", flush=True)
            await progress_msg.edit(content=await get_msg("highlight_err_riot_auth"))
            return
        except RiotRateLimitedError:
            await progress_msg.edit(content=await get_msg("highlight_err_riot_rate_limited"))
            return
        except RiotServerError as e:
            print(f"[HIGHLIGHT][WARN] Riot server error (status={e.status}, guild={guild_id})", flush=True)
            await progress_msg.edit(content=await get_msg("highlight_err_riot_server_error"))
            return
        except RiotTimeoutError:
            await progress_msg.edit(content=await get_msg("highlight_err_riot_timeout"))
            return
        except RiotNotFoundError:
            print(f"[HIGHLIGHT][WARN] Riot API 404 not found (stage={riot_call_stage}, url={riot_call_url}, "
                  f"guild={guild_id})", flush=True)
            await progress_msg.edit(content=await get_msg("highlight_err_match_not_found"))
            return
        except RiotAPIError as e:
            print(f"[HIGHLIGHT][ERROR] Unexpected Riot API error (guild={guild_id}): {type(e).__name__}: {e}", flush=True)
            await progress_msg.edit(content=await get_msg("highlight_err_unexpected"))
            return

        kills = _extract_champion_kills(timeline)
        names = _participant_id_to_name(chosen)
        selected = _select_kills_in_clip(kills, mapping, duration)
        if not selected:
            # 🛡️ [진단성] "OCR/크롭 자체가 잘못 읽었다" vs "매핑된 시간대엔 정말 킬이 없다"를
            # 로그만 보고 구별할 수 있게, 실패 직전 상태를 전부 남긴다 - 어느 크롭이
            # 최종적으로 성공했는지(crop_used)/그 매핑(slope,intercept)/그 매핑으로 추정된
            # 클립의 game_ms 범위(_select_kills_in_clip과 동일한 slack_sec 적용)/그 범위
            # 안에 든 킬 수(0)/매치 전체 킬 수/매치가 1차(창 기반)·2차(게임시각 근접) 중
            # 어느 단계에서 선택됐는지까지 - 지난 실패 조사에서 이 정보가 하나도 로그에
            # 남지 않아 원인(크롭 오류 vs 진짜 킬 없음)을 구별할 수 없었던 문제를 막는다.
            no_kills_slack_sec = 1.5  # _select_kills_in_clip의 기본 slack_sec과 동일하게 유지
            est_start_ms = _clip_t_to_game_ms(-no_kills_slack_sec, mapping)
            est_end_ms = _clip_t_to_game_ms(duration + no_kills_slack_sec, mapping)
            kills_in_range = sum(1 for k in kills if est_start_ms <= k["timestamp_ms"] <= est_end_ms)
            print(
                f"[HIGHLIGHT][WARN] No kills found in clip's estimated game-time window (guild={guild_id}): "
                f"crop_used={crop_used} mapping(slope,intercept)=({mapping[0]:.2f}, {mapping[1]:.2f}) "
                f"est_game_ms_range=[{est_start_ms:.0f}, {est_end_ms:.0f}] "
                f"kills_in_range={kills_in_range} total_kills_in_match={len(kills)} "
                f"match_id={match_id} match_pick_stage={match_pick_stage}",
                flush=True,
            )
            await progress_msg.edit(content=await get_msg("highlight_err_no_kills"))
            return

        kills_with_names = []
        for i, k in enumerate(selected):
            killer = names.get(k["killer_id"], {}).get("name", "Unknown") if k["killer_id"] else "미니언/포탑"
            victim = names.get(k["victim_id"], {}).get("name", "Unknown")
            assists = [names.get(a, {}).get("name", "Unknown") for a in k["assist_ids"]]
            # 🛡️ [협공 닉네임 샤우팅용] killer_id가 있으면(미니언/포탑 킬이 아니면) 그 팀
            # id(100/200)를 같이 들고 있는다 - 어시스트가 있는 킬일 때 "누구 닉네임"
            # 대신 팀명으로 샤우팅을 바꾸기 위해 필요(아래 hype_nickname_text_by_voice 참고).
            killer_team_id = names.get(k["killer_id"], {}).get("team_id") if k["killer_id"] else None
            # 🛡️ [EN 비영문 닉네임 미발화 - 표시용 이름을 여기서 한 번만 계산] GPT 프롬프트
            # (_generate_commentary)와 _run_pipeline의 검증/폴백/닉네임 샤우팅이 전부 같은
            # 표시용 이름을 써야 일관되므로, kills_with_names를 만드는 이 자리에서 딱 한 번
            # 계산해 공유한다. KO는 원문 이름을 그대로 쓰는 기존 동작과 완전히 동일(회귀 없음) -
            # EN만 _en_display_name으로 치환해, 원문 비ASCII 이름 자체가 GPT 프롬프트에
            # 노출되지 않게 한다("부르지 말라"는 지시에 기대는 대신 애초에 안 보여준다).
            if lang == "en":
                killer_position = names.get(k["killer_id"], {}).get("position") if k["killer_id"] else None
                victim_position = names.get(k["victim_id"], {}).get("position")
                killer_display = _en_display_name(killer, killer_position, "killer")
                victim_display = _en_display_name(victim, victim_position, "victim")
                assist_displays = [
                    _en_display_name(a_name, names.get(a_id, {}).get("position"), "assist")
                    for a_id, a_name in zip(k["assist_ids"], assists)
                ]
            else:
                killer_display, victim_display, assist_displays = killer, victim, list(assists)
            kills_with_names.append({
                "index": i, "timestamp_ms": k["timestamp_ms"],
                "killer": killer, "victim": victim, "assists": assists,
                "killer_display": killer_display, "victim_display": victim_display,
                "assist_displays": assist_displays,
                "killer_team_id": killer_team_id,
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

        # 🛡️ [상단 2단 스코어바용 데이터 - 킬 시점 기준, 추가 Riot API 호출 없음] 예전엔
        # chosen(매치 상세)의 "게임 최종 종료 시점" 누적치를 그대로 썼는데, 화면에 찍히는
        # 시간(킬 시점)과 기준이 달라 사고가 났다(실제 사고 사례: 킬이 4분에 났는데 드래곤
        # "2마리"가 뜸 - 드래곤은 보통 5분 이후 스폰이라 명백한 시점 불일치). timeline은
        # 이미 fetch돼 있으므로(클록 매핑/킬 검증용) 추가 API 호출 없이 _compute_scoreboard_
        # at_time(순수 함수, 실제 매치 2건으로 BUILDING_KILL 팀 반전 등 교차검증됨)으로
        # 킬 시점(game_time_ms) 기준 값을 다시 계산한다. 이 스코어보드는 FIRST BLOOD/
        # SOLO KILL 여부(hud_event)와 무관하게 모든 클립에 항상 표시되는 상시 UI라서
        # hud_event가 None이어도 채운다.
        game_time_ms = selected[0]["timestamp_ms"]
        scoreboard = _compute_scoreboard_at_time(timeline, chosen["info"]["participants"], game_time_ms)
        scoreboard["game_time_ms"] = game_time_ms

        # 🛡️ [드래곤 시간순 속성 시퀀스 - 팀별] timeline은 이미 fetch됨(추가 Riot API 호출
        # 없음). 순수 함수 _extract_dragon_sequence로 팀별 최근 DRAGON_SEQUENCE_MAX개의
        # monsterSubType 리스트를 뽑는다.
        team100_dragon_subtypes = _extract_dragon_sequence(timeline, 100)
        team200_dragon_subtypes = _extract_dragon_sequence(timeline, 200)

        # 🛡️ [하단 포지션별 5행 그리드용 데이터] participants 10명 전원은 chosen에 이미 다
        # fetch돼 있다(추가 Riot API 호출 없음). position(teamPosition)까지 같이 뽑아서
        # _pair_roster_by_position()이 팀 간 매칭에 쓴다.
        # 🛡️ [레벨/CS/KDA - 킬 시점 기준, 아이템은 매치 최종값 유지] 예전엔 레벨만 킬 시점
        # 기준이고 CS/KDA는 "매치 최종값"을 그대로 썼는데, 상단바(scoreboard)는 이미 킬 시점
        # 기준이라 로스터 패널과 기준 시점이 어긋나는 문제가 있었다(실제 검증: 17분대 킬
        # 클립에 23킬 같은 "미래" 최종 스탯이 찍힘). 레벨/CS는 같은 closest_frame 하나로
        # 끝나서 함수를 합쳤다(_compute_participant_frame_stats_at_time). KDA도 전용 순수
        # 함수로 분리(조사 라운드에서 설계/검증 완료, 상단바 킬 스코어와 100% 교차검증됨).
        # 🛡️ [아이템도 킬 시점 반영 - 여러 라운드의 실측 비교 끝에 결정] ITEM_UNDO 재료 복원
        # 버그를 고친 뒤(같은 timestamp DESTROYED 신호만 사용, Data Dragon from 폴백은
        # 실측으로 역효과 확인돼 제외) 20개 매치(~200명) 기준 집합 정확도 82.4%, 그리고
        # "화면이 예산상 불가능한 비율"로 비교하면 매치 최종값(10분 컷오프 88.5% 불가능) vs
        # 킬 시점 재생(0.5% 불가능)으로 압도적 차이가 났다 - 남은 ~18% 오차(대부분 타임라인
        # 이벤트 자체 누락, 코드로 못 고침)보다 "아직 벌지도 않은 돈으로 아이템을 들고 있는"
        # 쪽이 훨씬 눈에 띄는 문제라고 판단해 최종값 대신 킬 시점 재생으로 교체한다.
        # 🛡️ [폴백 안전장치] 재생 함수가 예외를 내거나(알 수 없는 timeline 스키마 변화 등)
        # timeline 자체가 비어있으면(falsy) 기존처럼 매치 최종값으로 폴백한다 - 아이템 패널이
        # 통째로 비는 것보다 "조금 안 맞을 수 있는 최종값"이라도 보여주는 쪽이 안전하다.
        try:
            if not timeline:
                raise ValueError("timeline empty")
            participant_items = _compute_participant_items_at_time(timeline, game_time_ms)
        except Exception as e:
            print(f"[HIGHLIGHT][WARN] 킬 시점 아이템 재생 실패(guild={guild_id}) - "
                  f"매치 최종값으로 폴백. {type(e).__name__}: {e}", flush=True)
            participant_items = None
        participant_frame_stats = _compute_participant_frame_stats_at_time(timeline, game_time_ms)
        participant_kda = _compute_participant_kda_at_time(timeline, game_time_ms)
        roster = []
        for p in chosen["info"]["participants"]:
            pid = p["participantId"]
            frame_stats = participant_frame_stats.get(pid, {})
            final_items = [p.get(f"item{i}", 0) for i in range(6)]
            items = participant_items.get(pid, final_items) if participant_items is not None else final_items
            # 🛡️ [룬/스펠 복원 - 키스톤 id 추출] Match-v5 스키마: perks.styles[0]이
            # primaryStyle(첫 슬롯이 항상 키스톤), 그 안의 selections[0].perk가 키스톤
            # 룬 id - 실제 응답으로 재확인함(위 DDRAGON_RUNE_ICON_URL_TEMPLATE 주석
            # 참고). perks/styles/selections 중 하나라도 비어있으면(이론상 거의 없지만
            # 방어적으로) None으로 안전하게 처리.
            styles = (p.get("perks") or {}).get("styles") or []
            primary_selections = styles[0].get("selections") if styles else None
            keystone_id = primary_selections[0].get("perk") if primary_selections else None
            roster.append({
                "participant_id": pid,
                "team_id": p.get("teamId"),
                "position": p.get("teamPosition") or None,
                "champion": p["championName"],
                "name": p.get("riotIdGameName") or p.get("summonerName") or "Unknown",
                "kda": participant_kda.get(pid, (0, 0, 0)),
                "cs": frame_stats.get("cs", 0),
                "level": frame_stats.get("level", p.get("champLevel", 1)),
                "items": items,
                "spell1_id": p.get("summoner1Id"),
                "spell2_id": p.get("summoner2Id"),
                "keystone_id": keystone_id,
            })
        team100_roster = [r for r in roster if r["team_id"] == 100]
        team200_roster = [r for r in roster if r["team_id"] == 200]
        roster_pairs = _pair_roster_by_position(team100_roster, team200_roster)

        # 🛡️ [라인전 골드 격차 - timeline은 이미 fetch됨, 추가 Riot API 호출 없음]
        # roster_pairs와 정확히 같은 순서로 정렬된 리스트를 만들어서 렌더 단계에서 인덱스만
        # 맞춰 쓰면 되게 한다.
        laning_gold_gaps = _compute_laning_gold_gaps(timeline, roster_pairs)

        # 🛡️ [룬/스펠 복원 - 역매핑용 맵을 먼저 받는다] 참가자별 스펠/룬 아이콘 fetch는
        # 이 맵이 있어야 숫자 id->실제 파일명/경로로 바꿀 수 있어서, 아래 "아이콘 전부
        # 병렬 fetch" 단계보다 먼저 awiat한다 - 로스터 10명이 각자 따로 summoner.json/
        # runesReforged.json을 받으면 낭비이므로 한 번만 받아서 공유. 실패하면 둘 다
        # None이 되고, 아래 _fetch_summoner_spell_icon/_fetch_rune_icon이 맵이 None이면
        # 바로 None을 반환하므로 패널 렌더링 자체엔 영향 없다(해당 아이콘만 조용히 생략).
        spell_map, rune_map = await asyncio.gather(
            self._fetch_summoner_spell_map(), self._fetch_rune_map())

        # 🛡️ [아이콘 전부 병렬 fetch] Data Dragon/Community Dragon 둘 다 Riot API 키/rate
        # limiter와 무관한 별개 CDN이라 전부 동시에 요청해도 안전하다 - asyncio.gather로
        # 한 번에 병렬화. item_id=0(빈 슬롯)은 _fetch_item_icon이 요청 자체를 안 보내고
        # 즉시 None을 반환하므로 안전하게 그대로 넘겨도 된다.
        champion_task = asyncio.gather(*(self._fetch_champion_icon(r["champion"]) for r in roster))
        item_tasks = [asyncio.gather(*(self._fetch_item_icon(item_id) for item_id in r["items"]))
                      for r in roster]
        spell_tasks = [asyncio.gather(
            self._fetch_summoner_spell_icon(r.get("spell1_id"), spell_map),
            self._fetch_summoner_spell_icon(r.get("spell2_id"), spell_map),
        ) for r in roster]
        rune_tasks = [self._fetch_rune_icon(r.get("keystone_id"), rune_map) for r in roster]
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
            *item_tasks, *dragon_variant_tasks, *spell_tasks, *rune_tasks)
        n = len(roster)
        item_icon_lists = rest[:n]
        dragon_variant_icon_paths = rest[n:n + len(dragon_variant_tasks)]
        spell_icon_lists = rest[n + len(dragon_variant_tasks):n + len(dragon_variant_tasks) + n]
        rune_icon_paths = rest[n + len(dragon_variant_tasks) + n:]
        for r, icon_path, item_icon_paths, spell_icon_paths, rune_icon_path in zip(
                roster, champion_icons, item_icon_lists, spell_icon_lists, rune_icon_paths):
            r["icon_path"] = icon_path
            r["item_icon_paths"] = item_icon_paths
            r["spell_icon_paths"] = spell_icon_paths
            r["rune_icon_path"] = rune_icon_path
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

        await progress_msg.edit(content=await get_msg("highlight_progress_scripting"))
        try:
            lines_raw = await self._generate_commentary(
                kills_with_names, lang, roster_pairs=roster_pairs,
                laning_gold_gaps=laning_gold_gaps, scoreboard=scoreboard,
            )
        except Exception as e:
            print(f"[HIGHLIGHT][ERROR] Commentary generation failed (guild={guild_id}): {type(e).__name__}: {e}", flush=True)
            await progress_msg.edit(content=await get_msg("highlight_err_ai_failed"))
            return

        # MAX_KILLS_PER_CLIP=1이라 kills_with_names/lines_raw는 항상 정확히 1건.
        kill_t = kills_with_names[0]["clip_t_sec"]
        killer_name = kills_with_names[0]["killer"]
        victim_name = kills_with_names[0]["victim"]
        # 🛡️ [EN 비영문 닉네임 미발화 - 검증/폴백도 표시용 이름 기준] EN은 GPT에게 애초에
        # killer_display/victim_display(비ASCII면 역할/대명사로 치환됨)만 넘겼으므로, "킬러
        # 이름이 문장에 들어있는지" 검증도 원문이 아니라 표시용 이름 기준이어야 한다 - 원문
        # 기준으로 검증하면 의도적으로 안 부른 비ASCII 이름이 "빠졌다"고 오판되어 폴백이
        # 발동하고, 그 폴백이 원문을 다시 끼워넣어버리는 역효과가 난다(아래 폴백도 동일).
        killer_display = kills_with_names[0].get("killer_display", killer_name)
        victim_display = kills_with_names[0].get("victim_display", victim_name)
        main_fact_text = lines_raw[0]["text"]

        # 🛡️ [킬러 이름 검증 - 3단계 Main 담당] GPT는 온도 0.8로 자유 생성돼서 "킬러 이름을
        # 강조하라"는 프롬프트 지시를 안 따르고 희생자만 부각시킨 문장을 내놓는 경우가 실제로
        # 확인됨 - 코드가 이걸 검증하는 지점이 아예 없었던 게 실질적 원인. 사실 서술 역할이
        # 3단계 Main으로 옮겨왔으므로 검증도 그대로 따라온다. LLM을 재호출하면 비용/시간이 또
        # 드니, 검증 실패 시 즉시 안전한 고정 템플릿으로 대체한다(재시도 없음).
        if not _commentary_names_killer(main_fact_text, killer_display if lang == "en" else killer_name):
            print(f"[HIGHLIGHT][WARN] Commentary text missing killer name (guild={guild_id}) - "
                  f"falling back to template. killer={killer_name!r} killer_display={killer_display!r} "
                  f"text={main_fact_text!r}", flush=True)
            main_fact_text = (
                f"{killer_display} takes down {victim_display}!!" if lang == "en" else
                f"{killer_name}{_i_or_ga(killer_name)} {victim_name}{_eul_or_reul(victim_name)} 처치했어요!"
            )
        elif lang != "en" and not _commentary_avoids_seumnida(main_fact_text):
            # 🛡️ [습니다체 사후 검증 - 킬러 이름 검증과 동일 위치/패턴] SYSTEM_PROMPT가 요체를
            # 강제하지만 GPT가 온도 0.8 자유생성에서 이 지시를 안 지키고 습니다체를 섞어
            # 내놓는 사례가 실제 배포에서 확인됨("완전히 찢어버렸습니다" 등). 킬러 이름
            # 누락과 달리 이건 "그 줄만 다시 GPT에게 물어볼 가치가 있는" 문제라 1회
            # 재생성을 먼저 시도하고, 재시도 결과도 습니다체거나 킬러 이름이 빠지면 그때
            # 비로소 안전한 고정 템플릿(요체)으로 대체한다.
            print(f"[HIGHLIGHT][WARN] Commentary text uses 습니다체 (guild={guild_id}) - "
                  f"retrying once. text={main_fact_text!r}", flush=True)
            retry_text = None
            try:
                retry_lines = await self._generate_commentary(
                    kills_with_names, lang, roster_pairs=roster_pairs,
                    laning_gold_gaps=laning_gold_gaps, scoreboard=scoreboard,
                )
                retry_text = retry_lines[0]["text"]
            except Exception as e:
                print(f"[HIGHLIGHT][WARN] 습니다체 재생성 요청 실패 (guild={guild_id}): "
                      f"{type(e).__name__}: {e}", flush=True)
            if (retry_text is not None and _commentary_avoids_seumnida(retry_text)
                    and _commentary_names_killer(retry_text, killer_name)):
                main_fact_text = retry_text
            else:
                print(f"[HIGHLIGHT][WARN] 재시도도 검증 실패(guild={guild_id}) - 폴백 템플릿 사용. "
                      f"retry_text={retry_text!r}", flush=True)
                main_fact_text = (
                    f"{killer_name}{_i_or_ga(killer_name)} {victim_name}{_eul_or_reul(victim_name)} 처치했어요!"
                )

        await progress_msg.edit(content=await get_msg("highlight_progress_rendering"))

        # ── 1단계(닉네임 샤우팅, 3보이스 동시 콜) + 3단계(Main 사실 전달)만 실시간 TTS
        # (렌더당 ElevenLabs 호출 정확히 4회, asyncio.gather로 병렬) - 나머지 네 자리는
        # 정적 풀에서 고른다.
        # 🛡️ [협공 킬 분기 - 팀명 샤우팅] 어시스트가 있으면(assists 비어있지 않음) 킬러
        # 개인 이름 대신 킬러의 팀명("레드팀"/"블루팀", EN: "Team Red"/"Team Blue")으로
        # 샤우팅 텍스트를 채운다 - 협공을 "누구 하나가 잡았다"처럼 들리게 하지 않기 위함.
        # killer_team_id가 없는 경우(미니언/포탑 킬 등 killer_id=0)는 팀 매핑이 불가능하니
        # 안전하게 기존 동작(킬러 이름 그대로)으로 폴백한다.
        killer_team_id = kills_with_names[0].get("killer_team_id")
        has_assists = bool(kills_with_names[0]["assists"])
        # 🛡️ [EN 비영문 닉네임 미발화 - 닉네임 샤우팅도 동일 원칙] killer_display는
        # kills_with_names 생성 시 이미 계산된 표시용 이름(비ASCII면 역할/대명사로 치환,
        # _en_display_name 참고) - 어시스트가 없어 개인 닉네임을 그대로 외치려던 자리에서도
        # 원문이 영문이 아니면 그대로 외치지 않는다. 팀 매핑이 가능하면(대부분의 경우) 팀명
        # 샤우팅으로 대체하고, 팀 매핑조차 불가능한 극히 드문 경우(killer_id=0, 미니언/포탑
        # 귀속 킬)에만 killer_display의 일반 대명사 폴백("the enemy")을 그대로 외친다.
        non_ascii_en_killer = lang == "en" and not _is_ascii_name(killer_name)
        if has_assists and killer_team_id in TEAM_ID_TO_NAME_KO:
            team_name_map = TEAM_ID_TO_NAME_EN if lang == "en" else TEAM_ID_TO_NAME_KO
            shout_name = team_name_map[killer_team_id]
            is_team_shout = True
        elif non_ascii_en_killer and killer_team_id in TEAM_ID_TO_NAME_KO:
            shout_name = TEAM_ID_TO_NAME_EN[killer_team_id]
            is_team_shout = True
        elif non_ascii_en_killer:
            shout_name = kills_with_names[0].get("killer_display", killer_name)
            is_team_shout = False
        else:
            shout_name = killer_name
            is_team_shout = False
        # 🛡️ [하이픈 늘려 부르기 적용 조건] 개인 닉네임(팀 샤우팅 아님) + 순수 한글 정확히
        # 3음절(NICKNAME_HYPHEN_STRETCH_MAX_LEN 주석 참고 - 4~5음절은 실측으로 중간 무음
        # 문제 발견돼 제외) + KO(EN은 한글 음절 분리 구조 자체가 의미 없어 대상 아님) +
        # _is_pure_hangul(영문 닉네임 "Nyx" 등을 "N-y-x"처럼 글자 단위로 쪼개는 무의미한
        # 표기가 되는 걸 막음, 실측 중 발견된 버그)일 때만 적용.
        use_hyphen_stretch = (
            lang != "en" and not is_team_shout
            and NICKNAME_STRETCH_MIN_LEN <= len(killer_name) <= NICKNAME_HYPHEN_STRETCH_MAX_LEN
            and _is_pure_hangul(killer_name)
        )
        # 🛡️ [보이스별 부분 적용 - sub 중간 무음 재현 확인 후] 하이픈 늘려 부르기를 3보이스
        # 공유 문자열 하나로 보냈더니, sub만 떼서 5회 재테스트해도 5/5 전부 같은 지점
        # (하이픈 이름과 리액션 문장 사이)에서 중간 무음이 재현됐다 - hype 탓이 아니라
        # sub 자체가 이 텍스트 구조와 구조적으로 안 맞는다는 뜻. main/lck_caster_dynamic
        # 둘만 늘림+리액션 텍스트를 받고, sub는 일반 템플릿(볼륨 스웰)을 그대로 받도록
        # 보이스별 텍스트 매핑으로 바꿨다 - 팀명 샤우팅(EN 포함)은 전부 use_hyphen_stretch
        # 가 False라 항상 일반 템플릿만 받는다(회귀 없음).
        plain_nickname_text = HYPE_NICKNAME_SHOUT_TEMPLATE.format(killer=shout_name)
        hype_nickname_text_by_voice = {
            vk: plain_nickname_text
            for vk in ("main", "lck_caster_dynamic", "sub", "sterling", "carter", "atlee")
        }
        if use_hyphen_stretch:
            stretched_nickname_text = HYPE_NICKNAME_SHOUT_STRETCHED_TEMPLATE.format(
                hyphenated=_hyphenate_korean_name(shout_name))
            hype_nickname_text_by_voice["main"] = stretched_nickname_text
            hype_nickname_text_by_voice["lck_caster_dynamic"] = stretched_nickname_text
            # sub는 plain_nickname_text 그대로(기존 볼륨 스웰 경로 유지)
        # 🛡️ [개인 닉네임 타임스트레치 - 글자 수 분기] 팀명 샤우팅은 고정 문자열(레드팀/
        # 블루팀/Team Red/Team Blue) 4개뿐이라 항상 타임스트레치를 적용해도 이미 실측
        # 검증됨(무조건 NICKNAME_SWELL_ATEMPO_RATIO). 이 값은 이제 "볼륨 스웰 경로를 타는
        # 보이스"(sub는 항상, main/lck_caster_dynamic은 use_hyphen_stretch=False일 때만)
        # 전용이라 use_hyphen_stretch 여부와 무관하게 글자 수 기준 그대로 계산한다 -
        # 3~5자(한글 기준 음절 수와 일치)면 스웰 타임스트레치, 2자 이하/6자 이상/EN은
        # atempo=1.0(무변화, 볼륨 스웰만).
        if is_team_shout:
            nickname_atempo_ratio = NICKNAME_SWELL_ATEMPO_RATIO
        elif NICKNAME_STRETCH_MIN_LEN <= len(killer_name) <= NICKNAME_STRETCH_MAX_LEN:
            nickname_atempo_ratio = NICKNAME_SWELL_ATEMPO_RATIO
        else:
            nickname_atempo_ratio = 1.0
        # 🛡️ [스웰 시작 비율도 이름 길이에 맞춰 동적으로] 스트레치 여부와는 별개로,
        # 볼륨 스웰 자체의 시작 지점도 이름이 짧으면 뒤로 밀어야 한다("넥스" 실측 문제) -
        # 이건 스트레치 대상 여부(위 nickname_atempo_ratio)와 무관하게 항상 적용한다.
        nickname_start_ratio = _nickname_swell_start_ratio(shout_name)
        # 🛡️ [3보이스 동시 콜 - KO만 유지, EN은 1보이스로 축소] 하이프 혼자 닉네임을
        # 외치던 것에서 세 캐스터(Main+lck_caster_dynamic+Sub)가 동시에 외치는 것으로
        # 바꿔 임팩트를 키운 건 KO만 그대로 둔다. EN은 Sterling/Carter/Atlee 중 매 렌더
        # 하나만 무작위로 골라 외치게 한다 - 3보이스 동시 콜은 "닉네임을 못 알아듣는다"는
        # 피드백으로 이어질 위험이 있고(특히 아직 서로 다른 세 목소리에 익숙하지 않은
        # 영어 콘텐츠에서), 한 보이스가 또렷하게 외치는 쪽이 지금 단계엔 더 안전하다는
        # 판단. main_fact는 서로 의존관계가 없는 독립 호출이라 그대로 asyncio.gather에
        # 묶는다(콜 수만 4->2로 줄어듦, 로직 변경 없음).
        nickname_voice_keys = (
            (random.choice(("sterling", "carter", "atlee")),) if lang == "en"
            else ("main", "lck_caster_dynamic", "sub")
        )
        try:
            *hype_nickname_wavs_raw, main_fact_wav = await asyncio.gather(
                *(self._synthesize_voice_line(hype_nickname_text_by_voice[vk], vk, work_dir, f"hype_nickname_raw_{i + 1}")
                  for i, vk in enumerate(nickname_voice_keys)),
                self._synthesize_voice_line(main_fact_text, "sterling" if lang == "en" else "main", work_dir, "main_fact"),
            )
        except Exception as e:
            print(f"[HIGHLIGHT][ERROR] ElevenLabs TTS failed (guild={guild_id}): {type(e).__name__}: {e}", flush=True)
            await progress_msg.edit(content=await get_msg("highlight_err_tts_failed"))
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
            atlee_sub_question_file = random.choice(ATLEE_SUB_QUESTION_POOL) if ATLEE_SUB_QUESTION_POOL else None

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
                atlee_sub_question_duration = (
                    await self._to_executor(self._probe_audio_duration, atlee_sub_question_file)
                    if atlee_sub_question_file else 0.0
                )
                hype_nickname_durations = [
                    await self._to_executor(self._probe_audio_duration, raw) for raw in hype_nickname_wavs_raw
                ]
                main_fact_duration = await self._to_executor(self._probe_audio_duration, main_fact_wav)
                # 🛡️ [EN 실시간 합성물 전부 atempo - "쉬지 않고 빠르게 말한다" 핵심 요소]
                # voice_settings.speed는 이 TTS 엔드포인트에서 무시되는 게 확인됐으므로
                # (조사 라운드 결론), KO battle_main/lck/sub 정적 풀과 동일하게 합성 후
                # ffmpeg atempo로 압축한다. main_fact는 스웰 후처리 대상이 아니라 여기서
                # 바로 적용한다.
                if lang == "en":
                    main_fact_atempo_path = os.path.join(work_dir, "main_fact_atempo.wav")
                    main_fact_duration = await self._to_executor(
                        self._apply_atempo, main_fact_wav, main_fact_atempo_path, EN_REALTIME_ATEMPO_RATIO)
                    main_fact_wav = main_fact_atempo_path
                # 닉네임 샤우팅 뒷부분에 볼륨 스웰+타임스트레치 후처리(3개 파일 각각 적용) -
                # tail이 실제로 늘어나므로 hype_nickname_durations를 반환된 새 길이로 갱신한다
                # (이 리스트를 그대로 쓰는 아래 plan_kill_sequence/end_times가 늘어난 길이를
                # 반영해야 뒤가 안 잘림).
                hype_nickname_wavs = []
                for i, (raw, dur) in enumerate(zip(hype_nickname_wavs_raw, hype_nickname_durations)):
                    swelled = os.path.join(work_dir, f"hype_nickname_{i + 1}.wav")
                    new_dur = await self._to_executor(
                        self._apply_nickname_swell, raw, dur, swelled,
                        nickname_atempo_ratio, nickname_start_ratio)
                    hype_nickname_durations[i] = new_dur
                    hype_nickname_wavs.append(swelled)
                # 🛡️ [닉네임도 EN이면 추가 atempo] 스웰 후처리가 끝난 결과물 위에 한 번 더
                # 압축한다 - 스웰의 볼륨 램프/타임스트레치 가정(시작 비율 등)은 이미 끝난
                # 뒤라 서로 간섭하지 않는다(단순 전체 길이 압축만 추가).
                if lang == "en":
                    for i, (swelled, dur) in enumerate(zip(hype_nickname_wavs, hype_nickname_durations)):
                        atempo_path = os.path.join(work_dir, f"hype_nickname_{i + 1}_atempo.wav")
                        new_dur = await self._to_executor(
                            self._apply_atempo, swelled, atempo_path, EN_REALTIME_ATEMPO_RATIO)
                        hype_nickname_wavs[i] = atempo_path
                        hype_nickname_durations[i] = new_dur
            except Exception as e:
                print(f"[HIGHLIGHT][ERROR] Failed to probe/post-process voice lines (guild={guild_id}): "
                      f"{type(e).__name__}: {e}", flush=True)
                await progress_msg.edit(content=await get_msg("highlight_err_render_failed"))
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

            # 1/2/3단계: sub_question은 이제 ATLEE_SUB_QUESTION_POOL이 생겨서 채운다(풀이 비어
            #있으면 atlee_sub_question_duration=0.0이라 plan_kill_sequence가 "즉시 끝난 것"으로
            # 계산해 예전과 동일하게 우아히 스킵된다 - 하드 실패 없음). 스케줄 키는 한국어와
            # 동일하게 "sub_question" 그대로 재사용(별도 키 불필요, _render_video는 언어를 모름).
            # 🛡️ [3보이스 동시 콜] stage1 길이는 세 닉네임 목소리 중 가장 긴 것 기준(max) -
            # 셋 다 hype_nickname_start에 동시 시작하므로, 다음 단계(2/3단계)가 밀리는 시점은
            # 가장 늦게 끝나는 목소리에 맞춰야 한다.
            seq = plan_kill_sequence(stage0_dur, max(hype_nickname_durations), atlee_sub_question_duration)
            hype_nickname_start = kill_t + seq["t1"]
            sub_question_start = kill_t + seq["t2"]
            main_fact_start = kill_t + seq["t3"]
            for i, (wav, dur) in enumerate(zip(hype_nickname_wavs, hype_nickname_durations)):
                schedule[f"hype_nickname_{i + 1}"] = {"wav": wav, "text": hype_nickname_text_by_voice[nickname_voice_keys[i]],
                                                       "start": hype_nickname_start, "duration": dur}
            if atlee_sub_question_file is not None:
                schedule["sub_question"] = {
                    "wav": atlee_sub_question_file,
                    "text": ATLEE_SUB_QUESTION_TEXT[os.path.basename(atlee_sub_question_file)],
                    "start": sub_question_start, "duration": atlee_sub_question_duration,
                }
            schedule["main_fact"] = {"wav": main_fact_wav, "text": main_fact_text,
                                      "start": main_fact_start, "duration": main_fact_duration}

            # 🛡️ [EN 리드인 재설계 - 단일 긴 내레이션, "여러 보이스 체인"은 폐기]
            # 지난 라운드의 "여러 보이스가 짧게 겹쳐 떠드는 체인"(KO BATTLE_MAIN_POOL류
            # 재사용)은 완전히 잘못된 방향이었다는 피드백으로 전면 재설계 - 실제 LCK/LCS는
            # 해설자 한 명이 빌드업~킬 임박 직전까지 끊김 없이 이어서 말한다. 기존
            # EN_LEADIN_POOL(정적 풀, 1~4개 유동 배치)과 en_battle_carter/atlee 체인
            # 스케줄링을 이 블록으로 완전히 대체한다 - 둘 다 Sterling 보이스와 겹쳐 쓰면
            # (EN_LEADIN_POOL도 Sterling 목소리) 같은 목소리가 서로 다른 말을 동시에 하는
            # 꼴이 되어 반드시 제거해야 했다. EN_LEADIN_POOL/EN_BATTLE_*_POOL 상수/에셋
            # 자체는 지우지 않았다(재사용 가능성 남김) - 단지 이 경로에서 더 이상 호출하지
            # 않는다.
            # 🛡️ [Sterling의 기존 "끝점=kill_t 고정" 공식을 그대로 확장] 끝점은
            # kill_t - EN_LEADIN_END_GAP_SEC(기존 상수 재사용)로 고정하고, 시작점은 실측
            # 길이로 역산한다 - 사전에 길이를 못박지 않고도 "항상 kill_t 근처에서 끝난다"를
            # 보장하는 이 코드베이스의 기존 철학 그대로.
            leadin_end_times = []
            en_narration_end = kill_t - EN_LEADIN_END_GAP_SEC
            en_narration_available = en_narration_end - EN_LEADIN_START_OFFSET_SEC
            if en_narration_available >= EN_NARRATION_MIN_AVAILABLE_SEC:
                narration_text = await self._generate_leadin_narration(en_narration_available)
                narration_wav = await self._synthesize_voice_line(
                    narration_text, "sterling", work_dir, "en_narration_raw")
                narration_duration = await self._to_executor(self._probe_audio_duration, narration_wav)
                # 🛡️ [가용 시간보다 길면 - 자르기 대신 약한 atempo 보정] 필요한 압축
                # 비율만큼만 적용하되 EN_NARRATION_MAX_ATEMPO_RATIO(1.3)를 상한으로 clamp -
                # 그래도 못 맞으면(가용 시간이 지나치게 짧음) 자연스러움을 해치는 과한
                # 압축 대신 시작점을 EN_LEADIN_START_OFFSET_SEC에서 고정하고 약간의
                # 침범을 감내한다(완전히 잘라내는 것보다 낫다고 판단).
                if narration_duration > en_narration_available:
                    needed_ratio = narration_duration / en_narration_available
                    atempo_ratio = min(EN_NARRATION_MAX_ATEMPO_RATIO, needed_ratio)
                    atempo_path = os.path.join(work_dir, "en_narration_atempo.wav")
                    narration_duration = await self._to_executor(
                        self._apply_atempo, narration_wav, atempo_path, atempo_ratio)
                    narration_wav = atempo_path
                narration_start = max(EN_LEADIN_START_OFFSET_SEC, en_narration_end - narration_duration)
                # 🛡️ [새 긴 내레이션 전용 무음 판정] 기존 짧은 발화 게이트(d=0.15)는 안
                # 쓴다 - 문장 사이 자연스러운 숨쉬기 무음이 당연히 여러 번 생기므로, 그보다
                # 긴(d=0.4 이상) 구간만 "비정상 무음"으로 집계해 경고 로그만 남긴다(렌더
                # 자체를 막지는 않음 - 재시도해도 같은 문제가 반복될 수 있고, 실시간
                # 경로라 재합성 비용도 있어 "일단 쓰고 로그로 남긴다"는 기존 하이픈
                # 스트레치 트림 실패 처리와 같은 원칙).
                n_long_silences = await self._to_executor(self._count_long_silences, narration_wav)
                if n_long_silences > 0:
                    print(f"[HIGHLIGHT][WARN] EN narration has {n_long_silences} long silence(s) "
                          f"(guild={guild_id}, >0.4s) - using as-is. text={narration_text!r}", flush=True)
                schedule["en_narration"] = {
                    "wav": narration_wav, "text": narration_text,
                    "start": narration_start, "duration": narration_duration,
                }
                leadin_end_times.append(narration_start + narration_duration)

                # 🛡️ [제3자 짧은 리액션 - main_fact 재생 도중으로 앵커 이전] 기존엔 "내레이션
                # 종료 직전"(킬 이전)에 걸려 있었으나, "Sterling이 결과(main_fact)를 말하는
                # 동안 옆에서 웃는다"는 그림에 맞춰 main_fact 시작 직후로 옮겼다 - main_fact_
                # start/duration은 이 블록보다 먼저(위쪽에서) 이미 계산되어 있으므로 그대로
                # 참조만 하면 된다. "Oh!?"/"Whoa!"/"Come on!"/"Yes!!"(f~i) + "Whoa-ho-ho!!"/
                # "Wooo-hoo!!"/"Oho-ho-ho!!"/"Ho-ho, unbelievable!!"(j~m, 호탕한 웃음) 총 8개
                # 중 보이스 하나를 무작위로 골라 한 번만 넣는다 - "제3의 해설자가 짧게
                # 리액션만 얹는다"는 설계는 그대로, 겹치는 대상만 내레이션 -> main_fact로 교체.
                reaction_voice = random.choice(("carter", "atlee"))
                reaction_pool = EN_BATTLE_CARTER_POOL if reaction_voice == "carter" else EN_BATTLE_ATLEE_POOL
                reaction_text_map = EN_BATTLE_CARTER_TEXT if reaction_voice == "carter" else EN_BATTLE_ATLEE_TEXT
                short_files = _filter_short_interjection_pool(reaction_pool)
                if short_files:
                    reaction_file = random.choice(short_files)
                    reaction_duration = await self._to_executor(self._probe_audio_duration, reaction_file)
                    reaction_start = main_fact_start + 0.3
                    schedule[f"en_reaction_{reaction_voice}"] = {
                        "wav": reaction_file,
                        "text": reaction_text_map[os.path.basename(reaction_file)],
                        "start": reaction_start, "duration": reaction_duration,
                    }
                    leadin_end_times.append(reaction_start + reaction_duration)

            end_times = stage0_end_times + leadin_end_times + [
                hype_nickname_start + dur for dur in hype_nickname_durations
            ] + [main_fact_start + main_fact_duration]
            if atlee_sub_question_file is not None:
                end_times.append(sub_question_start + atlee_sub_question_duration)
            total_duration = max(duration, max(end_times) + RENDER_TAIL_BUFFER_SEC)
            schedule["total_duration"] = total_duration
        else:
            if not (PRE_BUILDUP_POOL and MAIN_EXPLODE_POOL
                    and HYPE_EXPLODE_POOL and SUB_EXPLODE_POOL and SUB_QUESTION_POOL):
                print(f"[HIGHLIGHT][CRITICAL] Static voice pool missing files (guild={guild_id}): "
                      f"pre_buildup={len(PRE_BUILDUP_POOL)} "
                      f"main_explode={len(MAIN_EXPLODE_POOL)} hype_explode={len(HYPE_EXPLODE_POOL)} "
                      f"sub_explode={len(SUB_EXPLODE_POOL)} sub_question={len(SUB_QUESTION_POOL)}", flush=True)
                await progress_msg.edit(content=await get_msg("highlight_err_unexpected"))
                return

            # 🛡️ [N슬롯화 - 이전 라운드] 상황멘트를 1개 고정 대신 en_leadin과 동일한 패턴
            # (최대 PRE_BUILDUP_MAX_COUNT개)으로 쓴다.
            # 🛡️ [N-먼저-추정 재구성] 예전엔 파일을 먼저 뽑고(_sample_pre_buildup_distinct_
            # openers, 추임새 중복 방지 포함) 그 실제 길이로 자리 개수(N)를 나중에
            # 계산했다(_spread_fillers_evenly) - 이러면 N이 후보 개수보다 줄어들 때 schedule
            # 조립이 뒤쪽 후보를 조용히 버리는 구조라, "마지막 자리는 항상 특정 풀에서"라는
            # 보장을 할 수 없었다. 순서를 뒤집어 PRE_BUILDUP_POOL 전체 평균 길이("대표
            # 길이")로 N을 먼저 추정(_estimate_pre_buildup_count)하고, N이 확정된 뒤에
            # 정확히 N개(N-1개는 차분 풀, 마지막 1개는 PRE_BUILDUP_URGENT_POOL)만
            # 뽑는다(_pick_pre_buildup_slots, 추임새 중복 방지는 그 안에서 그대로 유지) -
            # 대표 길이와 실제 뽑힐 파일 길이가 달라 N이 ±1 오차 날 수 있음은 감내 가능한
            # 수준으로 판단, 별도 보정 없음.
            sub_question_file = random.choice(SUB_QUESTION_POOL)

            try:
                pre_buildup_available = plan_lead_in_forward_eoeo(kill_t)
                pre_buildup_pool_durations = [
                    await self._to_executor(self._probe_audio_duration, f) for f in PRE_BUILDUP_POOL
                ]
                avg_pre_buildup_dur = (
                    sum(pre_buildup_pool_durations) / len(pre_buildup_pool_durations)
                    if pre_buildup_pool_durations else 0.0
                )
                pre_buildup_slot_count = _estimate_pre_buildup_count(
                    pre_buildup_available, avg_pre_buildup_dur, PRE_BUILDUP_GAP_SEC, PRE_BUILDUP_MAX_COUNT)
                pre_buildup_candidates = _pick_pre_buildup_slots(
                    pre_buildup_slot_count, PRE_BUILDUP_POOL, PRE_BUILDUP_URGENT_POOL, PRE_BUILDUP_OPENER)
                pre_buildup_durations = [
                    await self._to_executor(self._probe_audio_duration, f) for f in pre_buildup_candidates
                ]
                # 🛡️ [0단계 환호 비중 확대 - 길이 가중 랜덤] "환호 비중을 늘려달라"는 요청에
                # 텍스트에 모음을 더 반복해서 새 긴 버전을 만들어보는 방법을 먼저 시도했으나,
                # 실측 결과 무음 게이트 통과율이 급격히 떨어지고(main_explode 신규 후보
                # 0/10, sub_shout 신규 후보 0/10 통과, hype만 7/10) 통과해도 기존 풀의
                # 최장 파일보다 길다는 보장조차 없었다(모음을 더 늘려도 실제 발화 길이가
                # 비례해서 늘어나지 않음 - 이 목소리/모델에서 텍스트 모음 반복으로 길이를
                # 통제하는 건 신뢰할 수 없다는 걸 재확인, 이름 늘려 부르기 실패 사례와 같은
                # 패턴). 새 녹음 없이도 안전하게 "평균 환호 길이"를 늘릴 수 있는 대안으로,
                # 이미 무음 게이트를 통과한 기존 풀 파일들을 길이에 비례한 가중치로 뽑는다
                # (긴 파일이 더 자주 걸리게) - 새 TTS 호출도, 무음 게이트 리스크도 없다.
                main_explode_durations_all = [
                    await self._to_executor(self._probe_audio_duration, f) for f in MAIN_EXPLODE_POOL
                ]
                hype_explode_durations_all = [
                    await self._to_executor(self._probe_audio_duration, f) for f in HYPE_EXPLODE_POOL
                ]
                sub_explode_durations_all = [
                    await self._to_executor(self._probe_audio_duration, f) for f in SUB_EXPLODE_POOL
                ]
                main_explode_idx = random.choices(range(len(MAIN_EXPLODE_POOL)), weights=main_explode_durations_all, k=1)[0]
                hype_explode_idx = random.choices(range(len(HYPE_EXPLODE_POOL)), weights=hype_explode_durations_all, k=1)[0]
                sub_explode_idx = random.choices(range(len(SUB_EXPLODE_POOL)), weights=sub_explode_durations_all, k=1)[0]
                main_explode_file = MAIN_EXPLODE_POOL[main_explode_idx]
                hype_explode_file = HYPE_EXPLODE_POOL[hype_explode_idx]
                sub_explode_file = SUB_EXPLODE_POOL[sub_explode_idx]
                main_explode_duration = main_explode_durations_all[main_explode_idx]
                hype_explode_duration = hype_explode_durations_all[hype_explode_idx]
                sub_explode_duration = sub_explode_durations_all[sub_explode_idx]
                hype_nickname_durations = [
                    await self._to_executor(self._probe_audio_duration, raw) for raw in hype_nickname_wavs_raw
                ]
                main_fact_duration = await self._to_executor(self._probe_audio_duration, main_fact_wav)
                sub_question_duration = await self._to_executor(self._probe_audio_duration, sub_question_file)
                # 🛡️ [보이스별 후처리 분기 - sub만 기존 방식 유지] use_hyphen_stretch여도
                # 더 이상 3개 전부 트림하지 않는다 - sub는 하이픈+리액션 텍스트를 애초에
                # 안 받으므로(hype_nickname_text_by_voice) 항상 기존 볼륨 스웰/타임스트레치
                # 경로(_apply_nickname_swell)를 타고, main/lck_caster_dynamic만 늘린
                # 텍스트를 받았을 때 트레일링 무음 트림(_trim_trailing_silence)을 쓴다.
                # 어느 쪽이든 길이가 바뀔 수 있으므로 hype_nickname_durations를 반환값으로
                # 갱신한다(아래 plan_kill_sequence/end_times가 늘어난 길이를 반영해야
                # 뒤가 안 잘림).
                hype_nickname_wavs = []
                for i, (raw, dur) in enumerate(zip(hype_nickname_wavs_raw, hype_nickname_durations)):
                    out_path = os.path.join(work_dir, f"hype_nickname_{i + 1}.wav")
                    vk = nickname_voice_keys[i]
                    if use_hyphen_stretch and vk in ("main", "lck_caster_dynamic"):
                        new_dur = await self._to_executor(self._trim_trailing_silence, raw, dur, out_path)
                    else:
                        new_dur = await self._to_executor(
                            self._apply_nickname_swell, raw, dur, out_path,
                            nickname_atempo_ratio, nickname_start_ratio)
                    hype_nickname_durations[i] = new_dur
                    hype_nickname_wavs.append(out_path)
            except Exception as e:
                print(f"[HIGHLIGHT][ERROR] Failed to probe/post-process voice lines (guild={guild_id}): "
                      f"{type(e).__name__}: {e}", flush=True)
                await progress_msg.edit(content=await get_msg("highlight_err_render_failed"))
                return

            # 0단계: Main+Hype+Sub 셋 다 kill_t 근처(0~150ms 각자 독립 랜덤 오프셋, 완전
            # 동시 시작이 "한 명처럼 들린다"는 문제의 원인일 수 있다고 판단해 추가 -
            # STAGE0_OFFSET_MAX_SEC 주석 참고)에서 시작(닉네임 없는 순수 폭발).
            # plan_kill_sequence()는 순수 함수 - 1/2/3단계 시작을 "직전 단계 최장 목소리 길이 ×
            # STAGE_OVERLAP_RATIO" 지점으로 잡는다(고정 초 아님, 0단계 길이와 무관하게 1/2/3단계
            # 상호 간격은 각자 자기 길이 × 비율로만 정해진다 - 0단계가 길어져도 t2-t1/t3-t2 간격
            # 자체는 안 변하고, 셋 다 kill_t 기준으로 똑같이 더 뒤로 밀릴 뿐이다).
            # 🛡️ [stage0_dur 계산 수정 - 오프셋 반영] 예전엔 셋 다 kill_t에서 동시 시작해
            # "길이의 최댓값=종료 시점의 최댓값"이 성립했지만, 트랙마다 시작이 달라지는
            # 지금은 "시작+길이"의 최댓값으로 계산해야 한다(_stage0_duration).
            main_explode_start, hype_explode_start, sub_explode_start = _stage0_track_starts(kill_t)
            stage0_dur = _stage0_duration(
                (main_explode_start, hype_explode_start, sub_explode_start),
                (main_explode_duration, hype_explode_duration, sub_explode_duration),
                kill_t)
            # 🛡️ [전투 지속 리액션 - pre_buildup_slot_count(N)>=2일 때만] 설계 검토에서
            # 확정된 결론 그대로 - N을 새 임계값 없이 그대로 재사용한다. 0단계->1단계
            # 전환 지점(stage0_dur*STAGE_OVERLAP_RATIO)에 앵커링해 0단계와 같은 패턴
            # (_stage0_track_starts)으로 3보이스가 서로 다른 문구를 겹쳐 외치고, 그
            # 길이(battle_reaction_dur)만큼 plan_kill_sequence가 1~3단계 전체를 뒤로
            # 민다 - 조건 미충족/풀 비어있음이면 battle_reaction_dur=0.0 그대로라 기존
            # 공식과 완전히 동일(회귀 없음).
            battle_reaction_dur = 0.0
            battle_reaction_starts: dict[str, float] = {}
            battle_reaction_files: dict[str, str] = {}
            battle_reaction_durations: dict[str, float] = {}
            if BATTLE_REACTION_ENABLED and pre_buildup_slot_count >= BATTLE_REACTION_MIN_PRE_BUILDUP_SLOTS:
                battle_reaction_files = _pick_battle_reaction_files() or {}
            if battle_reaction_files:
                battle_anchor = kill_t + stage0_dur * STAGE_OVERLAP_RATIO
                voice_order = list(battle_reaction_files.keys())
                raw_starts = _stage0_track_starts(battle_anchor)
                battle_reaction_starts = dict(zip(voice_order, raw_starts))
                battle_reaction_durations = {
                    vk: await self._to_executor(self._probe_audio_duration, path)
                    for vk, path in battle_reaction_files.items()
                }
                battle_reaction_dur = _stage0_duration(
                    tuple(battle_reaction_starts[vk] for vk in voice_order),
                    tuple(battle_reaction_durations[vk] for vk in voice_order),
                    battle_anchor)
            # 🛡️ [3보이스 동시 콜] stage1 길이는 세 닉네임 목소리 중 가장 긴 것 기준(max) -
            # 셋 다 hype_nickname_start에 동시 시작하므로, 다음 단계가 밀리는 시점은 가장
            # 늦게 끝나는 목소리에 맞춰야 한다.
            seq = plan_kill_sequence(stage0_dur, max(hype_nickname_durations), sub_question_duration,
                                      battle_reaction_dur)
            hype_nickname_start = kill_t + seq["t1"]
            sub_question_start = kill_t + seq["t2"]
            main_fact_start = kill_t + seq["t3"]

            # 킬 이전 리드인: 클립 시작(t=0) 기준으로 상황 멘트(1~PRE_BUILDUP_MAX_COUNT개,
            # 자리가 허락하는 만큼)를 배치한다. available 계산은 위에서
            # plan_lead_in_forward_eoeo로 이미 끝냈으므로, 여기서는 그 결과
            # (pre_buildup_available)와 이미 확정된 자리 개수(len(pre_buildup_candidates))로
            # 오프셋만 펼친다(_spread_fixed_n) - 실제 파일 길이로 자리 개수를 다시 계산하지
            # 않는다(마지막 슬롯이 조용히 잘리는 걸 막기 위함).
            pre_buildup_starts = _spread_fixed_n(
                PRE_BUILDUP_START_OFFSET_SEC, pre_buildup_available, len(pre_buildup_candidates))

            # 🛡️ [리드인 2보이스 겹침] LEADIN_OVERLAY_CHANCE 확률로만 시도한다 - 매번 나오면
            # 오히려 예측 가능한 패턴이 되어버리니 "가끔" 정도로만. 자리가 없으면(리드인 자체가
            # 스킵된 클립) pick_leadin_overlay_start가 None을 반환해 조용히 스킵된다.
            leadin_overlay_file = None
            leadin_overlay_start = None
            leadin_overlay_duration = 0.0
            if LEADIN_OVERLAY_POOL and random.random() < LEADIN_OVERLAY_CHANCE:
                leadin_overlay_file = random.choice(LEADIN_OVERLAY_POOL)
                leadin_overlay_duration = await self._to_executor(self._probe_audio_duration, leadin_overlay_file)
                # 🛡️ [EOEO 제거 후 - 항상 pre_buildup 폴백 경로] eoeo_start=None을 넘기면
                # pick_leadin_overlay_start가 원래 갖고 있던 "EOEO 없을 때" 폴백 분기(마지막
                # pre_buildup 슬롯 중간 지점)를 그대로 타게 된다 - 그 분기는 EOEO 유무와
                # 무관하게 이미 안전하게 동작하던 경로라 함수 자체는 손대지 않았다.
                leadin_overlay_start = pick_leadin_overlay_start(
                    pre_buildup_starts, pre_buildup_durations, None, 0.0,
                    leadin_overlay_duration, kill_t)
                if leadin_overlay_start is None:
                    leadin_overlay_file = None

            # 🛡️ [리드인 전투 리액션 - 올바른 자리] pre_buildup_slot_count(N)>=2일 때만,
            # 이미 배치된 상황 멘트 슬롯 하나의 시작 시점에 main+sub 2보이스를 겹쳐
            # 넣는다. 기존 pre_buildup_starts는 전혀 안 바꾸고(새 구간을 만들어 뒤로
            # 미는 0단계 방식이 아니다), kill_t - EOEO_GAP_SEC을 넘기면 조용히
            # 스킵한다(억지로 자르지 않는다는 기존 리드인 원칙 그대로).
            leadin_battle_files: dict[str, str] = {}
            leadin_battle_starts: dict[str, float] = {}
            leadin_battle_durations: dict[str, float] = {}
            leadin_battle_texts: dict[str, str] = {}
            if (BATTLE_LEADIN_OVERLAY_ENABLED
                    and pre_buildup_slot_count >= BATTLE_REACTION_MIN_PRE_BUILDUP_SLOTS):
                last_slot_is_urgent = bool(pre_buildup_candidates) and pre_buildup_candidates[-1] in PRE_BUILDUP_URGENT_POOL
                anchor_time = pick_leadin_battle_anchor(
                    pre_buildup_starts, pre_buildup_durations, last_slot_is_urgent)
                if anchor_time is not None and BATTLE_MAIN_POOL and BATTLE_SUB_POOL:
                    # 🛡️ [여백을 끊김 없이 채우는 연속 재생] 문구 하나만 겹쳐 넣고 나머지
                    # 여백을 비워두던 방식(재뽑기로 "맞으면 넣고 안 맞으면 스킵") 대신,
                    # main/sub 각자 자기 몫의 시작 시점부터 킬 직전(kill_t - EOEO_GAP_SEC)
                    # 까지 짧은 문구를 gap_sec(0.1~0.2s) 간격으로 계속 이어 붙인다
                    # (_pack_reaction_chain). 🛡️ [EOEO 제거로 종료 한계 변경] 예전엔 EOEO가
                    # 고정으로 킬 직전을 차지해서 그 시작 시점(eoeo_start)까지만 채웠는데,
                    # EOEO를 아예 없앴으므로 이제 리액션이 kill_t - EOEO_GAP_SEC(기존 "EOEO
                    # 종료~킬 안전 여백" 0.6초를 범용 안전 여백으로 재사용)까지 직접 이어진다
                    # - 리액션이 끊김 없이 킬 순간 직전까지 쭉 이어지는 그림이 된다. 이어 붙인
                    # 결과는 wave 모듈로 미리 하나의 파일로 합쳐(_concat_wav_chain) 기존
                    # schedule 구조(보이스당 파일 1개)를 그대로 재사용한다.
                    # 🛡️ [urgent 겹침 전환 - "뚝 끊김" 완화] "urgent 혼자 조용히 끝남 ->
                    # 갑자기 2인 합창 시작"으로 전환이 뚝 끊기던 문제를, urgent가 끝나기
                    # 0.3~0.5초 전부터 main/sub 중 하나(랜덤)가 짧은 추임새(f~i)로 먼저
                    # 겹쳐 들어오게 해서 완화한다(pick_urgent_preoverlap_starts) - 이
                    # 함수가 한 명씩 합류(이전 라운드) 로직을 대체한다. 먼저 끼어드는 쪽이
                    # 그대로 체인의 "첫 합류자"를 겸해서 같은 목소리가 끊김 없이 계속 말하는
                    # 느낌을 노리고, 나머지 하나는 기존처럼 anchor+0.3~0.6초 뒤에 합류한다.
                    # 늦게 합류하는 쪽은 그만큼 채울 시간이 줄어들지만, 그 처리는
                    # _pack_reaction_chain의 available(=end_limit-start_time) 계산에 이미
                    # 들어있어 여기서 추가로 신경 쓸 게 없다.
                    preoverlap_result = pick_urgent_preoverlap_starts(anchor_time)
                    preoverlap_voice = preoverlap_result["preoverlap_voice"]
                    main_jitter_start = preoverlap_result["starts"]["main"]
                    sub_jitter_start = preoverlap_result["starts"]["sub"]
                    main_pool_durations = {
                        os.path.basename(p): await self._to_executor(self._probe_audio_duration, p)
                        for p in BATTLE_MAIN_POOL
                    }
                    sub_pool_durations = {
                        os.path.basename(p): await self._to_executor(self._probe_audio_duration, p)
                        for p in BATTLE_SUB_POOL
                    }
                    reaction_end_limit = kill_t - EOEO_GAP_SEC

                    # 🛡️ [끼어드는 쪽은 짧은 추임새로 체인을 시작] preoverlap_voice만 첫
                    # 항목을 짧은 끼어들기 추임새(f~i)로 강제하고, 그 뒤 남는 시간은 기존
                    # _pack_reaction_chain으로 그대로 채운다 - 긴 반복형 문구(a~e)로
                    # 시작하면 urgent 본문을 가릴 만큼 커질 수 있어 피한다.
                    pool_durations_by_voice = {"main": main_pool_durations, "sub": sub_pool_durations}
                    starts_by_voice = {"main": main_jitter_start, "sub": sub_jitter_start}
                    short_main = {os.path.basename(p) for p in _filter_short_interjection_pool(BATTLE_MAIN_POOL)}
                    short_sub = {os.path.basename(p) for p in _filter_short_interjection_pool(BATTLE_SUB_POOL)}
                    short_by_voice = {"main": short_main, "sub": short_sub}

                    plans = {}
                    for vk in ("main", "sub"):
                        pool_durations = pool_durations_by_voice[vk]
                        start = starts_by_voice[vk]
                        short_pool = short_by_voice[vk]
                        if vk == preoverlap_voice and short_pool:
                            interject_basename = random.choice(list(short_pool))
                            interject_dur = pool_durations[interject_basename]
                            rest_start = start + interject_dur + BATTLE_LEADIN_CHAIN_GAP_SEC
                            rest_plan = _pack_reaction_chain(pool_durations, rest_start, reaction_end_limit)
                            plans[vk] = [(interject_basename, start)] + rest_plan
                        else:
                            plans[vk] = _pack_reaction_chain(pool_durations, start, reaction_end_limit)
                    main_plan = plans["main"]
                    sub_plan = plans["sub"]
                    text_by_voice = {"main": BATTLE_MAIN_TEXT, "sub": BATTLE_SUB_TEXT}
                    for vk, plan, pool_durations in (("main", main_plan, main_pool_durations),
                                                       ("sub", sub_plan, sub_pool_durations)):
                        if not plan:
                            continue
                        chain_paths = [os.path.join(VOICE_DIR, basename) for basename, _ in plan]
                        combined_path = os.path.join(work_dir, f"leadin_battle_{vk}_chain.wav")
                        await self._to_executor(
                            _concat_wav_chain, chain_paths, BATTLE_LEADIN_CHAIN_GAP_SEC, combined_path)
                        chain_start = plan[0][1]
                        last_basename, last_start = plan[-1]
                        leadin_battle_files[vk] = combined_path
                        leadin_battle_starts[vk] = chain_start
                        leadin_battle_durations[vk] = (last_start + pool_durations[last_basename]) - chain_start
                        leadin_battle_texts[vk] = " / ".join(
                            text_by_voice[vk].get(basename, basename) for basename, _ in plan)

            end_times = [
                main_explode_start + main_explode_duration,
                hype_explode_start + hype_explode_duration,
                sub_explode_start + sub_explode_duration,
                *(hype_nickname_start + dur for dur in hype_nickname_durations),
                sub_question_start + sub_question_duration,
                main_fact_start + main_fact_duration,
            ]
            if leadin_overlay_start is not None:
                end_times.append(leadin_overlay_start + leadin_overlay_duration)
            if battle_reaction_files:
                end_times.extend(
                    battle_reaction_starts[vk] + battle_reaction_durations[vk] for vk in battle_reaction_files
                )
            if leadin_battle_files:
                end_times.extend(
                    leadin_battle_starts[vk] + leadin_battle_durations[vk] for vk in leadin_battle_files
                )
            total_duration = max(duration, max(end_times) + RENDER_TAIL_BUFFER_SEC)

            schedule = {
                "kill_t": kill_t,
                "total_duration": total_duration,
                "main_explode": {"wav": main_explode_file, "text": MAIN_EXPLODE_TEXT[os.path.basename(main_explode_file)],
                                  "start": main_explode_start, "duration": main_explode_duration},
                "hype_explode": {"wav": hype_explode_file, "text": HYPE_EXPLODE_TEXT[os.path.basename(hype_explode_file)],
                                  "start": hype_explode_start, "duration": hype_explode_duration},
                "sub_explode": {"wav": sub_explode_file, "text": SUB_EXPLODE_TEXT[os.path.basename(sub_explode_file)],
                                 "start": sub_explode_start, "duration": sub_explode_duration},
                "sub_question": {"wav": sub_question_file, "text": SUB_QUESTION_TEXT[os.path.basename(sub_question_file)],
                                  "start": sub_question_start, "duration": sub_question_duration},
                "main_fact": {"wav": main_fact_wav, "text": main_fact_text,
                              "start": main_fact_start, "duration": main_fact_duration},
            }
            for i, (wav, dur) in enumerate(zip(hype_nickname_wavs, hype_nickname_durations)):
                schedule[f"hype_nickname_{i + 1}"] = {"wav": wav, "text": hype_nickname_text_by_voice[nickname_voice_keys[i]],
                                                       "start": hype_nickname_start, "duration": dur}
            for i, start in enumerate(pre_buildup_starts):
                f = pre_buildup_candidates[i]
                basename = os.path.basename(f)
                # 🛡️ [긴박 풀 텍스트 병행 조회] 마지막 슬롯이 PRE_BUILDUP_URGENT_POOL에서 뽑힌
                # 경우 PRE_BUILDUP_TEXT에는 없으므로 PRE_BUILDUP_URGENT_TEXT로 폴백한다.
                text = PRE_BUILDUP_TEXT.get(basename) or PRE_BUILDUP_URGENT_TEXT.get(basename)
                schedule[f"pre_buildup_{i + 1}"] = {"wav": f, "text": text,
                                                     "start": start, "duration": pre_buildup_durations[i]}
            if leadin_overlay_start is not None:
                schedule["leadin_overlay"] = {
                    "wav": leadin_overlay_file, "text": LEADIN_OVERLAY_TEXT[os.path.basename(leadin_overlay_file)],
                    "start": leadin_overlay_start, "duration": leadin_overlay_duration,
                }
            for vk, path in battle_reaction_files.items():
                basename = os.path.basename(path)
                text = BATTLE_MAIN_TEXT.get(basename) or BATTLE_LCK_TEXT.get(basename) or BATTLE_SUB_TEXT.get(basename)
                schedule[f"battle_{vk}"] = {
                    "wav": path, "text": text,
                    "start": battle_reaction_starts[vk], "duration": battle_reaction_durations[vk],
                }
            for vk, path in leadin_battle_files.items():
                schedule[f"leadin_battle_{vk}"] = {
                    "wav": path, "text": leadin_battle_texts.get(vk),
                    "start": leadin_battle_starts[vk], "duration": leadin_battle_durations[vk],
                }
        # 🛡️ [오버레이 HUD 타이밍] 명세서의 고정값이 아니라 이 렌더의 실제 schedule 타이밍을
        # 그대로 재사용한다 - kill_t(0단계, 킬 순간)에 등장해서 3단계(사실 전달)가 끝날 때
        # 같이 퇴장하는 것으로 잡았다(플레이어에게 "이 킬에 대한 설명이 끝났다"는 인상과
        # HUD 퇴장을 맞추기 위함) - plan_kill_sequence/plan_lead_in_forward_eoeo와 마찬가지로
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
            await progress_msg.edit(content=await get_msg("highlight_err_render_failed"))
            return

        await self._send_result_or_report_failure(interaction, progress_msg, guild_id, out_mp4, style_override)

    async def _send_result_or_report_failure(self, interaction, progress_msg, guild_id, out_mp4,
                                               style_override=None) -> None:
        """렌더링된 파일을 보내되, 용량 초과나 그 외 업로드 실패를 조용히 묻지 않고 progress_msg를
        적절한 에러로 되돌린다. 독립 메서드로 뺀 이유: 이 분기 로직 자체를 파이프라인 전체를
        돌리지 않고도 단위 테스트할 수 있어야 하기 때문. style_override는 _run_pipeline에서
        받은 값을 그대로 다시 전달받아, 성공/실패 메시지도 유저가 고른 style을 그대로 따른다."""
        get_msg = lambda key, **kw: self.get_msg(guild_id, key, lang_override=style_override, **kw)
        # 🛡️ 비트레이트 역산으로 크기를 목표 근처로 수렴시켰지만, 그래도 극단적인 경우(예상보다
        # 훨씬 복잡한 콘텐츠, 컨테이너/오디오 오버헤드 오차)를 대비해 실제 파일 크기를 보내기
        # 전에 먼저 확인한다 - 어차피 실패할 업로드를 시도해서 시간 버릴 필요 없이 바로 안내.
        out_size_bytes = os.path.getsize(out_mp4)
        if out_size_bytes > DISCORD_UPLOAD_LIMIT_BYTES:
            print(f"[HIGHLIGHT][WARN] Rendered output exceeds Discord upload limit "
                  f"({out_size_bytes / 1024 / 1024:.1f}MB, guild={guild_id})", flush=True)
            await progress_msg.edit(content=await get_msg("highlight_err_output_too_large"))
            return

        # 🛡️ [버그 수정] 이전에는 전송 성공 여부와 무관하게 먼저 "완성됐습니다"로 편집해버려서,
        # followup.send가 실패하면(용량 초과 등) 유저는 성공 메시지만 보고 실제 파일은 영영 못
        # 받는 상황이 조용히 묻혔다. 전송을 먼저 시도하고, 성공했을 때만 성공 메시지로 편집한다.
        try:
            await interaction.followup.send(
                content=await get_msg("highlight_success_caption"),
                file=discord.File(out_mp4, filename="highlight.mp4"),
                ephemeral=False,
            )
        except discord.HTTPException as e:
            print(f"[HIGHLIGHT][ERROR] Upload failed (status={e.status}, guild={guild_id}): {e}", flush=True)
            if e.status == 413:
                await progress_msg.edit(content=await get_msg("highlight_err_output_too_large"))
            else:
                await progress_msg.edit(content=await get_msg("highlight_err_upload_failed"))
            return
        except Exception as e:
            print(f"[HIGHLIGHT][ERROR] Unexpected upload failure (guild={guild_id}): {type(e).__name__}: {e}", flush=True)
            await progress_msg.edit(content=await get_msg("highlight_err_upload_failed"))
            return

        await progress_msg.edit(content=await get_msg("highlight_success_caption"))


async def setup(bot):
    if not HIGHLIGHT_FEATURE_ENABLED:
        print("[HIGHLIGHT] HIGHLIGHT_FEATURE_ENABLED is not set - skipping cog registration (command will not appear).", flush=True)
        return
    _log_ffmpeg_filter_support()
    cog = KyvoHighlight(bot)
    await bot.add_cog(cog)
    print("[⚡ HIGHLIGHT] Cog extension setup complete.", flush=True)
