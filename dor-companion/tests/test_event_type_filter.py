"""
scanner.py의 eventType 화이트리스트 필터링(DoubleKill 이상만 업로드) 검증.

1) 기존 DoubleKill 픽스처가 필터 추가 후에도 여전히 업로드되는지(회귀)
2) Kill/Death/Assist/Tower/Object(= 알려진 제외 목록)가 전부 업로드 안 되는지
3) 화이트리스트에도 알려진 제외 목록에도 없는 처음 보는 eventType이 나오면
   업로드는 안 하면서 dor_companion.log에 경고로 남기는지

실행: (이 폴더의 venv에서) python -m unittest tests.test_event_type_filter -v
"""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dor_companion import scanner
from dor_companion.app import CompanionApp
from dor_companion.logger import setup_logger
from dor_companion.state import UploadState

from tests import fixtures
from tests.fake_server import VALID_TOKEN, FakeDorServer


def _close_logger(logger):
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)


class EventTypeFilterUnitTests(unittest.TestCase):
    """scanner.ClipCandidate.is_uploadable()만 떼서 보는 순수 단위 테스트."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.log_path = os.path.join(self._tmp.name, "test.log")
        self.logger = setup_logger(path=self.log_path, name=f"test_filter_unit_{id(self)}")

    def tearDown(self):
        _close_logger(self.logger)
        self._tmp.cleanup()

    def _candidate(self, event_type, event_data=None):
        return scanner.ClipCandidate(
            session_dir="dummy",
            video_filename="dummy.mp4",
            clip_data={"eventType": event_type},
            event_data=event_data if event_data is not None else [{"EventName": "ChampionKill"}],
            basic_game_data={},
        )

    def test_whitelisted_multikill_types_are_uploadable(self):
        for et in ("DoubleKill", "TripleKill", "QuadraKill", "PentaKill"):
            with self.subTest(event_type=et):
                self.assertTrue(self._candidate(et).is_uploadable(self.logger))

    def test_known_skip_types_are_not_uploadable(self):
        for et in ("Kill", "Death", "Assist", "Tower", "Object", "Full", "End"):
            with self.subTest(event_type=et):
                self.assertFalse(self._candidate(et).is_uploadable(self.logger))

    def test_unknown_event_type_is_not_uploadable_but_logged(self):
        candidate = self._candidate("TripleKill_test")
        self.assertFalse(candidate.is_uploadable(self.logger))
        with open(self.log_path, "r", encoding="utf-8") as f:
            log_content = f.read()
        self.assertIn("알 수 없는 eventType 발견", log_content)
        self.assertIn("TripleKill_test", log_content)

    def test_known_skip_type_does_not_log_unknown_warning(self):
        self._candidate("Kill").is_uploadable(self.logger)
        with open(self.log_path, "r", encoding="utf-8") as f:
            log_content = f.read()
        self.assertNotIn("알 수 없는 eventType", log_content)


class EventTypeFilterEndToEndTests(unittest.TestCase):
    """실제 파일 스캔 + 가짜 서버까지 묶어서 돌리는 통합 테스트."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = self._tmp.name
        self.dor_root = os.path.join(self.tmp_dir, "DOR")
        os.makedirs(self.dor_root)
        self.state_path = os.path.join(self.tmp_dir, "uploaded_state.json")
        self.log_path = os.path.join(self.tmp_dir, "test.log")
        self.logger = setup_logger(path=self.log_path, name=f"test_filter_e2e_{id(self)}")
        self.server = FakeDorServer()

    def tearDown(self):
        self.server.stop()
        _close_logger(self.logger)
        self._tmp.cleanup()

    def _make_app(self):
        state = UploadState(self.state_path)
        return CompanionApp(
            server_url=self.server.url, token=VALID_TOKEN,
            dor_root=self.dor_root, state=state, logger=self.logger,
        )

    def test_double_kill_sample_still_uploads(self):
        session_dir = fixtures.make_session_dir(self.dor_root, "2026-10-09 16_15")
        fixtures.add_double_kill_clip(
            session_dir, "DOR 2026-10-09 16-31-53.mp4", event_time=941.71, rec_offset=914.99
        )

        app = self._make_app()
        try:
            app.start()
            self.assertTrue(app.wait_for_queue_drain(timeout=15))
        finally:
            app.stop()

        self.assertEqual(len(self.server.uploaded_calls), 1, "DoubleKill 샘플이 여전히 업로드돼야 함(회귀)")

    def test_kill_death_assist_tower_object_are_all_skipped(self):
        session_dir = fixtures.make_session_dir(self.dor_root, "2026-10-09 16_15")
        fixtures.add_clip_with_event_type(session_dir, "clip_kill.mp4", "Kill")
        fixtures.add_clip_with_event_type(session_dir, "clip_death.mp4", "Death")
        fixtures.add_clip_with_event_type(session_dir, "clip_assist.mp4", "Assist")
        fixtures.add_clip_with_event_type(session_dir, "clip_tower.mp4", "Tower", event_data=[])
        fixtures.add_clip_with_event_type(session_dir, "clip_object.mp4", "Object", event_data=[])

        app = self._make_app()
        try:
            app.start()
            time.sleep(2)
            app.wait_for_queue_drain(timeout=5)
        finally:
            app.stop()

        self.assertEqual(len(self.server.uploaded_calls), 0, "Kill/Death/Assist/Tower/Object는 업로드되면 안 됨")
        self.assertEqual(len(self.server.received_requests), 0, "서버에 요청 자체가 가면 안 됨")

    def test_unknown_event_type_is_skipped_and_logged(self):
        session_dir = fixtures.make_session_dir(self.dor_root, "2026-10-09 16_15")
        fixtures.add_clip_with_event_type(session_dir, "clip_mystery.mp4", "TripleKill_test")

        app = self._make_app()
        try:
            app.start()
            time.sleep(2)
            app.wait_for_queue_drain(timeout=5)
        finally:
            app.stop()

        self.assertEqual(len(self.server.uploaded_calls), 0, "처음 보는 eventType은 업로드되면 안 됨")
        with open(self.log_path, "r", encoding="utf-8") as f:
            log_content = f.read()
        self.assertIn("알 수 없는 eventType 발견", log_content)
        self.assertIn("TripleKill_test", log_content)


if __name__ == "__main__":
    unittest.main()
