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

    @property
    def is_uploadable(self) -> bool:
        # 서버가 못 쓰는 데이터(몇십 분짜리 전체 녹화) - eventType이 "Full"이거나
        # eventData가 빈 배열이면 건너뛴다.
        if self.clip_data.get("eventType") == "Full":
            return False
        if not self.event_data:
            return False
        return True


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
