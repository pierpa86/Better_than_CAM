from __future__ import annotations

from typing import Any

from .models import StatusSnapshot


MIN_LCD_UPLOAD_INTERVAL_SECONDS = 3.0
LCD_KEEPALIVE_SECONDS = 30.0


def lcd_update_key(snapshot: StatusSnapshot) -> tuple[Any, ...]:
    cooler = snapshot.cooler
    system = snapshot.system
    return (
        _round_int(cooler.liquid_temp_c),
        _bucket(cooler.pump_rpm, 50),
        _bucket(cooler.fan_rpm, 50),
        _round_int(system.cpu_temp_c),
        _temp_or_load_key(system.gpu_temp_c, system.gpu_load_percent),
        _round_int(system.memory_percent),
    )


def should_upload_lcd(
    snapshot: StatusSnapshot,
    last_key: tuple[Any, ...] | None,
    last_upload_at: float,
    now: float,
    first: bool = False,
    min_interval: float = MIN_LCD_UPLOAD_INTERVAL_SECONDS,
    keepalive_interval: float = LCD_KEEPALIVE_SECONDS,
) -> bool:
    if first or last_key is None:
        return True

    elapsed = now - last_upload_at
    if elapsed < min_interval:
        return False

    current_key = lcd_update_key(snapshot)
    return current_key != last_key or elapsed >= keepalive_interval


def _temp_or_load_key(temp_c: float | None, load_percent: float | None) -> tuple[str, int | None]:
    if temp_c is not None:
        return ("temp", _round_int(temp_c))
    return ("load", _bucket(load_percent, 10))


def _round_int(value: float | int | None) -> int | None:
    if value is None:
        return None
    return int(round(float(value)))


def _bucket(value: float | int | None, step: int) -> int | None:
    if value is None:
        return None
    return int(round(float(value) / step) * step)
