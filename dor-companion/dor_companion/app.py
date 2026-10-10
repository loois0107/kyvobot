"""
전체를 묶는 곳. 트레이 아이콘은 안 쓰고 로직만 쓰고 싶은 테스트에서도 바로
가져다 쓸 수 있도록, 트레이/감시 시작 여부와 상관없이 CompanionApp 하나로
스캔/업로드 로직을 전부 호출할 수 있게 만들었다.
"""
import queue
import threading
import time

from . import scanner
from .state import UploadState
from .uploader import UploadError, upload_clip

PERIODIC_RESCAN_SEC = 300


class CompanionApp:
    def __init__(self, server_url: str, token: str, dor_root: str, state: UploadState, logger):
        self.server_url = server_url
        self.token = token
        self.dor_root = dor_root
        self.state = state
        self.logger = logger

        self._queue: "queue.Queue[scanner.ClipCandidate]" = queue.Queue()
        self._pending: set[str] = set()
        self._pending_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._worker_thread: threading.Thread | None = None
        self._periodic_thread: threading.Thread | None = None
        self._observer = None

    # ── 스캔 ──────────────────────────────────────────────────
    def scan_once(self) -> int:
        """새로 발견한 업로드 대상을 큐에 넣는다. 몇 건을 새로 넣었는지 반환."""
        enqueued = 0
        for candidate in scanner.scan_all(self.dor_root):
            if not candidate.is_uploadable(self.logger):
                continue
            video_path = candidate.video_path
            if self.state.is_uploaded(video_path):
                continue
            with self._pending_lock:
                if video_path in self._pending:
                    continue
                self._pending.add(video_path)
            self._queue.put(candidate)
            enqueued += 1
        return enqueued

    # ── 업로드 워커 ───────────────────────────────────────────
    def _worker_loop(self):
        while not self._stop_event.is_set():
            try:
                candidate = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            video_path = candidate.video_path
            try:
                import os

                if not os.path.isfile(video_path):
                    self.logger.warning(f"영상 파일이 아직 없어 건너뜀 (다음 스캔에 재시도): {video_path}")
                    continue
                result = upload_clip(self.server_url, self.token, candidate, self.logger)
                self.state.mark_uploaded(video_path)
                self.logger.info(
                    f"업로드 성공: {candidate.video_filename} -> candidate_id={result.get('candidate_id')}"
                )
            except UploadError as e:
                self.logger.error(f"업로드 실패 - 포기하고 다음 주기적 스캔에 재시도함: {video_path}: {e}")
            except Exception as e:
                self.logger.error(
                    f"업로드 중 예상 못한 오류 (프로그램은 계속 돈다): {video_path}: "
                    f"{type(e).__name__}: {e}"
                )
            finally:
                with self._pending_lock:
                    self._pending.discard(video_path)

    # ── 주기적 재스캔(안전망: fs 이벤트를 놓쳤거나, 이전 업로드 실패를 재시도) ──
    def _periodic_loop(self):
        while not self._stop_event.wait(PERIODIC_RESCAN_SEC):
            try:
                n = self.scan_once()
                if n:
                    self.logger.info(f"주기적 재스캔으로 {n}건 새로 큐에 넣음")
            except Exception as e:
                self.logger.error(f"주기적 재스캔 중 오류: {type(e).__name__}: {e}")

    def on_fs_change(self):
        try:
            self.scan_once()
        except Exception as e:
            self.logger.error(f"파일 변경 감지 후 스캔 중 오류: {type(e).__name__}: {e}")

    def start(self):
        from .watcher import start_watching

        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker_thread.start()
        self._periodic_thread = threading.Thread(target=self._periodic_loop, daemon=True)
        self._periodic_thread.start()
        self._observer = start_watching(self.dor_root, self.on_fs_change)
        self.scan_once()
        self.logger.info(f"DOR Companion 시작됨 (dor_root={self.dor_root}, server_url={self.server_url})")

    def stop(self):
        self._stop_event.set()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
        if self._worker_thread is not None:
            self._worker_thread.join(timeout=5)
        self.logger.info("DOR Companion 종료됨")

    def wait_for_queue_drain(self, timeout: float = 30.0) -> bool:
        """테스트용: 큐+처리중인 항목이 다 빌 때까지 기다린다."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._pending_lock:
                pending_empty = not self._pending
            if pending_empty and self._queue.empty():
                return True
            time.sleep(0.05)
        return False
