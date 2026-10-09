"""
dor_root 아래 새로 생기는 세션 폴더 / ClipEvents.json 변경을 watchdog으로 감시.
실제 fs 이벤트를 받으면 on_change 콜백(보통 전체 재스캔)을 짧게 디바운스해서 호출한다 -
DOR이 ClipEvents.json을 한 번에 다 쓰지 않고 여러 번 touch할 수 있어서, 매 이벤트마다
즉시 재스캔하면 중복 작업이 생기기 때문.
"""
import threading

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from .scanner import CLIP_EVENTS_FILENAME

DEBOUNCE_SEC = 1.5


class _ClipEventsHandler(FileSystemEventHandler):
    def __init__(self, on_change):
        self._on_change = on_change
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def _schedule(self):
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(DEBOUNCE_SEC, self._on_change)
            self._timer.daemon = True
            self._timer.start()

    def _relevant(self, path: str) -> bool:
        return path.endswith(CLIP_EVENTS_FILENAME)

    def on_created(self, event):
        if not event.is_directory and self._relevant(event.src_path):
            self._schedule()

    def on_modified(self, event):
        if not event.is_directory and self._relevant(event.src_path):
            self._schedule()


def start_watching(dor_root: str, on_change) -> Observer:
    handler = _ClipEventsHandler(on_change)
    observer = Observer()
    observer.schedule(handler, dor_root, recursive=True)
    observer.start()
    return observer
