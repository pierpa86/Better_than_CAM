from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .models import CoolerStatus
from .subprocess_utils import hidden_subprocess_kwargs
from .winusb_fallback import clear_gif_buckets, install_liquidctl_winusb_fallback, switch_bucket, upload_gif_bucket


class LiquidctlError(RuntimeError):
    """Raised when liquidctl returns an error."""


class LiquidctlNotFoundError(LiquidctlError):
    """Raised when the liquidctl executable cannot be found."""


class LcdImageTransferError(LiquidctlError):
    """Raised when liquidctl cannot access the LCD bulk transfer endpoint."""


_BULK_OUT_ERROR = "Cannot find bulk out device"
_TRANSIENT_LCD_ERRORS = ("Handle non valido", "invalid handle", "OSError(9", "WinError 6", "read error")
_EMBEDDED_LIQUIDCTL_LOCK = threading.RLock()
_BULK_OUT_MESSAGE = (
    "Unable to send static images to the display: liquidctl cannot find "
    "the Kraken USB bulk-out interface. Sensors, brightness, orientation "
    "and liquid mode may still work. Close NZXT CAM and verify that Kraken "
    "USB interface 0 uses WinUSB without replacing the HID interface."
)


def _status_from_devices(devices: list[dict[str, Any]]) -> CoolerStatus:
    if not devices:
        raise LiquidctlError("No Kraken device found by liquidctl.")

    device = devices[0]
    status_items = device.get("status") or []
    status = CoolerStatus(
        description=device.get("description"),
        raw_status=status_items,
    )

    for item in status_items:
        key = str(item.get("key", "")).lower()
        value = item.get("value")
        if value is None:
            continue
        if key == "liquid temperature":
            status.liquid_temp_c = _as_float(value)
        elif key == "pump speed":
            status.pump_rpm = _as_int(value)
        elif key == "pump duty":
            status.pump_duty_percent = _as_float(value)
        elif key == "fan speed":
            status.fan_rpm = _as_int(value)
        elif key == "fan duty":
            status.fan_duty_percent = _as_float(value)

    return status


class LiquidctlClient:
    def __init__(self, executable: str = "liquidctl", match: str = "kraken", timeout: int = 30) -> None:
        self.executable = executable
        self.match = match
        self.timeout = timeout

    def list_devices(self) -> list[dict[str, Any]]:
        return self._run_json(["list", "--json"])

    def initialize(self) -> list[dict[str, Any]] | None:
        out = self._run(["initialize", "all", "--json"])
        if not out.strip():
            return None
        return json.loads(out)

    def status(self) -> CoolerStatus:
        return _status_from_devices(self._run_json_retry(["status", "--json"]))

    def status_if_idle(self) -> CoolerStatus | None:
        if not self._should_run_embedded():
            return self.status()

        if not _EMBEDDED_LIQUIDCTL_LOCK.acquire(blocking=False):
            return None

        try:
            return self.status()
        finally:
            _EMBEDDED_LIQUIDCTL_LOCK.release()

    def set_lcd_static(self, image_path: str | Path) -> None:
        args = ["set", "lcd", "screen", "static", str(image_path)]
        try:
            self._run(args)
        except LiquidctlError as exc:
            if not _is_transient_lcd_error(str(exc)):
                raise
            time.sleep(0.8)
            self._run(args)

    def set_lcd_gif(self, image_path: str | Path) -> None:
        args = ["set", "lcd", "screen", "gif", str(image_path)]
        try:
            self._run(args)
        except LiquidctlError as exc:
            if not _is_transient_lcd_error(str(exc)):
                raise
            time.sleep(0.8)
            self._run(args)

    def set_lcd_liquid(self) -> None:
        self._run(["set", "lcd", "screen", "liquid"])

    def set_lcd_brightness(self, value: int) -> None:
        value = max(0, min(100, int(value)))
        self._run(["set", "lcd", "screen", "brightness", str(value)])

    def set_lcd_orientation(self, degrees: int) -> None:
        if degrees not in {0, 90, 180, 270}:
            raise ValueError("Orientation must be 0, 90, 180 or 270.")
        self._run(["set", "lcd", "screen", "orientation", str(degrees)])

    def upload_lcd_gif_bucket(self, image_path: str | Path) -> int:
        return int(self._run_with_device(lambda device: upload_gif_bucket(device, image_path)))

    def clear_lcd_gif_buckets(self) -> None:
        self._run_with_device(clear_gif_buckets)

    def switch_lcd_bucket(self, bucket_index: int) -> None:
        self._run_with_device(lambda device: switch_bucket(device, bucket_index))

    def _run_json(self, args: list[str]) -> list[dict[str, Any]]:
        out = self._run(args)
        try:
            data = json.loads(out)
        except json.JSONDecodeError as exc:
            raise LiquidctlError(f"Invalid liquidctl output: {out[:500]}") from exc
        if not isinstance(data, list):
            raise LiquidctlError("Unexpected liquidctl output: the JSON response is not a list.")
        return data

    def _run_json_retry(self, args: list[str]) -> list[dict[str, Any]]:
        try:
            return self._run_json(args)
        except LiquidctlError as exc:
            if not _is_transient_lcd_error(str(exc)):
                raise
            time.sleep(0.8)
            return self._run_json(args)

    def _run(self, args: list[str]) -> str:
        if self._should_run_embedded():
            return self._run_embedded(args)

        command = self._command_prefix()
        if self.match:
            command.extend(["--match", self.match])
        command.extend(args)

        env = os.environ.copy()
        env.setdefault("LANG", "C")

        try:
            process = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env=env,
                **hidden_subprocess_kwargs(),
            )
        except FileNotFoundError as exc:
            raise LiquidctlNotFoundError(
                "liquidctl is not installed or is not in PATH. Install dependencies with requirements.txt."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise LiquidctlError("liquidctl did not respond before the timeout.") from exc

        if process.returncode != 0:
            message = (process.stderr or process.stdout or "Unknown liquidctl error.").strip()
            raise _error_from_message(message)
        return process.stdout

    def _command_prefix(self) -> list[str]:
        if self.executable != "liquidctl":
            return [self.executable]
        if shutil.which("liquidctl"):
            return ["liquidctl"]
        return [sys.executable, "-m", "liquidctl"]

    def _should_run_embedded(self) -> bool:
        return self.executable == "liquidctl"

    def _run_with_device(self, operation: Any) -> Any:
        if not self._should_run_embedded():
            raise LiquidctlError("Le operazioni LCD native richiedono liquidctl integrato.")

        with _EMBEDDED_LIQUIDCTL_LOCK:
            install_liquidctl_winusb_fallback()

            from liquidctl.cli import find_liquidctl_devices

            opts = {"match": self.match} if self.match else {}
            devices = list(find_liquidctl_devices(**opts))
            if not devices:
                raise LiquidctlError("No Kraken device found by liquidctl.")
            if len(devices) > 1:
                raise LiquidctlError("Multiple Kraken devices found: a more specific filter is required.")

            try:
                with devices[0].connect():
                    return operation(devices[0])
            except Exception as exc:
                raise _error_from_message(str(exc) or repr(exc)) from exc

    def _run_embedded(self, args: list[str]) -> str:
        with _EMBEDDED_LIQUIDCTL_LOCK:
            return self._run_embedded_unlocked(args)

    def _run_embedded_unlocked(self, args: list[str]) -> str:
        install_liquidctl_winusb_fallback()

        from liquidctl.cli import main as liquidctl_main

        argv = ["liquidctl"]
        if self.match:
            argv.extend(["--match", self.match])
        argv.extend(args)

        stdout = io.StringIO()
        stderr = io.StringIO()
        previous_argv = sys.argv
        root_logger = logging.getLogger()
        previous_handlers = root_logger.handlers[:]
        previous_level = root_logger.level

        try:
            sys.argv = argv
            root_logger.handlers.clear()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = liquidctl_main()
        except SystemExit as exc:
            code = int(exc.code or 0)
        except Exception as exc:
            raise LiquidctlError(f"Embedded liquidctl error: {exc}") from exc
        else:
            code = int(result or 0)
        finally:
            sys.argv = previous_argv
            root_logger.handlers.clear()
            root_logger.handlers.extend(previous_handlers)
            root_logger.setLevel(previous_level)

        out = stdout.getvalue()
        err = stderr.getvalue()
        if code != 0:
            message = (err or out or "Unknown liquidctl error.").strip()
            raise _error_from_message(message)
        return out


def _error_from_message(message: str) -> LiquidctlError:
    if _BULK_OUT_ERROR in message:
        return LcdImageTransferError(_BULK_OUT_MESSAGE)
    return LiquidctlError(message)


def _is_transient_lcd_error(message: str) -> bool:
    return any(fragment.lower() in message.lower() for fragment in _TRANSIENT_LCD_ERRORS)


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None
