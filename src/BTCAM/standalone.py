from __future__ import annotations

from datetime import datetime
import ctypes
import tempfile
import sys
from pathlib import Path

from BTCAM.config import AppConfig
from BTCAM.elevation import ensure_admin_or_relaunch, is_admin
from BTCAM.app import BTCAMApp
from BTCAM.liquidctl_client import LcdImageTransferError, LiquidctlClient, LiquidctlError
from BTCAM.models import StatusSnapshot
from BTCAM.renderer import LcdRenderer
from BTCAM.sensors import SystemSensorReader


def main() -> None:
    if "--self-test" in sys.argv:
        try:
            LiquidctlClient().list_devices()
        except LiquidctlError:
            raise SystemExit(1)
        raise SystemExit(0)

    if getattr(sys, "frozen", False):
        if not is_admin():
            _show_admin_required_message()
            return
    elif not ensure_admin_or_relaunch(args=["-m", "BTCAM.standalone", *sys.argv[1:]]):
        return

    if "--hardware-test" in sys.argv or "--lcd-test" in sys.argv:
        raise SystemExit(_hardware_test(write_lcd="--lcd-test" in sys.argv))

    BTCAMApp(start_minimized="--minimized" in sys.argv).mainloop()


def _hardware_test(write_lcd: bool = False) -> int:
    client = LiquidctlClient()
    try:
        if not client.list_devices():
            return 2
        with _ignore_liquidctl_errors():
            client.initialize()
        cooler = client.status()
        if write_lcd:
            config = AppConfig.load()
            snapshot = StatusSnapshot(
                cooler=cooler,
                system=SystemSensorReader().read(),
                captured_at=datetime.now(),
            )
            path = Path(tempfile.gettempdir()) / "btcam-lcd-test.png"
            LcdRenderer(config.display_size, background_path=config.background_image_path).save(snapshot, path)
            client.set_lcd_static(path)
    except LcdImageTransferError:
        return 5
    except LiquidctlError:
        return 3
    except Exception:
        return 4
    return 0


class _ignore_liquidctl_errors:
    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        return isinstance(exc, LiquidctlError)


def _show_admin_required_message() -> None:
    if sys.platform != "win32":
        return
    ctypes.windll.user32.MessageBoxW(
        None,
        "BTCAM deve essere avviato come amministratore.",
        "BTCAM",
        0x10,
    )


if __name__ == "__main__":
    main()
