"""
전체를 묶는 곳. 트레이 아이콘은 안 쓰고 로직만 쓰고 싶은 테스트에서도 바로
가져다 쓸 수 있도록, 트레이/감시 시작 여부와 상관없이 CompanionApp 하나로
스캔/업로드 로직을 전부 호출할 수 있게 만들었다.
"""
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import datetime, timezone

from . import scanner
from .state import UploadState
from .uploader import UploadError, upload_clip

PERIODIC_RESCAN_SEC = 300

# 🛡️ [스캔 타임아웃 값 선정] scan_once()의 실제 파일 I/O(os.walk/open/json.load)는 로컬
# 디스크 기준으로 보통 1초도 안 걸린다(DOR 세션 폴더 하나당 ClipEvents.json 1개 + mp4
# 수십~백개 단위). 60초는 "일시적으로 느린 외장 드라이브"와 "진짜로 응답 없는 드라이브"를
# 넉넉히 구분할 수 있는 값으로 판단했다 - 너무 짧으면 가끔 느린 디스크에서 정상 스캔까지
# 오탐으로 건너뛰게 되고, 너무 길면 멈춤을 감지하는 데까지 오래 걸린다.
SCAN_TIMEOUT_SEC = 60


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

        # 🛡️ [트레이 표시용] 타임아웃 없이 "진짜로" 끝난 스캔만 갱신한다 - 멈춰서 타임아웃
        # 처리된 주기는 "성공한 스캔"으로 치지 않는다(트레이에 보여줄 신뢰 가능한 신호).
        self.last_scan_at: datetime | None = None

    # ── 로깅 - 로깅 자체가 실패해도 호출부 스레드가 죽으면 안 됨 ──────────
    def _safe_log(self, level: str, message: str) -> None:
        """logger.info/warning/error 호출 자체가 예외를 던져도(디스크 꽉 찼거나 로그
        파일이 다른 프로세스에 잠긴 경우 등) 그 예외가 호출부(워커/주기 스레드)를 죽이지
        않게 막는다. 원인을 남겨야 할 로그가 실패해서 스레드가 흔적도 없이 죽어버리는
        역설적인 상황을 막기 위함 - 여기서 또 실패하면 더 할 수 있는 게 없으니 조용히
        넘어간다(무한 재귀 방지)."""
        try:
            getattr(self.logger, level)(message)
        except Exception:
            pass

    # ── 스캔 ──────────────────────────────────────────────────
    def _scan_once_blocking(self) -> int:
        """실제 파일 I/O를 하는 부분 - 응답 없는 드라이브 등으로 멈출 수 있어서 scan_once()가
        별도 스레드+타임아웃으로 감싸 호출한다. 이 메서드 자체는 순수하게 블로킹 I/O만
        한다(타임아웃/로깅 관련 처리는 전부 scan_once()에 있음)."""
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

    def scan_once(self) -> int:
        """새로 발견한 업로드 대상을 큐에 넣는다. 몇 건을 새로 넣었는지 반환.

        🛡️ [멈춤 방지 - 매 호출마다 새 ThreadPoolExecutor] _scan_once_blocking()이 응답
        없는 드라이브에서 영원히 안 끝날 수 있다(예외가 아니라 진짜 행 - try/except로
        못 잡음). 그래서 별도 스레드에서 돌리고 SCAN_TIMEOUT_SEC 안에 안 끝나면 그 결과를
        포기하고 0을 반환해 이번 주기만 건너뛴다. Python엔 스레드 강제 종료가 없어서 멈춘
        스레드 자체는 그대로 남지만(데몬이 아니어도 프로세스 종료를 막진 않음 - 아래 참고),
        매번 "새" executor를 쓰기 때문에 이전에 멈춘 스레드가 다음 scan_once() 호출을
        막지는 못한다(고정된 max_workers=1 풀을 재사용했다면 멈춘 스레드가 풀을 영원히
        점유해서 그다음 호출도 전부 막혔을 것). 멈춤이 반복되면 스레드가 하나씩 쌓이지만,
        각각 블로킹 syscall 안에서 대기만 하므로 메모리 비용은 미미하다 - 지금 구조(조용히
        전체가 죽음)보다는 훨씬 안전한 트레이드오프라고 판단했다."""
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dor-scan")
        future = executor.submit(self._scan_once_blocking)
        try:
            result = future.result(timeout=SCAN_TIMEOUT_SEC)
        except FutureTimeoutError:
            self._safe_log(
                "error",
                f"스캔이 {SCAN_TIMEOUT_SEC}초 안에 끝나지 않아 이번 주기를 건너뜁니다 "
                f"(dor_root 드라이브가 응답 없는 상태일 수 있음: {self.dor_root})",
            )
            return 0
        except Exception as e:
            self._safe_log("error", f"스캔 중 오류: {type(e).__name__}: {e}")
            return 0

        executor.shutdown(wait=False)
        self.last_scan_at = datetime.now(timezone.utc)
        return result

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
                    self._safe_log("warning", f"영상 파일이 아직 없어 건너뜀 (다음 스캔에 재시도): {video_path}")
                    continue
                result = upload_clip(self.server_url, self.token, candidate, self.logger)
                self.state.mark_uploaded(video_path)
                self._safe_log(
                    "info",
                    f"업로드 성공: {candidate.video_filename} -> candidate_id={result.get('candidate_id')}",
                )
            except UploadError as e:
                self._safe_log("error", f"업로드 실패 - 포기하고 다음 주기적 스캔에 재시도함: {video_path}: {e}")
            except Exception as e:
                self._safe_log(
                    "error",
                    f"업로드 중 예상 못한 오류 (프로그램은 계속 돈다): {video_path}: "
                    f"{type(e).__name__}: {e}",
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
                    self._safe_log("info", f"주기적 재스캔으로 {n}건 새로 큐에 넣음")
                else:
                    # 🛡️ [하트비트] n==0이어도 매번 한 줄 남긴다 - 이 줄이 하나도 안 찍히면
                    # "새 파일이 없다"가 아니라 "주기 스캔 자체가 멈췄다"는 뜻이 되도록,
                    # 로그만 보고도 둘을 구분할 수 있게 하기 위함.
                    self._safe_log("info", "주기적 재스캔 완료(새 항목 없음)")
            except Exception as e:
                self._safe_log("error", f"주기적 재스캔 중 오류: {type(e).__name__}: {e}")

    def on_fs_change(self):
        try:
            self.scan_once()
        except Exception as e:
            self._safe_log("error", f"파일 변경 감지 후 스캔 중 오류: {type(e).__name__}: {e}")

    def start(self):
        from .watcher import start_watching

        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker_thread.start()
        self._periodic_thread = threading.Thread(target=self._periodic_loop, daemon=True)
        self._periodic_thread.start()
        self._observer = start_watching(self.dor_root, self.on_fs_change)
        self.scan_once()
        self._safe_log("info", f"DOR Companion 시작됨 (dor_root={self.dor_root}, server_url={self.server_url})")

    def stop(self):
        self._stop_event.set()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
        if self._worker_thread is not None:
            self._worker_thread.join(timeout=5)
        self._safe_log("info", "DOR Companion 종료됨")

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
