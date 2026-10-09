"""
최초 실행 시(config.json 없음/토큰 비어있음) 뜨는 설정 입력창(SetupDialog/ensure_config)
통합 테스트. 실제 tkinter 창을 띄우고, mainloop가 돌기 시작한 뒤 after 콜백으로
Entry에 값을 넣고 확인 버튼 로직(_on_confirm)을 직접 호출해서(진짜 사용자가 타이핑하고
클릭하는 것과 동일한 경로) 끝까지 돌린다.

autodetect_dor_root는 이 개발 머신에 실제로 있는 드라이브(E:\\DOR 등)를 훑는 대신
테스트 픽스처 경로를 돌려주도록 patch해서, 결과가 이 머신의 실제 DOR 설치 여부에
영향받지 않게 고정한다.

실행: (이 폴더의 venv에서) python -m unittest tests.test_setup_dialog -v
"""
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dor_companion.logger import setup_logger
from dor_companion import setup_dialog as setup_dialog_module


def _close_logger(logger):
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)


class SetupDialogTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_dir = self._tmp.name
        self.config_path = os.path.join(self.tmp_dir, "config.json")
        self.dor_root_dir = os.path.join(self.tmp_dir, "DOR_fixture")
        os.makedirs(self.dor_root_dir, exist_ok=True)
        log_path = os.path.join(self.tmp_dir, "test.log")
        self.logger = setup_logger(path=log_path, name=f"test_setup_{id(self)}")

    def tearDown(self):
        _close_logger(self.logger)
        self._tmp.cleanup()

    def test_1_dialog_appears_when_config_missing(self):
        """빈(=존재하지 않는) config.json으로 시작하면 입력창이 실제로 화면에 뜨는지."""
        self.assertFalse(os.path.isfile(self.config_path))
        appeared = {"value": False}
        original_init = setup_dialog_module.SetupDialog.__init__

        def _spy_init(dialog_self, *a, **kw):
            original_init(dialog_self, *a, **kw)

            def _check_then_confirm():
                appeared["value"] = bool(dialog_self.root.winfo_viewable())
                dialog_self.token_var.set("dummy-token-for-appear-check")
                dialog_self._on_confirm()

            dialog_self.root.after(50, _check_then_confirm)

        with patch.object(setup_dialog_module, "autodetect_dor_root", return_value=self.dor_root_dir), \
             patch.object(setup_dialog_module.SetupDialog, "__init__", _spy_init):
            config = setup_dialog_module.ensure_config(self.logger, path=self.config_path)

        self.assertTrue(appeared["value"], "입력창(Tk 윈도우)이 실제로 화면에 뜨지 않음")
        self.assertIsNotNone(config)

    def test_2_confirm_saves_token_to_config_and_app_runs_normally(self):
        """입력하고 확인을 누르면 config.json에 실제로 저장되고, 유효한 Config가 반환되며,
        그 Config로 기존처럼 CompanionApp(트레이 뒤에서 도는 실제 로직)이 정상 동작하는지."""
        typed_token = "real-typed-token-abc123"
        original_init = setup_dialog_module.SetupDialog.__init__

        def _spy_init(dialog_self, *a, **kw):
            original_init(dialog_self, *a, **kw)
            dialog_self.token_var.set(typed_token)
            dialog_self.root.after(50, dialog_self._on_confirm)

        with patch.object(setup_dialog_module, "autodetect_dor_root", return_value=self.dor_root_dir), \
             patch.object(setup_dialog_module.SetupDialog, "__init__", _spy_init):
            config = setup_dialog_module.ensure_config(self.logger, path=self.config_path)

        self.assertIsNotNone(config, "확인 버튼을 눌렀는데 ensure_config가 None을 반환함")
        self.assertEqual(config.token, typed_token)
        self.assertEqual(config.dor_root, self.dor_root_dir)

        self.assertTrue(os.path.isfile(self.config_path), "확인 후 config.json이 생성되지 않음")
        with open(self.config_path, "r", encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["token"], typed_token)
        self.assertEqual(saved["dor_root"], self.dor_root_dir)
        self.assertTrue(saved["server_url"], "server_url이 기본값으로도 채워지지 않음")

        from dor_companion.app import CompanionApp
        from dor_companion.state import UploadState

        state = UploadState(os.path.join(self.tmp_dir, "uploaded_state.json"))
        app = CompanionApp(
            server_url=config.server_url, token=config.token,
            dor_root=config.dor_root, state=state, logger=self.logger,
        )
        try:
            app.start()  # 트레이 직전까지의 실제 동작 경로 - 예외 없이 시작되는지만 확인
        finally:
            app.stop()

    def test_3_existing_valid_config_skips_dialog_entirely(self):
        """토큰이 이미 있는 유효한 config.json으로 재실행하면 입력창을 전혀 안 띄우고
        바로 반환하는지(기존 동작이 안 깨지는지)."""
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "server_url": "https://example.test",
                    "token": "already-have-one",
                    "dor_root": self.dor_root_dir,
                },
                f,
            )

        with patch.object(setup_dialog_module, "SetupDialog") as mock_dialog_cls:
            config = setup_dialog_module.ensure_config(self.logger, path=self.config_path)

        mock_dialog_cls.assert_not_called()
        self.assertIsNotNone(config)
        self.assertEqual(config.token, "already-have-one")


if __name__ == "__main__":
    unittest.main()
