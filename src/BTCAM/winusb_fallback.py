from __future__ import annotations

import math
from pathlib import Path
import sys
import time


_WINUSB_INTERFACE_GUID = "{dee824ef-729b-4a0e-9c14-b7117d33a817}"
_NZXT_VENDOR_ID = 0x1E71
_KRAKEN_2023_ELITE_PID = 0x300C
_LCD_TOTAL_MEMORY_BLOCKS = 24320


def install_liquidctl_winusb_fallback() -> None:
    if sys.platform != "win32":
        return

    try:
        from liquidctl.driver import kraken3
        from winusbcdc import WinUsbPy
    except Exception:
        return

    if getattr(kraken3.KrakenZ3, "_btcam_winusb_fallback", False):
        return

    original_find = kraken3.KrakenZ3._find_winusb_device
    original_set_screen = kraken3.KrakenZ3.set_screen
    original_send_data = kraken3.KrakenZ3._send_data
    original_send_2023_data_fw2 = kraken3.KrakenZ3._send_2023_data_fw2

    def _find_winusb_device(self, vid: int, pid: int, serial: str | None) -> bool:
        if original_find(self, vid, pid, serial):
            return True

        path = _find_registry_winusb_path(vid, pid)
        if not path:
            return False

        self._btcam_winusb_path = path
        return True

    def _set_screen(self, channel: str, mode: str, value: object, **kwargs: object) -> object:
        opened = False
        if str(channel).lower() == "lcd" and str(mode).lower() in {"static", "gif"}:
            opened = _ensure_bulk_device_open(self, WinUsbPy)
            if str(mode).lower() == "static" and _is_bulk_device_open(self) and _should_use_2023_elite_bgr_stream(self):
                try:
                    return _set_2023_elite_bgr_stream(self, value)
                finally:
                    if opened:
                        _close_bulk_device(self)
        try:
            return original_set_screen(self, channel, mode, value, **kwargs)
        finally:
            if opened:
                _close_bulk_device(self)

    def _send_data(self, data: object, bulk_info: object) -> object:
        opened = _ensure_bulk_device_open(self, WinUsbPy)
        try:
            return original_send_data(self, data, bulk_info)
        finally:
            if opened:
                _close_bulk_device(self)

    def _send_2023_data_fw2(self, data: object, bulk_info: object) -> object:
        duplicate = _is_immediate_duplicate_fw2_send(self, data)
        if duplicate and getattr(kraken3.KrakenZ3, "_btcam_fw2_initial_double_send_done", False):
            return None

        opened = _ensure_bulk_device_open(self, WinUsbPy)
        try:
            result = original_send_2023_data_fw2(self, data, bulk_info)
        finally:
            if opened:
                _close_bulk_device(self)

        _remember_fw2_send(self, data)
        if duplicate:
            kraken3.KrakenZ3._btcam_fw2_initial_double_send_done = True
        return result

    kraken3.KrakenZ3._find_winusb_device = _find_winusb_device
    kraken3.KrakenZ3.set_screen = _set_screen
    kraken3.KrakenZ3._send_data = _send_data
    kraken3.KrakenZ3._send_2023_data_fw2 = _send_2023_data_fw2
    kraken3.KrakenZ3._btcam_winusb_fallback = True


def _ensure_bulk_device_open(device: object, winusb_py: type) -> bool:
    path = getattr(device, "_btcam_winusb_path", None)
    if not path:
        return False

    bulk_device = getattr(device, "bulk_device", None)
    if bulk_device is None:
        bulk_device = winusb_py()
        device.bulk_device = bulk_device

    if getattr(bulk_device, "is_open", False):
        return False

    if not bulk_device.init_winusb_device_with_path(path):
        device.bulk_device = None
        return False

    return True


def _close_bulk_device(device: object) -> None:
    bulk_device = getattr(device, "bulk_device", None)
    if bulk_device is None:
        return

    try:
        bulk_device.close_winusb_device()
    except Exception:
        pass
    finally:
        device.bulk_device = None


def _is_bulk_device_open(device: object) -> bool:
    bulk_device = getattr(device, "bulk_device", None)
    return bool(bulk_device is not None and getattr(bulk_device, "is_open", False))


def _is_immediate_duplicate_fw2_send(device: object, data: object) -> bool:
    last_data_id = getattr(device, "_btcam_last_fw2_data_id", None)
    last_sent_at = getattr(device, "_btcam_last_fw2_sent_at", 0.0)
    return last_data_id == id(data) and time.monotonic() - last_sent_at < 2.0


def _remember_fw2_send(device: object, data: object) -> None:
    device._btcam_last_fw2_data_id = id(data)
    device._btcam_last_fw2_sent_at = time.monotonic()


def _should_use_2023_elite_bgr_stream(device: object) -> bool:
    hid_device = getattr(device, "device", None)
    return (
        getattr(hid_device, "vendor_id", None) == _NZXT_VENDOR_ID
        and getattr(hid_device, "product_id", None) == _KRAKEN_2023_ELITE_PID
        and tuple(getattr(device, "lcd_resolution", ())) == (640, 640)
    )


def _set_2023_elite_bgr_stream(device: object, image_path: object) -> None:
    _refresh_lcd_info(device)
    data = _prepare_bgr888_file(image_path, getattr(device, "lcd_resolution"), getattr(device, "orientation", 0))
    bulk_info = [0x09, 0x0, 0x0, 0x0] + list(len(data).to_bytes(4, "little"))
    device._send_2023_data_fw2(data, bulk_info)


def upload_gif_bucket(device: object, image_path: object) -> int:
    from winusbcdc import WinUsbPy

    opened = _ensure_bulk_device_open(device, WinUsbPy)
    try:
        _refresh_lcd_info(device)
        data = device._prepare_gif_file(image_path, getattr(device, "orientation", 0))
        bulk_info = [0x01, 0x0, 0x0, 0x0] + list(len(data).to_bytes(4, "little"))
        return _send_bucket_data(device, data, bulk_info)
    finally:
        if opened:
            _close_bulk_device(device)


def clear_gif_buckets(device: object) -> None:
    device._delete_all_buckets()


def switch_bucket(device: object, bucket_index: int) -> None:
    bucket_index = int(bucket_index)
    if device._switch_bucket(bucket_index):
        return
    time.sleep(0.2)
    if device._switch_bucket(bucket_index):
        return
    raise RuntimeError(f"Unable to activate LCD bucket {bucket_index}.")


def _send_bucket_data(device: object, data: object, bulk_info: list[int]) -> int:
    if not _is_bulk_device_open(device):
        raise RuntimeError("Cannot find bulk out device")

    device._write_then_read([0x36, 0x03])
    buckets = _query_stable_buckets(device)
    bucket_index = _find_upload_bucket_index(buckets)
    bucket_index = device._prepare_bucket(bucket_index if bucket_index != -1 else 0, bucket_index == -1)

    header = [
        0x12,
        0xFA,
        0x01,
        0xE8,
        0xAB,
        0xCD,
        0xEF,
        0x98,
        0x76,
        0x54,
        0x32,
        0x10,
    ] + bulk_info

    data_size = math.ceil((len(header) + len(data)) / 1024)
    bucket_memory_start = _get_bucket_memory_offset(buckets, bucket_index, data_size)
    if bucket_memory_start == -1:
        device._delete_all_buckets()
        bucket_index = 0
        bucket_memory_start = [0x0, 0x0]

    if not device._setup_bucket(bucket_index, bucket_index + 1, bucket_memory_start, list(data_size.to_bytes(2, "little"))):
        raise RuntimeError("Unable to prepare the LCD bucket for the GIF.")

    device._write_then_read([0x36, 0x01, bucket_index])
    device._bulk_write(header)

    bulk_buffer_size = int(getattr(device, "bulk_buffer_size"))
    for index in range(0, len(data), bulk_buffer_size):
        device._bulk_write(list(data[index : index + bulk_buffer_size]))

    device._write([0x36, 0x02])
    switch_bucket(device, bucket_index)
    _wait_for_bucket_ready(device, bucket_index, data_size)
    return int(bucket_index)


def _find_upload_bucket_index(buckets: dict[int, object]) -> int:
    occupied = sorted(index for index, bucket in buckets.items() if _bucket_has_data(bucket))
    start_index = (occupied[-1] + 1) if occupied else 0
    for index in range(start_index, 16):
        if not _bucket_has_data(buckets.get(index, [])):
            return index
    for index in range(0, start_index):
        if not _bucket_has_data(buckets.get(index, [])):
            return index
    return -1


def _query_stable_buckets(device: object, attempts: int = 5, delay_seconds: float = 0.05) -> dict[int, object]:
    for attempt in range(max(1, int(attempts))):
        buckets = device._query_buckets()
        if _buckets_are_consistent(buckets):
            return buckets
        if attempt < attempts - 1:
            time.sleep(max(0.0, float(delay_seconds)))
    raise RuntimeError("Kraken LCD bucket table is unstable; retry the GIF upload.")


def _buckets_are_consistent(buckets: dict[int, object]) -> bool:
    ranges: list[tuple[int, int]] = []
    for bucket in buckets.values():
        if not _bucket_has_data(bucket):
            continue
        start = int.from_bytes([bucket[17], bucket[18]], "little")
        size = int.from_bytes([bucket[19], bucket[20]], "little")
        end = start + size
        if start < 0 or size <= 0 or end > _LCD_TOTAL_MEMORY_BLOCKS:
            return False
        ranges.append((start, end))

    previous_end = 0
    for start, end in sorted(ranges):
        if start < previous_end:
            return False
        previous_end = end
    return True


def _get_bucket_memory_offset(buckets: dict[int, object], bucket_index: int, data_size: int) -> list[int] | int:
    current = buckets.get(int(bucket_index))
    current_start = 0
    current_size = 0
    if current is not None and len(current) > 20:
        current_start = int.from_bytes([current[17], current[18]], "little")
        current_size = int.from_bytes([current[19], current[20]], "little")
        if _bucket_has_data(current) and data_size <= current_size:
            return [current[17], current[18]]

    occupied_ranges: list[tuple[int, int]] = []
    for index, bucket in buckets.items():
        if int(index) == int(bucket_index) or not _bucket_has_data(bucket):
            continue
        start = int.from_bytes([bucket[17], bucket[18]], "little")
        size = int.from_bytes([bucket[19], bucket[20]], "little")
        occupied_ranges.append((start, start + size))

    if current_start + data_size <= _LCD_TOTAL_MEMORY_BLOCKS and not _overlaps_any(
        current_start,
        current_start + data_size,
        occupied_ranges,
    ):
        return list(current_start.to_bytes(2, "little"))

    next_start = 0
    for start, end in sorted(occupied_ranges):
        if next_start + data_size <= start:
            return list(next_start.to_bytes(2, "little"))
        next_start = max(next_start, end)

    if next_start + data_size <= _LCD_TOTAL_MEMORY_BLOCKS:
        return list(next_start.to_bytes(2, "little"))
    return -1


def _bucket_has_data(bucket: object) -> bool:
    return len(bucket) > 20 and any(bucket[15:]) and int.from_bytes([bucket[19], bucket[20]], "little") > 0


def _overlaps_any(start: int, end: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start < occupied_end and end > occupied_start for occupied_start, occupied_end in ranges)


def _wait_for_bucket_ready(device: object, bucket_index: int, data_size: int, timeout_seconds: float = 2.0) -> bool:
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    while True:
        try:
            buckets = device._query_buckets()
            bucket = buckets.get(int(bucket_index))
            if bucket is not None:
                bucket_size = int.from_bytes([bucket[19], bucket[20]], "little")
                if any(bucket[15:]) and bucket_size >= int(data_size):
                    return True
        except Exception:
            pass

        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def _refresh_lcd_info(device: object) -> None:
    device._write([0x30, 0x01])

    def parse_lcd_info(msg: object) -> None:
        device.brightness = msg[0x18]
        device.orientation = msg[0x1A]

    device._read_until({b"\x31\x01": parse_lcd_info})


def _prepare_bgr888_file(image_path: object, resolution: object, rotation: int) -> bytes:
    from PIL import Image

    size = tuple(resolution)
    if len(size) != 2:
        raise ValueError(f"Invalid LCD resolution: {resolution!r}")

    with Image.open(Path(str(image_path))) as image:
        try:
            image.seek(0)
        except EOFError:
            pass
        frame = image.resize(size).rotate(int(rotation) * -90).convert("RGB")
        return frame.tobytes("raw", "BGR")


def _find_registry_winusb_path(vid: int, pid: int) -> str | None:
    try:
        import winreg
    except ImportError:
        return None

    needle = f"vid_{vid:04x}&pid_{pid:04x}"
    base = rf"SYSTEM\CurrentControlSet\Control\DeviceClasses\{_WINUSB_INTERFACE_GUID}"

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as key:
            candidates = _registry_interface_candidates(winreg, key, needle)
    except OSError:
        return None

    # Kraken Elite image transfers use the vendor-specific interface 0.
    for candidate in candidates:
        if "&mi_00" in candidate.lower():
            return candidate
    return candidates[0] if candidates else None


def _registry_interface_candidates(winreg: object, key: object, needle: str) -> list[str]:
    candidates: list[str] = []
    index = 0
    while True:
        try:
            name = winreg.EnumKey(key, index)
        except OSError:
            break

        index += 1
        lower = name.lower()
        if needle not in lower:
            continue

        path = _registry_key_to_device_path(name)
        if path:
            candidates.append(path)
    return candidates


def _registry_key_to_device_path(name: str) -> str | None:
    # DeviceClasses subkeys store "\\?\" paths with backslashes encoded as "#".
    # Example: ##?#USB#VID_1E71&PID_300C&MI_00#...#{guid}
    if name.startswith("##?#"):
        return "\\\\?\\" + name[4:]
    return None
