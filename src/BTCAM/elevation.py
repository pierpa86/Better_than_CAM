from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from collections.abc import Sequence


def is_admin() -> bool:
    if sys.platform != "win32":
        return True

    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def ensure_admin_or_relaunch(args: Sequence[str] | None = None) -> bool:
    if sys.platform != "win32" or is_admin():
        return True

    executable = sys.executable
    params = subprocess.list2cmdline(list(args if args is not None else sys.argv[1:]))
    result = ctypes.windll.shell32.ShellExecuteW(None, "runas", executable, params, os.getcwd(), 1)
    if result <= 32:
        raise RuntimeError(f"Richiesta amministratore non riuscita, codice ShellExecute {result}.")
    return False
