"""
config.json이 없거나 token이 비어있을 때 뜨는 최소 입력창.

main.py가 이 모듈을 호출하는 시점에는 아직 트레이 아이콘도 없고 콘솔도 없어서,
이 tkinter 창이 유일한 사용자 접점이다. server_url은 DEFAULT_SERVER_URL로 항상
자동으로 채워서 창에 안 보여주고, dor_root는 먼저 흔한 자동 설치 위치
("<드라이브>:\\DOR" 안에 실제 ClipEvents.json이 있는지)로 자동 감지를 시도한 뒤,
감지에 실패했을 때만 입력란을 추가로 보여준다 - token만 보여주는 게 기본, 꼭
필요할 때만 필드가 하나 늘어난다.
"""
import json
import os
import string
import tkinter as tk

from .config import Config, CONFIG_PATH, DEFAULT_SERVER_URL, load_config

DOR_FOLDER_NAME = "DOR"
CLIP_EVENTS_FILENAME = "ClipEvents.json"


def _read_raw_config(path: str) -> dict:
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _contains_clip_events(path: str) -> bool:
    for _dirpath, _dirnames, filenames in os.walk(path):
        if CLIP_EVENTS_FILENAME in filenames:
            return True
    return False


def autodetect_dor_root() -> str | None:
    """드라이브 루트에 바로 있는 'DOR' 폴더(E:\\DOR 같은 실제 관측된 배치)를 훑어서, 그
    안에 진짜 ClipEvents.json이 있는 경우에만 감지된 것으로 친다. 못 찾으면 None."""
    if os.name != "nt":
        return None
    for letter in string.ascii_uppercase:
        candidate = f"{letter}:\\{DOR_FOLDER_NAME}"
        if os.path.isdir(candidate) and _contains_clip_events(candidate):
            return candidate
    return None


def _save_config(path: str, server_url: str, token: str, dor_root: str) -> None:
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump({"server_url": server_url, "token": token, "dor_root": dor_root}, f, indent=2)
    os.replace(tmp_path, path)


class SetupDialog:
    """토큰(항상)과 dor_root(자동 감지 실패시에만) 입력란을 보여주는 창. 테스트에서는
    실제 tk 창을 띄운 뒤 위젯에 직접 값을 넣고 확인 버튼을 눌러 실제 흐름을 검증한다."""

    def __init__(self, initial_dor_root: str = "", show_dor_root_field: bool = False):
        self.show_dor_root_field = show_dor_root_field
        self.result: dict | None = None

        self.root = tk.Tk()
        self.root.title("DOR Companion 설정")
        self.root.resizable(False, False)
        try:
            self.root.attributes("-topmost", True)
        except Exception:
            pass

        pad = {"padx": 16, "pady": 6}
        tk.Label(
            self.root,
            text="디스코드에서 /dor_token 명령으로 받은 토큰을 붙여넣으세요.",
            justify="left",
            wraplength=360,
        ).pack(**pad)

        self.token_var = tk.StringVar()
        self.token_entry = tk.Entry(self.root, textvariable=self.token_var, width=50)
        self.token_entry.pack(padx=16, pady=(0, 10))
        self.token_entry.focus_set()

        self.dor_root_var = tk.StringVar(value=initial_dor_root)
        self.dor_root_entry = None
        if show_dor_root_field:
            tk.Label(
                self.root,
                text="DOR이 클립을 저장하는 폴더 경로 (예: E:\\DOR)",
                justify="left",
                wraplength=360,
            ).pack(padx=16, pady=(0, 6))
            self.dor_root_entry = tk.Entry(self.root, textvariable=self.dor_root_var, width=50)
            self.dor_root_entry.pack(padx=16, pady=(0, 10))

        self.error_label = tk.Label(self.root, text="", fg="red")
        self.error_label.pack(padx=16)

        self.confirm_button = tk.Button(self.root, text="확인", width=12, command=self._on_confirm)
        self.confirm_button.pack(pady=(4, 16))
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Return>", lambda _e: self._on_confirm())

    def _on_confirm(self):
        token = self.token_var.get().strip()
        dor_root = self.dor_root_var.get().strip()
        if not token:
            self.error_label.config(text="토큰을 입력해주세요.")
            return
        if self.show_dor_root_field and (not dor_root or not os.path.isdir(dor_root)):
            self.error_label.config(text="DOR 폴더 경로가 올바르지 않습니다.")
            return
        self.result = {"token": token, "dor_root": dor_root}
        self.root.destroy()

    def _on_close(self):
        self.result = None
        self.root.destroy()

    def run(self) -> dict | None:
        self.root.mainloop()
        return self.result


def ensure_config(logger, path: str = CONFIG_PATH) -> Config | None:
    """
    기존 load_config로 먼저 조용히 검증한다 - 이미 유효한 config면(토큰 포함) 창을
    전혀 띄우지 않고 바로 반환한다(재실행 시 기존 동작을 깨지 않기 위함). 유효하지
    않을 때만(파일 없음, 토큰 비어있음 등) 입력창을 띄운다.
    """
    config = load_config(logger, path=path)
    if config is not None:
        return config

    raw = _read_raw_config(path)
    server_url = str(raw.get("server_url") or "").strip() or DEFAULT_SERVER_URL

    dor_root = str(raw.get("dor_root") or "").strip()
    if not (dor_root and os.path.isdir(dor_root)):
        dor_root = autodetect_dor_root() or ""
    show_dor_root_field = not (dor_root and os.path.isdir(dor_root))

    logger.info("config.json에 토큰이 없어 입력창을 띄웁니다.")
    dialog = SetupDialog(initial_dor_root=dor_root, show_dor_root_field=show_dor_root_field)
    result = dialog.run()
    if result is None:
        logger.error("설정 입력창이 닫혀서 토큰을 받지 못했습니다 - 프로그램을 종료합니다.")
        return None

    final_dor_root = result["dor_root"] if show_dor_root_field else dor_root
    if not final_dor_root or not os.path.isdir(final_dor_root):
        logger.error(f"DOR 폴더 경로를 찾지 못해 종료합니다: {final_dor_root!r}")
        return None

    _save_config(path, server_url=server_url, token=result["token"], dor_root=final_dor_root)
    logger.info(f"입력창에서 받은 값으로 config.json을 저장했습니다 (dor_root={final_dor_root})")
    return load_config(logger, path=path)
