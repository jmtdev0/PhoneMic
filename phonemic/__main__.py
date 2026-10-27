"""Command line entry point: `python -m phonemic`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import secrets
import socket
import ssl
import sys
import webbrowser
from pathlib import Path

from aiohttp import web

from . import __version__
from .admin import create_admin_app
from .audio_out import AudioOutput, DeviceNotFoundError, find_output_device, output_devices
from .certs import ensure_certificate, format_pin, key_pin_hex, local_ips
from .devices import DeviceRegistry, EnrollmentTokens
from .server import ServerInfo, create_app

ROOT = Path(__file__).resolve().parent.parent
CERT_DIR = ROOT / "certs"
DATA_DIR = ROOT / "data"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="phonemic", description="Use a phone as a microphone for this PC.")
    parser.add_argument("--output", default="CABLE Input", help='playback device name fragment (default: "CABLE Input")')
    parser.add_argument("--port", type=int, default=8443, help="HTTPS port for phones (default: 8443)")
    parser.add_argument("--admin-port", type=int, default=8444, help="pairing page port, 127.0.0.1 only (default: 8444)")
    parser.add_argument("--host", default="0.0.0.0", help="address phones connect to (default: all interfaces)")
    parser.add_argument("--code", help="fixed 6-digit code for the browser page (default: random on every start)")
    parser.add_argument("--prebuffer-ms", type=int, default=60, help="audio buffered before playback starts")
    parser.add_argument("--open", action="store_true", help="open the pairing page in the browser")
    parser.add_argument("--paired", action="store_true", help="list the paired phones and exit")
    parser.add_argument("--list-devices", action="store_true", help="list WASAPI playback devices and exit")
    return parser.parse_args(argv)


async def _serve(app: web.Application, admin_app: web.Application, args: argparse.Namespace, ssl_context: ssl.SSLContext) -> None:
    runner = web.AppRunner(app, access_log=None)
    admin_runner = web.AppRunner(admin_app, access_log=None)
    await runner.setup()
    await admin_runner.setup()
    try:
        await web.TCPSite(runner, args.host, args.port, ssl_context=ssl_context).start()
        await web.TCPSite(admin_runner, "127.0.0.1", args.admin_port).start()
        await asyncio.Event().wait()
    finally:
        await admin_runner.cleanup()
        await runner.cleanup()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    sys.stdout.reconfigure(line_buffering=True)  # the banner must show up even when output is redirected
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")

    if args.list_devices:
        for index, name in output_devices():
            print(f"[{index}] {name}")
        return 0

    registry = DeviceRegistry(DATA_DIR / "devices.json")
    if args.paired:
        for device in registry.list():
            print(f"{device.id}  {device.name}  paired {device.created_at[:10]}  last seen {device.last_seen or 'never'}")
        if not registry.list():
            print("No phone is paired.")
        return 0

    try:
        device_index, device_name = find_output_device(args.output)
    except DeviceNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 2

    ips = local_ips()
    cert, key = ensure_certificate(CERT_DIR, ips)
    pin = key_pin_hex(cert)
    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
    ssl_context.load_cert_chain(cert, key)

    code = args.code or f"{secrets.randbelow(10**6):06d}"
    prebuffer_ms = args.prebuffer_ms
    info = ServerInfo(name=socket.gethostname(), port=args.port, pin_hex=pin, ips=tuple(ips))
    app = create_app(code, lambda rate: AudioOutput(device_index, rate, prebuffer_ms), registry=registry, enrollment=EnrollmentTokens(), info=info)
    admin_app = create_admin_app(app)
    admin_url = f"http://127.0.0.1:{args.admin_port}"

    print(f"PhoneMic {__version__}")
    print(f"Output device: {device_name}  [index {device_index}]")
    print(f"Paired phones: {len(registry.list())}")
    print(f"Pair a phone (QR code, devices, revoke): {admin_url}")
    print("Browser page for a phone that is not paired (accept the certificate warning once):")
    for ip in ips or ["<this-pc-ip>"]:
        print(f"    https://{ip}:{args.port}   code {code}")
    print(f"Server key pin: {format_pin(pin)}")
    print("Android app: open /app.apk on the same address to download it.")
    print("Press Ctrl+C to stop.\n")

    if (args.open or not registry.list()) and sys.stdout.isatty():
        webbrowser.open(admin_url)

    try:
        asyncio.run(_serve(app, admin_app, args, ssl_context))
    except KeyboardInterrupt:
        print("Stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
