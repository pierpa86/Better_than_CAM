# BTCAM

BTCAM is an unofficial Windows app for controlling the LCD display on NZXT
Kraken Elite coolers without using NZXT CAM. It uses `liquidctl` to read the
cooler, renders 640x640 LCD frames with Pillow, and provides both a Tkinter GUI
and CLI commands for previews, diagnostics, and display uploads.

The project is primarily aimed at Kraken Elite 2023/2024 devices. Sensor reads
and image uploads are handled as separate flows, so sensors can still work even
when LCD uploads are not available.

## Features

- Detects Kraken devices through `liquidctl`.
- Reads liquid temperature, pump speed, pump duty, fan speed, and fan duty when
  exposed by the device.
- Reads CPU load, RAM usage, and NVIDIA GPU data; on Windows it can also read
  CPU/GPU temperatures through `HardwareMonitor` / LibreHardwareMonitor.
- Renders 640x640 LCD screens with detailed temperature, dual CPU/GPU, or center
  gauge layouts.
- Customizes colors, visible elements, temperature sources, background,
  orientation, and LCD brightness.
- LCD carousel with temperature screens, local GIFs, and GIFs downloaded from
  Giphy.
- Integrated editor for cropping and preparing GIFs for the circular display.
- Tray icon, minimized startup, and Windows startup option.
- CLI for list/status, preview, `push-once`, continuous daemon mode, and LCD
  timing tests.
- One-file PyInstaller build with the `btcam.ico` icon.

## Requirements

- Windows 10/11.
- Python 3.11 or newer.
- A compatible NZXT Kraken connected through the internal USB cable.
- `liquidctl`, installed through the project's Python dependencies.
- Administrator privileges are recommended and are often required for hardware
  sensors and USB access.
- NZXT CAM must be closed while this app accesses the Kraken.

## Configuration

User configuration is saved in:

```text
%LOCALAPPDATA%\BTCAM\config.json
```

GIFs imported into the app library are copied to:

```text
%USERPROFILE%\Documents\BTCAM
```

The Giphy API key is optional. You can enter it in the GUI options or set it
with the `GIPHY_API_KEY` environment variable.

## CLI

With the virtual environment active:

```powershell
python -m BTCAM.cli list
python -m BTCAM.cli status
python -m BTCAM.cli sensor-debug
python -m BTCAM.cli preview --simulate --out preview.png
python -m BTCAM.cli push-once
python -m BTCAM.cli daemon --interval 1 --lcd-transport gif
python -m BTCAM.cli set-liquid
```

To investigate LCD uploads that happen too close together:

```powershell
python -m BTCAM.cli lcd-timing-test --lcd-transport gif --interval 2.0 --count 10
python -m BTCAM.cli lcd-timing-test --lcd-transport static --interval 3.0 --count 10
```

## Build EXE

The build uses PyInstaller and produces a one-file executable with a UAC
request:

```powershell
.\build_exe.ps1 -Clean
```

Output:

```text
dist\BTCAM.exe
```

The executable includes the `btcam.ico` icon and the required modules for
liquidctl, HardwareMonitor, and WinUSB.

## Hardware Diagnostics

If the Kraken is not detected:

- Close NZXT CAM and any other RGB or monitoring software.
- Check the Kraken internal USB cable.
- Run PowerShell as administrator.
- Verify `liquidctl list`.

If `Cannot find bulk out device` appears:

- The Kraken can be readable, but the USB interface required to send images to
  the display is not available.
- Sensor reads, liquid mode, orientation, and brightness can still work.
- On Windows, use Zadig to verify that the correct Kraken USB interface uses
  WinUSB without replacing the HID interface.

If the CPU temperature does not appear:

- Run the app as administrator.
- Run `python -m BTCAM.cli sensor-debug`.
- If no reliable CPU reading appears, the app leaves the value unavailable
  instead of showing an invented temperature.

## Note

This project is not affiliated with NZXT. Use it knowing that direct LCD control
depends on firmware/driver support and can change with future `liquidctl`
versions.
