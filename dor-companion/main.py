"""DOR Companion 진입점. --onefile --noconsole로 패키징되므로 콘솔 출력은 아무도 못 본다 -
모든 상태는 config.LOG_PATH(실행 파일 옆 dor_companion.log)로만 알린다."""
from dor_companion.app import CompanionApp
from dor_companion.config import STATE_PATH
from dor_companion.logger import setup_logger
from dor_companion.setup_dialog import ensure_config
from dor_companion.state import UploadState
from dor_companion.tray import run_tray


def main() -> None:
    logger = setup_logger()
    # ensure_config는 기존 config.json이 이미 유효하면 입력창을 안 띄우고 바로 돌려주고,
    # 토큰이 없거나 파일이 없을 때만 tkinter 입력창을 띄워서 받아온 뒤 config.json에
    # 저장한다. 그래도 못 받으면(창을 닫아버림 등) load_config처럼 None을 반환한다.
    config = ensure_config(logger)
    if config is None:
        return

    try:
        state = UploadState(STATE_PATH)
        app = CompanionApp(
            server_url=config.server_url,
            token=config.token,
            dor_root=config.dor_root,
            state=state,
            logger=logger,
        )
        app.start()
        run_tray(on_quit=app.stop)
    except Exception as e:
        logger.error(f"예상 못한 오류로 종료: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
