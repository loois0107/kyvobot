"""시스템 트레이 아이콘 - 우클릭 메뉴는 '로그 열기'/'종료' 두 개만."""
import os
import subprocess
import sys

import pystray
from PIL import Image, ImageDraw

from .config import LOG_PATH


def _make_icon_image() -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((4, 4, 60, 60), fill=(66, 135, 245, 255))
    draw.ellipse((22, 22, 42, 42), fill=(255, 255, 255, 255))
    return img


def _open_logs(_icon=None, _item=None):
    try:
        if sys.platform == "win32":
            os.startfile(LOG_PATH)  # noqa: S606
        else:
            subprocess.Popen(["xdg-open", LOG_PATH])
    except Exception:
        pass


def run_tray(on_quit) -> None:
    def _quit(icon, _item):
        try:
            on_quit()
        finally:
            icon.stop()

    icon = pystray.Icon(
        "DOR Companion",
        icon=_make_icon_image(),
        title="DOR Companion",
        menu=pystray.Menu(
            pystray.MenuItem("로그 열기", _open_logs),
            pystray.MenuItem("종료", _quit),
        ),
    )
    icon.run()
