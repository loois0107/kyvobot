"""
DOR 세션 폴더(dor_root 아래 임의 깊이)의 ClipEvents.json을 읽어서 업로드
대상 클립 목록을 뽑아낸다.

실제 파일 구조(E:\\DOR\\<세션 폴더>\\ClipEvents.json 직접 열어 확인함):
최상위 키는 그 폴더 안에 있는 영상 파일명 그대로이고(예: "DOR 2026-10-09
16-20-24.mp4"), 값은 {"clipData": {...}, "eventData": [...]}. "basicGameData"
라는 키가 딱 하나 더 있는데 이건 특정 클립이 아니라 그 세션(매치) 전체에
공용인 값이다.
"""
import json
import os
from dataclasses import dataclass
from typing import Iterator

CLIP_EVENTS_FILENAME = "ClipEvents.json"
BASIC_GAME_DATA_KEY = "basicGameData"

# 실제 DOR 세션 파일(E:\DOR\<세션 폴더>\TotalEvents.json)을 직접 열어서 확인함: 진짜
# 멀티킬(Riot의 EventName="Multikill" + KillStreak)이 난 순간만 clipData.eventType이
# "DoubleKill"로 찍히고, eventData 안 ChampionKill이 2건이어도 그 사이 간격이 멀티킬
# 유지시간(~10초)을 넘기면 eventType은 그냥 "Kill"로 남는다(16-24-16.mp4 실측 사례,
# 11.33초 간격) - 즉 eventData 개수나 시간 간격을 직접 재계산하지 않고 eventType
# 문자열 자체를 그대로 믿어도 된다.
# "TripleKill"/"QuadraKill"/"PentaKill"은 이번에 실제로 관측된 매치에 한 번도
# 나오지 않아서(최고 기록이 DoubleKill) 실측은 못했고, "Kill"->"DoubleKill" 네이밍
# 패턴상 추정만 한 값이다 - UNKNOWN_EVENT_TYPE_WARNING 쪽에서 혹시 이름이 다르게
# 찍히면 잡아내도록 해둔다.
UPLOADABLE_EVENT_TYPES = frozenset({"DoubleKill", "TripleKill", "QuadraKill", "PentaKill"})
# 실제로 관측됐고 업로드 대상이 아님이 확실한 값들 - 여기 없는 값은 "처음 보는" 값으로
# 취급해 로그에 남긴다(조용히 버리지 않음).
KNOWN_SKIP_EVENT_TYPES = frozenset({"Kill", "Death", "Assist", "Tower", "Object", "Full", "End"})


@dataclass
class ClipCandidate:
    session_dir: str
    video_filename: str
    clip_data: dict
    event_data: list
    basic_game_data: dict

    @property
    def video_path(self) -> str:
        return os.path.join(self.session_dir, self.video_filename)

    def is_uploadable(self, logger=None) -> bool:
        event_type = self.clip_data.get("eventType")
        if event_type in UPLOADABLE_EVENT_TYPES:
            # 멀티킬 태그인데 eventData가 비어있는 건 있을 수 없는 이상 케이스 - 방어적으로 제외.
            return bool(self.event_data)
        if event_type not in KNOWN_SKIP_EVENT_TYPES and logger is not None:
            logger.warning(f"알 수 없는 eventType 발견: {event_type!r}, 건너뜀 (파일: {self.video_filename})")
        return False


def _iter_clip_events_files(dor_root: str) -> Iterator[str]:
    for dirpath, _dirnames, filenames in os.walk(dor_root):
        if CLIP_EVENTS_FILENAME in filenames:
            yield os.path.join(dirpath, CLIP_EVENTS_FILENAME)


def parse_clip_events_file(path: str) -> list[ClipCandidate]:
    session_dir = os.path.dirname(path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return []

    basic = raw.get(BASIC_GAME_DATA_KEY) or {}
    candidates = []
    for key, value in raw.items():
        if key == BASIC_GAME_DATA_KEY:
            continue
        if not isinstance(value, dict):
            continue
        candidates.append(
            ClipCandidate(
                session_dir=session_dir,
                video_filename=key,
                clip_data=value.get("clipData") or {},
                event_data=value.get("eventData") or [],
                basic_game_data=basic,
            )
        )
    return candidates


def scan_all(dor_root: str) -> list[ClipCandidate]:
    candidates = []
    for ce_path in _iter_clip_events_files(dor_root):
        candidates.extend(parse_clip_events_file(ce_path))
    return candidates
