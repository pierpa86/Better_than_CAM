from __future__ import annotations

import threading
from typing import Any, Callable

from PIL import Image, ImageDraw

from .assets import app_icon_path


class TrayController:
    def __init__(
        self,
        app: Any,
        show_callback: Callable[[], None],
        exit_callback: Callable[[], None],
    ) -> None:
        self.app = app
        self.show_callback = show_callback
        self.exit_callback = exit_callback
        self.icon: Any | None = None
        self.thread: threading.Thread | None = None

    def hide_to_tray(self) -> bool:
        if self.icon is None:
            if not self._create_icon():
                self.app.iconify()
                return False

        self.app.withdraw()
        return True

    def show_window(self) -> None:
        self.app.deiconify()
        self.app.lift()
        self.app.focus_force()

    def stop(self) -> None:
        if self.icon is None:
            return
        try:
            self.icon.stop()
        finally:
            self.icon = None

    def _create_icon(self) -> bool:
        try:
            import pystray
        except ImportError:
            return False

        menu = pystray.Menu(
            pystray.MenuItem("Show", lambda _icon, _item: self.app.after(0, self.show_callback), default=True),
            pystray.MenuItem("Exit", lambda _icon, _item: self.app.after(0, self.exit_callback)),
        )
        self.icon = pystray.Icon("BTCAM", _tray_image(), "BTCAM", menu)

        if hasattr(self.icon, "run_detached"):
            self.icon.run_detached()
            return True

        self.thread = threading.Thread(target=self.icon.run, daemon=True)
        self.thread.start()
        return True


def _tray_image() -> Image.Image:
    icon_path = app_icon_path()
    if icon_path is not None:
        try:
            with Image.open(icon_path) as icon:
                image = icon.convert("RGBA")
            image.thumbnail((64, 64), Image.Resampling.LANCZOS)
            canvas = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            x = (64 - image.width) // 2
            y = (64 - image.height) // 2
            canvas.paste(image, (x, y), image)
            return canvas
        except OSError:
            pass

    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((6, 6, size - 6, size - 6), fill=(18, 24, 29, 255), outline=(77, 224, 202, 255), width=4)
    draw.arc((16, 16, size - 16, size - 16), 135, 405, fill=(245, 179, 82, 255), width=5)
    draw.ellipse((28, 28, 36, 36), fill=(238, 244, 240, 255))
    return image
