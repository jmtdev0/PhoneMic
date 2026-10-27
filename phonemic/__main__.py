"""Command line entry point: `python -m phonemic`."""

from __future__ import annotations

import argparse
import logging
import secrets
import ssl
import sys
from pathlib import Path

from aiohttp import web

from . import __version__
from .audio_out import AudioOutput, DeviceNotFoundError, find_output_device, output_devices
from .certs import ensure_certificate, fingerprint_sha256, local_ips
from .server import create_app

CERT_DIR = Path(__file__).resolve().parent.parent / "certs"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="phonemic", description="Use a phone as a microphone for this PC.")
    parser.add_argument("--output", default="CABLE Input", help='playback device name fragment (default: "CABLE Input")')
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--host", default="0.0.0.0", help="address to listen on (default: all interfaces)")
    parser.add_argument("--code", help="fixed 6-digit pairing code (default: random on every start)")
    parser.add_argument("--prebuffer-ms", type=int, default=60, help="audio buffered before playback starts")
    parser.add_argument("--list-devices", action="store_true", help="list WASAPI playback devices and exit")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    sys.stdout.reconfigure(line_buffering=True)  # the banner must show up even when output is redirected
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")

    if args.list_devices:
        for index, name in output_devices():
            print(f"[{index}] {name}")
        return 0

    try:
        device_index, device_name = find_output_device(args.output)
    except DeviceNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 2

    ips = local_ips()
    cert, key = ensure_certificate(CERT_DIR, ips)
    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
    ssl_context.load_cert_chain(cert, key)

    code = args.code or f"{secrets.randbelow(10**6):06d}"
    prebuffer_ms = args.prebuffer_ms
    app = create_app(code, lambda rate: AudioOutput(device_index, rate, prebuffer_ms))

    print(f"PhoneMic {__version__}")
    print(f"Output device: {device_name}  [index {device_index}]")
    print("Open this on the phone (same Wi-Fi), accept the certificate warning once:")
    for ip in ips or ["<this-pc-ip>"]:
        print(f"    https://{ip}:{args.port}")
    print(f"Pairing code: {code}")
    print(f"Certificate SHA-256 (the Android app shows the same value): {fingerprint_sha256(cert)}")
    print("Android app: open /app.apk on the same address to download it.")
    print("Press Ctrl+C to stop.\n")

    web.run_app(app, host=args.host, port=args.port, ssl_context=ssl_context, print=None, access_log=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
