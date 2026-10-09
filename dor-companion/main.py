"""DOR Companion 진입점. --onefile --noconsole로 패키징되므로 콘솔 출력은 아무도 못 본다 -
모든 상태는 config.LOG_PATH(실행 파일 옆 dor_companion.log)로만 알린다."""
from dor_companion.app import CompanionApp
from dor_companion.config import STATE_PATH, load_config
from dor_companion.logger import setup_logger
from dor_companion.state import UploadState
from dor_companion.tray import run_tray


def main() -> None:
    logger = setup_logger()
    config = load_config(logger)
    if config is None:
        # load_config가 이미 로그 파일에 사람이 읽을 수 있는 에러를 남겼다.
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
