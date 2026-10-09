"""
설정 파일(config.json) 로딩/검증.

exe 옆(또는 python main.py로 돌릴 때는 리포 루트)에 config.json이 없거나
필수 값이 비어있으면, 콘솔 창이 없는 --noconsole 빌드에서는 사람이 볼 수 있는
단서가 로그 파일뿐이므로 여기서 바로 raise하지 않고 logger에 사람이 읽을 수 있는
에러를 남긴 뒤 None을 반환한다 - 호출부(app.py)가 None을 받으면 조용히 종료한다.
"""
import json
import os
import sys
from dataclasses import dataclass


def _base_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


CONFIG_PATH = os.path.join(_base_dir(), "config.json")
STATE_PATH = os.path.join(_base_dir(), "uploaded_state.json")
LOG_PATH = os.path.join(_base_dir(), "dor_companion.log")

REQUIRED_KEYS = ("server_url", "token", "dor_root")


@dataclass
class Config:
    server_url: str
    token: str
    dor_root: str


def load_config(logger, path: str = CONFIG_PATH) -> Config | None:
    if not os.path.isfile(path):
        logger.error(
            f"설정 파일을 찾을 수 없습니다: {path}\n"
            f"이 exe와 같은 폴더에 config.json을 만들고 server_url/token/dor_root 세 값을 "
            f"채워주세요."
        )
        return None

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        logger.error(f"설정 파일을 읽을 수 없습니다 ({path}): {type(e).__name__}: {e}")
        return None

    missing = [k for k in REQUIRED_KEYS if not str(raw.get(k, "")).strip()]
    if missing:
        logger.error(
            f"설정 파일에 다음 값이 비어있거나 없습니다: {', '.join(missing)} "
            f"(파일: {path})"
        )
        return None

    dor_root = str(raw["dor_root"]).strip()
    if not os.path.isdir(dor_root):
        logger.error(f"설정 파일의 dor_root 경로가 실제로 존재하지 않습니다: {dor_root}")
        return None

    server_url = str(raw["server_url"]).strip().rstrip("/")
    return Config(server_url=server_url, token=str(raw["token"]).strip(), dor_root=dor_root)
