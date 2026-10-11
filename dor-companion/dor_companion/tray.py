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


def format_last_scan_text(app) -> str:
    """app.last_scan_at(마지막으로 "진짜 끝난"(타임아웃 안 걸린) 스캔 시각)을 트레이
    메뉴에 보여줄 문자열로 포맷한다. 별도 함수로 뺀 이유는 pystray 없이도(단위 테스트)
    바로 호출해서 검증할 수 있게 하기 위함."""
    ts = app.last_scan_at
    if ts is None:
        return "마지막 스캔: 아직 없음"
    return f"마지막 스캔: {ts.astimezone():%H:%M:%S}"


def run_tray(app, on_quit) -> None:
    def _quit(icon, _item):
        try:
            on_quit()
        finally:
            icon.stop()

    # 🛡️ [메뉴 열 때마다 새로 계산됨] pystray의 MenuItem.text는 콜러블이면 메뉴가 열릴
    # 때마다(= 우클릭할 때마다) item을 인자로 다시 호출된다 - 별도 갱신 스레드나 폴링이
    # 필요 없다.
    icon = pystray.Icon(
        "DOR Companion",
        icon=_make_icon_image(),
        title="DOR Companion",
        menu=pystray.Menu(
            pystray.MenuItem(lambda _item: format_last_scan_text(app), None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("로그 열기", _open_logs),
            pystray.MenuItem("종료", _quit),
        ),
    )
    icon.run()
