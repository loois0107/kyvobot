"""
실제 함수 호출로 돌리는 통합 테스트. 서버 쪽은 fake_server.FakeDorServer로
cogs/dor.py의 실제 통신 규격(헤더/필드명/상태코드)을 흉내낸다.

실행: (이 폴더의 venv에서) python -m unittest tests.test_integration -v
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dor_companion.app import CompanionApp
from dor_companion.config import load_config
from dor_companion.logger import setup_logger
from dor_companion.state import UploadState

from tests import fixtures
from tests.fake_server import VALID_TOKEN, FakeDorServer


class _CollectingHandler:
    """load_config/logger가 남기는 메시지를 파일 대신 메모리로도 받아서 바로 assert하기 쉽게."""


def _make_logger(tmp_dir, name):
    path = os.path.join(tmp_dir, "test.log")
    logger = setup_logger(path=path, name=name)
    return logger, path


def _close_logger(logger):
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)


class UploadFlowTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = self._tmp.name
        self.dor_root = os.path.join(self.tmp_dir, "DOR")
        os.makedirs(self.dor_root)
        self.state_path = os.path.join(self.tmp_dir, "uploaded_state.json")
        self.logger, self.log_path = _make_logger(self.tmp_dir, f"test_{id(self)}")
        self.server = FakeDorServer()

    def tearDown(self):
        self.server.stop()
        _close_logger(self.logger)
        self._tmp.cleanup()

    def _make_app(self):
        state = UploadState(self.state_path)
        return CompanionApp(
            server_url=self.server.url,
            token=VALID_TOKEN,
            dor_root=self.dor_root,
            state=state,
            logger=self.logger,
        )

    def test_1_detect_and_upload_new_clip(self):
        session_dir = fixtures.make_session_dir(self.dor_root, "2026-10-09 16_15")
        fixtures.add_kill_clip(session_dir, "DOR 2026-10-09 16-20-24.mp4", event_time=257.5, rec_offset=224.99)

        app = self._make_app()
        try:
            app.start()
            self.assertTrue(app.wait_for_queue_drain(timeout=15))
        finally:
            app.stop()

        self.assertEqual(len(self.server.uploaded_calls), 1)
        call = self.server.uploaded_calls[0]
        self.assertIn("eventData", call["clip_events"])
        self.assertEqual(call["clip_events"]["eventData"][0]["KillerName"], "Lucian")
        self.assertEqual(call["basic_game_data"]["matchId"], 8412367111)
        self.assertTrue(call["video_bytes"].startswith(b"\x00\x01fake-mp4-bytes"))

    def test_2_restart_does_not_reupload(self):
        session_dir = fixtures.make_session_dir(self.dor_root, "2026-10-09 16_15")
        fixtures.add_kill_clip(session_dir, "DOR 2026-10-09 16-20-24.mp4", event_time=257.5, rec_offset=224.99)

        app1 = self._make_app()
        try:
            app1.start()
            self.assertTrue(app1.wait_for_queue_drain(timeout=15))
        finally:
            app1.stop()
        self.assertEqual(len(self.server.uploaded_calls), 1)

        # "재시작" - 새 CompanionApp/UploadState 인스턴스를 같은 state 파일로 다시 띄움
        app2 = self._make_app()
        try:
            app2.start()
            app2.scan_once()
            import time
            time.sleep(2)
        finally:
            app2.stop()

        self.assertEqual(len(self.server.uploaded_calls), 1, "재시작 후 같은 클립을 다시 올리면 안 됨")

    def test_3_full_type_is_skipped(self):
        session_dir = fixtures.make_session_dir(self.dor_root, "2026-10-09 16_15")
        fixtures.add_full_recording_clip(session_dir, "2026-10-09 16-16-04.mp4")

        app = self._make_app()
        try:
            app.start()
            import time
            time.sleep(2)
            app.wait_for_queue_drain(timeout=5)
        finally:
            app.stop()

        self.assertEqual(len(self.server.uploaded_calls), 0, "Full 타입은 업로드 대상이 아니어야 함")
        self.assertEqual(len(self.server.received_requests), 0, "서버에 요청 자체가 가면 안 됨")

    def test_4_server_down_does_not_crash_and_retries_later(self):
        session_dir = fixtures.make_session_dir(self.dor_root, "2026-10-09 16_15")
        fixtures.add_kill_clip(session_dir, "DOR 2026-10-09 16-20-24.mp4", event_time=257.5, rec_offset=224.99)

        self.server.force_down = True
        app = self._make_app()
        try:
            app.start()
            # uploader의 재시도(최대 3회, 2s/5s/10s 백오프)까지 다 끝날 시간을 줌
            self.assertTrue(app.wait_for_queue_drain(timeout=25))
        finally:
            pass

        self.assertEqual(len(self.server.uploaded_calls), 0)
        self.assertEqual(app.state.is_uploaded(os.path.join(session_dir, "DOR 2026-10-09 16-20-24.mp4")), False)
        self.assertTrue(app._worker_thread.is_alive(), "서버가 죽어있어도 워커 스레드가 죽으면 안 됨")

        with open(self.log_path, "r", encoding="utf-8") as f:
            log_content = f.read()
        self.assertIn("업로드", log_content)

        # 서버가 복구된 뒤 재스캔하면 성공해야 함
        self.server.force_down = False
        app.scan_once()
        try:
            self.assertTrue(app.wait_for_queue_drain(timeout=15))
        finally:
            app.stop()
        self.assertEqual(len(self.server.uploaded_calls), 1)


class ConfigValidationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = self._tmp.name
        self.logger, self.log_path = _make_logger(self.tmp_dir, f"test_cfg_{id(self)}")

    def tearDown(self):
        _close_logger(self.logger)
        self._tmp.cleanup()

    def _read_log(self) -> str:
        with open(self.log_path, "r", encoding="utf-8") as f:
            return f.read()

    def test_missing_config_file_logs_human_readable_error(self):
        missing_path = os.path.join(self.tmp_dir, "does_not_exist.json")
        result = load_config(self.logger, path=missing_path)
        self.assertIsNone(result)
        self.assertIn("찾을 수 없습니다", self._read_log())

    def test_empty_values_log_human_readable_error(self):
        path = os.path.join(self.tmp_dir, "config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"server_url": "", "token": "", "dor_root": ""}, f)
        result = load_config(self.logger, path=path)
        self.assertIsNone(result)
        log_content = self._read_log()
        self.assertIn("server_url", log_content)
        self.assertIn("token", log_content)
        self.assertIn("dor_root", log_content)

    def test_nonexistent_dor_root_logs_error(self):
        path = os.path.join(self.tmp_dir, "config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {"server_url": "https://x.example", "token": "abc", "dor_root": "Z:\\nope\\nope"},
                f,
            )
        result = load_config(self.logger, path=path)
        self.assertIsNone(result)
        self.assertIn("존재하지 않습니다", self._read_log())

    def test_valid_config_loads(self):
        path = os.path.join(self.tmp_dir, "config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {"server_url": "https://x.example/", "token": "abc", "dor_root": self.tmp_dir},
                f,
            )
        result = load_config(self.logger, path=path)
        self.assertIsNotNone(result)
        self.assertEqual(result.server_url, "https://x.example")
        self.assertEqual(result.dor_root, self.tmp_dir)


if __name__ == "__main__":
    unittest.main()
