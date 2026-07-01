from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from pathlib import Path

from .config import AppConfig
from .lcd_update import lcd_update_key, should_upload_lcd
from .liquidctl_client import LcdImageTransferError, LiquidctlClient, LiquidctlError
from .renderer import LcdRenderer
from .runtime import SnapshotProvider, simulated_snapshot
from .sensors import SystemSensorReader


def main(argv: list[str] | None = None) -> int:
    config = AppConfig.load()
    parser = argparse.ArgumentParser(prog="btcam")
    parser.add_argument("--liquidctl", default=config.liquidctl_path, help="Percorso eseguibile liquidctl")
    parser.add_argument("--match", default=config.match, help="Filtro liquidctl, default: kraken")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list", help="Elenca dispositivi Kraken rilevati")
    subparsers.add_parser("status", help="Mostra stato corrente in JSON")
    subparsers.add_parser("sensor-debug", help="Mostra sensori temperatura grezzi e selezione")
    subparsers.add_parser("initialize", help="Inizializza il Kraken")
    subparsers.add_parser("set-liquid", help="Imposta lo schermo in modalita liquid")

    preview = subparsers.add_parser("preview", help="Genera una preview PNG")
    preview.add_argument("--out", default="preview.png", help="Percorso PNG")
    preview.add_argument("--simulate", action="store_true", help="Usa dati simulati")
    preview.add_argument("--background", default=config.background_image_path, help="Immagine di sfondo LCD")

    push_once = subparsers.add_parser("push-once", help="Renderizza e invia una schermata all'LCD")
    push_once.add_argument("--out", default=None, help="Percorso PNG temporaneo")
    push_once.add_argument("--simulate", action="store_true", help="Non legge il Kraken, utile per test preview")
    push_once.add_argument("--background", default=config.background_image_path, help="Immagine di sfondo LCD")
    push_once.add_argument("--lcd-transport", choices=("gif", "static"), default=config.lcd_transport)

    daemon = subparsers.add_parser("daemon", help="Aggiorna continuamente l'LCD")
    daemon.add_argument("--interval", type=int, default=config.interval_seconds)
    daemon.add_argument("--lcd-min-upload-interval", type=float, default=config.lcd_min_upload_interval_seconds)
    daemon.add_argument("--lcd-keepalive", type=float, default=config.lcd_keepalive_seconds)
    daemon.add_argument("--lcd-transport", choices=("gif", "static"), default=config.lcd_transport)
    daemon.add_argument("--simulate", action="store_true")
    daemon.add_argument("--background", default=config.background_image_path, help="Immagine di sfondo LCD")

    timing = subparsers.add_parser("lcd-timing-test", help="Prova un cooldown LCD preciso con frame numerati")
    timing.add_argument("--interval", type=float, default=config.lcd_min_upload_interval_seconds)
    timing.add_argument("--count", type=int, default=10)
    timing.add_argument("--lcd-transport", choices=("gif", "static"), default=config.lcd_transport)
    timing.add_argument("--background", default=config.background_image_path, help="Immagine di sfondo LCD")

    args = parser.parse_args(argv)
    client = LiquidctlClient(args.liquidctl, args.match)

    try:
        if args.command == "list":
            print(json.dumps(client.list_devices(), indent=2))
        elif args.command == "status":
            snapshot = SnapshotProvider(args.liquidctl, args.match).read()
            print(json.dumps(snapshot.to_jsonable(), indent=2))
        elif args.command == "sensor-debug":
            print(json.dumps(SystemSensorReader(temp_cache_seconds=0).debug_temperatures(), indent=2))
        elif args.command == "initialize":
            print(json.dumps(client.initialize(), indent=2))
        elif args.command == "set-liquid":
            client.set_lcd_liquid()
            print("Display impostato in modalita liquid.")
        elif args.command == "preview":
            snapshot = simulated_snapshot() if args.simulate else SnapshotProvider(args.liquidctl, args.match).read()
            path = LcdRenderer(config.display_size, background_path=args.background).save(snapshot, args.out)
            print(path)
        elif args.command == "push-once":
            snapshot = simulated_snapshot() if args.simulate else SnapshotProvider(args.liquidctl, args.match).read()
            transport = _lcd_transport(args.lcd_transport)
            path = Path(args.out) if args.out else _lcd_output_path(config, transport)
            _save_lcd_image(LcdRenderer(config.display_size, background_path=args.background), snapshot, path, transport)
            if not args.simulate:
                try:
                    _send_lcd_image(client, path, transport)
                except LcdImageTransferError as exc:
                    print(path)
                    print(f"Errore LCD: {exc}", file=sys.stderr)
                    return 2
            print(path)
        elif args.command == "daemon":
            return _daemon(args, config)
        elif args.command == "lcd-timing-test":
            return _lcd_timing_test(args, config, client)
    except LiquidctlError as exc:
        print(f"Errore: {exc}", file=sys.stderr)
        return 1
    return 0


def _daemon(args: argparse.Namespace, config: AppConfig) -> int:
    running = True

    def stop(_signum: int, _frame: object) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    provider = SnapshotProvider(args.liquidctl, args.match, simulate=args.simulate)
    client = LiquidctlClient(args.liquidctl, args.match)
    renderer = LcdRenderer(config.display_size, background_path=args.background)
    interval = max(1, int(args.interval))
    lcd_min_interval = max(1.0, float(args.lcd_min_upload_interval))
    lcd_keepalive = max(lcd_min_interval, float(args.lcd_keepalive))
    lcd_transport = _lcd_transport(args.lcd_transport)
    output_path = _lcd_output_path(config, lcd_transport)
    first = True
    last_lcd_key = None
    last_lcd_upload_at = 0.0

    print(f"Avvio aggiornamento LCD ogni {interval}s. Ctrl+C per uscire.")
    while running:
        cycle_started_at = time.monotonic()
        try:
            snapshot = provider.read()
            _save_lcd_image(renderer, snapshot, output_path, lcd_transport)
            if not args.simulate:
                now = time.monotonic()
                if should_upload_lcd(
                    snapshot,
                    last_lcd_key,
                    last_lcd_upload_at,
                    now,
                    first,
                    min_interval=lcd_min_interval,
                    keepalive_interval=lcd_keepalive,
                ):
                    try:
                        upload_started_at = time.monotonic()
                        _send_lcd_image(client, output_path, lcd_transport)
                    except LcdImageTransferError as exc:
                        print(f"Errore LCD: {exc}", file=sys.stderr)
                        print("Arrestato: preview generata ma invio immagine LCD non disponibile.")
                        return 2
                    last_lcd_key = lcd_update_key(snapshot)
                    last_lcd_upload_at = time.monotonic()
                    print(
                        f"upload={last_lcd_upload_at - upload_started_at:.3f}s "
                        f"cooldown={lcd_min_interval:.1f}s transport={lcd_transport}"
                    )
            liquid = snapshot.cooler.liquid_temp_c
            label = "--" if liquid is None else f"{int(round(liquid))}C"
            print(f"{snapshot.captured_at:%H:%M:%S} liquid={label}")
            first = False
        except LiquidctlError as exc:
            print(f"Errore: {exc}", file=sys.stderr)
        elapsed = time.monotonic() - cycle_started_at
        time.sleep(max(0.0, interval - elapsed))
    print("Arrestato.")
    return 0


def _lcd_timing_test(args: argparse.Namespace, config: AppConfig, client: LiquidctlClient) -> int:
    interval = max(1.0, float(args.interval))
    count = max(1, int(args.count))
    transport = _lcd_transport(args.lcd_transport)
    renderer = LcdRenderer(config.display_size, background_path=args.background)
    path = _lcd_output_path(config, transport).with_stem("btcam-lcd-timing")

    print(
        f"Test timing LCD: {count} upload, cooldown {interval:.1f}s dopo ogni upload, transport {transport}. "
        "Guarda il display: se diventa nero, questo intervallo e troppo basso."
    )
    last_finished_at = 0.0
    for index in range(count):
        if last_finished_at:
            remaining = interval - (time.monotonic() - last_finished_at)
            if remaining > 0:
                time.sleep(remaining)

        snapshot = simulated_snapshot()
        snapshot.system.gpu_temp_c = 25 + (index % 9) * 8
        snapshot.system.cpu_temp_c = 35 + (index % 6) * 4
        snapshot.cooler.liquid_temp_c = 28 + (index % 5)
        _save_lcd_image(renderer, snapshot, path, transport)

        started_at = time.monotonic()
        try:
            _send_lcd_image(client, path, transport)
        except LcdImageTransferError as exc:
            print(f"Errore LCD: {exc}", file=sys.stderr)
            return 2
        except LiquidctlError as exc:
            print(f"Errore: {exc}", file=sys.stderr)
            return 1
        finished_at = time.monotonic()
        last_finished_at = finished_at
        print(f"{index + 1}/{count}: upload={finished_at - started_at:.3f}s, next_cooldown={interval:.1f}s")

    print("Test completato.")
    return 0


def _lcd_transport(value: str | None) -> str:
    return "gif" if str(value or "").strip().lower() == "gif" else "static"


def _lcd_output_path(config: AppConfig, transport: str) -> Path:
    path = config.output_path
    return path.with_suffix(".gif") if transport == "gif" else path.with_suffix(".png")


def _save_lcd_image(renderer: LcdRenderer, snapshot: object, path: Path, transport: str) -> Path:
    if transport == "gif":
        return renderer.save_gif(snapshot, path)  # type: ignore[arg-type]
    return renderer.save(snapshot, path)  # type: ignore[arg-type]


def _send_lcd_image(client: LiquidctlClient, path: Path, transport: str) -> None:
    if transport == "gif":
        client.set_lcd_gif(path)
        return
    client.set_lcd_static(path)


if __name__ == "__main__":
    raise SystemExit(main())
