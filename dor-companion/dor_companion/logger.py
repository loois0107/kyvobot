"""--noconsole 빌드에서는 로그 파일이 유일한 단서이므로 항상 파일에 기록한다."""
import logging
import logging.handlers

from .config import LOG_PATH


def setup_logger(path: str = LOG_PATH, *, name: str = "dor_companion") -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger

    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)
    return logger
