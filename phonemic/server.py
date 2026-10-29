"""HTTPS + WebSocket server: serves the phone page, pairs phones and feeds their audio to the output device.

WebSocket at /ws. The phone first sends a text "hello" and is accepted in one of two ways:
  * Browser page: {"type": "hello", "code": "123456", "sampleRate": 48000} with the pairing code shown on the PC.
  * Paired app:   {"type": "hello", "device": "<id>", "sampleRate": 48000}. The server answers
    {"type": "challenge", "nonce": "<hex>"} and the phone replies {"type": "auth", "signature": "<base64 DER ECDSA>"}
    over the nonce, using the private key that never left the phone.
The server then answers {"type": "ready"} (or {"type": "error", "reason": ...} and closes) and the phone streams binary
messages of little-endian int16 mono PCM at the announced sample rate. Every couple of seconds the server sends
{"type": "stats", ...}.
Close codes: 4400 malformed message, 4401 wrong code or signature, 4403 unknown or revoked device,
4409 replaced by a newer connection, 4500 audio error.

POST /enroll swaps a one-time pairing token for a permanent device identity (see devices.py).

Remote control (paired phones only, each permission off until it is switched on in the pairing page):
  * {"type": "ready", "capabilities": {"dictation": bool, "send": bool}} tells the phone what it may do; the same
    {"type": "capabilities", ...} is pushed again when the permissions change.
  * The phone may send {"type": "control", "action": "dictation_start" | "dictation_stop" | "send"} and gets
    {"type": "control_result", "action", "ok", "code", "message", "dictation"} back. Phones never send text: Claude
    Desktop transcribes the audio they stream, and "send" presses its send button.
  * Safeguards: one action at a time, a rate limit, and "send" only after this phone dictated something.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import ipaddress
import logging
import json
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from aiohttp import WSMsgType, web

from .claude_control import ACTIONS, ControlResult
from .devices import DeviceRegistry, EnrollmentTokens, clean_name
from .pcm_queue import PcmQueue

log = logging.getLogger("phonemic")

WEB_DIR = Path(__file__).parent / "web"
DEFAULT_APK = Path(__file__).resolve().parent.parent / "android" / "app" / "build" / "outputs" / "apk" / "debug" / "app-debug.apk"
HELLO_TIMEOUT_S = 10
AUTH_FAIL_DELAY_S = 1  # slows down guessing
STATS_INTERVAL_S = 2.0
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_SECONDS = 60
MIN_SAMPLE_RATE, MAX_SAMPLE_RATE = 8000, 96000
MAX_CONTROLS_PER_MINUTE = 20
MIN_CONTROL_INTERVAL_S = 0.5
# The permission a control action needs.
CONTROL_PERMISSION = {"dictation_start": "dictation", "dictation_stop": "dictation", "send": "send"}

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


class Controller(Protocol):
    """Operates Claude Desktop; see claude_control.ClaudeRemote."""

    async def run(self, action: str) -> ControlResult: ...


class PairingGuard:
    """Constant-time pairing-code check with a per-address lockout after repeated failures."""

    def __init__(self, code: str, clock: Callable[[], float] = time.monotonic) -> None:
        self.code = code
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

    def record_failure(self, address: str) -> None:
        count = self._failures.get(address, 0) + 1
        self._failures[address] = count
        if count >= MAX_FAILED_ATTEMPTS:
            self._locked_until[address] = self._clock() + LOCKOUT_SECONDS

    def check(self, address: str, candidate: str) -> bool:
        if hmac.compare_digest(candidate.encode(), self.code.encode()):
            self._failures.pop(address, None)
            return True
        self.record_failure(address)
        return False


def _parse_address(address: str | None) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        ip = ipaddress.ip_address(address or "")
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def is_local_address(address: str | None) -> bool:
    """Only loopback and private-network clients may connect."""
    ip = _parse_address(address)
    return ip is not None and any(ip in network for network in LOCAL_NETWORKS if network.version == ip.version)


def is_loopback_address(address: str | None) -> bool:
    ip = _parse_address(address)
    return ip is not None and ip.is_loopback


@dataclass(frozen=True)
class ServerInfo:
    """What a phone needs to know about this PC, and what the admin page shows."""

    name: str = "this PC"
    port: int = 8443
    pin_hex: str = "00" * 32
    ips: tuple[str, ...] = ()


@dataclass
class _Session:
    ws: web.WebSocketResponse
    output: Output
    address: str
    device_id: str | None
    control_times: deque = field(default_factory=deque)  # when this phone last used remote control
    control_task: asyncio.Task | None = None  # at most one action at a time
    dictated: bool = False  # this phone dictated something that has not been sent yet


@dataclass
class ServerState:
    guard: PairingGuard
    output_factory: OutputFactory
    registry: DeviceRegistry
    enrollment: EnrollmentTokens
    info: ServerInfo
    apk_path: Path = DEFAULT_APK
    controller: Controller | None = None
    clock: Callable[[], float] = time.monotonic
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active: _Session | None = None


STATE = web.AppKey("state", ServerState)


async def revoke_device(state: ServerState, device_id: str) -> bool:
    """Remove a paired phone and cut its connection if it is streaming right now."""
    if not state.registry.revoke(device_id):
        return False
    session = state.active
    if session is not None and session.device_id == device_id:
        await session.ws.close(code=4403, message=b"revoked")
    log.info("Device %s revoked", device_id)
    return True


def capabilities(state: ServerState, device_id: str | None) -> dict[str, bool]:
    """What a connection may do; browser-page connections (no device) never control anything."""
    device = state.registry.get(device_id) if device_id else None
    permissions = device.permissions if device else {}
    return {"dictation": bool(permissions.get("dictation")), "send": bool(permissions.get("send"))}


async def set_permissions(state: ServerState, device_id: str, *, dictation: bool, send: bool) -> dict[str, bool] | None:
    """Change what a phone may do and tell it right away if it is connected."""
    device = state.registry.set_permissions(device_id, dictation=dictation, send=send)
    if device is None:
        return None
    session = state.active
    if session is not None and session.device_id == device_id and not session.ws.closed:
        await session.ws.send_json({"type": "capabilities", "capabilities": device.permissions})
    log.info("Permissions of %r set to %s", device.name, device.permissions)
    return dict(device.permissions)


async def _handle_control(state: ServerState, session: _Session, action: str | None) -> None:
    """Run one remote-control action requested by a paired phone and answer it."""

    async def reply(ok: bool, code: str, message: str, dictation: str | None = None) -> None:
        if not session.ws.closed:
            await session.ws.send_json(
                {"type": "control_result", "action": action, "ok": ok, "code": code, "message": message, "dictation": dictation}
            )

    needed = CONTROL_PERMISSION.get(action or "")
    if needed is None or action not in ACTIONS:
        await reply(False, "bad_action", "Unknown action.")
        return
    allowed = capabilities(state, session.device_id)
    if not allowed[needed]:
        log.warning("Refused %s from device %s: no %r permission", action, session.device_id, needed)
        await reply(False, "not_allowed", "This phone is not allowed to do that. Turn it on in the PC's pairing page.")
        return
    if state.controller is None:
        await reply(False, "unavailable", "Remote control is not available on this PC.")
        return
    now = state.clock()
    while session.control_times and now - session.control_times[0] > 60:
        session.control_times.popleft()
    if (session.control_times and now - session.control_times[-1] < MIN_CONTROL_INTERVAL_S) or len(session.control_times) >= MAX_CONTROLS_PER_MINUTE:
        await reply(False, "rate_limited", "Too many actions too fast. Wait a moment.")
        return
    if action == "send" and not session.dictated:
        await reply(False, "nothing_dictated", "Dictate something from this phone first.")
        return
    session.control_times.append(now)
    log.info("Remote control: %s from device %s", action, session.device_id)
    result = await state.controller.run(action)
    if result.ok:
        if action == "dictation_start":
            session.dictated = True
        elif action == "send":
            session.dictated = False
    await reply(result.ok, result.code, result.message, result.dictation)


async def _handle_text(state: ServerState, session: _Session, raw: str) -> None:
    """Text messages after the handshake: only remote-control requests, and only from paired phones."""
    try:
        message = json.loads(raw)
    except ValueError:
        return
    if not isinstance(message, dict) or message.get("type") != "control" or session.device_id is None:
        return
    action = message.get("action") if isinstance(message.get("action"), str) else None
    if session.control_task is not None and not session.control_task.done():
        if not session.ws.closed:
            await session.ws.send_json({"type": "control_result", "action": action, "ok": False, "code": "in_progress", "message": "Still working on the previous action.", "dictation": None})
        return
    # A separate task: pressing a button can take seconds and the audio must keep flowing meanwhile.
    session.control_task = asyncio.create_task(_handle_control(state, session, action))


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


async def _enroll(request: web.Request) -> web.StreamResponse:
    """Swap a one-time pairing token for a permanent device identity."""
    state = request.app[STATE]
    address = request.remote or ""
    if not is_local_address(address):
        raise web.HTTPForbidden(text="Local network clients only.")
    if state.guard.locked(address):
        raise web.HTTPTooManyRequests(text="Too many failed attempts. Try again in a minute.")
    try:
        body = await request.json()
        token = body["token"]
        public_key = base64.b64decode(body["publicKey"], validate=True)
        if not isinstance(token, str):
            raise TypeError("token must be a string")
        state.registry.validate_public_key(public_key)
        name = clean_name(body.get("name"))
    except (ValueError, KeyError, TypeError, binascii.Error):
        raise web.HTTPBadRequest(text="Bad pairing request.")
    if not state.enrollment.consume(token):
        state.guard.record_failure(address)
        await asyncio.sleep(AUTH_FAIL_DELAY_S)
        raise web.HTTPForbidden(text="The pairing code expired or was already used. Show a new one on the PC.")
    device = state.registry.add(name, public_key)
    log.info("Paired %r from %s as device %s", device.name, address, device.id)
    return web.json_response({"deviceId": device.id, "name": state.info.name})


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


async def _reject(ws: web.WebSocketResponse, code: int, reason: str) -> None:
    await ws.send_json({"type": "error", "reason": reason})
    await asyncio.sleep(AUTH_FAIL_DELAY_S)
    await ws.close(code=code, message=reason.encode())


async def _authenticate_device(
    state: ServerState, ws: web.WebSocketResponse, address: str, device_id: str
) -> bool:
    """Challenge a paired phone. On failure the socket is closed and False is returned."""
    if state.registry.get(device_id) is None:
        state.guard.record_failure(address)
        await _reject(ws, 4403, "unknown device")
        return False
    nonce = secrets.token_hex(16)
    await ws.send_json({"type": "challenge", "nonce": nonce})
    try:
        reply = await asyncio.wait_for(ws.receive_json(), HELLO_TIMEOUT_S)
        if reply.get("type") != "auth":
            raise ValueError("expected auth")
        signature = base64.b64decode(reply["signature"], validate=True)
    except (asyncio.TimeoutError, TypeError, ValueError, KeyError, binascii.Error):
        await ws.close(code=4400, message=b"bad auth")
        return False
    if not state.registry.verify(device_id, nonce, signature):
        state.guard.record_failure(address)
        await _reject(ws, 4401, "bad signature")
        return False
    state.registry.touch(device_id, address)
    return True


async def _ws_handler(request: web.Request) -> web.StreamResponse:
    state = request.app[STATE]
    address = request.remote or ""
    if not is_local_address(address):
        raise web.HTTPForbidden(text="Local network clients only.")
    if state.guard.locked(address):
        raise web.HTTPTooManyRequests(text="Too many failed attempts. Try again in a minute.")

    ws = web.WebSocketResponse(heartbeat=10, max_msg_size=64 * 1024)
    await ws.prepare(request)

    try:
        hello = await asyncio.wait_for(ws.receive_json(), HELLO_TIMEOUT_S)
        sample_rate = int(hello["sampleRate"])
        if hello.get("type") != "hello" or not MIN_SAMPLE_RATE <= sample_rate <= MAX_SAMPLE_RATE:
            raise ValueError("bad hello")
        device_id = hello.get("device")
        code = None if device_id is not None else str(hello["code"])
        if device_id is not None:
            device_id = str(device_id)
    except (asyncio.TimeoutError, TypeError, ValueError, KeyError):
        await ws.close(code=4400, message=b"bad hello")
        return ws

    if device_id is not None:
        if not await _authenticate_device(state, ws, address, device_id):
            return ws
    elif not state.guard.check(address, code or ""):
        log.warning("Wrong pairing code from %s", address)
        await _reject(ws, 4401, "wrong code")
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
        session = _Session(ws, output, address, device_id)
        state.active = session

    log.info("Phone connected from %s (%d Hz)%s", address, sample_rate, f" as device {device_id}" if device_id else "")
    await ws.send_json({"type": "ready", "capabilities": capabilities(state, device_id)})
    stats_task = asyncio.create_task(_send_stats(session))
    try:
        async for message in ws:
            if message.type == WSMsgType.BINARY:
                output.queue.push(message.data)
            elif message.type == WSMsgType.TEXT:
                await _handle_text(state, session, message.data)
            elif message.type == WSMsgType.ERROR:
                break
    finally:
        stats_task.cancel()
        if session.control_task is not None:
            session.control_task.cancel()
        async with state.lock:
            if state.active is session:
                state.active = None
                output.close()
        log.info("Phone disconnected (%s)", address)
    return ws


async def _close_active(app: web.Application) -> None:
    state = app[STATE]
    session = state.active
    if session is not None:
        state.active = None
        await session.ws.close(code=1001)
        session.output.close()


def create_app(
    code: str,
    output_factory: OutputFactory,
    apk_path: Path = DEFAULT_APK,
    registry: DeviceRegistry | None = None,
    enrollment: EnrollmentTokens | None = None,
    info: ServerInfo | None = None,
    controller: Controller | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> web.Application:
    app = web.Application(client_max_size=64 * 1024)
    app[STATE] = ServerState(
        guard=PairingGuard(code),
        output_factory=output_factory,
        registry=registry if registry is not None else DeviceRegistry(),
        enrollment=enrollment if enrollment is not None else EnrollmentTokens(),
        info=info if info is not None else ServerInfo(),
        apk_path=apk_path,
        controller=controller,
        clock=clock,
    )
    app.router.add_get("/", _index)
    app.router.add_get("/app.apk", _apk)
    app.router.add_post("/enroll", _enroll)
    app.router.add_get("/ws", _ws_handler)
    app.on_shutdown.append(_close_active)
    return app
