from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


APP_NAME = "BTCAM"
LEGACY_APP_NAME = "KrakenAltCam"
APP_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / APP_NAME
LEGACY_APP_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / LEGACY_APP_NAME
CONFIG_PATH = APP_DIR / "config.json"
LEGACY_CONFIG_PATH = LEGACY_APP_DIR / "config.json"


@dataclass(slots=True)
class AppConfig:
    liquidctl_path: str = "liquidctl"
    match: str = "kraken"
    interval_seconds: int = 1
    lcd_min_upload_interval_seconds: float = 1.0
    lcd_keepalive_seconds: float = 30.0
    lcd_transport: str = "static"
    brightness: int = 70
    orientation: int = 0
    display_size: int = 640
    output_dir: str = str(APP_DIR)
    initialize_on_start: bool = True
    start_lcd_on_launch: bool = False
    start_app_on_windows_login: bool = False
    minimize_to_tray_on_close: bool = False
    background_image_path: str | None = None
    carousel_enabled: bool = False
    carousel_gif_path: str | None = None
    carousel_phase_seconds: float = 5.0
    carousel_items: list[dict[str, str]] = field(default_factory=list)
    giphy_api_key: str = ""
    temperature_colors: dict[str, str] = field(default_factory=dict)
    temperature_elements: dict[str, bool] = field(default_factory=dict)
    temperature_layout: dict[str, dict[str, float]] = field(default_factory=dict)
    temperature_layout_mode: str = "detailed"
    temperature_center_source: str = "liquid"
    temperature_sources: dict[str, str] = field(default_factory=dict)
    temperature_center_title: str = "NZXT"

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> "AppConfig":
        default_path = path == CONFIG_PATH
        if default_path and not path.exists() and LEGACY_CONFIG_PATH.exists():
            path = LEGACY_CONFIG_PATH
        if not path.exists():
            return cls()
        try:
            values = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        allowed = {field for field in cls.__dataclass_fields__}
        filtered: dict[str, Any] = {k: v for k, v in values.items() if k in allowed}
        config = cls(**filtered)
        if not config.carousel_items and config.carousel_enabled and config.carousel_gif_path:
            config.carousel_items = [
                {"type": "temperature"},
                {"type": "gif", "path": config.carousel_gif_path},
            ]
        if default_path and path == LEGACY_CONFIG_PATH:
            if Path(config.output_dir) == LEGACY_APP_DIR:
                config.output_dir = str(APP_DIR)
            config.save(CONFIG_PATH)
        return config

    def save(self, path: Path = CONFIG_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    @property
    def output_path(self) -> Path:
        path = Path(self.output_dir)
        path.mkdir(parents=True, exist_ok=True)
        return path / "btcam-lcd.png"
