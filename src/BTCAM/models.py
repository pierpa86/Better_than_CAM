from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class CoolerStatus:
    description: str | None = None
    liquid_temp_c: float | None = None
    pump_rpm: int | None = None
    pump_duty_percent: float | None = None
    fan_rpm: int | None = None
    fan_duty_percent: float | None = None
    raw_status: list[dict[str, Any]] | None = None


@dataclass(slots=True)
class SystemStatus:
    cpu_load_percent: float | None = None
    memory_percent: float | None = None
    cpu_temp_c: float | None = None
    gpu_temp_c: float | None = None
    gpu_load_percent: float | None = None
    gpu_name: str | None = None


@dataclass(slots=True)
class StatusSnapshot:
    cooler: CoolerStatus
    system: SystemStatus
    captured_at: datetime
    simulated: bool = False

    def to_jsonable(self) -> dict[str, Any]:
        data = asdict(self)
        data["captured_at"] = self.captured_at.isoformat(timespec="seconds")
        return data

