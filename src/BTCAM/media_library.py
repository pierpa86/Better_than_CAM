from __future__ import annotations

import os
import shutil
from pathlib import Path

from PIL import Image, UnidentifiedImageError


def btcam_documents_dir() -> Path:
    return _documents_dir() / "BTCAM"


def _documents_dir() -> Path:
    if os.name == "nt":
        try:
            import winreg

            key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                value, _value_type = winreg.QueryValueEx(key, "Personal")
            documents = Path(os.path.expandvars(str(value)))
            if str(documents):
                return documents
        except (OSError, ImportError, ValueError):
            pass

    home = Path(os.environ.get("USERPROFILE") or Path.home())
    return home / "Documents"


def copy_gif_to_media_library(source: str | Path, library_dir: str | Path | None = None) -> Path:
    source_path = Path(source).expanduser()
    _validate_gif(source_path)

    target_dir = Path(library_dir).expanduser() if library_dir is not None else btcam_documents_dir()
    target_dir.mkdir(parents=True, exist_ok=True)

    source_path = source_path.resolve()
    target_dir = target_dir.resolve()
    if source_path.parent == target_dir:
        return source_path

    destination = _unique_destination(target_dir, _gif_filename(source_path))
    shutil.copy2(source_path, destination)
    return destination


def _validate_gif(path: Path) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"GIF not found: {path}")

    try:
        with Image.open(path) as image:
            if image.format != "GIF":
                raise ValueError("the selected file is not a GIF")
    except UnidentifiedImageError as exc:
        raise ValueError("the selected file is not a GIF") from exc


def _gif_filename(path: Path) -> str:
    stem = path.stem.strip() or "carousel"
    return f"{stem}.gif"


def _unique_destination(directory: Path, filename: str) -> Path:
    destination = directory / filename
    if not destination.exists():
        return destination

    stem = destination.stem
    suffix = destination.suffix
    counter = 2
    while True:
        candidate = directory / f"{stem}-{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1
