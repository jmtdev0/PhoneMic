"""HTTPS + WebSocket server: serves the phone page and feeds the incoming audio to the output device.

Protocol (WebSocket at /ws):
  1. The phone sends a text message {"type": "hello", "code": "123456", "sampleRate": 48000}.
  2. The server answers {"type": "ready"} or {"type": "error", "reason": ...} and closes on errors.
  3. The phone then sends binary messages of little-endian int16 mono PCM at that sample rate.
  4. Every couple of seconds the server sends {"type": "stats", ...} for the phone to display.
Close codes: 4400 malformed hello, 4401 wrong code, 4409 replaced by a newer connection, 4500 audio error.
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from aiohttp import WSMsgType, web

from .pcm_queue import PcmQueue

log = logging.getLogger("phonemic")

WEB_DIR = Path(__file__).parent / "web"
DEFAULT_APK = Path(__file__).resolve().parent.parent / "android" / "app" / "build" / "outputs" / "apk" / "debug" / "app-debug.apk"
HELLO_TIMEOUT_S = 10
AUTH_FAIL_DELAY_S = 1  # slows down code guessing
STATS_INTERVAL_S = 2.0
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_SECONDS = 60
MIN_SAMPLE_RATE, MAX_SAMPLE_RATE = 8000, 96000

# Explicit list on purpose: ipaddress.is_private also accepts reserved and documentation ranges.
LOCAL_NETWORKS = [
    ipaddress.ip_network(cidr)
    for cidr in ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "::1/128", "fe80::/10", "fc00::/7")
]


class Output(Protocol):
    queue: PcmQueue

    def start(self) -> None: ...

    def close(self) -> None: ...


OutputFactory = Callable[[int], Output]


class PairingGuard:
    """Constant-time pairing-code check with a per-address lockout after repeated failures."""

    def __init__(self, code: str, clock: Callable[[], float] = time.monotonic) -> None:
        self._code = code
        self._clock = clock
        self._failures: dict[str, int] = {}
        self._locked_until: dict[str, float] = {}

    def locked(self, address: str) -> bool:
        until = self._locked_until.get(address)
        if until is None:
            return False
        if self._clock() >= until:
            del self._locked_until[address]
            self._failures.pop(address, None)
            return False
        return True

    def check(self, address: str, candidate: str) -> bool:
        if hmac.compare_digest(candidate.encode(), self._code.encode()):
            self._failures.pop(address, None)
            return True
        count = self._failures.get(address, 0) + 1
        self._failures[address] = count
        if count >= MAX_FAILED_ATTEMPTS:
            self._locked_until[address] = self._clock() + LOCKOUT_SECONDS
        return False


def is_local_address(address: str | None) -> bool:
    """Only loopback and private-network clients may connect."""
    try:
        ip = ipaddress.ip_address(address or "")
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return any(ip in network for network in LOCAL_NETWORKS if network.version == ip.version)


@dataclass
class _Session:
    ws: web.WebSocketResponse
    output: Output
    address: str


@dataclass
class _State:
    guard: PairingGuard
    output_factory: OutputFactory
    apk_path: Path = DEFAULT_APK
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active: _Session | None = None


STATE = web.AppKey("state", _State)


async def _index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(
        WEB_DIR / "index.html",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


async def _apk(request: web.Request) -> web.StreamResponse:
    """The Android app, served so the phone can install it over Wi-Fi (no USB cable)."""
    if not is_local_address(request.remote):
        raise web.HTTPForbidden(text="Local network clients only.")
    apk = request.app[STATE].apk_path
    if not apk.is_file():
        raise web.HTTPNotFound(text="The Android app has not been built yet.")
    return web.FileResponse(
        apk,
        headers={
            "Content-Type": "application/vnd.android.package-archive",
            "Content-Disposition": 'attachment; filename="PhoneMic.apk"',
            "Cache-Control": "no-store",
        },
    )


async def _send_stats(session: _Session) -> None:
    queue = session.output.queue
    while not session.ws.closed:
        await asyncio.sleep(STATS_INTERVAL_S)
        await session.ws.send_json(
            {
                "type": "stats",
                "bufferedMs": queue.buffered_ms,
                "underruns": queue.underruns,
                "droppedMs": int(queue.dropped_samples * 1000 / queue.sample_rate),
            }
        )


async def _ws_handler(request: web.Request) -> web.StreamResponse:
    state = request.app[STATE]
    address = request.remote or ""
    if not is_local_address(address):
        raise web.HTTPForbidden(text="Local network clients only.")
    if state.guard.locked(address):
        raise web.HTTPTooManyRequests(text="Too many wrong codes. Try again in a minute.")

    ws = web.WebSocketResponse(heartbeat=10, max_msg_size=64 * 1024)
    await ws.prepare(request)

    try:
        hello = await asyncio.wait_for(ws.receive_json(), HELLO_TIMEOUT_S)
        sample_rate = int(hello["sampleRate"])
        code = str(hello["code"])
        if hello.get("type") != "hello" or not MIN_SAMPLE_RATE <= sample_rate <= MAX_SAMPLE_RATE:
            raise ValueError("bad hello")
    except (asyncio.TimeoutError, TypeError, ValueError, KeyError):
        await ws.close(code=4400, message=b"bad hello")
        return ws

    if not state.guard.check(address, code):
        log.warning("Wrong pairing code from %s", address)
        await ws.send_json({"type": "error", "reason": "wrong code"})
        await asyncio.sleep(AUTH_FAIL_DELAY_S)
        await ws.close(code=4401, message=b"wrong code")
        return ws

    async with state.lock:
        previous = state.active
        if previous is not None:
            state.active = None
            await previous.ws.close(code=4409, message=b"replaced")
            previous.output.close()
        try:
            output = state.output_factory(sample_rate)
            output.start()
        except Exception as exc:  # noqa: BLE001 - report any audio failure to the phone
            log.error("Could not open the audio output: %s", exc)
            await ws.send_json({"type": "error", "reason": f"audio output failed: {exc}"})
            await ws.close(code=4500, message=b"audio error")
            return ws
        session = _Session(ws, output, address)
        state.active = session

    log.info("Phone connected from %s (%d Hz)", address, sample_rate)
    await ws.send_json({"type": "ready"})
    stats_task = asyncio.create_task(_send_stats(session))
    try:
        async for message in ws:
            if message.type == WSMsgType.BINARY:
                output.queue.push(message.data)
            elif message.type == WSMsgType.ERROR:
                break
    finally:
        stats_task.cancel()
        async with state.lock:
            if state.active is session:
                state.active = None
                output.close()
        log.info("Phone disconnected (%s)", address)
    return ws


async def _close_active(app: web.Application) -> None:
    session = app[STATE].active
    if session is not None:
        app[STATE].active = None
        await session.ws.close(code=1001)
        session.output.close()


def create_app(code: str, output_factory: OutputFactory, apk_path: Path = DEFAULT_APK) -> web.Application:
    app = web.Application()
    app[STATE] = _State(guard=PairingGuard(code), output_factory=output_factory, apk_path=apk_path)
    app.router.add_get("/", _index)
    app.router.add_get("/app.apk", _apk)
    app.router.add_get("/ws", _ws_handler)
    app.on_shutdown.append(_close_active)
    return app
