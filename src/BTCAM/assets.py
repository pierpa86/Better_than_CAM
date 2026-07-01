from __future__ import annotations

import sys
from pathlib import Path


APP_ICON_FILENAME = "btcam.ico"


def app_icon_path() -> Path | None:
    candidates: list[Path] = []
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        candidates.append(Path(bundle_root) / APP_ICON_FILENAME)
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).with_name(APP_ICON_FILENAME))
    candidates.append(Path(__file__).resolve().parents[2] / APP_ICON_FILENAME)

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None
