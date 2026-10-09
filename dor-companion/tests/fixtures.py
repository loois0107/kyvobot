"""실제 E:\\DOR\\<세션 폴더>\\ClipEvents.json 구조(직접 읽어서 확인한 포맷)를 흉내낸
테스트 픽스처 생성 헬퍼."""
import json
import os

BASIC_GAME_DATA = {
    "summonerName": "테스트유저#0001",
    "riotUuid": "8404e8e4-889a-5105-a717-6bde79dde2ec",
    "gameMode": "CLASSIC",
    "queueId": 400,
    "queueType": "NORMAL",
    "gameDuration": 2128.787353515625,
    "champion": "Lucian",
    "kills": 19,
    "deaths": 5,
    "assists": 14,
    "matchId": 8412367111,
    "win": True,
    "tag": "DoubleKill",
    "clipDuration": 30,
}


def make_session_dir(root: str, session_name: str) -> str:
    session_dir = os.path.join(root, session_name)
    os.makedirs(session_dir, exist_ok=True)
    return session_dir


def add_kill_clip(session_dir: str, video_filename: str, event_time: float, rec_offset: float,
                   killer="Lucian", victim="Lulu") -> None:
    _write_video(session_dir, video_filename)
    _merge_clip_events(session_dir, {
        video_filename: {
            "clipData": {
                "clipEndTime": event_time + 10,
                "eventType": "Kill",
                "recOffsetSec": rec_offset,
            },
            "eventData": [
                {
                    "Assisters": [],
                    "EventID": 15,
                    "EventName": "ChampionKill",
                    "EventTime": event_time,
                    "KillerName": killer,
                    "VictimName": victim,
                }
            ],
        }
    })


def add_full_recording_clip(session_dir: str, video_filename: str) -> None:
    _write_video(session_dir, video_filename)
    _merge_clip_events(session_dir, {
        video_filename: {
            "clipData": {"eventType": "Full", "clipEndTime": 2142.86},
            "eventData": [],
        }
    })


def _write_video(session_dir: str, video_filename: str) -> None:
    with open(os.path.join(session_dir, video_filename), "wb") as f:
        f.write(b"\x00\x01fake-mp4-bytes" * 10)


def _merge_clip_events(session_dir: str, new_entries: dict) -> None:
    path = os.path.join(session_dir, "ClipEvents.json")
    data = {}
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    data.update(new_entries)
    data["basicGameData"] = BASIC_GAME_DATA
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
