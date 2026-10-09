"""
이미 업로드한 클립을 기록해두는 로컬 상태 파일(JSON).
재시작해도 같은 클립을 중복 업로드하지 않기 위한 것 - 키는 영상 파일의
절대경로 문자열이다(세션 폴더+파일명이 DOR 쪽에서 고유하게 생성되므로 충분함).
"""
import json
import os
import threading


class UploadState:
    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()
        self._uploaded: set[str] = set()
        self._load()

    def _load(self) -> None:
        if not os.path.isfile(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._uploaded = set(data.get("uploaded", []))
        except Exception:
            self._uploaded = set()

    def is_uploaded(self, video_path: str) -> bool:
        with self._lock:
            return os.path.normcase(os.path.abspath(video_path)) in self._uploaded

    def mark_uploaded(self, video_path: str) -> None:
        key = os.path.normcase(os.path.abspath(video_path))
        with self._lock:
            self._uploaded.add(key)
            tmp_path = self._path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump({"uploaded": sorted(self._uploaded)}, f)
            os.replace(tmp_path, self._path)
