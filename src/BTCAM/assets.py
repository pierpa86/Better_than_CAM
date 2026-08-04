from __future__ import annotations

import sys
from pathlib import Path


APP_ICON_FILENAME = "btcam.ico"
PAWNIO_INSTALLER_FILENAME = "PawnIO_setup.exe"


def _bundled_resource_path(filename: str) -> Path | None:
    candidates: list[Path] = []
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        candidates.append(Path(bundle_root) / filename)
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).with_name(filename))
    candidates.append(Path(__file__).resolve().parents[2] / filename)

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def app_icon_path() -> Path | None:
    return _bundled_resource_path(APP_ICON_FILENAME)


def pawnio_installer_path() -> Path | None:
    return _bundled_resource_path(PAWNIO_INSTALLER_FILENAME)
