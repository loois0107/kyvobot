"""
"조용히 죽는" 상황 재발 방지 조치 검증:
1) scan_once()가 멈춰도(타임아웃) 그 주기만 건너뛰고 다음 호출은 정상 진행되는지
2) logger.error() 자체가 예외를 던져도 워커/주기 스레드가 죽지 않는지
3) n==0인 정상 주기에도 하트비트 로그가 매번 찍히는지
4) 트레이의 마지막 스캔 시각 표시가 실제로 갱신되는지

실행: (이 폴더의 venv에서) python -m unittest tests.test_resilience -v
"""
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import dor_companion.app as app_module
from dor_companion.app import CompanionApp
from dor_companion.logger import setup_logger
from dor_companion.state import UploadState
from dor_companion.tray import format_last_scan_text

from tests import fixtures
from tests.fake_server import VALID_TOKEN, FakeDorServer


def _close_logger(logger):
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)


class RaisingLogger:
    """logger.error()를 호출하면 항상 예외를 던지는 가짜 logger - "로깅 자체가 실패하는"
    상황을 재현한다. info/warning은 평소처럼 동작(실제로 호출됐는지만 기록)."""

    def __init__(self):
        self.info_calls = []
        self.warning_calls = []

    def info(self, msg):
        self.info_calls.append(msg)

    def warning(self, msg):
        self.warning_calls.append(msg)

    def error(self, msg):
        raise OSError("디스크가 꽉 찼다고 가정(로깅 자체 실패 시뮬레이션)")


class ScanTimeoutTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = self._tmp.name
        self.dor_root = os.path.join(self.tmp_dir, "DOR")
        os.makedirs(self.dor_root)
        self.log_path = os.path.join(self.tmp_dir, "test.log")
        self.logger = setup_logger(path=self.log_path, name=f"test_timeout_{id(self)}")
        state = UploadState(os.path.join(self.tmp_dir, "uploaded_state.json"))
        self.app = CompanionApp(
            server_url="http://127.0.0.1:1", token="x", dor_root=self.dor_root,
            state=self.logger and state, logger=self.logger,
        )

    def tearDown(self):
        _close_logger(self.logger)
        self._tmp.cleanup()

    def test_hung_scan_times_out_and_next_scan_still_works(self):
        with patch.object(app_module, "SCAN_TIMEOUT_SEC", 0.3):
            # 1) 멈춘 스캔 시뮬레이션 - 타임아웃(0.3s)보다 훨씬 긴 sleep
            with patch.object(self.app, "_scan_once_blocking", side_effect=lambda: time.sleep(2)):
                start = time.monotonic()
                result = self.app.scan_once()
                elapsed = time.monotonic() - start

            self.assertEqual(result, 0, "멈춘 스캔은 0을 반환해야 함")
            self.assertLess(elapsed, 1.5, f"타임아웃(0.3s) 근처에서 돌아와야 하는데 {elapsed:.2f}s 걸림")
            self.assertIsNone(self.app.last_scan_at, "타임아웃된 스캔은 '성공'으로 치면 안 됨")

            with open(self.log_path, "r", encoding="utf-8") as f:
                log_content = f.read()
            self.assertIn("응답 없는", log_content)

            # 2) 바로 다음 scan_once() 호출은 멈춘 이전 호출과 무관하게 정상 진행돼야 함
            with patch.object(self.app, "_scan_once_blocking", return_value=3):
                result2 = self.app.scan_once()

            self.assertEqual(result2, 3, "이전 타임아웃이 다음 scan_once() 호출을 막으면 안 됨")
            self.assertIsNotNone(self.app.last_scan_at, "정상 완료된 스캔은 last_scan_at이 갱신돼야 함")


class SafeLoggingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = self._tmp.name
        self.dor_root = os.path.join(self.tmp_dir, "DOR")
        os.makedirs(self.dor_root)
        self.state_path = os.path.join(self.tmp_dir, "uploaded_state.json")

    def tearDown(self):
        self._tmp.cleanup()

    def test_safe_log_swallows_logger_failure(self):
        raising_logger = RaisingLogger()
        state = UploadState(self.state_path)
        app = CompanionApp(server_url="http://x", token="t", dor_root=self.dor_root, state=state, logger=raising_logger)
        try:
            app._safe_log("error", "이 호출은 터지면 안 됨")
        except Exception as e:
            self.fail(f"_safe_log가 예외를 삼키지 못함: {e}")

    def test_worker_thread_survives_logger_failure_and_keeps_processing(self):
        """logger.error()가 매번 예외를 던지는 상황에서, 업로드가 실패하는(401 등) 클립을
        처리해도 워커 스레드가 죽지 않고, 그 다음 정상 클립(더블킬)도 계속 처리하는지."""
        raising_logger = RaisingLogger()
        state = UploadState(self.state_path)
        server = FakeDorServer()
        try:
            app = CompanionApp(
                server_url=server.url, token="invalid-token-on-purpose",
                dor_root=self.dor_root, state=state, logger=raising_logger,
            )
            session_dir = fixtures.make_session_dir(self.dor_root, "s1")
            fixtures.add_clip_with_event_type(session_dir, "clip_bad_token.mp4", "Kill")

            app.start()
            self.assertTrue(app.wait_for_queue_drain(timeout=15))
            # 토큰이 틀려서 401 -> UploadError -> except 블록의 _safe_log("error", ...)가
            # RaisingLogger 때문에 내부적으로 실패하지만, _safe_log가 삼켜야 한다.
            self.assertTrue(app._worker_thread.is_alive(), "로깅 실패 때문에 워커 스레드가 죽으면 안 됨")

            # 이제 토큰을 고쳐서 정상 클립을 추가로 올려보고, 워커가 계속 일하는지 확인.
            # 🛡️ [첫 클립도 이번에 같이 재시도됨] clip_bad_token.mp4는 state.mark_uploaded가
            # 안 됐으니 다음 scan_once()가 다시 찾아서 재큐잉한다(기존 "실패하면 다음
            # 스캔에 재시도" 설계 그대로) - 이번엔 토큰이 유효해서 그것도 성공하므로
            # 업로드 성공 횟수는 1이 아니라 2가 된다.
            app.token = VALID_TOKEN
            double_dir = fixtures.make_session_dir(self.dor_root, "s2")
            fixtures.add_double_kill_clip(double_dir, "DOR 2026-10-09 16-31-53.mp4", event_time=941.71, rec_offset=914.99)
            app.scan_once()
            self.assertTrue(app.wait_for_queue_drain(timeout=15))
            self.assertTrue(app._worker_thread.is_alive(), "두 번째 처리 후에도 워커 스레드가 살아있어야 함")
            self.assertEqual(
                len(server.uploaded_calls), 2,
                "토큰을 고친 뒤엔 재시도된 첫 클립 + 새 더블킬 클립 둘 다 업로드돼야 함",
            )
        finally:
            try:
                app.stop()
            except Exception:
                pass
            server.stop()


class HeartbeatTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = self._tmp.name
        self.dor_root = os.path.join(self.tmp_dir, "DOR")
        os.makedirs(self.dor_root)
        self.log_path = os.path.join(self.tmp_dir, "test.log")
        self.logger = setup_logger(path=self.log_path, name=f"test_heartbeat_{id(self)}")

    def tearDown(self):
        _close_logger(self.logger)
        self._tmp.cleanup()

    def test_heartbeat_logged_every_cycle_even_when_nothing_new(self):
        state = UploadState(os.path.join(self.tmp_dir, "uploaded_state.json"))
        app = CompanionApp(server_url="http://x", token="t", dor_root=self.dor_root, state=state, logger=self.logger)
        with patch.object(app_module, "PERIODIC_RESCAN_SEC", 0.2):
            app._periodic_thread = __import__("threading").Thread(target=app._periodic_loop, daemon=True)
            app._periodic_thread.start()
            time.sleep(0.9)  # 0.2s 주기로 몇 바퀴 돌 시간
            app._stop_event.set()
            app._periodic_thread.join(timeout=3)

        with open(self.log_path, "r", encoding="utf-8") as f:
            log_content = f.read()
        heartbeat_count = log_content.count("주기적 재스캔 완료(새 항목 없음)")
        self.assertGreaterEqual(heartbeat_count, 2, f"하트비트가 반복적으로 찍혀야 하는데 {heartbeat_count}번만 찍힘")


class TrayLastScanTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = self._tmp.name
        self.dor_root = os.path.join(self.tmp_dir, "DOR")
        os.makedirs(self.dor_root)
        self.log_path = os.path.join(self.tmp_dir, "test.log")
        self.logger = setup_logger(path=self.log_path, name=f"test_tray_{id(self)}")

    def tearDown(self):
        _close_logger(self.logger)
        self._tmp.cleanup()

    def test_last_scan_text_updates_after_real_scan(self):
        state = UploadState(os.path.join(self.tmp_dir, "uploaded_state.json"))
        app = CompanionApp(server_url="http://x", token="t", dor_root=self.dor_root, state=state, logger=self.logger)

        self.assertIsNone(app.last_scan_at)
        self.assertEqual(format_last_scan_text(app), "마지막 스캔: 아직 없음")

        before = datetime.now(timezone.utc)
        app.scan_once()
        after = datetime.now(timezone.utc)

        self.assertIsNotNone(app.last_scan_at)
        self.assertTrue(before <= app.last_scan_at <= after, "last_scan_at이 실제 스캔 시각과 맞아야 함")

        text = format_last_scan_text(app)
        self.assertTrue(text.startswith("마지막 스캔: "))
        self.assertNotEqual(text, "마지막 스캔: 아직 없음")
        expected_hhmmss = f"{app.last_scan_at.astimezone():%H:%M:%S}"
        self.assertIn(expected_hhmmss, text)


if __name__ == "__main__":
    unittest.main()
