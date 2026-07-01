from __future__ import annotations

import argparse
import io
import math
import os
import queue
import tempfile
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox, ttk
from typing import Any

import psutil
from PIL import Image, ImageDraw, ImageOps, ImageSequence, ImageTk

from .assets import app_icon_path
from .carousel import (
    CAROUSEL_DURATION_OPTIONS,
    GIF_ITEM,
    TEMPERATURE_ITEM,
    CarouselGifPlayer,
    carousel_item_at,
    normalize_carousel_duration,
    normalize_carousel_items,
)
from .config import AppConfig
from .gif_editor import (
    clamp_crop_offset,
    initial_crop_zoom,
    render_cropped_gif_frame,
    sanitize_gif_stem,
    save_cropped_gif,
    save_lcd_ready_gif,
)
from .giphy_client import GiphyCategory, GiphyGif, download_giphy_file, giphy_categories, search_giphy, trending_giphy
from .liquidctl_client import LcdImageTransferError, LiquidctlClient, LiquidctlError
from .media_library import btcam_documents_dir, copy_gif_to_media_library
from .models import CoolerStatus, StatusSnapshot, SystemStatus
from .renderer import (
    DEFAULT_CENTER_GAUGE_LAYOUT,
    DEFAULT_DETAILED_TEMPERATURE_LAYOUT,
    DEFAULT_DUAL_TEMPERATURE_TITLE,
    DEFAULT_DUAL_TEMPERATURE_LAYOUT,
    DEFAULT_TEMPERATURE_COLORS,
    DEFAULT_TEMPERATURE_CENTER_TITLE,
    DEFAULT_TEMPERATURE_ELEMENTS,
    DEFAULT_TEMPERATURE_SLOT_SOURCES,
    DEGREE,
    LcdRenderer,
    TEMPERATURE_LAYOUT_CENTER_GAUGE,
    TEMPERATURE_LAYOUT_DETAILED,
    TEMPERATURE_LAYOUT_DUAL,
    normalize_temperature_colors,
    normalize_temperature_center_source,
    normalize_temperature_center_title,
    normalize_temperature_elements,
    normalize_temperature_layout,
    normalize_temperature_layout_mode,
    normalize_temperature_sources,
)
from .runtime import SnapshotProvider, simulated_snapshot
from .tray import TrayController
from .windows_startup import is_windows_startup_enabled, set_windows_startup


TEMPERATURE_COLOR_LABELS = {
    "background": "Background",
    "text": "Text",
    "gauge_start": "Gauge start",
    "gauge_end": "Gauge end",
    "gauge_track_start": "Track start",
    "gauge_track_end": "Track end",
    "center_gauge_start": "Center bar low",
    "center_gauge_end": "Center bar high",
    "center_track_start": "Center track low",
    "center_track_end": "Center track high",
    "dual_track": "Dual track",
    "dual_left_gauge": "Dual left bar",
    "dual_right_gauge": "Dual right bar",
    "divider": "Divider",
}

DETAILED_TEMPERATURE_COLOR_KEYS = (
    "background",
    "text",
    "gauge_start",
    "gauge_end",
    "gauge_track_start",
    "gauge_track_end",
    "divider",
)

CENTER_GAUGE_TEMPERATURE_COLOR_KEYS = (
    "background",
    "text",
    "center_gauge_start",
    "center_gauge_end",
    "center_track_start",
    "center_track_end",
)

DUAL_TEMPERATURE_COLOR_KEYS = (
    "background",
    "text",
    "dual_track",
    "dual_left_gauge",
    "dual_right_gauge",
)

LCD_GIF_OUTPUT_SIZE = 640
LCD_GIF_PALETTE_COLORS = 96
GIPHY_CROP_OUTPUT_SIZE = LCD_GIF_OUTPUT_SIZE
GIPHY_CROP_PALETTE_COLORS = LCD_GIF_PALETTE_COLORS
KRAKEN_NATIVE_BUCKET_MEMORY_BLOCKS = 24320
KRAKEN_NATIVE_BUCKET_HEADER_BYTES = 20

TEMPERATURE_ELEMENT_LABELS = {
    "gauge": "Gauge",
    "primary": "Main value",
    "divider": "Divider",
    "cpu": "Top right value",
    "liquid": "Bottom right value",
    "title": "Title text",
}

CENTER_GAUGE_ELEMENT_KEYS = ("gauge", "primary")
DUAL_TEMPERATURE_ELEMENT_KEYS = ("gauge", "cpu", "primary")

TEMPERATURE_LAYOUT_LABELS = {
    "gpu": "Main",
    "divider": "Divider",
    "cpu": "Top right",
    "liquid": "Bottom right",
    "center_gauge": "Gauge",
    "center_title": "Title",
    "center_primary": "Main value",
    "dual_title": "NZXT title",
    "dual_cpu": "CPU value",
    "dual_gpu": "GPU value",
}

DETAILED_TEMPERATURE_LAYOUT_KEYS = (*DEFAULT_DETAILED_TEMPERATURE_LAYOUT, "center_title")
CENTER_GAUGE_LAYOUT_KEYS = tuple(DEFAULT_CENTER_GAUGE_LAYOUT)
CENTER_GAUGE_HIT_TEST_KEYS = ("center_primary", "center_title")
CENTER_GAUGE_RESIZE_KEYS = ("center_primary", "center_title")
DUAL_TEMPERATURE_LAYOUT_KEYS = tuple(DEFAULT_DUAL_TEMPERATURE_LAYOUT)

TEMPERATURE_LAYOUT_MODE_LABELS = {
    TEMPERATURE_LAYOUT_DETAILED: "Triple infographic",
    TEMPERATURE_LAYOUT_CENTER_GAUGE: "Single Infographic",
    TEMPERATURE_LAYOUT_DUAL: "Dual infographic",
}

TEMPERATURE_LAYOUT_MODE_VALUES = {value: key for key, value in TEMPERATURE_LAYOUT_MODE_LABELS.items()}

TEMPERATURE_CENTER_SOURCE_LABELS = {
    "gpu": "GPU",
    "cpu": "CPU",
    "liquid": "Liquid",
}

TEMPERATURE_CENTER_SOURCE_VALUES = {value: key for key, value in TEMPERATURE_CENTER_SOURCE_LABELS.items()}


class NativeCarouselPreloadError(RuntimeError):
    pass


class BTCAMApp(tk.Tk):
    def __init__(self, start_minimized: bool = False) -> None:
        super().__init__()
        self.title("BTCAM")
        self.geometry("980x473")
        self.minsize(900, 473)
        self._window_icon_photo: ImageTk.PhotoImage | None = None
        self._apply_window_icon()
        if start_minimized:
            self.withdraw()

        self.config_model = AppConfig.load()
        self.queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.upload_worker: threading.Thread | None = None
        self.upload_event = threading.Event()
        self.latest_upload_snapshot: Any | None = None
        self.latest_upload_snapshot_lock = threading.Lock()
        self.options_window: tk.Toplevel | None = None
        self.temperature_editor_window: tk.Toplevel | None = None
        self.temperature_editor_preview_image: ImageTk.PhotoImage | None = None
        self.giphy_window: tk.Toplevel | None = None
        self.giphy_result_images: list[ImageTk.PhotoImage] = []
        self.giphy_preview_tiles: list[_AnimatedPreviewTile] = []
        self.giphy_crop_preview_image: ImageTk.PhotoImage | None = None
        self.carousel_strip: tk.Frame | None = None
        self.carousel_duration_combo: ttk.Combobox | None = None
        self.carousel_thumbnail_images: list[ImageTk.PhotoImage] = []
        self.carousel_item_canvases: list[tk.Canvas] = []
        self.carousel_item_image_ids: list[int] = []
        self.carousel_drop_indicator: tk.Frame | None = None
        self.carousel_drop_indicator_after_id: str | None = None
        self.carousel_drop_indicator_pulse = False
        self.selected_carousel_index: int | None = None
        self.carousel_drag_source_index: int | None = None
        self.carousel_drag_drop_index: int | None = None
        self.carousel_editing_locked = False
        self.temperature_carousel_preview_base: Image.Image | None = None
        self.temperature_carousel_preview_photos: dict[bool, ImageTk.PhotoImage] = {}
        self.is_exiting = False

        self._apply_hidden_gui_defaults()
        self.brightness_var = tk.IntVar(value=self.config_model.brightness)
        self.orientation_var = tk.IntVar(value=self.config_model.orientation)
        self.start_on_launch_var = tk.BooleanVar(value=self.config_model.start_lcd_on_launch)
        self.start_app_on_windows_login_var = tk.BooleanVar(value=is_windows_startup_enabled())
        self.minimize_to_tray_var = tk.BooleanVar(value=self.config_model.minimize_to_tray_on_close)
        self.giphy_api_key_var = tk.StringVar(value=self.config_model.giphy_api_key or os.environ.get("GIPHY_API_KEY", ""))
        self.carousel_items = self._copy_configured_carousel_gifs(
            normalize_carousel_items(self.config_model.carousel_items)
        )
        self.carousel_duration_var = tk.StringVar(
            value=_duration_label(normalize_carousel_duration(self.config_model.carousel_phase_seconds))
        )
        self.tray = TrayController(self, self._restore_from_tray, self._exit_application)

        self.status_var = tk.StringVar(value="Ready.")
        self.device_var = tk.StringVar(value="Device: not read")
        self.liquid_var = tk.StringVar(value=f"-- {DEGREE}C")
        self.pump_var = tk.StringVar(value="-- rpm")
        self.fan_var = tk.StringVar(value="-- rpm")
        self.cpu_var = tk.StringVar(value="--")
        self.gpu_var = tk.StringVar(value="--")
        self.memory_var = tk.StringVar(value="--")
        self.cam_warning_var = tk.StringVar(value="")

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close_request)
        self.after(250, self._drain_queue)
        self.after(1000, self._check_cam_process)
        if start_minimized:
            self.after(100, self._minimize_startup_to_tray)
        if self.config_model.start_lcd_on_launch:
            self.after(500, self.start_loop)

    def _apply_window_icon(self) -> None:
        icon_path = app_icon_path()
        if icon_path is None:
            return

        try:
            self.iconbitmap(default=str(icon_path))
        except tk.TclError:
            pass

        try:
            with Image.open(icon_path) as icon:
                image = icon.convert("RGBA")
            image.thumbnail((64, 64), Image.Resampling.LANCZOS)
            self._window_icon_photo = ImageTk.PhotoImage(image)
            self.iconphoto(True, self._window_icon_photo)
        except Exception:
            pass

    def _build_ui(self) -> None:
        self.configure(bg="#12181c")
        self._build_menu()
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background="#12181c")
        style.configure("Panel.TFrame", background="#182126")
        style.configure("TLabel", background="#12181c", foreground="#e9efed", font=("Segoe UI", 10))
        style.configure("Muted.TLabel", background="#12181c", foreground="#91a09d", font=("Segoe UI", 9))
        style.configure("Panel.TLabel", background="#182126", foreground="#e9efed", font=("Segoe UI", 10))
        style.configure("Value.TLabel", background="#182126", foreground="#f0f7f4", font=("Segoe UI Semibold", 20))
        style.configure("TButton", font=("Segoe UI", 10), padding=(10, 7))
        style.configure("Accent.TButton", font=("Segoe UI Semibold", 10), padding=(10, 7))
        style.configure("TCheckbutton", background="#12181c", foreground="#e9efed")
        style.configure("TRadiobutton", background="#12181c", foreground="#e9efed")
        style.configure(
            "Brightness.Horizontal.TScale",
            background="#747d7b",
            troughcolor="#12181c",
            bordercolor="#607174",
            lightcolor="#9ca4a2",
            darkcolor="#3f4a4c",
            sliderlength=16,
        )
        style.map(
            "Brightness.Horizontal.TScale",
            background=[("active", "#858e8c"), ("disabled", "#59615f")],
            troughcolor=[("active", "#12181c"), ("disabled", "#12181c")],
        )
        style.map(
            "TCheckbutton",
            background=[("active", "#182126"), ("selected", "#182126"), ("focus", "#182126")],
            foreground=[("active", "#ffffff"), ("selected", "#e9efed"), ("focus", "#e9efed")],
        )
        style.map(
            "TRadiobutton",
            background=[("active", "#182126"), ("selected", "#182126"), ("focus", "#182126")],
            foreground=[("active", "#ffffff"), ("selected", "#e9efed"), ("focus", "#e9efed")],
        )
        style.configure(
            "Dark.TCombobox",
            fieldbackground="#12181c",
            background="#182126",
            foreground="#e9efed",
            arrowcolor="#e9efed",
        )
        style.map(
            "Dark.TCombobox",
            fieldbackground=[("readonly", "#12181c")],
            foreground=[("readonly", "#e9efed")],
        )

        root = ttk.Frame(self, padding=18)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=0, minsize=310)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(2, weight=1, minsize=18)
        root.rowconfigure(4, weight=1)

        header_left = ttk.Frame(root)
        header_left.grid(row=0, column=0, sticky="ew", padx=(0, 18))

        title = ttk.Label(header_left, text="BTCAM", font=("Segoe UI Semibold", 18))
        title.pack(anchor="w")
        ttk.Label(header_left, text="Better then CAM", style="Muted.TLabel").pack(anchor="w")

        toolbar = ttk.Frame(root)
        toolbar.grid(row=0, column=1, sticky="ew")
        ttk.Button(toolbar, text="Detect", command=self.detect_devices).pack(side="left", padx=(0, 8))
        self.lcd_toggle_button = ttk.Button(toolbar, text="Start LCD", style="Accent.TButton", command=self.start_loop)
        self.lcd_toggle_button.pack(side="right", padx=(8, 0))

        left = ttk.Frame(root)
        left.grid(row=1, column=0, sticky="new", padx=(0, 18), pady=(18, 0))

        self._settings_panel(left)

        right = ttk.Frame(root)
        right.grid(row=1, column=1, sticky="new", pady=(18, 0))
        right.columnconfigure(0, weight=1)

        self._carousel_panel(right)

        bottom = ttk.Frame(right)
        bottom.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        ttk.Label(bottom, textvariable=self.status_var, style="Muted.TLabel").pack(side="left")
        ttk.Label(bottom, textvariable=self.cam_warning_var, foreground="#f5b352", background="#12181c").pack(side="right")

        status = ttk.Frame(root)
        status.grid(row=3, column=0, columnspan=2, sticky="ew")
        self._status_panel(status)

    def _build_menu(self) -> None:
        menu = tk.Menu(self)
        options_menu = tk.Menu(menu, tearoff=0)
        options_menu.add_command(label="Settings", command=self.open_options_window)
        options_menu.add_command(label="Temperature editor", command=self.open_temperature_editor)
        options_menu.add_separator()
        options_menu.add_command(label="Exit", command=self._exit_application)
        menu.add_cascade(label="Options", menu=options_menu)
        self.config(menu=menu)

    def _settings_panel(self, parent: ttk.Frame) -> None:
        panel = ttk.Frame(parent, style="Panel.TFrame", padding=14)
        panel.pack(fill="x")

        ttk.Label(panel, text="Settings", style="Panel.TLabel", font=("Segoe UI Semibold", 12)).pack(anchor="w")

        row = ttk.Frame(panel, style="Panel.TFrame")
        row.pack(fill="x", pady=(12, 0))
        ttk.Label(row, text="Brightness", style="Panel.TLabel").pack(side="left")
        ttk.Scale(
            row,
            from_=0,
            to=100,
            variable=self.brightness_var,
            orient="horizontal",
            style="Brightness.Horizontal.TScale",
        ).pack(side="right", fill="x", expand=True)

        orient = ttk.Frame(panel, style="Panel.TFrame")
        orient.pack(fill="x", pady=(12, 0))
        ttk.Label(orient, text="Orientation", style="Panel.TLabel").pack(anchor="w")
        buttons = ttk.Frame(orient, style="Panel.TFrame")
        buttons.pack(anchor="w", pady=(6, 0))
        for value in (0, 90, 180, 270):
            _dark_radiobutton(buttons, text=str(value), value=value, variable=self.orientation_var).pack(side="left", padx=(0, 12))

        ttk.Button(panel, text="Save settings", command=self.save_config).pack(fill="x", pady=(14, 0))

    def _carousel_panel(self, parent: ttk.Frame) -> None:
        panel_bg = "#182126"
        text_color = "#e9efed"

        panel = tk.Frame(parent, bg=panel_bg, padx=16, pady=12)
        panel.grid(row=0, column=0, sticky="ew")
        panel.columnconfigure(0, weight=1)

        header = tk.Frame(panel, bg=panel_bg)
        header.grid(row=0, column=0, sticky="ew")
        tk.Label(
            header,
            text="Carousel",
            bg=panel_bg,
            fg=text_color,
            font=("Segoe UI Semibold", 12),
        ).pack(side="left")

        duration = tk.Frame(header, bg=panel_bg)
        duration.pack(side="right")
        values = [_duration_label(seconds) for seconds in CAROUSEL_DURATION_OPTIONS]
        self.carousel_duration_combo = ttk.Combobox(
            duration,
            values=values,
            textvariable=self.carousel_duration_var,
            width=16,
            state="readonly",
            style="Dark.TCombobox",
        )
        self.carousel_duration_combo.pack(side="left")

        self.carousel_strip = tk.Frame(panel, bg=panel_bg)
        self.carousel_strip.grid(row=1, column=0, sticky="ew", pady=(14, 0))

        self._refresh_carousel_view()

    def _status_panel(self, parent: ttk.Frame) -> None:
        panel = ttk.Frame(parent, style="Panel.TFrame", padding=14)
        panel.pack(fill="x")

        header = ttk.Frame(panel, style="Panel.TFrame")
        header.pack(fill="x")
        ttk.Label(header, text="Status", style="Panel.TLabel", font=("Segoe UI Semibold", 12)).pack(side="left")
        ttk.Label(header, textvariable=self.device_var, style="Panel.TLabel", wraplength=760).pack(side="left", padx=(18, 0))

        grid = ttk.Frame(panel, style="Panel.TFrame")
        grid.pack(fill="x", pady=(10, 0))
        for index in range(6):
            grid.columnconfigure(index, weight=1, uniform="status")

        self._value_box(grid, "Liquid", self.liquid_var, 0, 0)
        self._value_box(grid, "Pump", self.pump_var, 0, 1)
        self._value_box(grid, "Fans", self.fan_var, 0, 2)
        self._value_box(grid, "CPU", self.cpu_var, 0, 3)
        self._value_box(grid, "GPU", self.gpu_var, 0, 4)
        self._value_box(grid, "RAM", self.memory_var, 0, 5)

    def _value_box(self, parent: ttk.Frame, label: str, variable: tk.StringVar, row: int, column: int) -> None:
        frame = ttk.Frame(parent, style="Panel.TFrame", padding=8)
        frame.grid(row=row, column=column, sticky="ew", padx=4, pady=4)
        ttk.Label(frame, text=label, style="Panel.TLabel").pack(anchor="w")
        ttk.Label(frame, textvariable=variable, style="Value.TLabel").pack(anchor="w")

    def save_config(self) -> None:
        if self._save_config():
            self.status_var.set("Settings saved.")

    def _save_config(self) -> bool:
        self._sync_config_from_ui()
        try:
            set_windows_startup(self.config_model.start_app_on_windows_login)
        except OSError as exc:
            messagebox.showerror("Options", f"Unable to update Windows startup:\n{exc}")
            return False
        self.config_model.save()
        return True

    def open_options_window(self) -> None:
        if self.options_window is not None and self.options_window.winfo_exists():
            self.options_window.lift()
            self.options_window.focus_force()
            return

        window = tk.Toplevel(self)
        self.options_window = window
        window.title("Options")
        window.configure(bg="#12181c")
        window.resizable(False, False)
        window.transient(self)

        start_lcd_var = tk.BooleanVar(window, value=self.start_on_launch_var.get())
        windows_start_var = tk.BooleanVar(window, value=self.start_app_on_windows_login_var.get())
        tray_close_var = tk.BooleanVar(window, value=self.minimize_to_tray_var.get())
        giphy_api_key_var = tk.StringVar(window, value=self.giphy_api_key_var.get())

        panel = ttk.Frame(window, style="Panel.TFrame", padding=16)
        panel.pack(fill="both", expand=True, padx=14, pady=14)

        ttk.Label(panel, text="Options", style="Panel.TLabel", font=("Segoe UI Semibold", 12)).pack(anchor="w")
        _dark_checkbutton(
            panel,
            text="Auto start",
            variable=start_lcd_var,
        ).pack(anchor="w", pady=(14, 0))
        _dark_checkbutton(
            panel,
            text="Windows startup",
            variable=windows_start_var,
        ).pack(anchor="w", pady=(10, 0))
        _dark_checkbutton(
            panel,
            text="Minimize to tray",
            variable=tray_close_var,
        ).pack(anchor="w", pady=(10, 0))
        ttk.Label(panel, text="GIPHY API key", style="Panel.TLabel").pack(anchor="w", pady=(16, 0))
        giphy_entry = tk.Entry(
            panel,
            textvariable=giphy_api_key_var,
            bg="#10171b",
            fg="#e9efed",
            insertbackground="#e9efed",
            relief="flat",
            highlightthickness=1,
            highlightbackground="#314047",
            highlightcolor="#4da3ff",
            width=42,
            show="*",
        )
        giphy_entry.pack(fill="x", pady=(6, 0))

        buttons = ttk.Frame(panel, style="Panel.TFrame")
        buttons.pack(fill="x", pady=(16, 0))

        def close_window() -> None:
            self.options_window = None
            window.destroy()

        def save_options() -> None:
            self.start_on_launch_var.set(start_lcd_var.get())
            self.start_app_on_windows_login_var.set(windows_start_var.get())
            self.minimize_to_tray_var.set(tray_close_var.get())
            self.giphy_api_key_var.set(giphy_api_key_var.get().strip())
            if self._save_config():
                self.status_var.set("Options saved.")
                close_window()

        ttk.Button(buttons, text="Cancel", command=close_window).pack(side="right")
        ttk.Button(buttons, text="Save", style="Accent.TButton", command=save_options).pack(side="right", padx=(0, 8))

        window.protocol("WM_DELETE_WINDOW", close_window)

    def open_temperature_editor(self) -> None:
        if self.temperature_editor_window is not None and self.temperature_editor_window.winfo_exists():
            self.temperature_editor_window.lift()
            self.temperature_editor_window.focus_force()
            return

        window = tk.Toplevel(self)
        self.temperature_editor_window = window
        window.title("Temperature Editor")
        window.configure(bg="#12181c")
        window.resizable(False, False)
        window.transient(self)

        colors = normalize_temperature_colors(self.config_model.temperature_colors)
        elements = normalize_temperature_elements(self.config_model.temperature_elements)
        layout = normalize_temperature_layout(self.config_model.temperature_layout)
        layout_mode = normalize_temperature_layout_mode(self.config_model.temperature_layout_mode)
        center_source = normalize_temperature_center_source(self.config_model.temperature_center_source)
        center_title = normalize_temperature_center_title(self.config_model.temperature_center_title)
        if layout_mode == TEMPERATURE_LAYOUT_DUAL and center_title == DEFAULT_TEMPERATURE_CENTER_TITLE:
            center_title = DEFAULT_DUAL_TEMPERATURE_TITLE
        temperature_sources = normalize_temperature_sources(self.config_model.temperature_sources)
        color_vars = {key: tk.StringVar(window, value=value) for key, value in colors.items()}
        element_vars = {key: tk.BooleanVar(window, value=value) for key, value in elements.items()}
        layout_mode_var = tk.StringVar(window, value=TEMPERATURE_LAYOUT_MODE_LABELS[layout_mode])
        center_source_var = tk.StringVar(window, value=TEMPERATURE_CENTER_SOURCE_LABELS[center_source])
        center_title_var = tk.StringVar(window, value=center_title)
        source_vars = {
            key: tk.StringVar(window, value=TEMPERATURE_CENTER_SOURCE_LABELS[value])
            for key, value in temperature_sources.items()
        }
        layout_vars = {
            key: {
                "x": tk.DoubleVar(window, value=value["x"]),
                "y": tk.DoubleVar(window, value=value["y"]),
                "scale": tk.DoubleVar(window, value=value["scale"]),
            }
            for key, value in layout.items()
        }
        editor_drag_key: list[str | None] = [None]
        editor_drag_offset: list[tuple[float, float] | None] = [None]
        selected_layout_key: list[str | None] = [None]
        move_list_keys: list[str] = []
        editor_snapshot = _temperature_editor_snapshot()

        root = ttk.Frame(window, padding=16)
        root.pack(fill="both", expand=True)

        preview_panel = ttk.Frame(root, style="Panel.TFrame", padding=14)
        preview_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        ttk.Label(preview_panel, text="Preview", style="Panel.TLabel", font=("Segoe UI Semibold", 12)).pack(anchor="w")
        preview_canvas = tk.Canvas(
            preview_panel,
            width=260,
            height=260,
            bg="#12181c",
            highlightthickness=0,
            bd=0,
            cursor="fleur",
        )
        preview_canvas.pack(pady=(12, 0))

        move_section = ttk.Frame(preview_panel, style="Panel.TFrame")
        move_section.pack(fill="x", pady=(14, 0))
        ttk.Label(move_section, text="Move", style="Panel.TLabel", font=("Segoe UI Semibold", 12)).pack(anchor="w")
        move_listbox = tk.Listbox(
            move_section,
            height=5,
            exportselection=False,
            activestyle="dotbox",
            bg="#12181c",
            fg="#e9efed",
            selectbackground="#254258",
            selectforeground="#ffffff",
            highlightthickness=1,
            highlightbackground="#314047",
            highlightcolor="#4da3ff",
            relief="flat",
            font=("Segoe UI", 10),
        )
        move_listbox.pack(fill="x", pady=(8, 0))

        controls = ttk.Frame(root, style="Panel.TFrame", padding=14)
        controls.grid(row=0, column=1, sticky="nsew")
        ttk.Label(controls, text="Layout", style="Panel.TLabel", font=("Segoe UI Semibold", 12)).pack(anchor="w")

        layout_row = tk.Frame(controls, bg="#182126")
        layout_row.pack(fill="x", pady=(10, 0))
        tk.Label(layout_row, text="Style", bg="#182126", fg="#e9efed", font=("Segoe UI", 10), width=16, anchor="w").pack(side="left")
        layout_combo = ttk.Combobox(
            layout_row,
            values=list(TEMPERATURE_LAYOUT_MODE_VALUES),
            textvariable=layout_mode_var,
            width=20,
            state="readonly",
            style="Dark.TCombobox",
        )
        layout_combo.pack(side="right")

        center_controls = ttk.Frame(controls, style="Panel.TFrame")
        center_controls.pack(fill="x")

        source_row = tk.Frame(center_controls, bg="#182126")
        source_row.pack(fill="x", pady=(10, 0))
        tk.Label(source_row, text="Center value", bg="#182126", fg="#e9efed", font=("Segoe UI", 10), width=16, anchor="w").pack(side="left")
        source_combo = ttk.Combobox(
            source_row,
            values=list(TEMPERATURE_CENTER_SOURCE_VALUES),
            textvariable=center_source_var,
            width=14,
            state="readonly",
            style="Dark.TCombobox",
        )
        source_combo.pack(side="right")

        title_controls = ttk.Frame(controls, style="Panel.TFrame")
        title_controls.pack(fill="x")

        title_row = tk.Frame(title_controls, bg="#182126")
        title_row.pack(fill="x", pady=(10, 0))
        tk.Label(title_row, text="Title text", bg="#182126", fg="#e9efed", font=("Segoe UI", 10), width=16, anchor="w").pack(side="left")
        title_entry = tk.Entry(
            title_row,
            textvariable=center_title_var,
            width=16,
            bg="#12181c",
            fg="#e9efed",
            insertbackground="#e9efed",
            relief="flat",
            highlightthickness=1,
            highlightbackground="#314047",
            highlightcolor="#4da3ff",
            font=("Segoe UI", 10),
        )
        title_entry.pack(side="right")

        detailed_controls = ttk.Frame(controls, style="Panel.TFrame")
        detailed_controls.pack(fill="x")
        ttk.Label(detailed_controls, text="Triple infographic values", style="Panel.TLabel", font=("Segoe UI Semibold", 12)).pack(anchor="w", pady=(18, 0))
        source_combos: list[ttk.Combobox] = []
        for key in DEFAULT_TEMPERATURE_SLOT_SOURCES:
            row = tk.Frame(detailed_controls, bg="#182126")
            row.pack(fill="x", pady=(10, 0))
            tk.Label(
                row,
                text=TEMPERATURE_LAYOUT_LABELS[key],
                bg="#182126",
                fg="#e9efed",
                font=("Segoe UI", 10),
                width=16,
                anchor="w",
            ).pack(side="left")
            combo = ttk.Combobox(
                row,
                values=list(TEMPERATURE_CENTER_SOURCE_VALUES),
                textvariable=source_vars[key],
                width=14,
                state="readonly",
                style="Dark.TCombobox",
            )
            combo.pack(side="right")
            source_combos.append(combo)

        colors_section = ttk.Frame(controls, style="Panel.TFrame")
        colors_section.pack(fill="x")
        ttk.Label(colors_section, text="Colors", style="Panel.TLabel", font=("Segoe UI Semibold", 12)).pack(anchor="w", pady=(18, 0))

        color_buttons: dict[str, tk.Button] = {}
        color_rows: dict[str, tk.Frame] = {}

        def current_colors() -> dict[str, str]:
            return {key: variable.get() for key, variable in color_vars.items()}

        def current_elements() -> dict[str, bool]:
            return {key: bool(variable.get()) for key, variable in element_vars.items()}

        def current_layout() -> dict[str, dict[str, float]]:
            return {
                key: {
                    "x": variables["x"].get(),
                    "y": variables["y"].get(),
                    "scale": variables["scale"].get(),
                }
                for key, variables in layout_vars.items()
            }

        def current_layout_mode() -> str:
            return TEMPERATURE_LAYOUT_MODE_VALUES.get(layout_mode_var.get(), TEMPERATURE_LAYOUT_DETAILED)

        def current_center_source() -> str:
            return TEMPERATURE_CENTER_SOURCE_VALUES.get(center_source_var.get(), "liquid")

        def current_center_title() -> str:
            return normalize_temperature_center_title(center_title_var.get())

        def current_temperature_sources() -> dict[str, str]:
            return {
                key: TEMPERATURE_CENTER_SOURCE_VALUES.get(variable.get(), DEFAULT_TEMPERATURE_SLOT_SOURCES[key])
                for key, variable in source_vars.items()
            }

        def select_move_key(key: str | None) -> None:
            selected_layout_key[0] = key
            move_listbox.selection_clear(0, "end")
            if key is None or key not in move_list_keys:
                return
            index = move_list_keys.index(key)
            move_listbox.selection_set(index)
            move_listbox.activate(index)

        def refresh_move_list() -> None:
            previous = selected_layout_key[0]
            move_listbox.delete(0, "end")
            move_list_keys[:] = list(_temperature_editor_movable_keys(current_layout_mode(), current_elements()))
            for key in move_list_keys:
                move_listbox.insert("end", TEMPERATURE_LAYOUT_LABELS[key])
            if previous not in move_list_keys:
                previous = None
            select_move_key(previous)

        def on_move_list_select(_event: tk.Event) -> None:
            selection = move_listbox.curselection()
            if not selection:
                return
            index = int(selection[0])
            if 0 <= index < len(move_list_keys):
                selected_layout_key[0] = move_list_keys[index]
                update_preview()

        def update_preview() -> None:
            preview_size = 260
            renderer = LcdRenderer(
                320,
                background_path=self.config_model.background_image_path,
                temperature_colors=current_colors(),
                temperature_elements=current_elements(),
                temperature_layout=current_layout(),
                temperature_layout_mode=current_layout_mode(),
                temperature_center_source=current_center_source(),
                temperature_sources=current_temperature_sources(),
                temperature_center_title=current_center_title(),
            )
            image = renderer.render(editor_snapshot)
            image = image.resize((preview_size, preview_size), Image.Resampling.LANCZOS)
            self.temperature_editor_preview_image = ImageTk.PhotoImage(image)
            preview_canvas.delete("all")
            preview_canvas.create_image(0, 0, anchor="nw", image=self.temperature_editor_preview_image)
            _draw_temperature_editor_handles(
                preview_canvas,
                current_layout(),
                current_elements(),
                preview_size,
                current_layout_mode(),
                selected_layout_key[0],
            )
            for key, button in color_buttons.items():
                color = color_vars[key].get()
                text_color = _readable_swatch_text(color)
                button.configure(
                    text=color,
                    bg=color,
                    activebackground=color,
                    fg=text_color,
                    activeforeground=text_color,
                )

        def choose_color(key: str) -> None:
            chosen = colorchooser.askcolor(color=color_vars[key].get(), parent=window, title=f"Choose {TEMPERATURE_COLOR_LABELS[key]}")
            if chosen[1]:
                color_vars[key].set(chosen[1].lower())
                update_preview()

        def layout_key_at(x: int, y: int) -> str | None:
            for key in _temperature_editor_hit_test_keys(current_layout_mode()):
                variables = layout_vars[key]
                if not _temperature_layout_visible(key, current_elements(), current_layout_mode()):
                    continue
                bounds = _temperature_editor_bounds(
                    key,
                    {
                        "x": variables["x"].get(),
                        "y": variables["y"].get(),
                        "scale": variables["scale"].get(),
                    },
                    260,
                )
                if bounds[0] <= x <= bounds[2] and bounds[1] <= y <= bounds[3]:
                    return key
            return None

        def start_layout_drag(event: tk.Event) -> None:
            preview_canvas.focus_set()
            key = layout_key_at(int(event.x), int(event.y))
            if key is not None:
                select_move_key(key)
                update_preview()
            else:
                select_move_key(None)
                update_preview()
                editor_drag_key[0] = None
                editor_drag_offset[0] = None
                return
            editor_drag_key[0] = key
            if key is None:
                editor_drag_offset[0] = None
                return
            editor_drag_offset[0] = (
                float(event.x) / 260 - layout_vars[key]["x"].get(),
                float(event.y) / 260 - layout_vars[key]["y"].get(),
            )

        def update_layout_drag(event: tk.Event) -> None:
            key = editor_drag_key[0]
            offset = editor_drag_offset[0]
            if key is None or offset is None:
                return
            x = float(event.x) / 260 - offset[0]
            y = float(event.y) / 260 - offset[1]
            x, y = _snap_editor_layout_position(key, x, y, layout_vars[key]["scale"].get())
            layout_vars[key]["x"].set(x)
            layout_vars[key]["y"].set(y)
            update_preview()

        def finish_layout_drag(_event: tk.Event) -> None:
            editor_drag_key[0] = None
            editor_drag_offset[0] = None

        def move_selected_layout_key(dx: int, dy: int) -> None:
            key = selected_layout_key[0]
            if key is None:
                return
            step = 1 / 260
            x = layout_vars[key]["x"].get() + dx * step
            y = layout_vars[key]["y"].get() + dy * step
            x, y = _snap_editor_layout_position(key, x, y, layout_vars[key]["scale"].get(), snap_to_center=False)
            layout_vars[key]["x"].set(x)
            layout_vars[key]["y"].set(y)
            update_preview()

        def move_selected_with_key(event: tk.Event) -> str | None:
            deltas = {
                "Left": (-1, 0),
                "Right": (1, 0),
                "Up": (0, -1),
                "Down": (0, 1),
            }
            delta = deltas.get(str(event.keysym))
            if delta is None or selected_layout_key[0] is None:
                return None
            move_selected_layout_key(*delta)
            return "break"

        preview_canvas.bind("<ButtonPress-1>", start_layout_drag)
        preview_canvas.bind("<B1-Motion>", update_layout_drag)
        preview_canvas.bind("<ButtonRelease-1>", finish_layout_drag)
        move_listbox.bind("<<ListboxSelect>>", on_move_list_select)
        for widget in (preview_canvas, move_listbox):
            widget.bind("<Left>", move_selected_with_key)
            widget.bind("<Right>", move_selected_with_key)
            widget.bind("<Up>", move_selected_with_key)
            widget.bind("<Down>", move_selected_with_key)
        layout_combo.bind("<<ComboboxSelected>>", lambda _event: update_preview())
        source_combo.bind("<<ComboboxSelected>>", lambda _event: update_preview())
        title_entry.bind("<KeyRelease>", lambda _event: update_preview())
        for combo in source_combos:
            combo.bind("<<ComboboxSelected>>", lambda _event: update_preview())

        for key in DEFAULT_TEMPERATURE_COLORS:
            row = tk.Frame(colors_section, bg="#182126")
            row.pack(fill="x", pady=(10, 0))
            color_rows[key] = row
            tk.Label(
                row,
                text=TEMPERATURE_COLOR_LABELS[key],
                bg="#182126",
                fg="#e9efed",
                font=("Segoe UI", 10),
                width=16,
                anchor="w",
            ).pack(side="left")
            button = tk.Button(
                row,
                text=color_vars[key].get(),
                command=lambda color_key=key: choose_color(color_key),
                width=10,
                bg=color_vars[key].get(),
                fg=_readable_swatch_text(color_vars[key].get()),
                activeforeground=_readable_swatch_text(color_vars[key].get()),
                relief="flat",
            )
            button.pack(side="right")
            color_buttons[key] = button

        elements_section = ttk.Frame(preview_panel, style="Panel.TFrame")
        elements_section.pack(fill="x")
        ttk.Label(elements_section, text="Elements", style="Panel.TLabel", font=("Segoe UI Semibold", 12)).pack(anchor="w", pady=(18, 0))
        element_widgets: dict[str, tk.Checkbutton] = {}
        for key in DEFAULT_TEMPERATURE_ELEMENTS:
            checkbox = _dark_checkbutton(elements_section, text=TEMPERATURE_ELEMENT_LABELS[key], variable=element_vars[key])
            checkbox.configure(command=lambda: (refresh_move_list(), update_preview()))
            checkbox.pack(anchor="w", pady=(8, 0))
            element_widgets[key] = checkbox

        resize_section = ttk.Frame(controls, style="Panel.TFrame")
        resize_section.pack(fill="x")
        ttk.Label(resize_section, text="Resize", style="Panel.TLabel", font=("Segoe UI Semibold", 12)).pack(anchor="w", pady=(18, 0))
        resize_rows: dict[str, tk.Frame] = {}
        for key in dict.fromkeys((*DETAILED_TEMPERATURE_LAYOUT_KEYS, *CENTER_GAUGE_RESIZE_KEYS, *DUAL_TEMPERATURE_LAYOUT_KEYS)):
            row = tk.Frame(resize_section, bg="#182126")
            resize_rows[key] = row
            row.pack(fill="x", pady=(8, 0))
            tk.Label(
                row,
                text=TEMPERATURE_LAYOUT_LABELS[key],
                bg="#182126",
                fg="#e9efed",
                font=("Segoe UI", 10),
                width=12,
                anchor="w",
            ).pack(side="left")
            scale = tk.Scale(
                row,
                from_=0.5,
                to=1.6,
                resolution=0.05,
                orient="horizontal",
                variable=layout_vars[key]["scale"],
                command=lambda _value: update_preview(),
                bg="#182126",
                fg="#e9efed",
                activebackground="#223038",
                troughcolor="#12181c",
                highlightthickness=0,
                length=150,
                showvalue=True,
            )
            scale.pack(side="right")

        def refresh_style_sections() -> None:
            layout_mode_value = current_layout_mode()
            is_detailed = layout_mode_value == TEMPERATURE_LAYOUT_DETAILED
            is_dual = layout_mode_value == TEMPERATURE_LAYOUT_DUAL
            center_controls.pack_forget()
            title_controls.pack_forget()
            detailed_controls.pack_forget()
            resize_section.pack_forget()
            if is_detailed:
                detailed_controls.pack(fill="x", before=colors_section)
                title_controls.pack(fill="x", before=colors_section)
            elif is_dual:
                title_controls.pack(fill="x", before=colors_section)
            elif not is_dual:
                center_controls.pack(fill="x", before=colors_section)
                title_controls.pack(fill="x", before=colors_section)
            resize_section.pack(fill="x", after=colors_section)

            if is_detailed:
                color_keys = DETAILED_TEMPERATURE_COLOR_KEYS
            elif is_dual:
                color_keys = DUAL_TEMPERATURE_COLOR_KEYS
            else:
                color_keys = CENTER_GAUGE_TEMPERATURE_COLOR_KEYS
            for row in color_rows.values():
                row.pack_forget()
            for key in color_keys:
                color_rows[key].pack(fill="x", pady=(10, 0))

            if is_detailed:
                element_keys = DEFAULT_TEMPERATURE_ELEMENTS
            elif is_dual:
                element_keys = DUAL_TEMPERATURE_ELEMENT_KEYS
            else:
                element_keys = CENTER_GAUGE_ELEMENT_KEYS
            for widget in element_widgets.values():
                widget.pack_forget()
            for key in element_keys:
                element_widgets[key].pack(anchor="w", pady=(8, 0))

            if is_detailed:
                resize_keys = DETAILED_TEMPERATURE_LAYOUT_KEYS
            elif is_dual:
                resize_keys = DUAL_TEMPERATURE_LAYOUT_KEYS
            else:
                resize_keys = CENTER_GAUGE_RESIZE_KEYS
            for row in resize_rows.values():
                row.pack_forget()
            for key in resize_keys:
                resize_rows[key].pack(fill="x", pady=(8, 0))

            refresh_move_list()
            preview_canvas.configure(cursor="fleur")
            window.update_idletasks()

        def change_layout_style() -> None:
            if current_layout_mode() == TEMPERATURE_LAYOUT_DUAL and current_center_title() == DEFAULT_TEMPERATURE_CENTER_TITLE:
                center_title_var.set(DEFAULT_DUAL_TEMPERATURE_TITLE)
            refresh_style_sections()
            update_preview()

        layout_combo.bind("<<ComboboxSelected>>", lambda _event: change_layout_style())

        buttons = ttk.Frame(root)
        buttons.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(14, 0))

        def close_window() -> None:
            self.temperature_editor_window = None
            self.temperature_editor_preview_image = None
            window.destroy()

        def reset_editor() -> None:
            if current_layout_mode() == TEMPERATURE_LAYOUT_DETAILED:
                for key in DETAILED_TEMPERATURE_COLOR_KEYS:
                    color_vars[key].set(DEFAULT_TEMPERATURE_COLORS[key])
                for key, value in DEFAULT_TEMPERATURE_ELEMENTS.items():
                    element_vars[key].set(value)
                for key, value in DEFAULT_DETAILED_TEMPERATURE_LAYOUT.items():
                    layout_vars[key]["x"].set(value["x"])
                    layout_vars[key]["y"].set(value["y"])
                    layout_vars[key]["scale"].set(value["scale"])
                title_layout = DEFAULT_CENTER_GAUGE_LAYOUT["center_title"]
                layout_vars["center_title"]["x"].set(title_layout["x"])
                layout_vars["center_title"]["y"].set(title_layout["y"])
                layout_vars["center_title"]["scale"].set(title_layout["scale"])
                for key, value in DEFAULT_TEMPERATURE_SLOT_SOURCES.items():
                    source_vars[key].set(TEMPERATURE_CENTER_SOURCE_LABELS[value])
                center_title_var.set(DEFAULT_TEMPERATURE_CENTER_TITLE)
            elif current_layout_mode() == TEMPERATURE_LAYOUT_DUAL:
                for key in DUAL_TEMPERATURE_COLOR_KEYS:
                    color_vars[key].set(DEFAULT_TEMPERATURE_COLORS[key])
                for key in DUAL_TEMPERATURE_ELEMENT_KEYS:
                    element_vars[key].set(DEFAULT_TEMPERATURE_ELEMENTS[key])
                for key, value in DEFAULT_DUAL_TEMPERATURE_LAYOUT.items():
                    layout_vars[key]["x"].set(value["x"])
                    layout_vars[key]["y"].set(value["y"])
                    layout_vars[key]["scale"].set(value["scale"])
                center_title_var.set(DEFAULT_DUAL_TEMPERATURE_TITLE)
            else:
                for key in CENTER_GAUGE_TEMPERATURE_COLOR_KEYS:
                    color_vars[key].set(DEFAULT_TEMPERATURE_COLORS[key])
                for key in CENTER_GAUGE_ELEMENT_KEYS:
                    element_vars[key].set(DEFAULT_TEMPERATURE_ELEMENTS[key])
                for key, value in DEFAULT_CENTER_GAUGE_LAYOUT.items():
                    layout_vars[key]["x"].set(value["x"])
                    layout_vars[key]["y"].set(value["y"])
                    layout_vars[key]["scale"].set(value["scale"])
                center_source_var.set(TEMPERATURE_CENTER_SOURCE_LABELS["liquid"])
                center_title_var.set(DEFAULT_TEMPERATURE_CENTER_TITLE)
            refresh_style_sections()
            update_preview()

        def save_editor() -> None:
            self.config_model.temperature_colors = normalize_temperature_colors(current_colors())
            self.config_model.temperature_elements = normalize_temperature_elements(current_elements())
            self.config_model.temperature_layout = normalize_temperature_layout(current_layout())
            self.config_model.temperature_layout_mode = normalize_temperature_layout_mode(current_layout_mode())
            self.config_model.temperature_center_source = normalize_temperature_center_source(current_center_source())
            self.config_model.temperature_sources = normalize_temperature_sources(current_temperature_sources())
            self.config_model.temperature_center_title = normalize_temperature_center_title(current_center_title())
            self._clear_temperature_preview_cache()
            if self._save_config():
                self.status_var.set("Temperature editor saved.")
                close_window()

        ttk.Button(buttons, text="Cancel", command=close_window).pack(side="right")
        ttk.Button(buttons, text="Save", style="Accent.TButton", command=save_editor).pack(side="right", padx=(0, 8))
        ttk.Button(buttons, text="Reset", command=reset_editor).pack(side="left")

        window.protocol("WM_DELETE_WINDOW", close_window)
        refresh_style_sections()
        update_preview()

    def _set_carousel_editing_locked(self, locked: bool) -> None:
        locked = bool(locked)
        if self.carousel_editing_locked == locked:
            return
        self.carousel_editing_locked = locked
        self._hide_carousel_drop_indicator()
        if self.carousel_duration_combo is not None:
            self.carousel_duration_combo.configure(state="disabled" if locked else "readonly")
        self._refresh_carousel_view(select_index=self.selected_carousel_index)

    def _require_carousel_stopped(self) -> bool:
        if not self.carousel_editing_locked:
            return True
        self.status_var.set("Stop LCD before editing carousel.")
        return False

    def _add_temperature_item(self) -> None:
        if not self._require_carousel_stopped():
            return
        self.carousel_items.append({"type": TEMPERATURE_ITEM})
        self._refresh_carousel_view(select_index=len(self.carousel_items) - 1)

    def _add_gif_item(self) -> None:
        if not self._require_carousel_stopped():
            return
        path = filedialog.askopenfilename(
            title="Choose carousel GIF",
            filetypes=(("GIF", "*.gif"), ("All files", "*.*")),
        )
        if path:
            copied_path = self._copy_selected_gif(path)
            if copied_path is None:
                return
            self.carousel_items.append({"type": GIF_ITEM, "path": copied_path})
            self._refresh_carousel_view(select_index=len(self.carousel_items) - 1)

    def _add_giphy_item(self) -> None:
        if not self._require_carousel_stopped():
            return
        self._open_giphy_picker()

    def _open_giphy_picker(self) -> None:
        if not self._require_carousel_stopped():
            return
        if self.giphy_window is not None and self.giphy_window.winfo_exists():
            self.giphy_window.lift()
            self.giphy_window.focus_force()
            return

        page_limit = 16
        grid_columns = 4
        preview_width = 150
        preview_height = 88
        result_tile_height = 118
        category_tile_height = 138
        window = tk.Toplevel(self)
        self.giphy_window = window
        window.title("Giphy")
        window.configure(bg="#12181c")
        window.geometry("760x820")
        window.minsize(700, 760)
        window.transient(self)

        query_var = tk.StringVar(window)
        status_var = tk.StringVar(window, value="GIPHY categories.")
        page_var = tk.StringVar(window, value="")
        state: dict[str, Any] = {"mode": "categories", "query": "", "offset": 0, "last_count": 0}

        root = ttk.Frame(window, padding=16)
        root.pack(fill="both", expand=True)
        root.rowconfigure(3, weight=1)
        root.columnconfigure(0, weight=1)

        header = ttk.Frame(root)
        header.grid(row=0, column=0, sticky="ew")
        ttk.Label(header, text="GIPHY", font=("Segoe UI Semibold", 20)).pack(side="left")
        ttk.Label(header, text="powered by GIPHY", style="Muted.TLabel").pack(side="left", padx=(14, 0), pady=(8, 0))

        form = ttk.Frame(root)
        form.grid(row=1, column=0, sticky="ew", pady=(14, 10))
        form.columnconfigure(1, weight=1)
        ttk.Label(form, text="Search").grid(row=0, column=0, sticky="w", padx=(0, 8))
        search_entry = tk.Entry(
            form,
            textvariable=query_var,
            bg="#182126",
            fg="#e9efed",
            insertbackground="#e9efed",
            relief="flat",
            highlightthickness=1,
            highlightbackground="#314047",
            highlightcolor="#4da3ff",
        )
        search_entry.grid(row=0, column=1, sticky="ew")
        buttons = ttk.Frame(form)
        buttons.grid(row=0, column=2, sticky="e", padx=(8, 0))

        nav = ttk.Frame(root)
        nav.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        nav.columnconfigure(2, weight=1)

        results_canvas = tk.Canvas(root, bg="#12181c", highlightthickness=0, bd=0)
        results_canvas.grid(row=3, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(root, orient="vertical", command=results_canvas.yview)
        scrollbar.grid(row=3, column=1, sticky="ns")
        results_canvas.configure(yscrollcommand=scrollbar.set)
        results_frame = tk.Frame(results_canvas, bg="#12181c")
        results_canvas.create_window((0, 0), window=results_frame, anchor="nw")
        results_frame.bind("<Configure>", lambda _event: results_canvas.configure(scrollregion=results_canvas.bbox("all")))

        def scroll_results(event: tk.Event) -> str:
            delta = getattr(event, "delta", 0)
            if delta:
                steps = int(delta / 120)
                if steps == 0:
                    steps = 1 if delta > 0 else -1
                units = -max(-3, min(3, steps))
            else:
                units = -3 if getattr(event, "num", 0) == 4 else 3
            results_canvas.yview_scroll(units, "units")
            return "break"

        window.bind("<MouseWheel>", scroll_results)
        results_canvas.bind("<MouseWheel>", scroll_results)
        results_frame.bind("<MouseWheel>", scroll_results)
        window.bind("<Button-4>", scroll_results)
        window.bind("<Button-5>", scroll_results)

        footer = ttk.Frame(root)
        footer.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        ttk.Label(footer, textvariable=status_var, style="Muted.TLabel").pack(side="left")

        def close_window() -> None:
            self.giphy_window = None
            for tile in self.giphy_preview_tiles:
                tile.stop()
            self.giphy_preview_tiles = []
            self.giphy_result_images = []
            window.destroy()

        def clear_results() -> None:
            for tile in self.giphy_preview_tiles:
                tile.stop()
            self.giphy_preview_tiles = []
            for child in results_frame.winfo_children():
                child.destroy()
            self.giphy_result_images = []
            results_canvas.yview_moveto(0)

        def api_key() -> str | None:
            value = self.giphy_api_key_var.get().strip() or os.environ.get("GIPHY_API_KEY", "").strip()
            if not value:
                messagebox.showerror("Giphy", "Set the GIPHY API key in Options.")
                return None
            self.giphy_api_key_var.set(value)
            self.config_model.giphy_api_key = value
            return value

        def refresh_pagination() -> None:
            mode = str(state["mode"])
            offset = int(state["offset"])
            last_count = int(state["last_count"])
            page_var.set("" if mode == "categories" else f"Page {offset // page_limit + 1}")
            previous_button.configure(state=("normal" if mode != "categories" and offset > 0 else "disabled"))
            next_button.configure(state=("normal" if mode != "categories" and last_count >= page_limit else "disabled"))

        def show_results(results: list[GiphyGif], previews: list[tuple[list[Image.Image], list[int]]]) -> None:
            clear_results()
            if not results:
                status_var.set("No GIFs found.")
                refresh_pagination()
                return
            for index, result in enumerate(results):
                row = index // grid_columns
                column = index % grid_columns
                tile = tk.Frame(results_frame, bg="#182126", padx=6, pady=6)
                tile.grid(row=row, column=column, padx=6, pady=6, sticky="nsew")
                canvas = tk.Canvas(tile, width=preview_width, height=result_tile_height, bg="#182126", highlightthickness=0, bd=0, cursor="hand2")
                canvas.pack()
                image_id = canvas.create_image(preview_width / 2, preview_height / 2)
                player = _AnimatedPreviewTile(canvas, image_id, previews[index][0], previews[index][1])
                self.giphy_preview_tiles.append(player)
                player.start()
                canvas.create_text(preview_width / 2, result_tile_height - 14, text=_short_text(result.title, 20), fill="#e9efed", font=("Segoe UI", 9))
                canvas.bind("<Button-1>", lambda _event, gif=result: download_for_crop(gif))
                canvas.bind("<MouseWheel>", scroll_results)
                canvas.bind("<Button-4>", scroll_results)
                canvas.bind("<Button-5>", scroll_results)
            status_var.set(f"{len(results)} GIFs loaded.")
            refresh_pagination()

        def show_categories(categories: list[GiphyCategory], previews: list[tuple[list[Image.Image], list[int]]]) -> None:
            clear_results()
            state.update({"mode": "categories", "query": "", "offset": 0, "last_count": 0})
            if not categories:
                status_var.set("No GIPHY categories found.")
                refresh_pagination()
                return
            for index, category in enumerate(categories):
                row = index // grid_columns
                column = index % grid_columns
                tile = tk.Frame(results_frame, bg="#182126", padx=6, pady=6)
                tile.grid(row=row, column=column, padx=6, pady=6, sticky="nsew")
                canvas = tk.Canvas(tile, width=preview_width, height=category_tile_height, bg="#182126", highlightthickness=0, bd=0, cursor="hand2")
                canvas.pack()
                image_id = canvas.create_image(preview_width / 2, preview_height / 2)
                player = _AnimatedPreviewTile(canvas, image_id, previews[index][0], previews[index][1])
                self.giphy_preview_tiles.append(player)
                player.start()
                canvas.create_text(preview_width / 2, preview_height + 16, text=_short_text(category.name.title(), 20), fill="#e9efed", font=("Segoe UI Semibold", 10))
                if category.subcategories:
                    canvas.create_text(preview_width / 2, category_tile_height - 12, text=_short_text(", ".join(category.subcategories), 26), fill="#98a6ac", font=("Segoe UI", 8))
                canvas.bind("<Button-1>", lambda _event, selected=category: load_results(selected.query, 0, "search"))
                canvas.bind("<MouseWheel>", scroll_results)
                canvas.bind("<Button-4>", scroll_results)
                canvas.bind("<Button-5>", scroll_results)
            status_var.set("GIPHY categories loaded.")
            refresh_pagination()

        def load_categories() -> None:
            key = api_key()
            if key is None:
                return
            status_var.set("Loading GIPHY categories...")
            clear_results()

            def worker() -> None:
                try:
                    categories = giphy_categories(key)
                    previews = [
                        _giphy_preview_animation(download_giphy_file(category.preview_url), preview_width, preview_height)
                        if category.preview_url
                        else (
                            [_placeholder_preview_image("GIPHY", preview_height).resize((preview_width, preview_height), Image.Resampling.LANCZOS)],
                            [250],
                        )
                        for category in categories
                    ]
                except Exception as exc:
                    message = str(exc)
                    window.after(0, lambda: messagebox.showerror("Giphy", f"Unable to load categories:\n{message}"))
                    window.after(0, lambda: status_var.set("GIPHY load failed."))
                    return
                window.after(0, lambda: show_categories(categories, previews))

            threading.Thread(target=worker, daemon=True).start()

        def load_results(query: str, offset: int = 0, mode: str | None = None) -> None:
            key = api_key()
            if key is None:
                return
            query = query.strip()
            result_mode = mode or ("search" if query else "trending")
            offset = max(0, int(offset))
            state.update({"mode": result_mode, "query": query, "offset": offset, "last_count": 0})
            status_var.set("Loading GIFs from GIPHY...")
            refresh_pagination()
            clear_results()

            def worker() -> None:
                try:
                    results = search_giphy(key, query, limit=page_limit, offset=offset) if query else trending_giphy(key, limit=page_limit, offset=offset)
                    previews = [_giphy_preview_animation(download_giphy_file(result.preview_url), preview_width, preview_height) for result in results]
                except Exception as exc:
                    message = str(exc)
                    window.after(0, lambda: messagebox.showerror("Giphy", f"Unable to load GIFs:\n{message}"))
                    window.after(0, lambda: status_var.set("GIPHY load failed."))
                    return

                def finish() -> None:
                    state["last_count"] = len(results)
                    show_results(results, previews)

                window.after(0, finish)

            threading.Thread(target=worker, daemon=True).start()

        def load_page(direction: int) -> None:
            if state["mode"] == "categories":
                return
            next_offset = max(0, int(state["offset"]) + direction * page_limit)
            load_results(str(state["query"]), next_offset, str(state["mode"]))

        def download_for_crop(result: GiphyGif) -> None:
            status_var.set(f"Downloading {result.title or 'GIF'}...")

            def worker() -> None:
                try:
                    data = download_giphy_file(result.gif_url)
                    temp_dir = Path(tempfile.gettempdir()) / "BTCAMGiphy"
                    temp_dir.mkdir(parents=True, exist_ok=True)
                    temp_path = temp_dir / f"{sanitize_gif_stem(result.title, 'giphy')}-{result.id or int(time.time())}.gif"
                    temp_path.write_bytes(data)
                except Exception as exc:
                    message = str(exc)
                    window.after(0, lambda: messagebox.showerror("Giphy", f"Unable to download GIF:\n{message}"))
                    window.after(0, lambda: status_var.set("GIPHY download failed."))
                    return
                window.after(0, lambda: self._open_gif_crop_editor(temp_path, result.title, close_window))

            threading.Thread(target=worker, daemon=True).start()

        ttk.Button(buttons, text="Search", style="Accent.TButton", command=lambda: load_results(query_var.get(), 0)).pack(side="left")
        categories_button = ttk.Button(nav, text="Categories", command=load_categories)
        categories_button.grid(row=0, column=0, sticky="w")
        ttk.Button(nav, text="Trending", command=lambda: load_results("", 0, "trending")).grid(row=0, column=1, sticky="w", padx=(8, 0))
        previous_button = ttk.Button(nav, text="Previous", command=lambda: load_page(-1))
        previous_button.grid(row=0, column=4, sticky="e", padx=(8, 0))
        next_button = ttk.Button(nav, text="Next", command=lambda: load_page(1))
        next_button.grid(row=0, column=5, sticky="e", padx=(8, 0))
        ttk.Label(nav, textvariable=page_var, style="Muted.TLabel").grid(row=0, column=3, sticky="e", padx=(10, 0))
        search_entry.bind("<Return>", lambda _event: load_results(query_var.get(), 0))
        window.protocol("WM_DELETE_WINDOW", close_window)
        search_entry.focus_set()
        refresh_pagination()
        if self.giphy_api_key_var.get().strip() or os.environ.get("GIPHY_API_KEY", "").strip():
            window.after(100, load_categories)
        else:
            status_var.set("Set the GIPHY API key in Options, then open Categories or Search.")

    def _open_gif_crop_editor(self, source_path: Path, title: str, on_done: Any) -> None:
        window = tk.Toplevel(self)
        window.title("Edit GIF")
        window.configure(bg="#12181c")
        window.resizable(False, False)
        window.transient(self)

        preview_size = 360
        output_size = min(self.config_model.display_size, GIPHY_CROP_OUTPUT_SIZE)
        preview_frames: list[Image.Image] = []
        preview_durations_ms: list[int] = []
        with Image.open(source_path) as source:
            for frame in ImageSequence.Iterator(source):
                preview_frames.append(ImageOps.exif_transpose(frame.convert("RGBA")))
                preview_durations_ms.append(max(35, int(frame.info.get("duration") or source.info.get("duration") or 100)))
                if len(preview_frames) >= 80:
                    break
        if not preview_frames:
            messagebox.showerror("Giphy", "Unable to preview GIF: no readable frames.")
            window.destroy()
            return

        base_frame = preview_frames[0]
        zoom_var = tk.DoubleVar(window, value=initial_crop_zoom(base_frame.width, base_frame.height, output_size))
        offset = [0.0, 0.0]
        drag_start: list[tuple[int, int, float, float] | None] = [None]
        current_frame_index = [0]
        preview_after_id: list[str | None] = [None]
        preview_stopped = [False]

        root = ttk.Frame(window, padding=16)
        root.pack(fill="both", expand=True)
        ttk.Label(root, text="Edit GIF", font=("Segoe UI Semibold", 20)).pack(anchor="w")
        ttk.Label(root, text="Drag to reposition; scroll to zoom.", style="Muted.TLabel").pack(anchor="w", pady=(8, 12))
        canvas = tk.Canvas(root, width=preview_size, height=preview_size, bg="#111111", highlightthickness=0, bd=0, cursor="fleur")
        canvas.pack()

        def update_preview() -> None:
            offset[0], offset[1] = clamp_crop_offset(base_frame.width, base_frame.height, output_size, zoom_var.get(), offset[0], offset[1])
            frame = preview_frames[current_frame_index[0]]
            image = render_cropped_gif_frame(frame, output_size, zoom_var.get(), offset[0], offset[1], mask_to_circle=True)
            preview = image.resize((preview_size, preview_size), Image.Resampling.LANCZOS).convert("RGBA")
            overlay = Image.new("RGBA", preview.size, (0, 0, 0, 0))
            draw = ImageDraw.Draw(overlay)
            draw.ellipse((1, 1, preview_size - 2, preview_size - 2), outline=(255, 255, 255, 190), width=1)
            draw.line((preview_size / 2 - 5, preview_size / 2, preview_size / 2 + 5, preview_size / 2), fill=(255, 255, 255, 160))
            draw.line((preview_size / 2, preview_size / 2 - 5, preview_size / 2, preview_size / 2 + 5), fill=(255, 255, 255, 160))
            preview.alpha_composite(overlay)
            self.giphy_crop_preview_image = ImageTk.PhotoImage(preview)
            canvas.delete("all")
            canvas.create_image(0, 0, anchor="nw", image=self.giphy_crop_preview_image)

        def stop_preview_animation() -> None:
            preview_stopped[0] = True
            if preview_after_id[0] is None:
                return
            try:
                window.after_cancel(preview_after_id[0])
            except tk.TclError:
                pass
            preview_after_id[0] = None

        def schedule_preview_frame() -> None:
            if preview_stopped[0] or len(preview_frames) < 2:
                return
            delay = preview_durations_ms[current_frame_index[0] % len(preview_durations_ms)]
            preview_after_id[0] = window.after(delay, advance_preview_frame)

        def advance_preview_frame() -> None:
            preview_after_id[0] = None
            if preview_stopped[0]:
                return
            current_frame_index[0] = (current_frame_index[0] + 1) % len(preview_frames)
            update_preview()
            schedule_preview_frame()

        def close_crop_editor() -> None:
            stop_preview_animation()
            window.destroy()

        def start_drag(event: tk.Event) -> None:
            drag_start[0] = (int(event.x), int(event.y), offset[0], offset[1])

        def update_drag(event: tk.Event) -> None:
            start = drag_start[0]
            if start is None:
                return
            dx = (int(event.x) - start[0]) * output_size / preview_size
            dy = (int(event.y) - start[1]) * output_size / preview_size
            offset[0] = start[2] + dx
            offset[1] = start[3] + dy
            update_preview()

        def zoom(event: tk.Event) -> str:
            delta = getattr(event, "delta", 0)
            factor = 1.10 if delta > 0 else 1 / 1.10
            min_zoom = initial_crop_zoom(base_frame.width, base_frame.height, output_size)
            zoom_var.set(max(min_zoom, min(zoom_var.get() * factor, min_zoom * 5.0)))
            update_preview()
            return "break"

        def save_crop() -> None:
            if not self._require_carousel_stopped():
                return
            try:
                temp_dir = Path(tempfile.gettempdir()) / "BTCAMGiphy"
                output = temp_dir / f"{sanitize_gif_stem(title, 'giphy')}-crop.gif"
                source_size_bytes = Path(source_path).stat().st_size
                save_cropped_gif(
                    source_path,
                    output,
                    output_size,
                    zoom_var.get(),
                    offset[0],
                    offset[1],
                    mask_to_circle=True,
                    palette_colors=GIPHY_CROP_PALETTE_COLORS,
                )
                copied_path = copy_gif_to_media_library(output)
                final_size_bytes = copied_path.stat().st_size
            except (OSError, ValueError) as exc:
                messagebox.showerror("Giphy", f"Unable to save GIF:\n{exc}")
                return
            self.carousel_items.append({"type": GIF_ITEM, "path": str(copied_path)})
            self._refresh_carousel_view(select_index=len(self.carousel_items) - 1)
            ratio = final_size_bytes / source_size_bytes if source_size_bytes else 0
            self.status_var.set(
                "GIPHY GIF copied to "
                f"{copied_path.parent}. Size: {_format_file_size(source_size_bytes)} -> {_format_file_size(final_size_bytes)} ({ratio:.1f}x)."
            )
            close_crop_editor()
            on_done()

        canvas.bind("<ButtonPress-1>", start_drag)
        canvas.bind("<B1-Motion>", update_drag)
        canvas.bind("<MouseWheel>", zoom)
        buttons = ttk.Frame(root)
        buttons.pack(fill="x", pady=(14, 0))
        ttk.Button(buttons, text="Cancel", command=close_crop_editor).pack(side="left")
        ttk.Button(buttons, text="Save", style="Accent.TButton", command=save_crop).pack(side="right")
        window.protocol("WM_DELETE_WINDOW", close_crop_editor)
        update_preview()
        schedule_preview_frame()

    def _replace_gif_item(self, index: int) -> None:
        if not self._require_carousel_stopped():
            return
        if index < 0 or index >= len(self.carousel_items):
            return
        path = filedialog.askopenfilename(
            title="Choose carousel GIF",
            filetypes=(("GIF", "*.gif"), ("All files", "*.*")),
        )
        if path:
            copied_path = self._copy_selected_gif(path)
            if copied_path is None:
                return
            self.carousel_items[index] = {"type": GIF_ITEM, "path": copied_path}
            self._refresh_carousel_view(select_index=index)

    def _copy_selected_gif(self, path: str) -> str | None:
        try:
            source_size_bytes = Path(path).expanduser().stat().st_size
            copied_path = _copy_lcd_ready_gif_to_media_library(path, self.config_model.display_size)
            final_size_bytes = copied_path.stat().st_size
        except (OSError, ValueError) as exc:
            messagebox.showerror(
                "Carousel",
                f"Unable to convert GIF for BTCAM to {btcam_documents_dir()}:\n{exc}",
            )
            return None
        ratio = final_size_bytes / source_size_bytes if source_size_bytes else 0
        if Path(path).expanduser().resolve() == copied_path.resolve():
            self.status_var.set(f"GIF already in {copied_path.parent}; reused without duplicate.")
        else:
            self.status_var.set(
                "GIF converted and copied to "
                f"{copied_path.parent}. Size: {_format_file_size(source_size_bytes)} -> {_format_file_size(final_size_bytes)} ({ratio:.1f}x)."
            )
        return str(copied_path)

    def _remove_carousel_item(self, index: int | None = None) -> None:
        if not self._require_carousel_stopped():
            return
        if index is None:
            index = self._selected_carousel_index()
        if index is None:
            return
        del self.carousel_items[index]
        if self.carousel_items:
            index = min(index, len(self.carousel_items) - 1)
        else:
            index = None
        self._refresh_carousel_view(select_index=index)

    def _select_carousel_item(self, index: int) -> None:
        if index < 0 or index >= len(self.carousel_items):
            return
        self._set_selected_carousel_index(index)

    def _selected_carousel_index(self) -> int | None:
        if self.selected_carousel_index is None:
            return None
        if self.selected_carousel_index < 0 or self.selected_carousel_index >= len(self.carousel_items):
            return None
        return self.selected_carousel_index

    def _refresh_carousel_view(self, select_index: int | None = None) -> None:
        if self.carousel_strip is None:
            return
        self._hide_carousel_drop_indicator()

        if select_index is not None and 0 <= select_index < len(self.carousel_items):
            self.selected_carousel_index = select_index
        elif not self.carousel_items:
            self.selected_carousel_index = None
        elif self.selected_carousel_index is not None:
            self.selected_carousel_index = min(self.selected_carousel_index, len(self.carousel_items) - 1)

        for child in self.carousel_strip.winfo_children():
            child.destroy()
        self.carousel_thumbnail_images = []
        self.carousel_item_canvases = []
        self.carousel_item_image_ids = []
        self.carousel_drop_indicator = None

        for index, item in enumerate(self.carousel_items):
            self._add_carousel_item_thumbnail(index, item)
        self._add_carousel_add_button()

    def _add_carousel_item_thumbnail(self, index: int, item: dict[str, str]) -> None:
        if self.carousel_strip is None:
            return

        panel_bg = "#182126"
        selected = index == self.selected_carousel_index

        canvas = tk.Canvas(
            self.carousel_strip,
            width=88,
            height=96,
            bg=panel_bg,
            highlightthickness=0,
            bd=0,
            cursor="arrow" if self.carousel_editing_locked else "fleur",
        )
        canvas.pack(side="left", padx=(0, 14))
        self.carousel_item_canvases.append(canvas)
        image = self._carousel_thumbnail_image(item, selected)
        self.carousel_thumbnail_images.append(image)
        image_id = canvas.create_image(44, 40, image=image)
        self.carousel_item_image_ids.append(image_id)
        canvas.create_text(44, 88, text=_short_carousel_label(item), fill="#d5dfdc", font=("Segoe UI", 8))
        canvas.bind("<ButtonPress-1>", lambda event, i=index: self._start_carousel_drag(i, event))
        canvas.bind("<B1-Motion>", self._update_carousel_drag)
        canvas.bind("<ButtonRelease-1>", self._finish_carousel_drag)
        canvas.bind("<Double-Button-1>", lambda event, i=index: self._open_carousel_item_menu(i, event))
        canvas.bind("<Button-3>", lambda event, i=index: self._open_carousel_item_menu(i, event))

    def _add_carousel_add_button(self) -> None:
        if self.carousel_strip is None:
            return

        panel_bg = "#182126"
        canvas = tk.Canvas(
            self.carousel_strip,
            width=88,
            height=96,
            bg=panel_bg,
            highlightthickness=0,
            bd=0,
            cursor="arrow" if self.carousel_editing_locked else "hand2",
        )
        canvas.pack(side="left", padx=(4, 0))
        image = ImageTk.PhotoImage(_plus_circle_image(72))
        self.carousel_thumbnail_images.append(image)
        canvas.create_image(44, 40, image=image)
        if not self.carousel_editing_locked:
            canvas.bind("<Button-1>", self._open_add_carousel_menu)

    def _open_add_carousel_menu(self, event: tk.Event) -> None:
        if not self._require_carousel_stopped():
            return
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="Temperature", command=self._add_temperature_item)
        menu.add_command(label="GIF", command=self._add_gif_item)
        menu.add_command(label="Giphy", command=self._add_giphy_item)
        menu.tk_popup(event.x_root, event.y_root)
        menu.grab_release()

    def _open_carousel_item_menu(self, index: int, event: tk.Event) -> None:
        if index < 0 or index >= len(self.carousel_items):
            return
        self._set_selected_carousel_index(index)
        if not self._require_carousel_stopped():
            return

        item = self.carousel_items[index]
        menu = tk.Menu(self, tearoff=0)
        if item.get("type") == GIF_ITEM:
            menu.add_command(label="Change GIF", command=lambda: self._replace_gif_item(index))
            menu.add_separator()
        menu.add_command(label="Remove", command=lambda: self._remove_carousel_item(index))
        menu.tk_popup(event.x_root, event.y_root)
        menu.grab_release()

    def _set_selected_carousel_index(self, index: int) -> None:
        self.selected_carousel_index = index
        for item_index, canvas in enumerate(self.carousel_item_canvases):
            if item_index >= len(self.carousel_item_image_ids):
                continue
            selected = item_index == index
            image = self._carousel_thumbnail_image(self.carousel_items[item_index], selected)
            self.carousel_thumbnail_images[item_index] = image
            canvas.itemconfigure(self.carousel_item_image_ids[item_index], image=image)

    def _start_carousel_drag(self, index: int, _event: tk.Event) -> None:
        if index < 0 or index >= len(self.carousel_items):
            return
        if not self._require_carousel_stopped():
            self._set_selected_carousel_index(index)
            return
        self.carousel_drag_source_index = index
        self.carousel_drag_drop_index = index
        self._set_selected_carousel_index(index)
        self._show_carousel_drop_indicator(index)

    def _update_carousel_drag(self, event: tk.Event) -> None:
        if self.carousel_drag_source_index is None:
            return
        target_index = self._carousel_drop_index(event.x_root)
        if target_index == self.carousel_drag_drop_index:
            return
        self.carousel_drag_drop_index = target_index
        self._show_carousel_drop_indicator(target_index)

    def _finish_carousel_drag(self, event: tk.Event) -> None:
        source_index = self.carousel_drag_source_index
        self.carousel_drag_source_index = None
        self.carousel_drag_drop_index = None
        self._hide_carousel_drop_indicator()
        if source_index is None or source_index < 0 or source_index >= len(self.carousel_items):
            return

        target_index = self._carousel_drop_index(event.x_root)
        self._reorder_carousel_item(source_index, target_index)

    def _carousel_drop_index(self, x_root: int) -> int:
        if not self.carousel_item_canvases:
            return 0

        for index, canvas in enumerate(self.carousel_item_canvases):
            center = canvas.winfo_rootx() + (canvas.winfo_width() / 2)
            if x_root < center:
                return index
        return len(self.carousel_item_canvases)

    def _reorder_carousel_item(self, source_index: int, target_index: int) -> None:
        if not self._require_carousel_stopped():
            return
        target_index = max(0, min(target_index, len(self.carousel_items)))
        adjusted_target = target_index - 1 if target_index > source_index else target_index
        if adjusted_target == source_index:
            self._set_selected_carousel_index(source_index)
            return

        item = self.carousel_items.pop(source_index)
        adjusted_target = max(0, min(adjusted_target, len(self.carousel_items)))
        self.carousel_items.insert(adjusted_target, item)
        self._refresh_carousel_view(select_index=adjusted_target)
        self.status_var.set("Carousel reordered.")

    def _show_carousel_drop_indicator(self, target_index: int) -> None:
        if self.carousel_strip is None:
            return
        self.carousel_strip.update_idletasks()

        if self.carousel_drop_indicator is None:
            self.carousel_drop_indicator = tk.Frame(self.carousel_strip, bg="#39a8ff")

        x = self._carousel_drop_indicator_x(target_index)
        self.carousel_drop_indicator.place(x=max(0, x - 2), y=6, width=4, height=80)
        self.carousel_drop_indicator.lift()
        self._start_carousel_drop_indicator_pulse()

    def _hide_carousel_drop_indicator(self) -> None:
        if self.carousel_drop_indicator_after_id is not None:
            try:
                self.after_cancel(self.carousel_drop_indicator_after_id)
            except tk.TclError:
                pass
            self.carousel_drop_indicator_after_id = None
        self.carousel_drop_indicator_pulse = False
        if self.carousel_drop_indicator is not None:
            self.carousel_drop_indicator.place_forget()

    def _carousel_drop_indicator_x(self, target_index: int) -> int:
        if not self.carousel_item_canvases:
            return 8
        if target_index <= 0:
            return self.carousel_item_canvases[0].winfo_x() - 8
        if target_index >= len(self.carousel_item_canvases):
            last = self.carousel_item_canvases[-1]
            return last.winfo_x() + last.winfo_width() + 8

        previous = self.carousel_item_canvases[target_index - 1]
        current = self.carousel_item_canvases[target_index]
        return int((previous.winfo_x() + previous.winfo_width() + current.winfo_x()) / 2)

    def _start_carousel_drop_indicator_pulse(self) -> None:
        if self.carousel_drop_indicator_after_id is not None:
            return
        self._pulse_carousel_drop_indicator()

    def _pulse_carousel_drop_indicator(self) -> None:
        if self.carousel_drop_indicator is None:
            self.carousel_drop_indicator_after_id = None
            return
        self.carousel_drop_indicator_pulse = not self.carousel_drop_indicator_pulse
        self.carousel_drop_indicator.configure(bg="#76d9ff" if self.carousel_drop_indicator_pulse else "#39a8ff")
        self.carousel_drop_indicator_after_id = self.after(120, self._pulse_carousel_drop_indicator)

    def _carousel_thumbnail_image(self, item: dict[str, str], selected: bool) -> ImageTk.PhotoImage:
        size = 72
        if item.get("type") == GIF_ITEM:
            image = _gif_preview_image(item.get("path"), size)
        else:
            cached = self.temperature_carousel_preview_photos.get(selected)
            if cached is not None:
                return cached
            image = self._temperature_carousel_preview_base()
        border_color = "#39a8ff" if selected else "#516169"
        border_width = 4 if selected else 2
        photo = ImageTk.PhotoImage(_circle_thumbnail(image, size, border_color, border_width))
        if item.get("type") != GIF_ITEM:
            self.temperature_carousel_preview_photos[selected] = photo
        return photo

    def _temperature_carousel_preview_base(self) -> Image.Image:
        if self.temperature_carousel_preview_base is None:
            self.temperature_carousel_preview_base = self._renderer().render(simulated_snapshot())
        return self.temperature_carousel_preview_base

    def _clear_temperature_preview_cache(self) -> None:
        self.temperature_carousel_preview_base = None
        self.temperature_carousel_preview_photos = {}
        self._refresh_carousel_view(select_index=self.selected_carousel_index)

    def detect_devices(self) -> None:
        try:
            devices = self._client().list_devices()
        except LiquidctlError as exc:
            messagebox.showerror("liquidctl", str(exc))
            return
        if not devices:
            self.device_var.set("Device: no Kraken found")
            return
        names = ", ".join(str(device.get("description", "Kraken")) for device in devices)
        self.device_var.set(f"Device: {names}")
        self.status_var.set(f"Found {len(devices)} device(s).")

    def refresh_once(self) -> None:
        try:
            snapshot = self._provider().read()
        except LiquidctlError as exc:
            self.status_var.set(str(exc))
            return
        self._apply_snapshot(snapshot)

    def start_loop(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        self._sync_config_from_ui()
        if not self._validate_carousel_settings():
            return
        self.config_model.save()
        self.stop_event.clear()
        self.upload_event.clear()
        with self.latest_upload_snapshot_lock:
            self.latest_upload_snapshot = None
        self.lcd_toggle_button.configure(text="Stop", state="normal", command=self.stop_loop)
        self._set_carousel_editing_locked(True)
        self.upload_worker = threading.Thread(target=self._lcd_upload_worker, daemon=True)
        self.worker = threading.Thread(target=self._sensor_worker, daemon=True)
        self.upload_worker.start()
        self.worker.start()
        self.status_var.set("LCD update started.")

    def stop_loop(self) -> None:
        self.stop_event.set()
        self.upload_event.set()
        self._hide_carousel_drop_indicator()
        self.lcd_toggle_button.configure(text="Stopping...", state="disabled", command=self.start_loop)
        self.status_var.set("Stop requested.")

    def _on_close_request(self) -> None:
        self._sync_config_from_ui()
        if self.config_model.minimize_to_tray_on_close and not self.is_exiting:
            if self.tray.hide_to_tray():
                self.status_var.set("Program minimized to tray.")
            else:
                self.status_var.set("Tray unavailable, window minimized.")
            return
        self._exit_application()

    def _restore_from_tray(self) -> None:
        self.tray.show_window()
        self.status_var.set("Program restored.")

    def _minimize_startup_to_tray(self) -> None:
        if self.tray.hide_to_tray():
            self.status_var.set("Program started minimized to tray.")
        else:
            self.status_var.set("Tray unavailable, window minimized.")

    def _exit_application(self) -> None:
        self.is_exiting = True
        self.stop_event.set()
        self.upload_event.set()
        self.tray.stop()
        if self.options_window is not None and self.options_window.winfo_exists():
            self.options_window.destroy()
            self.options_window = None
        if self.temperature_editor_window is not None and self.temperature_editor_window.winfo_exists():
            self.temperature_editor_window.destroy()
            self.temperature_editor_window = None
        self.destroy()

    def _validate_carousel_settings(self) -> bool:
        items = normalize_carousel_items(self.config_model.carousel_items)
        if not items:
            return True

        for item in items:
            if item["type"] != GIF_ITEM:
                continue
            path = item.get("path")
            if not path:
                messagebox.showerror("Carousel", "Select a GIF for every GIF carousel item.")
                return False
            if not Path(path).exists():
                messagebox.showerror("Carousel", f"GIF not found:\n{path}")
                return False
        return True

    def _sensor_worker(self) -> None:
        provider = self._provider()
        interval = max(1, int(self.config_model.interval_seconds))
        first = True
        last_cooler: CoolerStatus | None = None

        while not self.stop_event.is_set():
            cycle_started_at = time.monotonic()
            try:
                system = provider.system.read()
                cooler = provider.client.status_if_idle()
                if cooler is None:
                    cooler = last_cooler or CoolerStatus(description="Kraken")
                else:
                    last_cooler = cooler
                snapshot = StatusSnapshot(cooler=cooler, system=system, captured_at=datetime.now())
                self.queue.put(("snapshot", snapshot))
                self._publish_snapshot_for_upload(snapshot)
            except LiquidctlError as exc:
                self.queue.put(("error", str(exc)))
                if first:
                    break
            first = False
            elapsed = time.monotonic() - cycle_started_at
            wait_seconds = max(0.0, interval - elapsed)
            self.stop_event.wait(wait_seconds)

        self.stop_event.set()
        self.upload_event.set()
        self.queue.put(("stopped", None))

    def _lcd_upload_worker(self) -> None:
        client = self._client()
        renderer = self._renderer()
        lcd_transport = _lcd_transport(self.config_model.lcd_transport)
        output_path = _lcd_output_path(self.config_model, lcd_transport)

        if self.config_model.initialize_on_start:
            try:
                client.initialize()
            except LiquidctlError as exc:
                self.queue.put(("error", str(exc)))

        try:
            client.set_lcd_brightness(self.config_model.brightness)
            client.set_lcd_orientation(self.config_model.orientation)
        except LiquidctlError as exc:
            self.queue.put(("error", str(exc)))

        carousel_items = normalize_carousel_items(self.config_model.carousel_items)
        if carousel_items:
            try:
                native_items = self._preload_carousel_buckets(client, carousel_items)
            except NativeCarouselPreloadError as exc:
                self.queue.put(("error", str(exc)))
                self.stop_event.set()
                return
            self._lcd_native_carousel_upload_loop(client, renderer, output_path.with_suffix(".png"), native_items)
            return

        first = True
        while not self.stop_event.is_set():
            if not self.upload_event.wait(0.25):
                continue
            if self.stop_event.is_set():
                break

            snapshot = self._take_snapshot_for_upload()
            if snapshot is None:
                continue

            try:
                _save_lcd_image(renderer, snapshot, output_path, lcd_transport)
                upload_started_at = time.monotonic()
                _send_lcd_image(client, output_path, lcd_transport)
            except LcdImageTransferError as exc:
                self.queue.put(("error", str(exc)))
                self.stop_event.set()
                break
            except LiquidctlError as exc:
                self.queue.put(("error", str(exc)))
                if first:
                    self.stop_event.set()
                    break
            else:
                upload_elapsed = time.monotonic() - upload_started_at
                self.queue.put(("message", f"LCD updated in {upload_elapsed:.2f}s."))
            first = False

    def _preload_carousel_buckets(
        self,
        client: LiquidctlClient,
        items: list[dict[str, str]],
    ) -> list[dict[str, Any]]:
        runtime_items: list[dict[str, Any]] = []
        used_buckets: set[int] = set()
        started_at = time.monotonic()
        requirements = _native_carousel_requirements(
            items,
            self.config_model.display_size,
            self.config_model.orientation,
        )
        total_blocks = sum(requirement["blocks"] for requirement in requirements)
        if total_blocks > KRAKEN_NATIVE_BUCKET_MEMORY_BLOCKS:
            raise NativeCarouselPreloadError(_native_carousel_size_error(requirements, total_blocks))
        if requirements:
            try:
                client.clear_lcd_gif_buckets()
            except LiquidctlError as exc:
                raise NativeCarouselPreloadError(f"Unable to clear Kraken GIF buckets: {exc}") from exc

        for item in items:
            if item["type"] == TEMPERATURE_ITEM:
                runtime_items.append({"type": TEMPERATURE_ITEM})
                continue

            try:
                bucket_index = client.upload_lcd_gif_bucket(item["path"])
            except LiquidctlError as exc:
                raise NativeCarouselPreloadError(f"Native GIF upload failed for {Path(item['path']).name}: {exc}") from exc
            if bucket_index in used_buckets:
                raise NativeCarouselPreloadError(
                    f"Kraken reused native GIF bucket {bucket_index} while loading {Path(item['path']).name}. "
                    "The native carousel is not valid."
                )
            used_buckets.add(bucket_index)
            runtime_items.append({"type": GIF_ITEM, "path": item["path"], "bucket": bucket_index})

        gif_count = sum(1 for item in runtime_items if item["type"] == GIF_ITEM)
        if gif_count:
            elapsed = time.monotonic() - started_at
            self.queue.put(("message", f"{gif_count} GIF bucket(s) uploaded to Kraken in {elapsed:.2f}s."))
        return runtime_items

    def _create_streamed_carousel_items(self, items: list[dict[str, str]]) -> list[dict[str, Any]] | None:
        runtime_items: list[dict[str, Any]] = []
        for item in items:
            if item["type"] == TEMPERATURE_ITEM:
                runtime_items.append({"type": TEMPERATURE_ITEM})
                continue

            try:
                player = CarouselGifPlayer(item["path"], self.config_model.display_size)
            except (OSError, ValueError) as exc:
                self.queue.put(("error", f"Invalid carousel GIF: {exc}"))
                self.stop_event.set()
                return None
            runtime_items.append({"type": GIF_ITEM, "path": item["path"], "player": player})
        return runtime_items

    def _lcd_native_carousel_upload_loop(
        self,
        client: LiquidctlClient,
        renderer: LcdRenderer,
        output_path: Path,
        items: list[dict[str, Any]],
    ) -> None:
        latest_snapshot: Any | None = None
        cycle_started_at = time.monotonic()
        upload_interval = max(1.0, float(self.config_model.lcd_min_upload_interval_seconds))
        item_seconds = normalize_carousel_duration(self.config_model.carousel_phase_seconds)
        next_temperature_upload_at = 0.0
        last_item_index: int | None = None
        first = True

        while not self.stop_event.is_set():
            snapshot = self._take_snapshot_for_upload()
            if snapshot is not None:
                latest_snapshot = snapshot

            now = time.monotonic()
            elapsed_in_cycle = now - cycle_started_at
            item_index, item, item_elapsed = carousel_item_at(items, elapsed_in_cycle, item_seconds)

            if item["type"] == GIF_ITEM:
                if last_item_index != item_index:
                    try:
                        client.switch_lcd_bucket(int(item["bucket"]))
                    except LiquidctlError as exc:
                        self.queue.put(("error", str(exc)))
                        if first:
                            self.stop_event.set()
                            break
                    else:
                        self.queue.put(("message", "LCD GIF activated by Kraken."))
                        last_item_index = item_index
                self.upload_event.wait(min(0.25, _seconds_until_phase_change(item_elapsed, item_seconds)))
                first = False
                continue

            if latest_snapshot is None:
                self.upload_event.wait(0.25)
                continue

            if last_item_index != item_index or now >= next_temperature_upload_at:
                try:
                    _save_lcd_image(renderer, latest_snapshot, output_path, "static")
                    upload_started_at = time.monotonic()
                    _send_lcd_image(client, output_path, "static")
                except LcdImageTransferError as exc:
                    self.queue.put(("error", str(exc)))
                    self.stop_event.set()
                    break
                except LiquidctlError as exc:
                    self.queue.put(("error", str(exc)))
                    if first:
                        self.stop_event.set()
                        break
                else:
                    upload_elapsed = time.monotonic() - upload_started_at
                    self.queue.put(("message", f"LCD temperature updated in {upload_elapsed:.2f}s."))
                    next_temperature_upload_at = max(upload_started_at + upload_interval, time.monotonic())
                    last_item_index = item_index
                first = False
                continue

            self.upload_event.wait(min(0.25, next_temperature_upload_at - now))

    def _lcd_streamed_carousel_upload_loop(
        self,
        client: LiquidctlClient,
        renderer: LcdRenderer,
        output_path: Path,
        items: list[dict[str, Any]],
    ) -> None:
        latest_snapshot: Any | None = None
        cycle_started_at = time.monotonic()
        next_upload_at = 0.0
        upload_interval = max(1.0, float(self.config_model.lcd_min_upload_interval_seconds))
        item_seconds = normalize_carousel_duration(self.config_model.carousel_phase_seconds)
        first = True
        last_item_index: int | None = None

        while not self.stop_event.is_set():
            snapshot = self._take_snapshot_for_upload()
            if snapshot is not None:
                latest_snapshot = snapshot

            now = time.monotonic()
            elapsed_in_cycle = now - cycle_started_at
            item_index, item, item_elapsed = carousel_item_at(items, elapsed_in_cycle, item_seconds)
            if item_index != last_item_index:
                next_upload_at = 0.0

            if now < next_upload_at:
                self.upload_event.wait(min(0.05, next_upload_at - now))
                continue

            if item["type"] == GIF_ITEM:
                player: CarouselGifPlayer = item["player"]
                player.frame_at(item_elapsed).save(output_path)
                mode_label = "GIF"
                next_delay = player.seconds_until_next_frame(item_elapsed)
            else:
                if latest_snapshot is None:
                    self.upload_event.wait(0.25)
                    continue
                _save_lcd_image(renderer, latest_snapshot, output_path, "static")
                mode_label = "temperature"
                next_delay = upload_interval

            upload_started_at = time.monotonic()
            try:
                _send_lcd_image(client, output_path, "static")
            except LcdImageTransferError as exc:
                self.queue.put(("error", str(exc)))
                self.stop_event.set()
                break
            except LiquidctlError as exc:
                self.queue.put(("error", str(exc)))
                if first:
                    self.stop_event.set()
                    break
            else:
                upload_elapsed = time.monotonic() - upload_started_at
                self.queue.put(("message", f"LCD {mode_label} updated in {upload_elapsed:.2f}s."))
                next_upload_at = upload_started_at + next_delay
                last_item_index = item_index
            first = False

    def _publish_snapshot_for_upload(self, snapshot: Any) -> None:
        with self.latest_upload_snapshot_lock:
            self.latest_upload_snapshot = snapshot
            self.upload_event.set()

    def _take_snapshot_for_upload(self) -> Any | None:
        with self.latest_upload_snapshot_lock:
            snapshot = self.latest_upload_snapshot
            self.latest_upload_snapshot = None
            self.upload_event.clear()
            return snapshot

    def _drain_queue(self) -> None:
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "snapshot":
                    self._apply_snapshot(payload)
                elif kind == "message":
                    self.status_var.set(payload)
                elif kind == "error":
                    self.status_var.set(payload)
                elif kind == "stopped":
                    self.lcd_toggle_button.configure(text="Start LCD", state="normal", command=self.start_loop)
                    self._set_carousel_editing_locked(False)
        except queue.Empty:
            pass
        self.after(250, self._drain_queue)

    def _apply_snapshot(self, snapshot: Any) -> None:
        cooler = snapshot.cooler
        system = snapshot.system
        self.device_var.set(f"Device: {cooler.description or 'Kraken'}")
        self.liquid_var.set(_temp(cooler.liquid_temp_c))
        self.pump_var.set(_rpm(cooler.pump_rpm))
        self.fan_var.set(_rpm(cooler.fan_rpm))
        self.cpu_var.set(_temp(system.cpu_temp_c) if system.cpu_temp_c is not None else _percent(system.cpu_load_percent))
        self.gpu_var.set(_temp(system.gpu_temp_c) if system.gpu_temp_c is not None else _percent(system.gpu_load_percent))
        self.memory_var.set(_percent(system.memory_percent))

    def _renderer(self) -> LcdRenderer:
        background = self.config_model.background_image_path
        return LcdRenderer(
            self.config_model.display_size,
            background_path=background,
            temperature_colors=self.config_model.temperature_colors,
            temperature_elements=self.config_model.temperature_elements,
            temperature_layout=self.config_model.temperature_layout,
            temperature_layout_mode=self.config_model.temperature_layout_mode,
            temperature_center_source=self.config_model.temperature_center_source,
            temperature_sources=self.config_model.temperature_sources,
            temperature_center_title=self.config_model.temperature_center_title,
        )

    def _client(self) -> LiquidctlClient:
        return LiquidctlClient(self.config_model.liquidctl_path, self.config_model.match)

    def _provider(self) -> SnapshotProvider:
        return SnapshotProvider(
            self.config_model.liquidctl_path,
            self.config_model.match,
        )

    def _sync_config_from_ui(self) -> None:
        self._apply_hidden_gui_defaults()
        self.config_model.brightness = max(0, min(100, int(self.brightness_var.get())))
        self.config_model.orientation = int(self.orientation_var.get())
        self.config_model.start_lcd_on_launch = bool(self.start_on_launch_var.get())
        self.config_model.start_app_on_windows_login = bool(self.start_app_on_windows_login_var.get())
        self.config_model.minimize_to_tray_on_close = bool(self.minimize_to_tray_var.get())
        self.config_model.giphy_api_key = self.giphy_api_key_var.get().strip()
        self.config_model.carousel_phase_seconds = _duration_from_label(self.carousel_duration_var.get())
        self.config_model.carousel_items = normalize_carousel_items(self.carousel_items)
        self.config_model.carousel_enabled = bool(self.config_model.carousel_items)
        first_gif = next((item.get("path") for item in self.config_model.carousel_items if item["type"] == GIF_ITEM), None)
        self.config_model.carousel_gif_path = first_gif or None

    def _copy_configured_carousel_gifs(self, items: list[dict[str, str]]) -> list[dict[str, str]]:
        copied_items: list[dict[str, str]] = []
        changed = False

        for item in items:
            if item.get("type") != GIF_ITEM:
                copied_items.append(item)
                continue

            path = item.get("path") or ""
            try:
                copied_path = _copy_lcd_ready_gif_to_media_library(path, self.config_model.display_size)
            except (OSError, ValueError):
                copied_items.append(item)
                continue

            copied_item = {"type": GIF_ITEM, "path": str(copied_path)}
            copied_items.append(copied_item)
            changed = changed or copied_item != item

        if changed:
            self.config_model.carousel_items = copied_items
            first_gif = next((item.get("path") for item in copied_items if item["type"] == GIF_ITEM), None)
            self.config_model.carousel_gif_path = first_gif or None
            self.config_model.carousel_enabled = bool(copied_items)
            self.config_model.save()
        return copied_items

    def _apply_hidden_gui_defaults(self) -> None:
        defaults = AppConfig()
        self.config_model.liquidctl_path = defaults.liquidctl_path
        self.config_model.match = defaults.match
        self.config_model.interval_seconds = defaults.interval_seconds
        self.config_model.lcd_min_upload_interval_seconds = defaults.lcd_min_upload_interval_seconds
        self.config_model.lcd_transport = defaults.lcd_transport
        self.config_model.initialize_on_start = True

    def _check_cam_process(self) -> None:
        warning = ""
        for process in psutil.process_iter(["name"]):
            name = (process.info.get("name") or "").lower()
            if "nzxt cam" in name or name == "cam.exe":
                warning = "NZXT CAM appears open"
                break
        self.cam_warning_var.set(warning)
        self.after(5000, self._check_cam_process)


def _dark_radiobutton(parent: tk.Widget, text: str, value: int, variable: tk.IntVar) -> tk.Radiobutton:
    return tk.Radiobutton(
        parent,
        text=text,
        value=value,
        variable=variable,
        bg="#182126",
        fg="#e9efed",
        activebackground="#182126",
        activeforeground="#ffffff",
        selectcolor="#12181c",
        disabledforeground="#6f7f7c",
        highlightthickness=0,
        bd=0,
        font=("Segoe UI", 10),
    )


def _dark_checkbutton(parent: tk.Widget, text: str, variable: tk.BooleanVar) -> tk.Checkbutton:
    return tk.Checkbutton(
        parent,
        text=text,
        variable=variable,
        bg="#182126",
        fg="#e9efed",
        activebackground="#182126",
        activeforeground="#ffffff",
        selectcolor="#12181c",
        disabledforeground="#6f7f7c",
        highlightthickness=0,
        bd=0,
        font=("Segoe UI", 10),
    )


def _draw_temperature_editor_handles(
    canvas: tk.Canvas,
    layout: dict[str, dict[str, float]],
    elements: dict[str, bool],
    preview_size: int,
    layout_mode: str,
    selected_key: str | None = None,
) -> None:
    center = preview_size / 2
    canvas.create_line(center, 0, center, preview_size, fill="#2d5262", dash=(4, 4))
    canvas.create_line(0, center, preview_size, center, fill="#2d5262", dash=(4, 4))

    for key in _temperature_editor_movable_keys(layout_mode, elements):
        values = layout[key]
        bounds = _temperature_editor_bounds(key, values, preview_size)

        if key == selected_key:
            _draw_editor_selection_box(canvas, bounds)
        else:
            canvas.create_rectangle(bounds, outline="", fill="", width=0)


def _draw_editor_selection_box(canvas: tk.Canvas, bounds: tuple[float, float, float, float]) -> None:
    left, top, right, bottom = bounds
    color = "#4da3ff"
    canvas.create_line(left, top, right, top, fill=color, width=2)
    canvas.create_line(left, top, left, bottom, fill=color, width=2)
    canvas.create_line(right, top, right, bottom, fill=color, width=2)
    canvas.create_line(left, bottom, right, bottom, fill=color, width=2)


def _temperature_editor_bounds(key: str, values: dict[str, float], preview_size: int) -> tuple[float, float, float, float]:
    x = values["x"] * preview_size
    y = values["y"] * preview_size
    scale = values["scale"]
    # Tune these width/height and offset values to adjust editor selection boxes.
    if key == "center_gauge":
        radius = 113 * scale
        width = 16 * scale
        edge = radius + width / 2
        return x - edge, y - edge, x + edge, y + edge

    if key == "center_title":
        width = 88 * scale
        height = 27 * scale
        return x - width / 2, y, x + width / 2, y + height

    if key == "center_primary":
        width = 121 * scale
        height = 92 * scale
        dx = 6.5 * scale
        dy = 27 * scale
        return x + dx - width / 2, y + dy, x + dx + width / 2, y + dy + height

    if key == "dual_title":
        width = 60 * scale
        height = 26 * scale
        return x - width / 2, y, x + width / 2, y + height

    if key in {"dual_cpu", "dual_gpu"}:
        width = 65 * scale
        height = 70 * scale
        dx = 0 * scale
        dy = 15 * scale
        return x + dx - width / 2, y + dy, x + dx + width / 2, y + dy + height

    if key == "gpu":
        width = 100 * scale
        height = 75 * scale
        dx = 5 * scale
        dy = 22 * scale
        return x + dx - width / 2, y + dy, x + dx + width / 2, y + dy + height

    if key == "divider":
        width = 8 * scale
        height = 81 * scale
        return x - width / 2, y, x + width / 2, y + height

    width = 43 * scale
    height = 31 * scale
    dx = -0.5 * scale
    dy = 9 * scale
    return x + dx, y + dy, x + dx + width, y + dy + height


def _temperature_layout_visible(key: str, elements: dict[str, bool], layout_mode: str) -> bool:
    if layout_mode == TEMPERATURE_LAYOUT_DUAL:
        if key == "dual_title":
            return True
        if key == "dual_cpu":
            return bool(elements.get("cpu", True))
        if key == "dual_gpu":
            return bool(elements.get("primary", True))
        return False

    if layout_mode == TEMPERATURE_LAYOUT_CENTER_GAUGE:
        if key == "center_gauge":
            return bool(elements.get("gauge", True))
        if key == "center_primary":
            return bool(elements.get("primary", True))
        return key == "center_title"

    if key == "center_title":
        return bool(elements.get("title", False))
    if key == "gpu":
        return bool(elements.get("primary", True))
    return bool(elements.get(key, True))


def _snap_editor_layout_position(
    key: str,
    x: float,
    y: float,
    scale: float,
    snap_to_center: bool = True,
) -> tuple[float, float]:
    width, height = _temperature_editor_cell_size(key, scale)
    if key in {"gpu", "divider", "center_gauge", "center_title", "center_primary", "dual_title", "dual_cpu", "dual_gpu"}:
        x = max(width / 2, min(1 - width / 2, x))
        if key == "center_gauge":
            y = max(height / 2, min(1 - height / 2, y))
            center_y = y
        else:
            y = max(0.02, min(1 - height, y))
            center_y = y + height / 2
        center_x = x
        if snap_to_center and abs(center_x - 0.5) <= 0.025:
            x = 0.5
        if snap_to_center and abs(center_y - 0.5) <= 0.025:
            y = 0.5 if key == "center_gauge" else 0.5 - height / 2
        if key == "center_gauge":
            return x, max(height / 2, min(1 - height / 2, y))
        return x, max(0.02, min(1 - height, y))

    x = max(0.02, min(1 - width, x))
    y = max(0.02, min(1 - height, y))
    center_x = x + width / 2
    center_y = y + height / 2
    if snap_to_center and abs(center_x - 0.5) <= 0.025:
        x = 0.5 - width / 2
    if snap_to_center and abs(center_y - 0.5) <= 0.025:
        y = 0.5 - height / 2
    return max(0.02, min(1 - width, x)), max(0.02, min(1 - height, y))


def _temperature_editor_cell_size(key: str, scale: float) -> tuple[float, float]:
    if key == "center_gauge":
        edge = (113 + 8) * scale
        return edge * 2 / 260, edge * 2 / 260
    if key == "center_title":
        return 88 * scale / 260, 27 * scale / 260
    if key == "center_primary":
        return 121 * scale / 260, 92 * scale / 260
    if key == "dual_title":
        return 60 * scale / 260, 26 * scale / 260
    if key in {"dual_cpu", "dual_gpu"}:
        return 65 * scale / 260, 70 * scale / 260
    if key == "gpu":
        return 100 * scale / 260, 75 * scale / 260
    if key == "divider":
        return 8 * scale / 260, 81 * scale / 260
    return 43 * scale / 260, 31 * scale / 260


def _temperature_editor_layout_keys(layout_mode: str) -> tuple[str, ...]:
    if layout_mode == TEMPERATURE_LAYOUT_DUAL:
        return DUAL_TEMPERATURE_LAYOUT_KEYS
    if layout_mode == TEMPERATURE_LAYOUT_CENTER_GAUGE:
        return CENTER_GAUGE_LAYOUT_KEYS
    return DETAILED_TEMPERATURE_LAYOUT_KEYS


def _temperature_editor_hit_test_keys(layout_mode: str) -> tuple[str, ...]:
    if layout_mode == TEMPERATURE_LAYOUT_DUAL:
        return DUAL_TEMPERATURE_LAYOUT_KEYS
    if layout_mode == TEMPERATURE_LAYOUT_CENTER_GAUGE:
        return CENTER_GAUGE_HIT_TEST_KEYS
    return DETAILED_TEMPERATURE_LAYOUT_KEYS


def _temperature_editor_movable_keys(layout_mode: str, elements: dict[str, bool]) -> tuple[str, ...]:
    return tuple(
        key
        for key in _temperature_editor_hit_test_keys(layout_mode)
        if _temperature_layout_visible(key, elements, layout_mode)
    )


def _temperature_editor_snapshot() -> StatusSnapshot:
    return StatusSnapshot(
        cooler=CoolerStatus(
            description="Editor preview",
            liquid_temp_c=36,
            pump_rpm=2150,
            pump_duty_percent=70,
            fan_rpm=980,
            fan_duty_percent=42,
        ),
        system=SystemStatus(
            cpu_load_percent=45,
            memory_percent=62,
            cpu_temp_c=28,
            gpu_temp_c=37,
            gpu_load_percent=38,
            gpu_name="Editor GPU",
        ),
        captured_at=datetime.now(),
        simulated=True,
    )


def _temp(value: float | None) -> str:
    if value is None:
        return f"-- {DEGREE}C"
    return f"{int(round(value))}{DEGREE}C"


def _rpm(value: int | None) -> str:
    if value is None:
        return "-- rpm"
    return f"{value} rpm"


def _percent(value: float | None) -> str:
    if value is None:
        return "--"
    return f"{value:.0f}%"


def _seconds_until_phase_change(elapsed_seconds: float, phase_seconds: float) -> float:
    phase_seconds = max(1.0, float(phase_seconds))
    return max(0.01, phase_seconds - (float(elapsed_seconds) % phase_seconds))


def _duration_label(seconds: int) -> str:
    return f"Duration: {int(seconds)}s"


def _duration_from_label(value: str) -> int:
    digits = "".join(character for character in str(value) if character.isdigit())
    return normalize_carousel_duration(digits)


def _short_text(value: str, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 1)]}."


def _short_carousel_label(item: dict[str, str]) -> str:
    if item.get("type") != GIF_ITEM:
        return "Temp"
    name = Path(item.get("path") or "").stem or "GIF"
    if len(name) <= 10:
        return name
    return f"{name[:9]}."


def _format_file_size(size_bytes: int) -> str:
    size = float(max(0, int(size_bytes)))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _copy_lcd_ready_gif_to_media_library(path: str | Path, display_size: int) -> Path:
    source_path = Path(path).expanduser()
    if _is_media_library_gif(source_path):
        return source_path.resolve()

    prepared_path = _prepare_lcd_ready_gif(source_path, display_size)
    return copy_gif_to_media_library(prepared_path)


def _prepare_lcd_ready_gif(source_path: str | Path, display_size: int) -> Path:
    source_path = Path(source_path).expanduser()
    output_size = min(max(1, int(display_size or LCD_GIF_OUTPUT_SIZE)), LCD_GIF_OUTPUT_SIZE)
    temp_dir = Path(tempfile.gettempdir()) / "BTCAMGifs"
    output_path = temp_dir / f"{sanitize_gif_stem(source_path.stem, 'gif')}-kraken.gif"
    return save_lcd_ready_gif(
        source_path,
        output_path,
        output_size,
        mask_to_circle=True,
        palette_colors=LCD_GIF_PALETTE_COLORS,
    )


def _is_media_library_gif(path: Path) -> bool:
    if not path.is_file() or path.suffix.lower() != ".gif":
        return False
    try:
        return path.parent.resolve() == btcam_documents_dir().resolve()
    except OSError:
        return False


def _native_carousel_requirements(
    items: list[dict[str, str]],
    display_size: int,
    orientation_degrees: int,
) -> list[dict[str, Any]]:
    requirements: list[dict[str, Any]] = []
    for item in items:
        if item.get("type") != GIF_ITEM:
            continue
        path = Path(item.get("path") or "")
        payload_size = _prepared_native_gif_size(path, display_size, orientation_degrees)
        requirements.append(
            {
                "path": str(path),
                "file_size": path.stat().st_size,
                "payload_size": payload_size,
                "blocks": math.ceil((KRAKEN_NATIVE_BUCKET_HEADER_BYTES + payload_size) / 1024),
            }
        )
    return requirements


def _prepared_native_gif_size(path: Path, display_size: int, orientation_degrees: int) -> int:
    rotation = int(orientation_degrees) // 90
    lcd_resolution = (int(display_size), int(display_size))
    with Image.open(path) as source:
        frames = ImageSequence.Iterator(source)

        def prepare_frames() -> Any:
            for frame in frames:
                yield frame.copy().resize(lcd_resolution).rotate(rotation * -90)

        prepared = prepare_frames()
        try:
            result = next(prepared)
        except StopIteration as exc:
            raise ValueError(f"GIF does not contain readable frames: {path}") from exc
        result.info = source.info
        result_bytes = io.BytesIO()
        result.save(
            result_bytes,
            format="GIF",
            interlace=False,
            save_all=True,
            append_images=list(prepared),
            loop=0,
        )
    return len(result_bytes.getvalue())


def _native_carousel_size_error(requirements: list[dict[str, Any]], total_blocks: int) -> str:
    parts = [
        "Native GIF carousel too large for Kraken bucket memory after resize: "
        f"{total_blocks:,}/{KRAKEN_NATIVE_BUCKET_MEMORY_BLOCKS:,} blocks."
    ]
    for requirement in requirements:
        parts.append(
            f"{Path(str(requirement['path'])).name}: "
            f"{_format_file_size(int(requirement['file_size']))} file, "
            f"{_format_file_size(int(requirement['payload_size']))} native, "
            f"{int(requirement['blocks']):,} blocks"
        )
    parts.append("Reduce GIF duration/frame count or remove one GIF; native mode cannot fit this set.")
    return " ".join(parts)


def _gif_preview_image(path: str | None, size: int) -> Image.Image:
    if not path:
        return _placeholder_preview_image("GIF", size)

    try:
        with Image.open(Path(path)) as source:
            source.seek(0)
            frame = ImageOps.exif_transpose(source.convert("RGBA"))
            background = Image.new("RGBA", frame.size, (18, 24, 28, 255))
            background.alpha_composite(frame)
            return background.convert("RGB")
    except (OSError, ValueError):
        return _placeholder_preview_image("GIF", size)


def _giphy_preview_image(data: bytes, width: int, height: int) -> Image.Image:
    try:
        with Image.open(io.BytesIO(data)) as source:
            source.seek(0)
            frame = ImageOps.exif_transpose(source.convert("RGBA"))
            background = Image.new("RGBA", frame.size, (18, 24, 28, 255))
            background.alpha_composite(frame)
            return ImageOps.fit(background.convert("RGB"), (width, height), method=Image.Resampling.LANCZOS)
    except (OSError, ValueError):
        return _placeholder_preview_image("GIF", min(width, height)).resize((width, height), Image.Resampling.LANCZOS)


def _giphy_preview_animation(data: bytes, width: int, height: int) -> tuple[list[Image.Image], list[int]]:
    try:
        with Image.open(io.BytesIO(data)) as source:
            frames: list[Image.Image] = []
            durations: list[int] = []
            for frame in ImageSequence.Iterator(source):
                rgba = ImageOps.exif_transpose(frame.convert("RGBA"))
                background = Image.new("RGBA", rgba.size, (18, 24, 28, 255))
                background.alpha_composite(rgba)
                frames.append(ImageOps.fit(background.convert("RGB"), (width, height), method=Image.Resampling.LANCZOS))
                durations.append(max(35, int(frame.info.get("duration") or source.info.get("duration") or 100)))
                if len(frames) >= 60:
                    break
            if frames:
                return frames, durations
    except (OSError, ValueError):
        pass

    return [_placeholder_preview_image("GIF", min(width, height)).resize((width, height), Image.Resampling.LANCZOS)], [250]


class _AnimatedPreviewTile:
    def __init__(
        self,
        canvas: tk.Canvas,
        image_id: int,
        frames: list[Image.Image],
        durations_ms: list[int],
    ) -> None:
        self.canvas = canvas
        self.image_id = image_id
        self.photos = [ImageTk.PhotoImage(frame) for frame in frames]
        self.durations_ms = [max(35, int(duration)) for duration in durations_ms] or [250]
        self.index = 0
        self.after_id: str | None = None
        self.stopped = False
        if self.photos:
            self.canvas.itemconfigure(self.image_id, image=self.photos[0])

    def start(self) -> None:
        if len(self.photos) > 1:
            self.after_id = self.canvas.after(self.durations_ms[0], self._advance)

    def stop(self) -> None:
        self.stopped = True
        if self.after_id is None:
            return
        try:
            self.canvas.after_cancel(self.after_id)
        except tk.TclError:
            pass
        self.after_id = None

    def _advance(self) -> None:
        if self.stopped or not self.photos:
            return
        self.index = (self.index + 1) % len(self.photos)
        try:
            self.canvas.itemconfigure(self.image_id, image=self.photos[self.index])
            self.after_id = self.canvas.after(self.durations_ms[self.index], self._advance)
        except tk.TclError:
            self.stopped = True
            self.after_id = None


def _placeholder_preview_image(label: str, size: int) -> Image.Image:
    image = Image.new("RGB", (size, size), (35, 45, 51))
    draw = ImageDraw.Draw(image)
    text_box = draw.textbbox((0, 0), label)
    text_width = text_box[2] - text_box[0]
    text_height = text_box[3] - text_box[1]
    draw.text(
        ((size - text_width) / 2, (size - text_height) / 2 - 1),
        label,
        fill=(233, 239, 237),
    )
    return image


def _circle_thumbnail(image: Image.Image, size: int, border_color: str, border_width: int) -> Image.Image:
    scale = 4
    high_size = int(size) * scale
    high_border = max(1, int(border_width) * scale)
    inner_size = high_size - (high_border * 2)

    output = Image.new("RGBA", (high_size, high_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(output)
    draw.ellipse((0, 0, high_size - 1, high_size - 1), fill=_hex_to_rgba(border_color))

    fitted = ImageOps.fit(image.convert("RGBA"), (inner_size, inner_size), method=Image.Resampling.LANCZOS)
    mask = Image.new("L", (inner_size, inner_size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, inner_size - 1, inner_size - 1), fill=255)
    output.paste(fitted, (high_border, high_border), mask)
    return output.resize((size, size), Image.Resampling.LANCZOS)


def _plus_circle_image(size: int) -> Image.Image:
    scale = 4
    high_size = int(size) * scale
    output = Image.new("RGBA", (high_size, high_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(output)
    margin = 2 * scale
    draw.ellipse(
        (margin, margin, high_size - margin - 1, high_size - margin - 1),
        fill=_hex_to_rgba("#223038"),
        outline=_hex_to_rgba("#516169"),
        width=2 * scale,
    )
    center = high_size // 2
    half = 16 * scale
    draw.line((center, center - half, center, center + half), fill=_hex_to_rgba("#d5dfdc"), width=2 * scale)
    draw.line((center - half, center, center + half, center), fill=_hex_to_rgba("#d5dfdc"), width=2 * scale)
    return output.resize((size, size), Image.Resampling.LANCZOS)


def _hex_to_rgba(value: str) -> tuple[int, int, int, int]:
    value = value.strip().lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16), 255


def _readable_swatch_text(value: str) -> str:
    try:
        red, green, blue, _alpha = _hex_to_rgba(value)
    except (ValueError, IndexError):
        return "#ffffff"
    brightness = (red * 299 + green * 587 + blue * 114) / 1000
    return "#101820" if brightness > 150 else "#ffffff"


def _lcd_transport(value: str | None) -> str:
    return "gif" if str(value or "").strip().lower() == "gif" else "static"


def _lcd_output_path(config: AppConfig, transport: str) -> Path:
    path = config.output_path
    return path.with_suffix(".gif") if transport == "gif" else path.with_suffix(".png")


def _save_lcd_image(renderer: LcdRenderer, snapshot: Any, path: Path, transport: str) -> Path:
    if transport == "gif":
        return renderer.save_gif(snapshot, path)
    return renderer.save(snapshot, path)


def _send_lcd_image(client: LiquidctlClient, path: Path, transport: str) -> None:
    if transport == "gif":
        client.set_lcd_gif(path)
        return
    client.set_lcd_static(path)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="btcam-gui")
    parser.add_argument("--minimized", action="store_true")
    args = parser.parse_args(argv)

    from .elevation import ensure_admin_or_relaunch

    relaunch_args = ["-m", "BTCAM.app"]
    if args.minimized:
        relaunch_args.append("--minimized")

    if ensure_admin_or_relaunch(args=relaunch_args):
        BTCAMApp(start_minimized=args.minimized).mainloop()


if __name__ == "__main__":
    main()
