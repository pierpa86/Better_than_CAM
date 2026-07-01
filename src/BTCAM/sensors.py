from __future__ import annotations

import contextlib
import io
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Any

import psutil

from .models import SystemStatus
from .subprocess_utils import hidden_subprocess_kwargs


@dataclass(slots=True)
class _CachedTemps:
    updated_at: float = 0.0
    cpu_temp_c: float | None = None
    gpu_temp_c: float | None = None


class SystemSensorReader:
    def __init__(self, temp_cache_seconds: float = 1.0) -> None:
        self._temps = _CachedTemps()
        self._temp_cache_seconds = temp_cache_seconds

    def read(self) -> SystemStatus:
        now = time.monotonic()
        cpu_load = psutil.cpu_percent(interval=None)
        memory = psutil.virtual_memory().percent
        gpu_temp, gpu_load, gpu_name = self._read_nvidia_smi()

        if now - self._temps.updated_at > self._temp_cache_seconds:
            monitor_cpu, monitor_gpu = self._read_hardware_monitor_internal()
            monitor_cpu, monitor_gpu = _fill_missing_temperatures(
                monitor_cpu,
                monitor_gpu,
                self._temps.cpu_temp_c,
                self._temps.gpu_temp_c,
            )
            self._temps = _CachedTemps(time.monotonic(), monitor_cpu, monitor_gpu)

        if gpu_temp is None:
            gpu_temp = self._temps.gpu_temp_c

        return SystemStatus(
            cpu_load_percent=cpu_load,
            memory_percent=memory,
            cpu_temp_c=self._temps.cpu_temp_c,
            gpu_temp_c=gpu_temp,
            gpu_load_percent=gpu_load,
            gpu_name=gpu_name,
        )

    def _read_nvidia_smi(self) -> tuple[float | None, float | None, str | None]:
        if not shutil.which("nvidia-smi"):
            return None, None, None

        command = [
            "nvidia-smi",
            "--query-gpu=temperature.gpu,utilization.gpu,name",
            "--format=csv,noheader,nounits",
        ]
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
                **hidden_subprocess_kwargs(),
            )
        except (OSError, subprocess.TimeoutExpired):
            return None, None, None
        if result.returncode != 0 or not result.stdout.strip():
            return None, None, None

        first_line = result.stdout.strip().splitlines()[0]
        parts = [part.strip() for part in first_line.split(",", 2)]
        if len(parts) < 3:
            return None, None, None
        return _float_or_none(parts[0]), _float_or_none(parts[1]), parts[2]

    def _read_hardware_monitor_internal(self) -> tuple[float | None, float | None]:
        rows = self._read_hardware_monitor_internal_rows()
        return _pick_temperature(rows, "cpu"), _pick_temperature(rows, "gpu")

    def _read_hardware_monitor_internal_rows(self) -> list[dict[str, Any]]:
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
                from HardwareMonitor.Hardware import Computer
        except (ImportError, OSError, RuntimeError):
            return []

        rows: list[dict[str, Any]] = []
        computer = Computer()
        for attr in (
            "IsCpuEnabled",
            "IsGpuEnabled",
            "IsMotherboardEnabled",
            "IsControllerEnabled",
            "IsMemoryEnabled",
            "IsStorageEnabled",
        ):
            setattr(computer, attr, True)

        try:
            with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
                computer.Open()
                for hardware in computer.Hardware:
                    _update_hardware(hardware)
                    _collect_internal_temperatures(hardware, rows)
        except Exception:
            return []
        finally:
            try:
                with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
                    computer.Close()
            except Exception:
                pass

        return rows

    def debug_temperatures(self) -> dict[str, Any]:
        rows = self._read_hardware_monitor_internal_rows()
        return {"sources": [_debug_source("internal", rows)]}


def _fill_missing_temperatures(
    current_cpu: float | None,
    current_gpu: float | None,
    fallback_cpu: float | None,
    fallback_gpu: float | None,
) -> tuple[float | None, float | None]:
    return (
        current_cpu if current_cpu is not None else fallback_cpu,
        current_gpu if current_gpu is not None else fallback_gpu,
    )


def _debug_source(name: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    cpu_temp, gpu_temp = _pick_rows_temperatures(rows)
    return {
        "source": name,
        "selected_cpu_temp_c": cpu_temp,
        "selected_gpu_temp_c": gpu_temp,
        "rows": rows,
    }


def _update_hardware(hardware: Any) -> None:
    with contextlib.suppress(Exception):
        hardware.Update()
    for subhardware in hardware.SubHardware:
        with contextlib.suppress(Exception):
            subhardware.Update()


def _collect_internal_temperatures(hardware: Any, rows: list[dict[str, Any]]) -> None:
    parent = f"{hardware.HardwareType} {hardware.Name}"
    _collect_internal_sensor_rows(hardware.Sensors, parent, rows)
    for subhardware in hardware.SubHardware:
        sub_parent = f"{parent} {subhardware.HardwareType} {subhardware.Name}"
        _collect_internal_sensor_rows(subhardware.Sensors, sub_parent, rows)


def _collect_internal_sensor_rows(sensors: Any, parent: str, rows: list[dict[str, Any]]) -> None:
    for sensor in sensors:
        if str(sensor.SensorType) != "Temperature":
            continue
        value = _float_or_none(sensor.Value)
        if value is None:
            continue
        rows.append(
            {
                "Name": str(sensor.Name),
                "Parent": parent,
                "Value": value,
            }
        )


def _pick_temperature(rows: list[dict[str, Any]], target: str) -> float | None:
    if target.lower() == "cpu":
        cpu_package = _pick_exact_cpu_package_temperature(rows)
        if cpu_package is not None:
            return cpu_package

    weighted: list[tuple[int, float]] = []
    target = target.lower()
    for row in rows:
        name = str(row.get("Name", "")).lower()
        parent = str(row.get("Parent", "")).lower()
        value = _float_or_none(row.get("Value"))
        if value is None:
            continue
        haystack = f"{name} {parent}"
        if target not in haystack:
            continue
        if _is_excluded_temperature_sensor(name):
            continue
        weight = _temperature_sensor_weight(name, target)
        weighted.append((weight, value))
    if not weighted:
        return None
    weighted.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return weighted[0][1]


def _pick_exact_cpu_package_temperature(rows: list[dict[str, Any]]) -> float | None:
    for row in rows:
        name = str(row.get("Name", "")).strip().lower()
        parent = str(row.get("Parent", "")).strip().lower()
        if name != "cpu package":
            continue
        if "cpu" not in parent:
            continue
        value = _float_or_none(row.get("Value"))
        if value is not None:
            return value
    return None


def _pick_rows_temperatures(rows: list[dict[str, Any]]) -> tuple[float | None, float | None]:
    return _pick_temperature(rows, "cpu"), _pick_temperature(rows, "gpu")


def _is_excluded_temperature_sensor(name: str) -> bool:
    return any(
        token in name
        for token in (
            "distance to tjmax",
            "tj max",
            "tjmax",
            "critical",
            "warning",
            "throttle",
        )
    )


def _temperature_sensor_weight(name: str, target: str) -> int:
    if target == "cpu":
        if "package" in name:
            return 100
        if "tctl" in name or "tdie" in name:
            return 90
        if "core average" in name:
            return 70
        if "core max" in name:
            return 60
        if "core" in name:
            return 50
        return 0

    weight = 0
    if "core" in name:
        weight += 3
    if "hot spot" in name or "junction" in name:
        weight += 2
    return weight


def _float_or_none(value: Any) -> float | None:
    try:
        cleaned = (
            str(value)
            .strip()
            .replace("%", "")
            .replace("\N{DEGREE SIGN}C", "")
            .replace("C", "")
            .replace("c", "")
        )
        return float(cleaned)
    except (TypeError, ValueError):
        return None
