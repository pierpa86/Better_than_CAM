from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from .subprocess_utils import hidden_subprocess_kwargs


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_RUN_NAME = "BTCAM"
TASK_NAME = "BTCAM"


def build_startup_command(project_root: Path | None = None, minimized: bool = True) -> str:
    if getattr(sys, "frozen", False):
        command = _quote(str(Path(sys.executable)))
        return f"{command} --minimized" if minimized else command

    command = f"{_quote(sys.executable)} -m BTCAM.app"
    return f"{command} --minimized" if minimized else command


def is_windows_startup_enabled() -> bool:
    if sys.platform != "win32":
        return False

    if _use_scheduled_task():
        return _startup_task_exists()

    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, APP_RUN_NAME)
            return True
    except OSError:
        return False


def set_windows_startup(enabled: bool) -> None:
    if sys.platform != "win32":
        return

    if _use_scheduled_task():
        if enabled:
            _create_startup_task()
            _delete_run_value()
        else:
            _delete_startup_task()
            _delete_run_value()
        return

    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, APP_RUN_NAME, 0, winreg.REG_SZ, build_startup_command())
        else:
            try:
                winreg.DeleteValue(key, APP_RUN_NAME)
            except FileNotFoundError:
                pass


def _use_scheduled_task() -> bool:
    return bool(getattr(sys, "frozen", False))


def _create_startup_task() -> None:
    _run_schtasks(
        [
            "/Create",
            "/TN",
            TASK_NAME,
            "/SC",
            "ONLOGON",
            "/TR",
            build_startup_command(),
            "/RL",
            "HIGHEST",
            "/F",
        ],
        check=True,
    )


def _delete_startup_task() -> None:
    _run_schtasks(["/Delete", "/TN", TASK_NAME, "/F"], check=False)


def _startup_task_exists() -> bool:
    result = _run_schtasks(["/Query", "/TN", TASK_NAME], check=False)
    return result.returncode == 0


def _run_schtasks(args: list[str], check: bool) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["schtasks.exe", *args],
        capture_output=True,
        text=True,
        **hidden_subprocess_kwargs(),
    )
    if check and result.returncode != 0:
        message = (result.stderr or result.stdout or "Unable to update Windows startup task.").strip()
        raise OSError(message)
    return result


def _delete_run_value() -> None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, APP_RUN_NAME)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _quote(value: str) -> str:
    return '"' + value.replace('"', r"\"") + '"'
