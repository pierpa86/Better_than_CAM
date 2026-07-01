from __future__ import annotations

import math
import random
from datetime import datetime

from .liquidctl_client import LiquidctlClient
from .models import CoolerStatus, StatusSnapshot, SystemStatus
from .sensors import SystemSensorReader


class SnapshotProvider:
    def __init__(
        self,
        liquidctl_path: str = "liquidctl",
        match: str = "kraken",
        simulate: bool = False,
    ) -> None:
        self.simulate = simulate
        self.client = LiquidctlClient(liquidctl_path, match)
        self.system = SystemSensorReader()

    def read(self) -> StatusSnapshot:
        if self.simulate:
            return simulated_snapshot()
        cooler = self.client.status()
        system = self.system.read()
        return StatusSnapshot(cooler=cooler, system=system, captured_at=datetime.now())


def simulated_snapshot() -> StatusSnapshot:
    now = datetime.now()
    wave = math.sin(now.timestamp() / 18)
    liquid = 34.5 + wave * 3.2 + random.uniform(-0.2, 0.2)
    cpu = 38 + math.sin(now.timestamp() / 9) * 11
    gpu = 42 + math.cos(now.timestamp() / 13) * 8
    pump = 2150 + int(wave * 190)
    return StatusSnapshot(
        cooler=CoolerStatus(
            description="Simulated NZXT Kraken Elite",
            liquid_temp_c=liquid,
            pump_rpm=pump,
            pump_duty_percent=70,
            fan_rpm=980 + int(wave * 120),
            fan_duty_percent=42,
        ),
        system=SystemStatus(
            cpu_load_percent=45 + wave * 18,
            memory_percent=62,
            cpu_temp_c=cpu,
            gpu_temp_c=gpu,
            gpu_load_percent=38,
            gpu_name="Simulated GPU",
        ),
        captured_at=now,
        simulated=True,
    )

