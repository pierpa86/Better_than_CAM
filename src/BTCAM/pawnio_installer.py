from __future__ import annotations

import subprocess
import sys

from .assets import pawnio_installer_path
from .subprocess_utils import hidden_subprocess_kwargs

_UNINSTALL_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\PawnIO"


def is_pawnio_installed() -> bool:
    if sys.platform != "win32":
        return True

    import winreg

    access_flags = winreg.KEY_READ
    if sys.maxsize < 2**32:
        access_flags |= winreg.KEY_WOW64_64KEY
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _UNINSTALL_KEY, 0, access_flags):
            return True
    except OSError:
        return False


def install_pawnio_silently(timeout: float = 60.0) -> bool:
    installer = pawnio_installer_path()
    if installer is None:
        return False

    try:
        result = subprocess.run(
            [str(installer), "-install", "-silent"],
            check=False,
            capture_output=True,
            timeout=timeout,
            **hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0
